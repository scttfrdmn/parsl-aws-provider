"""Sweep for AWS resources this provider left behind, and delete them.

Installed as the ``parsl-ephemeral-cleanup`` console script. It lives in the package
rather than under ``tools/`` because that is the only way it reaches a user who
installed from a wheel: ``tools/`` ships in neither the wheel nor the sdist, so
every documented ``python tools/cleanup_aws_resources.py`` invocation worked
only inside a git clone (#198).

That mattered more than a normal missing-file bug, because nothing stops the
billing automatically. Parsl never calls ``provider.shutdown()``;
``HighThroughputExecutor.shutdown()`` says outright that it does not terminate
workers; and ``parsl/dataflow/dflow.py``'s ``atexit_cleanup`` only logs. So a
``KeyboardInterrupt``, an uncaught exception, or a driver crash leaves EC2
instances running until someone notices, and this is the tool that bounds the
damage.

Usage
-----
    parsl-ephemeral-cleanup --dry-run --region us-east-1    # report only
    parsl-ephemeral-cleanup --region us-east-1              # delete, after confirming

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import logging
import sys
import time
from typing import Dict, List, Optional

import boto3

from parsl_ephemeral_provider.utils.aws import (
    delete_ssm_instance_profile,
    ssm_instance_profile_names,
)

logger = logging.getLogger(__name__)

# Both halves of the pair are named for the provider_id, so the suffix is what
# identifies an orphan. See ssm_instance_profile_names().
_ROLE_PREFIX, _PROFILE_PREFIX = (
    prefix[: -len("suffix")] for prefix in ssm_instance_profile_names("suffix")
)

# The package tags EC2 resources two different ways, and a sweep that knows only
# one of them misses whole modes:
#
#   StandardMode          CreatedBy=ParslEphemeralProvider, ProviderId=...
#   DetachedMode          ParslResource=true, ParslWorkflowId=...
#   ServerlessMode fleet  ParslResource=true, ParslWorkflowId=...
#
# This tool previously filtered on `tag:parsl_provider` matching `*aws-enhanced*`
# and on security groups named `aws-enhanced-*`. Neither string appears anywhere
# in the package -- not as a tag key, not as a value, not as a group name -- so
# the sweep documented as the way to find orphaned billable resources reported
# "No resources to clean up!" against an account full of them (#198).
#
# Tag *keys* rather than values are matched where possible, so a renamed value
# cannot silently narrow the sweep again.
#
# `ParslEphemeralAWSProvider` is the pre-#213 value and is swept alongside the
# current one, not replaced by it. The rename dropped `AWS` from every identifier
# (see CHANGELOG), and this tag value went with them -- but live accounts hold
# instances already tagged the old way, and this is the tool that bounds the bill
# after a crash. Narrowing it to the new value alone would leave those running and
# invisible. Both are listed under one filter because EC2 ORs the `Values` of a
# single filter while ANDing separate filters.
_INSTANCE_TAG_FILTERS = (
    {
        "Name": "tag:CreatedBy",
        "Values": ["ParslEphemeralProvider", "ParslEphemeralAWSProvider"],
    },
    {"Name": "tag-key", "Values": ["ParslResource"]},
)

# Security groups are no longer created by the provider (#69), so any match is a
# pre-v0.7.0 leftover. `parsl-ephemeral-sg` is DEFAULT_SECURITY_GROUP_NAME, which
# is the name the removed creation path used; the wildcard also catches the
# suffixed variants.
_SECURITY_GROUP_NAME_PATTERNS = ["parsl-ephemeral-sg", "parsl-ephemeral-sg-*"]


class AWSResourceCleaner:
    """Cleans up AWS resources created during testing."""

    def __init__(self, profile_name: Optional[str] = None, region: str = "us-east-1"):
        """Initialize the cleaner with AWS session.

        A None profile_name means the standard boto3 credential chain, which
        honours AWS_PROFILE. The default used to be the literal "aws" -- the
        profile name this project's developers use locally, per CLAUDE.md, but
        one that exists on no CI runner. CI's post-E2E sweep therefore died on
        "The config profile (aws) could not be found" instead of reporting
        orphans, which is the one moment orphans are most likely (#161). The
        runner authenticates via OIDC, so it has credentials but no profile.
        """
        try:
            if profile_name:
                self.session = boto3.Session(
                    profile_name=profile_name, region_name=region
                )
            else:
                self.session = boto3.Session(region_name=region)
            self.ec2_client = self.session.client("ec2")
            self.iam_client = self.session.client("iam")
            self.region = region
            logger.info(
                f"Initialized AWS session for region {region} with profile "
                f"{profile_name or 'default credential chain'}"
            )
        except Exception as e:
            logger.error(f"Failed to initialize AWS session: {e}")
            sys.exit(1)

    def get_parsl_instances(self) -> List[Dict]:
        """Get all live instances created by this provider, across all modes.

        One ``describe_instances`` call per tag convention, unioned by instance
        ID. They cannot be combined into a single call: EC2 ``Filters`` are
        ANDed, so passing both would return only instances carrying *both*
        conventions -- which no mode writes.
        """
        instances: Dict[str, Dict] = {}

        for tag_filter in _INSTANCE_TAG_FILTERS:
            try:
                paginator = self.ec2_client.get_paginator("describe_instances")
                pages = paginator.paginate(
                    Filters=[
                        dict(tag_filter),
                        {
                            "Name": "instance-state-name",
                            "Values": ["running", "pending", "stopping", "stopped"],
                        },
                    ]
                )
                for page in pages:
                    for reservation in page["Reservations"]:
                        for instance in reservation["Instances"]:
                            instances[instance["InstanceId"]] = {
                                "id": instance["InstanceId"],
                                "state": instance["State"]["Name"],
                                "launch_time": instance["LaunchTime"],
                                "name": next(
                                    (
                                        tag["Value"]
                                        for tag in instance.get("Tags", [])
                                        if tag["Key"] == "Name"
                                    ),
                                    "No Name",
                                ),
                            }
            except Exception as e:
                logger.error(f"Error getting instances for {tag_filter['Name']}: {e}")

        return sorted(instances.values(), key=lambda x: x["launch_time"], reverse=True)

    def get_parsl_security_groups(self) -> List[Dict]:
        """Get security groups left over from before #69 removed SG creation.

        The provider has created no security group since v0.7.0, so a match here
        is a pre-v0.7.0 orphan rather than something a current run left behind.
        """
        try:
            response = self.ec2_client.describe_security_groups(
                Filters=[
                    {"Name": "group-name", "Values": _SECURITY_GROUP_NAME_PATTERNS}
                ]
            )

            return [
                {
                    "id": sg["GroupId"],
                    "name": sg["GroupName"],
                    "description": sg["Description"],
                }
                for sg in response["SecurityGroups"]
            ]

        except Exception as e:
            logger.error(f"Error getting security groups: {e}")
            return []

    def terminate_instances(self, instance_ids: List[str]) -> bool:
        """Terminate the specified instances."""
        if not instance_ids:
            logger.info("No instances to terminate")
            return True

        try:
            logger.info(f"Terminating {len(instance_ids)} instances...")
            response = self.ec2_client.terminate_instances(InstanceIds=instance_ids)

            for instance in response["TerminatingInstances"]:
                logger.info(
                    f"  {instance['InstanceId']}: {instance['PreviousState']['Name']} → {instance['CurrentState']['Name']}"
                )

            return True

        except Exception as e:
            logger.error(f"Error terminating instances: {e}")
            return False

    def wait_for_instance_termination(
        self, instance_ids: List[str], timeout: int = 300
    ) -> bool:
        """Wait for instances to fully terminate."""
        if not instance_ids:
            return True

        logger.info(
            f"Waiting for {len(instance_ids)} instances to terminate (timeout: {timeout}s)..."
        )

        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                response = self.ec2_client.describe_instances(InstanceIds=instance_ids)

                all_terminated = True
                for reservation in response["Reservations"]:
                    for instance in reservation["Instances"]:
                        state = instance["State"]["Name"]
                        if state not in ["terminated", "shutting-down"]:
                            all_terminated = False
                            break
                    if not all_terminated:
                        break

                if all_terminated:
                    logger.info("✅ All instances terminated")
                    return True

                time.sleep(10)
                logger.info(
                    f"  Still waiting... ({int(time.time() - start_time)}s elapsed)"
                )

            except Exception as e:
                logger.error(f"Error checking instance status: {e}")
                return False

        logger.warning("⚠️ Timeout waiting for instances to terminate")
        return False

    def delete_security_groups(self, security_groups: List[Dict]) -> bool:
        """Delete the specified security groups."""
        if not security_groups:
            logger.info("No security groups to delete")
            return True

        success_count = 0
        failed_groups = []

        for sg in security_groups:
            try:
                self.ec2_client.delete_security_group(GroupId=sg["id"])
                logger.info(f"✅ Deleted security group: {sg['name']} ({sg['id']})")
                success_count += 1

            except Exception as e:
                logger.warning(
                    f"❌ Failed to delete security group {sg['name']} ({sg['id']}): {e}"
                )
                failed_groups.append(sg)

        if failed_groups:
            logger.info(
                f"Successfully deleted {success_count}/{len(security_groups)} security groups"
            )
            logger.info(
                "Failed security groups may have dependent objects or network interfaces"
            )
            return False
        else:
            logger.info(f"✅ All {success_count} security groups deleted successfully")
            return True

    def get_parsl_iam_resources(self) -> List[str]:
        """Get the provider_id suffixes of orphaned SSM roles and instance profiles.

        Roles and profiles can be orphaned independently — a partial teardown may
        leave one without the other — so both listings are scanned and the suffixes
        unioned. IAM is a global service, so this is not scoped by ``--region``.
        """
        suffixes = set()

        try:
            for page in self.iam_client.get_paginator("list_roles").paginate():
                for role in page["Roles"]:
                    if role["RoleName"].startswith(_ROLE_PREFIX):
                        suffixes.add(role["RoleName"][len(_ROLE_PREFIX) :])
        except Exception as e:
            logger.error(f"Error listing IAM roles: {e}")

        try:
            paginator = self.iam_client.get_paginator("list_instance_profiles")
            for page in paginator.paginate():
                for profile in page["InstanceProfiles"]:
                    name = profile["InstanceProfileName"]
                    if name.startswith(_PROFILE_PREFIX):
                        suffixes.add(name[len(_PROFILE_PREFIX) :])
        except Exception as e:
            logger.error(f"Error listing IAM instance profiles: {e}")

        return sorted(suffixes)

    def delete_iam_resources(self, suffixes: List[str]) -> bool:
        """Delete the SSM role and instance profile for each suffix."""
        if not suffixes:
            logger.info("No IAM roles or instance profiles to delete")
            return True

        success_count = 0
        for suffix in suffixes:
            role_name, profile_name = ssm_instance_profile_names(suffix)
            if delete_ssm_instance_profile(self.session, suffix):
                logger.info(f"✅ Deleted IAM role/profile pair: {suffix}")
                success_count += 1
            else:
                logger.warning(
                    f"❌ Failed to fully delete {role_name} / {profile_name}"
                )

        if success_count < len(suffixes):
            logger.info(
                f"Successfully deleted {success_count}/{len(suffixes)} "
                "IAM role/profile pairs"
            )
            logger.info(
                "Failed pairs may still be attached to an instance that has not "
                "finished terminating"
            )
            return False

        logger.info(f"✅ All {success_count} IAM role/profile pairs deleted")
        return True

    def cleanup_all(self, dry_run: bool = False) -> bool:
        """Clean up all Parsl AWS resources."""
        logger.info("🧹 Starting AWS resource cleanup")
        logger.info("=" * 50)

        if dry_run:
            logger.info("DRY RUN MODE - No resources will be deleted")
            logger.info("=" * 50)

        # Get all resources
        instances = self.get_parsl_instances()
        security_groups = self.get_parsl_security_groups()
        iam_suffixes = self.get_parsl_iam_resources()

        # Report what was found
        logger.info(f"Found {len(instances)} instances to clean up:")
        for instance in instances:
            logger.info(
                f"  {instance['id']} - {instance['state']} - {instance['name']}"
            )

        logger.info(f"\nFound {len(security_groups)} security groups to clean up:")
        for sg in security_groups:
            logger.info(f"  {sg['id']} - {sg['name']}")

        logger.info(f"\nFound {len(iam_suffixes)} IAM role/profile pairs to clean up:")
        for suffix in iam_suffixes:
            role_name, profile_name = ssm_instance_profile_names(suffix)
            logger.info(f"  {role_name} / {profile_name}")

        if not instances and not security_groups and not iam_suffixes:
            logger.info("🎉 No resources to clean up!")
            return True

        if dry_run:
            logger.info("\nDRY RUN COMPLETE - No resources were deleted")
            return True

        # Confirm cleanup
        print("\n" + "=" * 50)
        try:
            response = input(
                f"Delete {len(instances)} instances, {len(security_groups)} security "
                f"groups and {len(iam_suffixes)} IAM role/profile pairs? (yes/no): "
            )
            if response.lower() not in ["yes", "y"]:
                logger.info("❌ Cleanup cancelled by user")
                return False
        except (EOFError, KeyboardInterrupt):
            logger.info("❌ Cleanup cancelled (no input)")
            return False

        success = True

        # Terminate instances
        if instances:
            running_instance_ids = [
                inst["id"]
                for inst in instances
                if inst["state"] in ["running", "pending"]
            ]
            if running_instance_ids:
                if not self.terminate_instances(running_instance_ids):
                    success = False
                else:
                    # Wait for termination
                    if not self.wait_for_instance_termination(running_instance_ids):
                        logger.warning("⚠️ Some instances may still be terminating")

            # Check for stopped instances that need termination
            stopped_instance_ids = [
                inst["id"] for inst in instances if inst["state"] == "stopped"
            ]
            if stopped_instance_ids:
                logger.info(
                    f"Also terminating {len(stopped_instance_ids)} stopped instances..."
                )
                if not self.terminate_instances(stopped_instance_ids):
                    success = False

        # Delete security groups (after instances are terminated)
        if security_groups:
            logger.info("\nCleaning up security groups...")
            time.sleep(30)  # Give AWS time to clean up network interfaces
            if not self.delete_security_groups(security_groups):
                success = False

        # Delete IAM roles and instance profiles last: IAM refuses to delete a
        # profile that is still attached to an instance, so the terminations above
        # have to have settled first.
        if iam_suffixes:
            logger.info("\nCleaning up IAM roles and instance profiles...")
            if not self.delete_iam_resources(iam_suffixes):
                success = False

        if success:
            logger.info("\n🎉 Cleanup completed successfully!")
        else:
            logger.warning("\n⚠️ Cleanup completed with some failures")

        return success


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point for the ``parsl-ephemeral-cleanup`` console script.

    Returns an exit status rather than calling ``sys.exit()`` so the function is
    testable; ``console_scripts`` uses the return value as the process status.

    ``basicConfig`` is called here rather than at import, because this module is
    now part of the installed package: configuring the root logger on import
    would hijack logging for anyone who merely imports ``parsl_ephemeral_provider``.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="parsl-ephemeral-cleanup",
        description=(
            "Find and delete AWS resources left behind by this provider. "
            "Sweeps by tag, so it finds resources no state file names -- the "
            "case after a crash or KeyboardInterrupt, which Parsl does not "
            "clean up."
        ),
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="AWS profile to use (default: the standard credential chain, "
        "which honours AWS_PROFILE)",
    )
    parser.add_argument(
        "--region", default="us-east-1", help="AWS region (default: us-east-1)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without deleting",
    )

    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    cleaner = AWSResourceCleaner(profile_name=args.profile, region=args.region)
    success = cleaner.cleanup_all(dry_run=args.dry_run)

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())

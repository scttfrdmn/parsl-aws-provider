"""Utilities for cleaning up fleet resources left behind by a workflow.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import logging
import time
from typing import Dict, List, Any, Set

import boto3
from botocore.exceptions import ClientError

from parsl_ephemeral_aws.constants import (
    TAG_AWS_FLEET_ID,
    TAG_PREFIX,
    TAG_WORKFLOW_ID,
)
from parsl_ephemeral_aws.utils.aws import delete_ec2_fleet


logger = logging.getLogger(__name__)

# Tag keys that have been used to stamp a workflow ID onto a fleet's instances.
# TAG_WORKFLOW_ID is what this package writes; "ParslWorkflowId" is what the
# detached mode's bastion manager script writes (detached.py:1005).
WORKFLOW_ID_TAG_KEYS = (TAG_WORKFLOW_ID, "ParslWorkflowId")

# Name prefixes under which a Spot Fleet service role may have been created.
#
# Two producers used two different prefixes, and the sweep only ever knew about
# the first, so roles made by the second were never reclaimed:
#
#   parsl-aws-spot-fleet-role-  ecs_worker.yml:233 (CloudFormation)
#   parsl-ephemeral-spot-fleet-role-  detached.py:884 (bastion manager script)
#
# ``CreateFleet`` has no ``IamFleetRole``, so nothing this package runs creates
# either any more (#86). They are swept for the sake of a workflow that was
# started before the upgrade.
SPOT_FLEET_ROLE_PREFIXES = (
    "parsl-aws-spot-fleet-role-",
    f"{TAG_PREFIX}-spot-fleet-role-",
)


def cleanup_spot_fleet_role(
    session: boto3.Session,
    role_name: str,
    wait_for_detachment: bool = True,
    max_attempts: int = 3,
    delay_seconds: int = 5,
) -> bool:
    """Clean up IAM role created for Spot Fleet requests.

    This function handles detaching policies and deleting the IAM role created
    for Spot Fleet requests. It includes retry logic to handle potential
    eventual consistency issues in the IAM service.

    Parameters
    ----------
    session : boto3.Session
        AWS session to use for API calls
    role_name : str
        Name of the IAM role to delete
    wait_for_detachment : bool, optional
        Whether to wait for policy detachment to complete, by default True
    max_attempts : int, optional
        Maximum number of attempts for cleanup, by default 3
    delay_seconds : int, optional
        Delay between cleanup attempts in seconds, by default 5

    Returns
    -------
    bool
        True if role was successfully deleted, False otherwise
    """
    iam = session.client("iam")

    for attempt in range(max_attempts):
        try:
            # Get the role to ensure it exists
            try:
                iam.get_role(RoleName=role_name)
            except ClientError as e:
                if e.response["Error"]["Code"] == "NoSuchEntity":
                    logger.debug(f"Role {role_name} not found, nothing to clean up")
                    return True
                raise

            # Get attached policies
            attached_policies = iam.list_attached_role_policies(RoleName=role_name)

            # Detach all policies
            for policy in attached_policies.get("AttachedPolicies", []):
                policy_arn = policy["PolicyArn"]
                logger.debug(f"Detaching policy {policy_arn} from role {role_name}")
                iam.detach_role_policy(RoleName=role_name, PolicyArn=policy_arn)

            # Wait for policies to detach (if requested)
            if wait_for_detachment and attached_policies.get("AttachedPolicies"):
                logger.debug(f"Waiting for policies to detach from role {role_name}")
                time.sleep(delay_seconds)

            # Delete the role
            logger.debug(f"Deleting IAM role {role_name}")
            iam.delete_role(RoleName=role_name)

            logger.info(f"Successfully deleted Spot Fleet IAM role {role_name}")
            return True

        except ClientError as e:
            if e.response["Error"]["Code"] in [
                "DeleteConflict",  # Role is still being used
                "ResourceInUseException",  # Role has resources attached
            ]:
                if attempt < max_attempts - 1:
                    logger.warning(
                        f"Could not delete role {role_name} on attempt {attempt + 1}, "
                        f"retrying in {delay_seconds} seconds..."
                    )
                    time.sleep(delay_seconds)
                    continue
                else:
                    logger.warning(
                        f"Could not delete role {role_name} after {max_attempts} attempts. "
                        "The role might still be in use by active Spot Fleet requests."
                    )
                    return False
            else:
                logger.error(f"Error deleting IAM role {role_name}: {e}")
                return False
        except Exception as e:
            logger.error(f"Unexpected error deleting IAM role {role_name}: {e}")
            return False

    return False


def find_fleet_ids_for_workflow(ec2_client: Any, workflow_id: str) -> List[str]:
    """Find the EC2 Fleets belonging to *workflow_id*, via their instances.

    The sweep has to go through ``describe_instances`` rather than
    ``describe_fleets`` (#86). An ``instant`` fleet is invisible to a
    fleet-level listing -- AWS: "if a fleet is of type instant, you must specify
    the fleet ID in the request, otherwise the fleet does not appear in the
    response" -- and a tag filter on ``describe_fleets`` does not find one
    either. Both verified against real EC2. Since the point of a sweep is to
    find fleets whose IDs are *not* known, the only workable route is the
    ``aws:ec2:fleet-id`` tag EC2 stamps on every fleet-launched instance.

    No instance-state filter is applied. A fleet all of whose instances have
    terminated should still be deleted, and deleting an already-empty fleet is
    harmless.

    Parameters
    ----------
    ec2_client : Any
        A boto3 EC2 client.
    workflow_id : str
        Workflow whose fleets to find.

    Returns
    -------
    List[str]
        Fleet IDs, deduplicated. Empty if the workflow launched no fleets, or
        EC2 has already forgotten their instances.
    """
    fleet_ids: Set[str] = set()

    # One pass per workflow tag key: describe_instances ANDs its filters, so
    # the two keys cannot be expressed in a single call.
    for tag_key in WORKFLOW_ID_TAG_KEYS:
        try:
            paginator = ec2_client.get_paginator("describe_instances")
            pages = paginator.paginate(
                Filters=[
                    {"Name": f"tag:{tag_key}", "Values": [workflow_id]},
                    {"Name": "tag-key", "Values": [TAG_AWS_FLEET_ID]},
                ]
            )
            for page in pages:
                for reservation in page.get("Reservations", []):
                    for instance in reservation.get("Instances", []):
                        for tag in instance.get("Tags", []):
                            if tag["Key"] == TAG_AWS_FLEET_ID:
                                fleet_ids.add(tag["Value"])
        except ClientError as e:
            logger.warning(
                f"Error searching for fleet instances tagged {tag_key}="
                f"{workflow_id}: {e}"
            )

    return sorted(fleet_ids)


def find_legacy_spot_fleet_request_ids(ec2_client: Any, workflow_id: str) -> List[str]:
    """Find legacy Spot Fleet requests belonging to *workflow_id*.

    Retained only for a workflow that was started before #86 replaced
    ``RequestSpotFleet`` with ``CreateFleet``. Nothing in this package creates a
    Spot Fleet request any more, so on a workflow started after the upgrade this
    returns an empty list -- at the cost of one ``describe_spot_fleet_requests``
    pagination.

    Parameters
    ----------
    ec2_client : Any
        A boto3 EC2 client.
    workflow_id : str
        Workflow whose requests to find.

    Returns
    -------
    List[str]
        Spot Fleet request IDs.
    """
    request_ids: List[str] = []

    paginator = ec2_client.get_paginator("describe_spot_fleet_requests")
    for page in paginator.paginate():
        for config in page.get("SpotFleetRequestConfigs", []):
            request_id = config["SpotFleetRequestId"]

            # A Spot Fleet request's own tags are not in the describe response,
            # so they have to be read separately.
            try:
                tags_response = ec2_client.describe_tags(
                    Filters=[{"Name": "resource-id", "Values": [request_id]}]
                )
            except ClientError as e:
                logger.warning(
                    f"Error checking tags for Spot Fleet request {request_id}: {e}"
                )
                continue

            for tag in tags_response.get("Tags", []):
                if tag["Key"] in WORKFLOW_ID_TAG_KEYS and tag["Value"] == workflow_id:
                    request_ids.append(request_id)
                    break

    return request_ids


def cleanup_all_spot_fleet_resources(
    session: boto3.Session,
    workflow_id: str,
    cancel_active_requests: bool = True,
    cleanup_iam_roles: bool = True,
) -> Dict[str, Any]:
    """Clean up all fleet resources associated with a workflow.

    This function handles cleaning up:

    1. EC2 Fleets, and the instances they launched
    2. Legacy Spot Fleet requests, for a workflow that predates #86
    3. IAM service roles created for those legacy requests

    Parameters
    ----------
    session : boto3.Session
        AWS session to use for API calls
    workflow_id : str
        Workflow ID used to identify resources to clean up
    cancel_active_requests : bool, optional
        Whether to delete fleets and cancel Spot Fleet requests, by default True
    cleanup_iam_roles : bool, optional
        Whether to clean up IAM roles, by default True

    Returns
    -------
    Dict[str, Any]
        Dictionary with cleanup results:

        - deleted_fleets: IDs of EC2 Fleets deleted
        - cancelled_requests: IDs of legacy Spot Fleet requests cancelled
        - cleaned_roles: names of IAM roles deleted
        - errors: errors encountered, one dict per failure
    """
    result: Dict[str, Any] = {
        "deleted_fleets": [],
        "cancelled_requests": [],
        "cleaned_roles": [],
        "errors": [],
    }

    ec2 = session.client("ec2")
    iam = session.client("iam")

    # Delete EC2 Fleets.
    if cancel_active_requests:
        fleet_ids = find_fleet_ids_for_workflow(ec2, workflow_id)
        if fleet_ids:
            logger.info(f"Deleting {len(fleet_ids)} EC2 Fleets")

        # One fleet per call. delete_fleets takes a list, but reports per-fleet
        # failures in UnsuccessfulFleetDeletions rather than failing the call,
        # so batching would let one stuck fleet hide the others.
        for fleet_id in fleet_ids:
            try:
                delete_ec2_fleet(ec2, fleet_id)
                result["deleted_fleets"].append(fleet_id)
            except Exception as e:
                logger.error(f"Error deleting EC2 Fleet {fleet_id}: {e}")
                result["errors"].append(
                    {
                        "resource_id": fleet_id,
                        "operation": "delete_fleets",
                        "error": str(e),
                    }
                )

    # Cancel legacy Spot Fleet requests, if any survive from before #86.
    if cancel_active_requests:
        try:
            request_ids = find_legacy_spot_fleet_request_ids(ec2, workflow_id)
        except Exception as e:
            logger.error(f"Error finding Spot Fleet requests: {e}")
            result["errors"].append(
                {"operation": "find_spot_fleet_requests", "error": str(e)}
            )
            request_ids = []

        if request_ids:
            logger.info(f"Cancelling {len(request_ids)} legacy Spot Fleet requests")
            try:
                response = ec2.cancel_spot_fleet_requests(
                    SpotFleetRequestIds=request_ids, TerminateInstances=True
                )

                for success in response.get("SuccessfulFleetRequests", []):
                    result["cancelled_requests"].append(success["SpotFleetRequestId"])

                for failure in response.get("UnsuccessfulFleetRequests", []):
                    result["errors"].append(
                        {
                            "resource_id": failure["SpotFleetRequestId"],
                            "operation": "cancel_spot_fleet_requests",
                            "error": failure.get("Error", {}).get(
                                "Message", "Unknown error"
                            ),
                        }
                    )
            except Exception as e:
                logger.error(f"Error cancelling Spot Fleet requests: {e}")
                result["errors"].append(
                    {"operation": "cancel_spot_fleet_requests", "error": str(e)}
                )

    # Clean up IAM roles
    if cleanup_iam_roles:
        try:
            role_prefixes = tuple(
                f"{prefix}{workflow_id[:8]}" for prefix in SPOT_FLEET_ROLE_PREFIXES
            )

            roles_to_clean = []
            paginator = iam.get_paginator("list_roles")

            for page in paginator.paginate():
                for role in page["Roles"]:
                    if role["RoleName"].startswith(role_prefixes):
                        roles_to_clean.append(role["RoleName"])

            # Clean up each role
            for role_name in roles_to_clean:
                logger.info(f"Cleaning up Spot Fleet IAM role {role_name}")
                if cleanup_spot_fleet_role(session, role_name):
                    result["cleaned_roles"].append(role_name)
                else:
                    result["errors"].append(
                        {
                            "resource_id": role_name,
                            "operation": "cleanup_spot_fleet_role",
                            "error": "Failed to clean up role",
                        }
                    )
        except Exception as e:
            logger.error(f"Error cleaning up Spot Fleet IAM roles: {e}")
            result["errors"].append({"operation": "cleanup_iam_roles", "error": str(e)})

    return result

"""Unit tests for the ``parsl-ephemeral-cleanup`` orphan sweep.

This tool is the only thing that bounds the bill after a crash. Parsl never
calls ``provider.shutdown()``, ``HighThroughputExecutor.shutdown()`` documents
that it does not terminate workers, and ``dflow.py``'s ``atexit_cleanup`` only
logs -- so after a ``KeyboardInterrupt`` or an uncaught exception, instances keep
running until someone runs this.

It had no tests at all, and it did not work. Its EC2 filters named a tag key and
a security-group prefix that appear nowhere in the package, so it reported
"No resources to clean up!" against an account holding orphans. These tests pin
the filters to the tags the modes actually write, in both directions: a resource
tagged either way is found, and a resource tagged neither way is left alone
(#198).

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import re
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from parsl_ephemeral_provider.cleanup import (
    _INSTANCE_TAG_FILTERS,
    _SECURITY_GROUP_NAME_PATTERNS,
    AWSResourceCleaner,
    main,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

# What each mode actually writes onto an instance. Kept as literals rather than
# imported, so that renaming a tag in the package fails these tests instead of
# silently moving both sides together.
STANDARD_MODE_TAGS = [
    {"Key": "Name", "Value": "parsl-worker-abc12345"},
    {"Key": "CreatedBy", "Value": "ParslEphemeralProvider"},
    {"Key": "ProviderId", "Value": "prov-1"},
]
DETACHED_MODE_TAGS = [
    {"Key": "Name", "Value": "parsl-worker-def67890"},
    {"Key": "ParslResource", "Value": "true"},
    {"Key": "ParslWorkflowId", "Value": "prov-2"},
]
UNRELATED_TAGS = [
    {"Key": "Name", "Value": "someone-elses-instance"},
    {"Key": "CreatedBy", "Value": "Terraform"},
]
# The pre-#213 value, still live on instances in real accounts. The rename that
# dropped `AWS` from every identifier took this tag value with it, and a sweep
# narrowed to the new value alone would leave those instances running and
# invisible -- which is the opposite of what this tool is for.
LEGACY_STANDARD_MODE_TAGS = [
    {"Key": "Name", "Value": "parsl-worker-9f3c1a70"},
    {"Key": "CreatedBy", "Value": "ParslEphemeralAWSProvider"},
    {"Key": "ProviderId", "Value": "prov-legacy"},
]


@pytest.fixture
def aws(monkeypatch):
    """A moto-backed AWS with synthetic credentials.

    ``AWS_PROFILE`` is cleared so a developer's real profile cannot be reached:
    this suite creates and deletes instances and security groups.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("AWS_PROFILE", raising=False)

    with mock_aws():
        yield boto3.Session(region_name="us-east-1")


def _launch(session, tags):
    """Launch one instance carrying ``tags``, returning its ID."""
    ec2 = session.client("ec2")
    images = ec2.describe_images(Owners=["amazon"])["Images"]
    response = ec2.run_instances(
        ImageId=images[0]["ImageId"],
        MinCount=1,
        MaxCount=1,
        TagSpecifications=[{"ResourceType": "instance", "Tags": tags}],
    )
    return response["Instances"][0]["InstanceId"]


class TestInstanceDiscovery:
    """The sweep has to find what every mode leaves behind."""

    def test_standard_mode_tags_are_found(self, aws):
        """``CreatedBy=ParslEphemeralProvider`` -- what StandardMode writes."""
        instance_id = _launch(aws, STANDARD_MODE_TAGS)

        cleaner = AWSResourceCleaner(region="us-east-1")
        found = {inst["id"] for inst in cleaner.get_parsl_instances()}

        assert instance_id in found

    def test_detached_and_serverless_tags_are_found(self, aws):
        """``ParslResource=true`` -- what DetachedMode and the fleet path write."""
        instance_id = _launch(aws, DETACHED_MODE_TAGS)

        cleaner = AWSResourceCleaner(region="us-east-1")
        found = {inst["id"] for inst in cleaner.get_parsl_instances()}

        assert instance_id in found

    def test_the_pre_rename_created_by_value_is_still_found(self, aws):
        """``CreatedBy=ParslEphemeralAWSProvider`` -- what StandardMode wrote before #213.

        The rename dropped ``AWS`` from every identifier, this tag value included.
        Instances launched by an earlier version are still running in real
        accounts, and this is the tool that bounds the bill after a crash, so the
        old value has to stay in the sweep rather than be replaced by the new one.
        """
        instance_id = _launch(aws, LEGACY_STANDARD_MODE_TAGS)

        cleaner = AWSResourceCleaner(region="us-east-1")
        found = {inst["id"] for inst in cleaner.get_parsl_instances()}

        assert instance_id in found

    def test_both_created_by_values_are_found_in_one_sweep(self, aws):
        """The new value does not shadow the old one, or vice versa.

        Both live in the ``Values`` list of a *single* EC2 filter, which is what
        makes this work: EC2 ORs the values within one filter and ANDs separate
        filters, so splitting them into two filters would match only an instance
        carrying both values at once -- that is, nothing.
        """
        current = _launch(aws, STANDARD_MODE_TAGS)
        legacy = _launch(aws, LEGACY_STANDARD_MODE_TAGS)

        cleaner = AWSResourceCleaner(region="us-east-1")
        found = {inst["id"] for inst in cleaner.get_parsl_instances()}

        assert {current, legacy} <= found

    def test_both_conventions_are_found_together(self, aws):
        """Neither convention shadows the other.

        The two conventions need two ``describe_instances`` calls: EC2 ANDs its
        ``Filters``, so a single call passing both tag filters returns only
        instances carrying *both* -- which is to say nothing. A regression to one
        combined call would leave one mode's orphans invisible, and this is the
        test that catches it.
        """
        standard = _launch(aws, STANDARD_MODE_TAGS)
        detached = _launch(aws, DETACHED_MODE_TAGS)

        cleaner = AWSResourceCleaner(region="us-east-1")
        found = {inst["id"] for inst in cleaner.get_parsl_instances()}

        assert {standard, detached} <= found

    def test_an_instance_is_reported_once_not_twice(self, aws):
        """An instance carrying both conventions is deduplicated.

        The union is keyed by instance ID rather than accumulated into a list,
        so an instance matching both filters is not offered for termination
        twice.
        """
        instance_id = _launch(aws, STANDARD_MODE_TAGS + DETACHED_MODE_TAGS[1:])

        cleaner = AWSResourceCleaner(region="us-east-1")
        ids = [inst["id"] for inst in cleaner.get_parsl_instances()]

        assert ids.count(instance_id) == 1

    def test_someone_elses_instance_is_left_alone(self, aws):
        """The sweep terminates instances, so a false positive is destructive.

        This is the direction that matters most: the tool asks for confirmation
        and then calls ``terminate_instances``, so matching too broadly destroys
        a third party's work.
        """
        unrelated = _launch(aws, UNRELATED_TAGS)

        cleaner = AWSResourceCleaner(region="us-east-1")
        found = {inst["id"] for inst in cleaner.get_parsl_instances()}

        assert unrelated not in found

    def test_terminated_instances_are_not_reported(self, aws):
        """A terminated instance keeps its tags but costs nothing.

        Reporting it would make ``--dry-run`` list resources that cannot be
        cleaned up, and the count never reaches zero.
        """
        instance_id = _launch(aws, STANDARD_MODE_TAGS)
        aws.client("ec2").terminate_instances(InstanceIds=[instance_id])

        cleaner = AWSResourceCleaner(region="us-east-1")
        found = {inst["id"] for inst in cleaner.get_parsl_instances()}

        assert instance_id not in found


class TestSecurityGroupDiscovery:
    """Pre-v0.7.0 leftovers, since #69 removed security-group creation."""

    def test_the_provider_default_name_is_found(self, aws):
        """``parsl-ephemeral-sg-<suffix>`` is what the removed path created.

        Verified against a real account: five such groups were present and the
        old ``aws-enhanced-*`` filter reported none of them.
        """
        ec2 = aws.client("ec2")
        vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
        group_id = ec2.create_security_group(
            GroupName="parsl-ephemeral-sg-78050a0b",
            Description="orphan from before #69",
            VpcId=vpc,
        )["GroupId"]

        cleaner = AWSResourceCleaner(region="us-east-1")
        found = {sg["id"] for sg in cleaner.get_parsl_security_groups()}

        assert group_id in found

    def test_an_unrelated_group_is_left_alone(self, aws):
        """Deleting a security group the caller supplied is the #100 hazard."""
        ec2 = aws.client("ec2")
        vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
        group_id = ec2.create_security_group(
            GroupName="my-production-sg",
            Description="not ours",
            VpcId=vpc,
        )["GroupId"]

        cleaner = AWSResourceCleaner(region="us-east-1")
        found = {sg["id"] for sg in cleaner.get_parsl_security_groups()}

        assert group_id not in found


class TestFiltersMatchThePackage:
    """The filters must name strings the package actually writes.

    The original defect was not a logic error -- the queries were well-formed and
    returned nothing, because they searched for a tag key (``parsl_provider``)
    and a group prefix (``aws-enhanced-``) that exist nowhere in this package.
    Asserting the filter values against the package source is what makes that
    class of defect visible.
    """

    def _package_source(self) -> str:
        modes = (REPO_ROOT / "parsl_ephemeral_provider" / "modes").glob("*.py")
        return "\n".join(path.read_text(encoding="utf-8") for path in modes)

    def test_every_instance_tag_filter_is_written_by_some_mode(self):
        """Each filter names a tag key that appears in ``modes/``."""
        source = self._package_source()

        missing = []
        for tag_filter in _INSTANCE_TAG_FILTERS:
            if tag_filter["Name"] == "tag-key":
                keys = tag_filter["Values"]
            else:
                keys = [tag_filter["Name"].split(":", 1)[1]]
            for key in keys:
                if f'"{key}"' not in source and f"'{key}'" not in source:
                    missing.append(key)

        assert not missing, f"filtered on tag keys no mode writes: {missing}"

    def test_the_security_group_pattern_matches_the_constant(self):
        """The pattern tracks ``DEFAULT_SECURITY_GROUP_NAME``.

        Pinned to the constant rather than the literal, so renaming the default
        without updating the sweep fails here instead of silently narrowing it.
        """
        from parsl_ephemeral_provider.constants import DEFAULT_SECURITY_GROUP_NAME

        assert any(
            re.fullmatch(pattern.replace("*", ".*"), DEFAULT_SECURITY_GROUP_NAME)
            for pattern in _SECURITY_GROUP_NAME_PATTERNS
        ), (
            f"{DEFAULT_SECURITY_GROUP_NAME!r} matches none of "
            f"{_SECURITY_GROUP_NAME_PATTERNS}"
        )


class TestEntryPoint:
    """``parsl-ephemeral-cleanup`` is a console script, so ``main`` is the contract."""

    def test_dry_run_on_an_empty_account_succeeds(self, aws):
        """Exit status 0 and nothing deleted."""
        assert main(["--dry-run", "--region", "us-east-1"]) == 0

    def test_dry_run_deletes_nothing(self, aws):
        """``--dry-run`` reports and returns before any delete call.

        It is also what every document tells a user to run first, so a
        ``--dry-run`` that deleted would be the worst possible defect here.
        """
        instance_id = _launch(aws, STANDARD_MODE_TAGS)

        assert main(["--dry-run", "--region", "us-east-1"]) == 0

        state = aws.client("ec2").describe_instances(InstanceIds=[instance_id])
        assert state["Reservations"][0]["Instances"][0]["State"]["Name"] == "running"

    def test_main_returns_a_status_rather_than_exiting(self, aws):
        """``console_scripts`` uses the return value as the exit status.

        Returning instead of calling ``sys.exit()`` is also what makes the two
        tests above possible.
        """
        result = main(["--dry-run", "--region", "us-east-1"])

        assert isinstance(result, int)

    def test_importing_the_module_does_not_configure_logging(self):
        """``basicConfig`` belongs in ``main``, not at import.

        The module moved into the installed package, so configuring the root
        logger at import would hijack logging for anyone who merely imports
        ``parsl_ephemeral_provider``.
        """
        import logging
        import subprocess
        import sys

        result = subprocess.run(  # nosec B603 -- fixed argv, no shell
            [
                sys.executable,
                "-c",
                "import logging, parsl_ephemeral_provider.cleanup; "
                "print(len(logging.getLogger().handlers))",
            ],
            capture_output=True,
            text=True,
            check=True,
        )

        assert result.stdout.strip() == "0", (
            f"importing cleanup.py added a root logging handler: {result.stdout!r}"
        )
        assert logging  # the import above is the subject, not incidental

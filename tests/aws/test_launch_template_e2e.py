"""Real-AWS end-to-end tests for launch templates and IMDSv2 (issue #85).

Unit tests can only assert what this package *sends*. Every claim below is about
what EC2 actually *does* with it:

* ``MetadataOptions.HttpTokens="required"`` reaches the template and the
  instances launched from it, on the on-demand *and* spot paths. On spot there is
  no other route: ``RequestSpotInstances`` accepts no ``LaunchTemplate``, and its
  ``LaunchSpecification`` shape has no ``MetadataOptions`` member at all -- which
  is why the spot path moved to ``RunInstances`` + ``InstanceMarketOptions``.
* ``InstanceInitiatedShutdownBehavior="terminate"`` survives on a *spot*
  instance. ``LaunchSpecification`` has no such member either, so the old path
  silently kept EC2's ``stop`` default: a self-shutting-down spot instance was
  left ``stopped`` with a billed EBS volume, which ``EC2_STATUS_MAPPING`` reports
  as COMPLETED -- orphaning the volume and its tracking record together. A mock
  cannot tell you EC2 accepts ``terminate`` on a one-time spot request; a real
  launch can.
* The hop limit is left at EC2's default of 2 rather than tightened to 1, so
  metadata stays reachable from a container spawned by ``worker_init``. EC2 does
  not echo an unset template field back, so the template omits it and the
  instance reports 2.
* The template is tagged with the provider ID and deleted on shutdown, so it does
  not accumulate against the account's per-region limit.

``InstanceInitiatedShutdownBehavior`` is not part of the ``describe_instances``
response -- it is an instance *attribute* and has to be fetched separately, which
is what ``_shutdown_behavior`` below is for.

The provider constructor initialises the operating mode, so the fixtures yield a
provider whose launch template already exists; there is no ``initialize()`` to
call on the provider itself.

Run with::

    AWS_TEST_REGION=us-east-1 AWS_TEST_VPC_ID=vpc-xxx \\
    AWS_TEST_SUBNET_ID=subnet-xxx AWS_TEST_SG_ID=sg-xxx \\
    AWS_PROFILE=aws uv run pytest tests/aws/test_launch_template_e2e.py \\
        -m aws --no-cov -v

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import logging

import pytest
from botocore.exceptions import ClientError

from parsl_aws_provider.constants import LAUNCH_TEMPLATE_NAME_PREFIX

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.aws, pytest.mark.slow]


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _template_data(ec2, mode):
    """Return the template document as EC2 stored it.

    The version is pinned to the one the mode built -- a template adopted from a
    previous run can carry several, and ``$Default`` need not be ours.
    """
    versions = ec2.describe_launch_template_versions(
        LaunchTemplateId=mode._launch_template_id,
        Versions=[mode._launch_template_version],
    )["LaunchTemplateVersions"]
    return versions[0]["LaunchTemplateData"]


def _instance(ec2, instance_id):
    return ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0][
        "Instances"
    ][0]


def _shutdown_behavior(ec2, instance_id):
    """Fetch the shutdown behaviour, which describe_instances does not return."""
    return ec2.describe_instance_attribute(
        InstanceId=instance_id, Attribute="instanceInitiatedShutdownBehavior"
    )["InstanceInitiatedShutdownBehavior"]["Value"]


def _cancel_quietly(provider, job_id):
    try:
        provider.cancel([job_id])
    except Exception as exc:
        logger.warning("teardown cancel raised (ignored): %s", exc)


# ---------------------------------------------------------------------------
# TestLaunchTemplateCreation
# ---------------------------------------------------------------------------


class TestLaunchTemplateCreation:
    """The template EC2 stores has to carry what the mode meant to set."""

    def test_template_is_created_and_tagged(
        self, aws_provider, aws_session, aws_region, test_run_id
    ):
        """Initialisation creates exactly one template, tagged for cleanup."""
        mode = aws_provider.operating_mode
        assert mode._launch_template_id, (
            "mode initialisation created no launch template; the spot path "
            "cannot enforce IMDSv2 without one"
        )

        ec2 = aws_session.client("ec2", region_name=aws_region)
        described = ec2.describe_launch_templates(
            LaunchTemplateIds=[mode._launch_template_id]
        )["LaunchTemplates"][0]

        assert described["LaunchTemplateName"] == mode.launch_template_name
        assert described["LaunchTemplateName"].startswith(LAUNCH_TEMPLATE_NAME_PREFIX)
        # EC2 caps the name at 128 characters and a UUID provider ID is appended.
        assert len(described["LaunchTemplateName"]) <= 128

        tags = {t["Key"]: t["Value"] for t in described.get("Tags", [])}
        assert tags.get("ProviderId") == aws_provider.provider_id
        assert tags.get("CreatedBy") == "ParslEphemeralAWSProvider"
        # additional_tags must reach the template too, or the E2E sweep and any
        # account-level cost allocation cannot see it.
        assert tags.get("E2ETestRunId") == test_run_id

    def test_template_requires_imdsv2(self, aws_provider, aws_session, aws_region):
        """IMDSv2 is the point of the exercise; assert EC2 recorded it."""
        ec2 = aws_session.client("ec2", region_name=aws_region)

        data = _template_data(ec2, aws_provider.operating_mode)

        assert data["MetadataOptions"]["HttpTokens"] == "required"
        # The endpoint itself must stay reachable: SSM reads the instance
        # identity document from IMDS, and one-shot dispatch depends on SSM.
        assert data["MetadataOptions"]["HttpEndpoint"] == "enabled"

    def test_template_leaves_the_hop_limit_at_the_default(
        self, aws_provider, aws_session, aws_region
    ):
        """Deliberately unset, so containers can still reach metadata."""
        ec2 = aws_session.client("ec2", region_name=aws_region)

        data = _template_data(ec2, aws_provider.operating_mode)

        assert "HttpPutResponseHopLimit" not in data["MetadataOptions"]

    def test_template_carries_shutdown_network_and_profile(
        self, aws_provider, aws_session, aws_region, network_ids
    ):
        """The three per-call kwargs #85 folded into the template."""
        ec2 = aws_session.client("ec2", region_name=aws_region)

        data = _template_data(ec2, aws_provider.operating_mode)

        assert data["InstanceInitiatedShutdownBehavior"] == "terminate"
        # The interface form, not top-level SecurityGroupIds: EC2 rejects a
        # template carrying both, and only the interface takes a public IP.
        interface = data["NetworkInterfaces"][0]
        assert interface["SubnetId"] == network_ids["subnet_id"]
        assert interface["Groups"] == [network_ids["security_group_id"]]
        # Resolved by _resolve_instance_profile() during initialisation, which
        # runs *after* __init__ -- so this cannot have been read in __init__.
        assert data.get("IamInstanceProfile", {}).get("Arn"), (
            "no IAM instance profile in the template; SSM will never come "
            "online and every dispatch falls back to UserData"
        )


# ---------------------------------------------------------------------------
# TestOnDemandLaunchFromTemplate
# ---------------------------------------------------------------------------


class TestOnDemandLaunchFromTemplate:
    """A submitted job has to inherit the template's hardening."""

    def test_instance_inherits_imdsv2_profile_and_terminate(
        self, aws_provider, aws_session, aws_region
    ):
        ec2 = aws_session.client("ec2", region_name=aws_region)
        job_id = aws_provider.submit("echo launch-template-e2e", tasks_per_node=1)
        try:
            instance_id = aws_provider.job_map[job_id]["resource_id"]
            instance = _instance(ec2, instance_id)

            assert instance["MetadataOptions"]["HttpTokens"] == "required"
            # AWS's default, not tightened to 1: a request from inside a
            # container traverses the host network namespace, costing a hop.
            assert instance["MetadataOptions"]["HttpPutResponseHopLimit"] == 2
            assert _shutdown_behavior(ec2, instance_id) == "terminate"
            assert instance.get("IamInstanceProfile"), "no instance profile attached"
        finally:
            _cancel_quietly(aws_provider, job_id)


# ---------------------------------------------------------------------------
# TestSpotLaunchFromTemplate
# ---------------------------------------------------------------------------


class TestSpotLaunchFromTemplate:
    """The spot path is why a template is mandatory rather than merely tidy."""

    def test_spot_instance_gets_imdsv2_and_keeps_terminate(
        self, spot_provider, aws_session, aws_region
    ):
        """Neither property is expressible through ``RequestSpotInstances``.

        Also asserts the instance really is spot -- the move to ``RunInstances``
        + ``InstanceMarketOptions`` must not have silently downgraded it to
        on-demand, which would cost roughly 3x and pass every other assertion
        here.
        """
        ec2 = aws_session.client("ec2", region_name=aws_region)
        job_id = spot_provider.submit("echo spot-launch-template-e2e", tasks_per_node=1)
        try:
            instance_id = spot_provider.job_map[job_id]["resource_id"]
            instance = _instance(ec2, instance_id)

            assert instance.get("InstanceLifecycle") == "spot", (
                "instance is not spot; InstanceMarketOptions did not take effect"
            )
            assert instance.get("SpotInstanceRequestId")
            assert instance["MetadataOptions"]["HttpTokens"] == "required"
            # Reverting to "stop" here would recreate the EBS cost leak that
            # Phase 1.3a closed on the on-demand path.
            assert _shutdown_behavior(ec2, instance_id) == "terminate"
        finally:
            _cancel_quietly(spot_provider, job_id)


# ---------------------------------------------------------------------------
# TestLaunchTemplateCleanup
# ---------------------------------------------------------------------------


class TestLaunchTemplateCleanup:
    """A leaked template counts against the per-region limit indefinitely."""

    def test_shutdown_deletes_the_template(self, aws_provider, aws_session, aws_region):
        template_id = aws_provider.operating_mode._launch_template_id
        assert template_id
        ec2 = aws_session.client("ec2", region_name=aws_region)

        aws_provider.shutdown()

        with pytest.raises(ClientError) as exc_info:
            ec2.describe_launch_templates(LaunchTemplateIds=[template_id])
        assert "NotFound" in exc_info.value.response["Error"]["Code"]
        # And the mode must forget it, so a later launch cannot reference a
        # template that no longer exists.
        assert aws_provider.operating_mode._launch_template_id is None

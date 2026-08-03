"""Unit tests for launch templates and IMDSv2 (#85).

Two things are being pinned down here.

**IMDSv2.** ``MetadataOptions`` was set on no launch path at all, so every
instance this package created accepted the unauthenticated IMDSv1 GET that an
SSRF-ed application can be tricked into making to read the instance's role
credentials. The setting has to reach EC2 on *every* path, and the paths do not
all accept it the same way -- verified against the botocore service model and
real EC2 in us-east-1:

- ``RunInstances`` takes ``MetadataOptions`` directly, and also takes a
  ``LaunchTemplate`` reference.
- ``RequestSpotInstances`` takes neither: it has no ``LaunchTemplate`` parameter,
  and its ``LaunchSpecification`` shape has no ``MetadataOptions`` member. IMDSv2
  cannot be set on that path by any means, which is why the spot path moves to
  ``RunInstances`` + ``InstanceMarketOptions`` whenever a template exists.
- ``RequestSpotFleet`` likewise: ``SpotFleetLaunchSpecification`` has no
  ``MetadataOptions``, so a launch template is the only route.

**The launch template itself**, which Phase 6's EC2 Fleet/ASG migration needs --
those APIs accept a template reference and nothing resembling ``RunInstances``
kwargs. Three traps are covered:

1. ``CreateLaunchTemplate`` does *not* base64-encode ``UserData``. botocore
   installs that handler on ``RunInstances`` and
   ``CreateLaunchConfiguration`` only, so plaintext user data passed to
   ``CreateLaunchTemplate`` is stored verbatim, base64-*decoded* by cloud-init,
   and produces garbage -- the instance boots fine and never runs the worker.
2. ``RequestSpotFleet`` accepts ``LaunchSpecifications`` and
   ``LaunchTemplateConfigs`` together (DryRun returns ``DryRunOperation``, not an
   error), but then the template silently loses -- taking IMDSv2 with it. Exactly
   one key may be sent.
3. The AMI builder must keep ``InstanceInitiatedShutdownBehavior="stop"``. The
   ``InstanceStopped`` waiter names ``terminated`` as an explicit *failure*
   acceptor, so inheriting the template's ``terminate`` would fail the bake, not
   merely slow it.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import ast
import base64
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError

from parsl_ephemeral_provider.compute.spot_fleet import SpotFleetManager
from parsl_ephemeral_provider.constants import (
    IMDSV2_METADATA_OPTIONS,
    LAUNCH_TEMPLATE_NAME_PREFIX,
)
from parsl_ephemeral_provider.exceptions import (
    ResourceCreationError,
    ResourceDeletionError,
)
from parsl_ephemeral_provider.modes.standard import StandardMode
from parsl_ephemeral_provider.utils.aws import (
    build_launch_template_data,
    create_launch_template,
    delete_launch_template,
    encode_user_data,
)

pytestmark = pytest.mark.unit


def _client_error(code, operation):
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class TestEncodeUserData:
    """botocore encodes user data for RunInstances only."""

    def test_round_trips_through_base64(self):
        script = "#!/bin/bash\necho hello\n"

        assert base64.b64decode(encode_user_data(script)).decode() == script

    def test_output_is_not_the_input(self):
        """The failure mode is silent, so assert the transform actually happened.

        Handing plaintext to ``CreateLaunchTemplate`` produces an instance that
        boots normally and simply never runs the worker -- nothing raises.
        """
        script = "#!/bin/bash\necho hello\n"

        assert encode_user_data(script) != script


class TestBuildLaunchTemplateData:
    """The document every launch path shares."""

    def test_imdsv2_is_required(self):
        data = build_launch_template_data("ami-1", "t3.micro")

        assert data["MetadataOptions"]["HttpTokens"] == "required"

    def test_metadata_endpoint_stays_enabled(self):
        """SSM reads the instance identity document from IMDS.

        Disabling the endpoint outright would break warm-pool and one-shot
        dispatch, both of which run over SSM ``SendCommand``.
        """
        data = build_launch_template_data("ami-1", "t3.micro")

        assert data["MetadataOptions"]["HttpEndpoint"] == "enabled"

    def test_hop_limit_is_not_lowered_to_one(self):
        """A hop limit of 1 leaves metadata unreachable from a container.

        ``worker_init`` may run containers, and a container's request traverses
        the host network namespace, which costs a hop. AWS's default of 2 is left
        alone rather than tightened, so the key must be absent.
        """
        data = build_launch_template_data("ami-1", "t3.micro")

        assert "HttpPutResponseHopLimit" not in data["MetadataOptions"]

    def test_metadata_options_are_copied_not_shared(self):
        """A caller must not be able to mutate the module-level constant."""
        data = build_launch_template_data("ami-1", "t3.micro")
        data["MetadataOptions"]["HttpTokens"] = "optional"

        assert IMDSV2_METADATA_OPTIONS["HttpTokens"] == "required"

    def test_shutdown_behavior_defaults_to_terminate(self):
        """A stopped instance still bills for its EBS volume (#66, 1.3a)."""
        data = build_launch_template_data("ami-1", "t3.micro")

        assert data["InstanceInitiatedShutdownBehavior"] == "terminate"

    def test_shutdown_behavior_is_overridable(self):
        data = build_launch_template_data("ami-1", "t3.micro", shutdown_behavior="stop")

        assert data["InstanceInitiatedShutdownBehavior"] == "stop"

    def test_subnet_produces_a_network_interface(self):
        data = build_launch_template_data(
            "ami-1", "t3.micro", subnet_id="subnet-1", security_group_id="sg-1"
        )

        assert data["NetworkInterfaces"] == [
            {
                "DeviceIndex": 0,
                "SubnetId": "subnet-1",
                "AssociatePublicIpAddress": True,
                "Groups": ["sg-1"],
            }
        ]

    def test_network_interface_and_top_level_groups_are_exclusive(self):
        """EC2 rejects a template carrying both.

        The interface form is required for ``AssociatePublicIpAddress``, so it
        wins whenever a subnet is known.
        """
        data = build_launch_template_data(
            "ami-1", "t3.micro", subnet_id="subnet-1", security_group_id="sg-1"
        )

        assert "SecurityGroupIds" not in data

    def test_security_group_without_subnet_goes_top_level(self):
        data = build_launch_template_data("ami-1", "t3.micro", security_group_id="sg-1")

        assert data["SecurityGroupIds"] == ["sg-1"]
        assert "NetworkInterfaces" not in data

    def test_public_ip_can_be_disabled(self):
        data = build_launch_template_data(
            "ami-1", "t3.micro", subnet_id="subnet-1", associate_public_ip=False
        )

        assert data["NetworkInterfaces"][0]["AssociatePublicIpAddress"] is False

    def test_user_data_is_base64_encoded(self):
        script = "#!/bin/bash\necho hi\n"

        data = build_launch_template_data("ami-1", "t3.micro", user_data=script)

        assert base64.b64decode(data["UserData"]).decode() == script

    def test_absent_user_data_is_omitted(self):
        """The mode leaves it out on purpose -- it is per-job, not per-template."""
        data = build_launch_template_data("ami-1", "t3.micro")

        assert "UserData" not in data

    def test_iam_profile_is_wrapped_in_arn(self):
        data = build_launch_template_data(
            "ami-1", "t3.micro", iam_instance_profile_arn="arn:aws:iam::1:x/y"
        )

        assert data["IamInstanceProfile"] == {"Arn": "arn:aws:iam::1:x/y"}

    def test_optional_fields_are_omitted_when_unset(self):
        data = build_launch_template_data("ami-1", "t3.micro")

        for key in ("KeyName", "IamInstanceProfile", "Monitoring"):
            assert key not in data


class TestCreateLaunchTemplate:
    """Creation has to be idempotent and has to publish a new version."""

    def test_returns_id_and_version_as_strings(self):
        ec2 = MagicMock()
        ec2.create_launch_template.return_value = {
            "LaunchTemplate": {"LaunchTemplateId": "lt-1", "LatestVersionNumber": 1}
        }

        assert create_launch_template(ec2, "n", {"ImageId": "ami-1"}) == ("lt-1", "1")

    def test_tags_are_attached_to_the_template_resource(self):
        """Cleanup and orphan-hunting both key off the provider ID tag."""
        ec2 = MagicMock()
        ec2.create_launch_template.return_value = {
            "LaunchTemplate": {"LaunchTemplateId": "lt-1", "LatestVersionNumber": 1}
        }
        tags = [{"Key": "ProviderId", "Value": "p-1"}]

        create_launch_template(ec2, "n", {"ImageId": "ami-1"}, tags)

        assert ec2.create_launch_template.call_args.kwargs["TagSpecifications"] == [
            {"ResourceType": "launch-template", "Tags": tags}
        ]

    def test_existing_template_gets_a_new_version(self):
        """``initialize()`` may run again after a partial failure.

        A second ``CreateLaunchTemplate`` under the same name is rejected with
        ``InvalidLaunchTemplateName.AlreadyExistsException``. A *new version* is
        published rather than the old one reused, so a changed AMI or instance
        type actually takes effect.
        """
        ec2 = MagicMock()
        ec2.create_launch_template.side_effect = _client_error(
            "InvalidLaunchTemplateName.AlreadyExistsException", "CreateLaunchTemplate"
        )
        ec2.create_launch_template_version.return_value = {
            "LaunchTemplateVersion": {
                "LaunchTemplateId": "lt-1",
                "VersionNumber": 4,
            }
        }

        assert create_launch_template(ec2, "n", {"ImageId": "ami-2"}) == ("lt-1", "4")
        assert ec2.create_launch_template_version.call_args.kwargs[
            "LaunchTemplateData"
        ] == {"ImageId": "ami-2"}

    def test_other_client_errors_raise(self):
        ec2 = MagicMock()
        ec2.create_launch_template.side_effect = _client_error(
            "UnauthorizedOperation", "CreateLaunchTemplate"
        )

        with pytest.raises(ResourceCreationError):
            create_launch_template(ec2, "n", {"ImageId": "ami-1"})

    def test_failed_version_creation_raises(self):
        ec2 = MagicMock()
        ec2.create_launch_template.side_effect = _client_error(
            "InvalidLaunchTemplateName.AlreadyExistsException", "CreateLaunchTemplate"
        )
        ec2.create_launch_template_version.side_effect = _client_error(
            "UnauthorizedOperation", "CreateLaunchTemplateVersion"
        )

        with pytest.raises(ResourceCreationError):
            create_launch_template(ec2, "n", {"ImageId": "ami-1"})


class TestDeleteLaunchTemplate:
    """Deletion must tolerate a template that is already gone."""

    def test_deletes_by_id(self):
        ec2 = MagicMock()

        delete_launch_template(ec2, "lt-1")

        ec2.delete_launch_template.assert_called_once_with(LaunchTemplateId="lt-1")

    def test_missing_template_is_not_an_error(self):
        ec2 = MagicMock()
        ec2.delete_launch_template.side_effect = _client_error(
            "InvalidLaunchTemplateId.NotFound", "DeleteLaunchTemplate"
        )

        delete_launch_template(ec2, "lt-1")

    def test_other_errors_raise(self):
        ec2 = MagicMock()
        ec2.delete_launch_template.side_effect = _client_error(
            "UnauthorizedOperation", "DeleteLaunchTemplate"
        )

        with pytest.raises(ResourceDeletionError):
            delete_launch_template(ec2, "lt-1")


class TestStandardModeLaunchTemplate:
    """The mode builds one template in initialize() and deletes it on cleanup."""

    @pytest.fixture
    def ec2(self):
        client = MagicMock()
        client.create_launch_template.return_value = {
            "LaunchTemplate": {"LaunchTemplateId": "lt-1", "LatestVersionNumber": 3}
        }
        client.run_instances.return_value = {
            "Instances": [{"InstanceId": "i-1", "State": {"Name": "pending"}}]
        }
        client.describe_instances.return_value = {
            "Reservations": [
                {"Instances": [{"InstanceId": "i-1", "State": {"Name": "running"}}]}
            ]
        }
        return client

    @pytest.fixture
    def mode(self, ec2):
        session = MagicMock(spec=boto3.Session)
        session.region_name = "us-east-1"
        session.client.return_value = ec2
        store = MagicMock()
        store.load_state.return_value = None
        return StandardMode(
            provider_id="test-provider",
            session=session,
            state_store=store,
            instance_type="t3.micro",
            image_id="ami-12345678",
            region="us-east-1",
            vpc_id="vpc-12345",
            subnet_id="subnet-12345",
            security_group_id="sg-12345",
        )

    def _initialize(self, mode):
        with patch.object(mode, "_verify_resources"):
            mode.initialize()

    def test_initialize_creates_the_template(self, mode, ec2):
        self._initialize(mode)

        ec2.create_launch_template.assert_called_once()
        assert mode._launch_template_id == "lt-1"
        assert mode._launch_template_version == "3"

    def test_template_requires_imdsv2(self, mode, ec2):
        self._initialize(mode)

        data = ec2.create_launch_template.call_args.kwargs["LaunchTemplateData"]
        assert data["MetadataOptions"]["HttpTokens"] == "required"

    def test_template_carries_shutdown_behavior(self, mode, ec2):
        self._initialize(mode)

        data = ec2.create_launch_template.call_args.kwargs["LaunchTemplateData"]
        assert data["InstanceInitiatedShutdownBehavior"] == "terminate"

    def test_template_is_named_for_the_provider(self, mode, ec2):
        self._initialize(mode)

        name = ec2.create_launch_template.call_args.kwargs["LaunchTemplateName"]
        assert name == f"{LAUNCH_TEMPLATE_NAME_PREFIX}-test-provider"
        assert len(name) <= 128

    def test_template_is_tagged_with_the_provider_id(self, mode, ec2):
        """Nothing else identifies the template as ours during orphan cleanup."""
        self._initialize(mode)

        tags = ec2.create_launch_template.call_args.kwargs["TagSpecifications"][0][
            "Tags"
        ]
        assert {"Key": "ProviderId", "Value": "test-provider"} in tags

    def test_template_omits_per_job_fields(self, mode, ec2):
        """UserData and tags are per-job and are passed at launch instead."""
        self._initialize(mode)

        data = ec2.create_launch_template.call_args.kwargs["LaunchTemplateData"]
        assert "UserData" not in data
        assert "TagSpecifications" not in data

    def test_template_references_the_baked_ami(self, mode, ec2):
        """Order matters: baking reassigns ``self.image_id``.

        Building the template first would pin the base AMI and every worker
        would launch without the baked-in ``worker_init``.
        """
        mode.bake_ami = True
        with patch.object(mode, "_bake_ami", return_value="ami-baked"):
            self._initialize(mode)

        data = ec2.create_launch_template.call_args.kwargs["LaunchTemplateData"]
        assert data["ImageId"] == "ami-baked"

    def test_template_carries_the_resolved_iam_profile(self, ec2):
        """Resolved in initialize(), so it cannot be read in __init__."""
        session = MagicMock(spec=boto3.Session)
        session.region_name = "us-east-1"
        session.client.return_value = ec2
        store = MagicMock()
        store.load_state.return_value = None
        mode = StandardMode(
            provider_id="test-provider",
            session=session,
            state_store=store,
            image_id="ami-12345678",
            vpc_id="vpc-12345",
            subnet_id="subnet-12345",
            security_group_id="sg-12345",
            auto_create_instance_profile=True,
        )
        with patch(
            "parsl_ephemeral_provider.modes.standard.get_or_create_ssm_instance_profile",
            return_value="arn:aws:iam::1:instance-profile/p",
        ):
            self._initialize(mode)

        data = ec2.create_launch_template.call_args.kwargs["LaunchTemplateData"]
        assert data["IamInstanceProfile"] == {
            "Arn": "arn:aws:iam::1:instance-profile/p"
        }

    def test_template_creation_failure_is_not_fatal(self, mode, ec2):
        """An account without ec2:CreateLaunchTemplate must keep working."""
        ec2.create_launch_template.side_effect = _client_error(
            "UnauthorizedOperation", "CreateLaunchTemplate"
        )

        self._initialize(mode)

        assert mode.initialized is True
        assert mode._launch_template_id is None

    def test_cleanup_deletes_the_template(self, mode, ec2):
        self._initialize(mode)

        mode.cleanup_infrastructure()

        ec2.delete_launch_template.assert_called_once_with(LaunchTemplateId="lt-1")
        assert mode._launch_template_id is None

    def test_failed_deletion_still_clears_the_id(self, mode, ec2):
        """Cleanup is not retried, so a stale ID would be used by a later launch."""
        self._initialize(mode)
        ec2.delete_launch_template.side_effect = _client_error(
            "UnauthorizedOperation", "DeleteLaunchTemplate"
        )

        mode.cleanup_infrastructure()

        assert mode._launch_template_id is None

    def test_template_survives_a_state_round_trip(self, mode, ec2):
        """Otherwise a resumed provider leaks the old one and builds a second."""
        self._initialize(mode)
        state = mode.state_store.save_state.call_args.args[1]
        assert state["launch_template_id"] == "lt-1"
        assert state["launch_template_version"] == "3"

        mode._launch_template_id = None
        mode._launch_template_version = None
        mode.state_store.load_state.return_value = state
        assert mode.load_state() is True

        assert mode._launch_template_id == "lt-1"
        assert mode._launch_template_version == "3"

    def test_resumed_pre_85_state_gets_a_template(self, mode, ec2):
        """A v0.6.0 state document carries no template ID.

        Without this the resumed provider would sit on the fallback path
        permanently, so its spot launches would never get IMDSv2.
        """
        mode.state_store.load_state.return_value = {
            "provider_id": "test-provider",
            "resources": {},
            "initialized": True,
            "vpc_id": "vpc-12345",
            "subnet_id": "subnet-12345",
            "security_group_id": "sg-12345",
        }

        self._initialize(mode)

        assert mode._launch_template_id == "lt-1"
        ec2.create_launch_template.assert_called_once()

    def test_version_is_pinned_not_latest(self, mode):
        """An adopted template may carry versions this mode did not build."""
        mode._launch_template_id = "lt-1"
        mode._launch_template_version = "3"

        assert mode._launch_template_reference() == {
            "LaunchTemplateId": "lt-1",
            "Version": "3",
        }

    def test_no_reference_without_a_template(self, mode):
        assert mode._launch_template_reference() is None


class TestStandardModeLaunchPaths:
    """Each launch path has to send the template rather than raw kwargs."""

    @pytest.fixture
    def ec2(self):
        client = MagicMock()
        client.create_launch_template.return_value = {
            "LaunchTemplate": {"LaunchTemplateId": "lt-1", "LatestVersionNumber": 3}
        }
        client.run_instances.return_value = {
            "Instances": [{"InstanceId": "i-1", "State": {"Name": "pending"}}]
        }
        return client

    def _mode(self, ec2, **kwargs):
        session = MagicMock(spec=boto3.Session)
        session.region_name = "us-east-1"
        session.client.return_value = ec2
        store = MagicMock()
        store.load_state.return_value = None
        mode = StandardMode(
            provider_id="test-provider",
            session=session,
            state_store=store,
            instance_type="t3.micro",
            image_id="ami-12345678",
            region="us-east-1",
            vpc_id="vpc-12345",
            subnet_id="subnet-12345",
            security_group_id="sg-12345",
            **kwargs,
        )
        with patch.object(mode, "_verify_resources"):
            mode.initialize()
        return mode

    def test_on_demand_launch_uses_the_template(self, ec2):
        mode = self._mode(ec2)

        with patch("parsl_ephemeral_provider.modes.standard.wait_for_resource"):
            mode._create_instance("#!/bin/bash\necho hi\n", "job-1")

        kwargs = ec2.run_instances.call_args.kwargs
        assert kwargs["LaunchTemplate"] == {
            "LaunchTemplateId": "lt-1",
            "Version": "3",
        }
        # The template owns these now; repeating them would be the duplication
        # #85 removes, and a stale copy here would silently win.
        for key in (
            "ImageId",
            "InstanceType",
            "NetworkInterfaces",
            "MetadataOptions",
            "InstanceInitiatedShutdownBehavior",
        ):
            assert key not in kwargs

    def test_on_demand_launch_still_overrides_per_job_fields(self, ec2):
        mode = self._mode(ec2)

        with patch("parsl_ephemeral_provider.modes.standard.wait_for_resource"):
            mode._create_instance("#!/bin/bash\necho hi\n", "job-1")

        kwargs = ec2.run_instances.call_args.kwargs
        # Plaintext: botocore base64-encodes UserData for RunInstances, so
        # encoding it here as well would double-encode it.
        assert kwargs["UserData"] == "#!/bin/bash\necho hi\n"
        tags = kwargs["TagSpecifications"][0]["Tags"]
        assert {"Key": "JobId", "Value": "job-1"} in tags

    def test_fallback_path_sets_metadata_options_itself(self, ec2):
        """No template means IMDSv2 has to be set per launch instead."""
        mode = self._mode(ec2)
        mode._launch_template_id = None

        with patch("parsl_ephemeral_provider.modes.standard.wait_for_resource"):
            mode._create_instance("#!/bin/bash\necho hi\n", "job-1")

        kwargs = ec2.run_instances.call_args.kwargs
        assert kwargs["MetadataOptions"]["HttpTokens"] == "required"
        assert kwargs["InstanceInitiatedShutdownBehavior"] == "terminate"
        assert kwargs["ImageId"] == "ami-12345678"

    def test_spot_launch_goes_through_run_instances(self, ec2):
        """``RequestSpotInstances`` can never carry IMDSv2.

        It accepts no ``LaunchTemplate``, and its ``LaunchSpecification`` shape
        has no ``MetadataOptions`` member -- so the only way to harden a spot
        instance is ``RunInstances`` + ``InstanceMarketOptions``.
        """
        mode = self._mode(ec2, use_spot=True)

        with patch("parsl_ephemeral_provider.modes.standard.wait_for_resource"):
            mode._create_instance("#!/bin/bash\necho hi\n", "job-1")

        ec2.request_spot_instances.assert_not_called()
        kwargs = ec2.run_instances.call_args.kwargs
        assert kwargs["InstanceMarketOptions"] == {"MarketType": "spot"}
        assert kwargs["LaunchTemplate"]["LaunchTemplateId"] == "lt-1"

    def test_spot_launch_keeps_terminate_shutdown_behavior(self, ec2):
        """Verified against real EC2: a spot instance accepts ``terminate``.

        The old ``RequestSpotInstances`` path had to drop the setting -- its
        ``LaunchSpecification`` has no such member -- so a self-shutting-down
        spot instance was left *stopped*, with a billed EBS volume that
        ``EC2_STATUS_MAPPING`` then reported as COMPLETED, dropping the tracking
        record. Forcing ``stop`` back on here would recreate that leak.
        """
        mode = self._mode(ec2, use_spot=True)

        with patch("parsl_ephemeral_provider.modes.standard.wait_for_resource"):
            mode._create_instance("#!/bin/bash\necho hi\n", "job-1")

        kwargs = ec2.run_instances.call_args.kwargs
        assert kwargs.get("InstanceInitiatedShutdownBehavior") != "stop"
        data = ec2.create_launch_template.call_args.kwargs["LaunchTemplateData"]
        assert data["InstanceInitiatedShutdownBehavior"] == "terminate"

    def test_spot_max_price_becomes_spot_options(self, ec2):
        mode = self._mode(ec2, use_spot=True, spot_max_price="0.05")

        with patch("parsl_ephemeral_provider.modes.standard.wait_for_resource"):
            mode._create_instance("#!/bin/bash\necho hi\n", "job-1")

        kwargs = ec2.run_instances.call_args.kwargs
        assert kwargs["InstanceMarketOptions"] == {
            "MarketType": "spot",
            "SpotOptions": {"MaxPrice": "0.05"},
        }

    def test_spot_falls_back_to_request_spot_instances_without_a_template(self, ec2):
        mode = self._mode(ec2, use_spot=True)
        mode._launch_template_id = None
        ec2.request_spot_instances.return_value = {
            "SpotInstanceRequests": [{"SpotInstanceRequestId": "sir-1"}]
        }
        ec2.describe_spot_instance_requests.return_value = {
            "SpotInstanceRequests": [{"InstanceId": "i-1"}]
        }

        with patch("parsl_ephemeral_provider.modes.standard.wait_for_resource"):
            mode._create_instance("#!/bin/bash\necho hi\n", "job-1")

        ec2.request_spot_instances.assert_called_once()

    def test_ami_builder_keeps_stop_and_avoids_the_template(self, ec2):
        """The one launch that must not inherit ``terminate``.

        UserData ends in ``shutdown -h now`` and ``create_image`` needs the
        stopped instance to snapshot. The ``InstanceStopped`` waiter names
        ``terminated`` as an explicit *failure* acceptor, so inheriting the
        template's ``terminate`` would fail the bake outright.
        """
        mode = self._mode(ec2)

        mode._launch_builder_instance()

        kwargs = ec2.run_instances.call_args.kwargs
        assert kwargs["InstanceInitiatedShutdownBehavior"] == "stop"
        assert "LaunchTemplate" not in kwargs

    def test_ami_builder_still_requires_imdsv2(self, ec2):
        """A worker_init that works under IMDSv1 here would break every worker
        launched from the resulting AMI."""
        mode = self._mode(ec2)

        mode._launch_builder_instance()

        kwargs = ec2.run_instances.call_args.kwargs
        assert kwargs["MetadataOptions"]["HttpTokens"] == "required"


class TestSpotFleetLaunchTemplate:
    """An EC2 Fleet reaches its instances only through a launch template.

    True by construction since #86: ``CreateFleet`` has no
    ``LaunchSpecifications`` member, so there is nothing to fall back to and no
    way to launch without a template. Under the legacy ``RequestSpotFleet`` the
    template was merely preferred, and the fallback silently dropped IMDSv2.
    """

    @pytest.fixture
    def ec2(self):
        client = MagicMock()
        client.create_launch_template.return_value = {
            "LaunchTemplate": {"LaunchTemplateId": "lt-fleet", "LatestVersionNumber": 1}
        }
        client.create_fleet.return_value = {
            "FleetId": "fleet-1",
            "Instances": [{"InstanceIds": ["i-1"]}],
        }
        client.describe_spot_price_history.return_value = {
            "SpotPriceHistory": [{"SpotPrice": "0.01"}]
        }
        return client

    @pytest.fixture
    def manager(self, ec2):
        provider = type(
            "SimpleProvider",
            (),
            {
                "workflow_id": "wf-1",
                "region": "us-east-1",
                "aws_profile": None,
                "vpc_id": "vpc-1",
                "subnet_id": "subnet-1",
                "security_group_id": "sg-1",
                "image_id": "ami-1",
                "instance_type": "t3.micro",
                "instance_types": ["t3.micro", "t3.small"],
                "key_name": None,
                "use_public_ips": True,
                "nodes_per_block": 1,
                "spot_max_price_percentage": 100,
                "worker_init": "echo hi",
                "tags": {},
                "iam_instance_profile_arn": "arn:aws:iam::1:instance-profile/p",
            },
        )()
        session = MagicMock()
        session.client.return_value = ec2
        session.resource.return_value = MagicMock()
        with patch(
            "parsl_ephemeral_provider.compute.spot_fleet.CredentialManager"
        ) as cred_cls:
            cred_cls.return_value.create_boto3_session.return_value = session
            return SpotFleetManager(provider)

    def _request(self, manager):
        manager._create_fleet(
            "block-1",
            {
                "vpc_id": "vpc-1",
                "subnet_id": "subnet-1",
                "security_group_id": "sg-1",
            },
            1,
        )
        return manager.ec2_client.create_fleet.call_args.kwargs

    def test_fleet_request_sends_only_the_template_form(self, manager):
        """There is no other form to send.

        Under ``RequestSpotFleet`` this was a real hazard: EC2 accepted a request
        carrying ``LaunchSpecifications`` *and* ``LaunchTemplateConfigs`` --
        DryRun against real EC2 returned ``DryRunOperation`` -- then let the
        specifications win, taking IMDSv2 with them. ``CreateFleet`` has no
        ``LaunchSpecifications`` member at all, so the hazard is structural now
        rather than a matter of care (#86). Asserted anyway: the legacy key would
        be rejected outright, so sending it would fail every launch.
        """
        config = self._request(manager)

        assert "LaunchTemplateConfigs" in config
        assert "LaunchSpecifications" not in config
        assert "SpotFleetRequestConfig" not in config

    def test_fleet_template_requires_imdsv2(self, manager, ec2):
        """The only route: ``SpotFleetLaunchSpecification`` has no
        ``MetadataOptions`` member at all."""
        self._request(manager)

        data = ec2.create_launch_template.call_args.kwargs["LaunchTemplateData"]
        assert data["MetadataOptions"]["HttpTokens"] == "required"

    def test_fleet_template_carries_encoded_user_data(self, manager, ec2):
        """``Overrides`` cannot carry ``UserData``, so it lives in the template.

        And ``CreateLaunchTemplate`` does not base64-encode it, unlike
        ``RunInstances`` -- plaintext here boots an instance that never runs the
        worker, with nothing raising.
        """
        self._request(manager)

        data = ec2.create_launch_template.call_args.kwargs["LaunchTemplateData"]
        assert base64.b64decode(data["UserData"]).decode().startswith("#!/bin/bash")

    def test_fleet_template_carries_the_iam_profile(self, manager, ec2):
        self._request(manager)

        data = ec2.create_launch_template.call_args.kwargs["LaunchTemplateData"]
        assert data["IamInstanceProfile"] == {
            "Arn": "arn:aws:iam::1:instance-profile/p"
        }

    def test_every_instance_type_becomes_an_override(self, manager):
        """One template covers every pool the fleet may draw from.

        Each type must appear exactly once. ``CreateFleet`` rejects a repeated
        pool -- "InvalidFleetConfig: The fleet configuration contains duplicate
        instance pools" -- and ``DryRun=True`` does *not* catch it, so a
        duplicate would only surface on a real launch.
        """
        config = self._request(manager)

        overrides = config["LaunchTemplateConfigs"][0]["Overrides"]
        pools = [(o["InstanceType"], o["SubnetId"]) for o in overrides]
        assert len(pools) == len(set(pools))
        assert [o["InstanceType"] for o in overrides] == ["t3.micro", "t3.small"]
        assert all(o["SubnetId"] == "subnet-1" for o in overrides)

    def test_template_failure_fails_the_block(self, manager, ec2):
        """A template that cannot be created must fail the block, not degrade it.

        This inverts what the legacy path did. ``RequestSpotFleet`` accepted
        inline ``LaunchSpecifications``, so a failed template fell back to them
        -- launching a working fleet, but silently without IMDSv2, since
        ``SpotFleetLaunchSpecification`` has no ``MetadataOptions`` member.
        ``CreateFleet`` has no such member either, so there is nothing to fall
        back to and the failure surfaces (#86). Worth asserting rather than
        assuming: a fallback reintroduced here would be an unannounced security
        downgrade.
        """
        ec2.create_launch_template.side_effect = _client_error(
            "UnauthorizedOperation", "CreateLaunchTemplate"
        )

        with pytest.raises(ResourceCreationError):
            self._request(manager)

        ec2.create_fleet.assert_not_called()

    def test_no_fleet_is_requested_without_a_template(self, manager, ec2):
        """Every launch goes through the template, so every launch gets IMDSv2.

        The one property the fallback used to break. Asserting on the encoded
        UserData is how the launch is shown to be template-borne: the fleet
        request itself carries none, because ``CreateFleet``'s ``Overrides``
        shape has no ``UserData`` member.
        """
        config = self._request(manager)

        assert "UserData" not in config
        data = ec2.create_launch_template.call_args.kwargs["LaunchTemplateData"]
        assert base64.b64decode(data["UserData"]).decode().startswith("#!/bin/bash")
        assert data["MetadataOptions"]["HttpTokens"] == "required"

    def test_template_is_tracked_per_block(self, manager):
        self._request(manager)

        assert manager.launch_templates == {"block-1": "lt-fleet"}

    def test_terminating_a_block_deletes_its_template(self, manager, ec2):
        self._request(manager)
        manager.blocks["block-1"] = {
            "fleet_request_id": "fleet-1",
            "instance_ids": [],
            "status": "RUNNING",
        }

        manager.terminate_block("block-1")

        ec2.delete_launch_template.assert_called_once_with(LaunchTemplateId="lt-fleet")
        assert manager.launch_templates == {}

    def test_a_block_with_no_fleet_request_still_drops_its_template(self, manager, ec2):
        """The template belongs to the block, not to the fleet request.

        ``terminate_block`` returns early when the request ID was never
        recorded, which would otherwise leak the template until
        ``cleanup_all_resources``.
        """
        self._request(manager)
        manager.blocks["block-1"] = {"status": "RUNNING"}

        manager.terminate_block("block-1")

        ec2.delete_launch_template.assert_called_once_with(LaunchTemplateId="lt-fleet")

    def test_cleanup_deletes_every_remaining_template(self, manager, ec2):
        self._request(manager)

        manager.cleanup_all_resources()

        ec2.delete_launch_template.assert_any_call(LaunchTemplateId="lt-fleet")
        assert manager.launch_templates == {}


class TestBastionWorkerMetadataOptions:
    """The bastion launches workers from a script it runs standalone.

    It cannot import from the package, so the options are injected as a literal
    at script generation time. ``str.replace`` finding nothing is not an error,
    so the generated text is what has to be asserted on -- not the source.
    """

    def _script(self):
        from parsl_ephemeral_provider.modes.detached import DetachedMode

        mode = DetachedMode(
            provider_id="test-provider",
            session=MagicMock(),
            state_store=MagicMock(),
            workflow_id="test-workflow",
            instance_type="t3.small",
            image_id="ami-12345678",
            region="us-east-1",
            vpc_id="vpc-12345",
            subnet_id="subnet-12345",
            security_group_id="sg-12345",
        )
        return mode._get_bastion_manager_script()

    def _injected_value(self, script):
        for line in script.splitlines():
            if line.startswith("METADATA_OPTIONS = "):
                return ast.literal_eval(
                    line.removeprefix("METADATA_OPTIONS = ").split("  #")[0]
                )
        raise AssertionError("script defines no METADATA_OPTIONS constant")

    def test_injected_options_match_the_package_constant(self):
        """A copy in the template would drift the next time the constant moves."""
        assert self._injected_value(self._script()) == IMDSV2_METADATA_OPTIONS

    def test_workers_launched_by_the_bastion_get_imdsv2(self):
        assert self._injected_value(self._script())["HttpTokens"] == "required"

    def test_generated_script_is_valid_python(self):
        """The substitution must not break the script the bastion has to run."""
        ast.parse(self._script())

    def test_worker_launch_reads_the_constant(self):
        script = self._script()

        assert "'MetadataOptions': METADATA_OPTIONS," in script

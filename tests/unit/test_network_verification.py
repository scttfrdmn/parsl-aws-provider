"""Unit tests for network-resource verification across all three modes.

Since #69 the VPC, subnet, and security group are supplied by the caller and are
never created by this package. Two places still behaved as though they could be
conjured back:

* ``_verify_resources()`` set the attribute to ``None`` when the describe call
  came back NotFound, so that ``initialize()`` would rebuild it. Nothing rebuilds
  it now, and the ``None`` travelled to ``run_instances`` to surface as an opaque
  ``InvalidParameterValue`` with no mention of the resource that was actually
  missing.
* ``OperatingMode.load_state()`` restored these IDs unconditionally, so a state
  document written before they were required could put ``None`` back over an ID
  the constructor had just validated.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError

from parsl_ephemeral_aws.exceptions import ResourceNotFoundError
from parsl_ephemeral_aws.modes.detached import DetachedMode
from parsl_ephemeral_aws.modes.serverless import ServerlessMode
from parsl_ephemeral_aws.modes.standard import StandardMode


pytestmark = pytest.mark.unit

NETWORK_IDS = {
    "vpc_id": "vpc-12345",
    "subnet_id": "subnet-12345",
    "security_group_id": "sg-12345",
}

#: (attribute, describe method, NotFound code) for each network resource.
RESOURCES = [
    ("vpc_id", "describe_vpcs", "InvalidVpcID.NotFound"),
    ("subnet_id", "describe_subnets", "InvalidSubnetID.NotFound"),
    ("security_group_id", "describe_security_groups", "InvalidGroup.NotFound"),
]

#: EC2 reports a syntactically invalid ID with a distinct code rather than
#: NotFound. Confirmed against real AWS: ``sg-00000000000000000`` gives
#: ``InvalidGroupId.Malformed``, while a subnet or VPC ID of the same shape gives
#: ``NotFound``. Both mean the supplied ID is unusable.
MALFORMED = [
    ("vpc_id", "describe_vpcs", "InvalidVpcID.Malformed"),
    ("subnet_id", "describe_subnets", "InvalidSubnetId.Malformed"),
    ("security_group_id", "describe_security_groups", "InvalidGroupId.Malformed"),
]

#: All three modes verify the same three resources with the same code, which is
#: why the implementation now lives once on ``OperatingMode``. Each mode is still
#: exercised separately -- the bug was three copies drifting, and a single
#: inherited method is only an improvement if every mode really does inherit it.
MODES = [StandardMode, DetachedMode, ServerlessMode]


def _client_error(code, operation="DescribeVpcs"):
    """Return a ClientError carrying *code*, as boto3 would raise it."""
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


@pytest.fixture
def ec2():
    """A mock EC2 client whose describe calls all succeed by default."""
    client = MagicMock()
    client.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-12345"}]}
    client.describe_subnets.return_value = {"Subnets": [{"SubnetId": "subnet-12345"}]}
    client.describe_security_groups.return_value = {
        "SecurityGroups": [{"GroupId": "sg-12345"}]
    }
    return client


@pytest.fixture
def session(ec2):
    session = MagicMock(spec=boto3.Session)
    session.region_name = "us-east-1"
    session.client.return_value = ec2
    return session


@pytest.fixture
def state_store():
    store = MagicMock()
    store.load_state.return_value = None
    return store


def _mode(mode_class, session, state_store, **overrides):
    params = {
        "provider_id": "test-provider",
        "session": session,
        "state_store": state_store,
        "image_id": "ami-12345",
        **NETWORK_IDS,
    }
    params.update(overrides)
    return mode_class(**params)


class TestVerifyResourcesRaisesOnMissingNetwork:
    """A missing network resource must be named, not silently nulled."""

    @pytest.mark.parametrize("mode_class", MODES, ids=lambda c: c.__name__)
    @pytest.mark.parametrize("attribute,describe,code", RESOURCES, ids=lambda v: str(v))
    def test_missing_resource_raises_and_names_it(
        self, mode_class, attribute, describe, code, session, state_store, ec2
    ):
        """The ID and the attribute both appear in the message.

        Previously this logged a warning and set the attribute to ``None``, so
        the failure resurfaced later as ``InvalidParameterValue`` from inside
        ``run_instances`` -- an error that names neither the resource nor the
        reason.
        """
        mode = _mode(mode_class, session, state_store)
        getattr(ec2, describe).side_effect = _client_error(code)

        with pytest.raises(ResourceNotFoundError) as excinfo:
            mode._verify_resources()

        message = str(excinfo.value)
        assert attribute in message
        assert NETWORK_IDS[attribute] in message

        # And the ID is left intact for the caller to inspect or correct.
        assert getattr(mode, attribute) == NETWORK_IDS[attribute]

    @pytest.mark.parametrize("mode_class", MODES, ids=lambda c: c.__name__)
    @pytest.mark.parametrize("attribute,describe,code", MALFORMED, ids=lambda v: str(v))
    def test_a_malformed_id_is_reported_the_same_way(
        self, mode_class, attribute, describe, code, session, state_store, ec2
    ):
        """A typo'd ID is the same user error as a deleted one.

        Only NotFound was matched at first, so a malformed ID escaped as a raw
        ClientError from whichever describe happened to run -- found by feeding
        a bogus ID to a real AWS account.
        """
        mode = _mode(mode_class, session, state_store)
        getattr(ec2, describe).side_effect = _client_error(code)

        with pytest.raises(ResourceNotFoundError) as excinfo:
            mode._verify_resources()

        assert attribute in str(excinfo.value)

    @pytest.mark.parametrize("mode_class", MODES, ids=lambda c: c.__name__)
    def test_the_error_code_is_read_not_string_matched(
        self, mode_class, session, state_store, ec2
    ):
        """Matching must key off the code field, not the rendered message.

        A resource whose *name* or ARN contains the text "InvalidVpcID.NotFound"
        would otherwise be mistaken for a missing one, and the reverse: any
        future change to botocore's message formatting would silently stop the
        match. The code field is the contract.
        """
        mode = _mode(mode_class, session, state_store)
        ec2.describe_vpcs.side_effect = ClientError(
            {
                "Error": {
                    "Code": "RequestLimitExceeded",
                    "Message": "throttled; unrelated to InvalidVpcID.NotFound",
                }
            },
            "DescribeVpcs",
        )

        with pytest.raises(ClientError) as excinfo:
            mode._verify_resources()

        assert excinfo.value.response["Error"]["Code"] == "RequestLimitExceeded"

    @pytest.mark.parametrize("mode_class", MODES, ids=lambda c: c.__name__)
    def test_all_present_verifies_quietly(self, mode_class, session, state_store, ec2):
        """The happy path checks all three and changes nothing."""
        mode = _mode(mode_class, session, state_store)

        mode._verify_resources()

        ec2.describe_vpcs.assert_called_once_with(VpcIds=["vpc-12345"])
        ec2.describe_subnets.assert_called_once_with(SubnetIds=["subnet-12345"])
        ec2.describe_security_groups.assert_called_once_with(GroupIds=["sg-12345"])
        for attribute, expected in NETWORK_IDS.items():
            assert getattr(mode, attribute) == expected

    @pytest.mark.parametrize("mode_class", MODES, ids=lambda c: c.__name__)
    def test_unrelated_client_errors_propagate_unchanged(
        self, mode_class, session, state_store, ec2
    ):
        """An auth or throttling failure is not a missing resource.

        Reporting ``UnauthorizedOperation`` as ResourceNotFoundError would send
        the caller looking for a VPC that is in fact right where they left it.
        """
        mode = _mode(mode_class, session, state_store)
        ec2.describe_vpcs.side_effect = _client_error("UnauthorizedOperation")

        with pytest.raises(ClientError) as excinfo:
            mode._verify_resources()

        assert excinfo.value.response["Error"]["Code"] == "UnauthorizedOperation"

    @pytest.mark.parametrize("mode_class", MODES, ids=lambda c: c.__name__)
    def test_the_vpc_is_checked_before_its_children(
        self, mode_class, session, state_store, ec2
    ):
        """A deleted VPC is reported as such, not as a missing subnet.

        Deleting a VPC takes its subnets and security groups with it, so all
        three describes fail at once. Checking the VPC first means the message
        names the cause rather than one of its consequences.
        """
        mode = _mode(mode_class, session, state_store)
        for _, describe, code in RESOURCES:
            getattr(ec2, describe).side_effect = _client_error(code)

        with pytest.raises(ResourceNotFoundError) as excinfo:
            mode._verify_resources()

        assert "vpc_id" in str(excinfo.value)
        ec2.describe_subnets.assert_not_called()
        ec2.describe_security_groups.assert_not_called()

    def test_lambda_only_serverless_has_nothing_to_verify(self, session, state_store):
        """Lambda supplies its own networking, so no IDs means no describes.

        ``require_network_resources=False`` lets this mode construct without
        them; verification has to tolerate the same absence or the mode could
        never initialize.
        """
        mode = ServerlessMode(
            provider_id="test-provider",
            session=session,
            state_store=state_store,
            worker_type="lambda",
        )
        assert mode.vpc_id is None

        mode._verify_resources()

        session.client.return_value.describe_vpcs.assert_not_called()


class TestInitializeVerifiesOnBothPaths:
    """Verification has to run on a first launch, not only on resume.

    ``_verify_resources()`` was called from inside the ``if load_state():``
    branch, so it only ever ran when a state document was already present. A
    first-run provider -- the common case, and the only case for a fresh state
    path -- never checked its network IDs at all. Found by pointing a real
    provider at a deleted security group: nothing raised, because with no state
    to load, the check was skipped entirely.
    """

    @pytest.mark.parametrize("mode_class", MODES, ids=lambda c: c.__name__)
    def test_a_first_launch_verifies(self, mode_class, session, state_store, ec2):
        """No state to load, and the VPC is gone: initialize() must still fail."""
        mode = _mode(mode_class, session, state_store)
        state_store.load_state.return_value = None
        ec2.describe_vpcs.side_effect = _client_error("InvalidVpcID.NotFound")

        with pytest.raises(Exception) as excinfo:
            mode.initialize()

        # Standard and detached wrap initialize() failures in
        # ResourceCreationError; the cause is what identifies the defect.
        assert isinstance(excinfo.value, ResourceNotFoundError) or isinstance(
            excinfo.value.__cause__, ResourceNotFoundError
        )

    @pytest.mark.parametrize("mode_class", MODES, ids=lambda c: c.__name__)
    def test_a_resume_verifies(self, mode_class, session, state_store, ec2):
        """The path that always did verify must keep doing so."""
        mode = _mode(mode_class, session, state_store)
        state_store.load_state.return_value = {
            "provider_id": "test-provider",
            "resources": {},
            "initialized": True,
            **NETWORK_IDS,
        }
        ec2.describe_vpcs.side_effect = _client_error("InvalidVpcID.NotFound")

        with pytest.raises(Exception) as excinfo:
            mode.initialize()

        assert isinstance(excinfo.value, ResourceNotFoundError) or isinstance(
            excinfo.value.__cause__, ResourceNotFoundError
        )


class TestDetachedBastionVerification:
    """Detached mode adds the bastion, which it *can* legitimately rebuild."""

    def test_a_missing_bastion_is_cleared_not_raised(self, session, state_store, ec2):
        """Unlike the network, the bastion belongs to this mode.

        ``initialize()`` reads the cleared ``bastion_id`` as its signal to build
        a replacement, so clearing it is correct here and raising would strand a
        provider that could recover on its own.
        """
        mode = _mode(DetachedMode, session, state_store, bastion_host_type="ec2")
        mode.bastion_id = "i-deadbeef"
        ec2.describe_instances.side_effect = _client_error(
            "InvalidInstanceID.NotFound", "DescribeInstances"
        )

        mode._verify_resources()

        assert mode.bastion_id is None

    def test_the_network_is_verified_before_the_bastion(
        self, session, state_store, ec2
    ):
        """A missing VPC stops the check; the bastion is moot without one."""
        mode = _mode(DetachedMode, session, state_store, bastion_host_type="ec2")
        mode.bastion_id = "i-deadbeef"
        ec2.describe_vpcs.side_effect = _client_error("InvalidVpcID.NotFound")

        with pytest.raises(ResourceNotFoundError):
            mode._verify_resources()

        ec2.describe_instances.assert_not_called()
        assert mode.bastion_id == "i-deadbeef"

    def test_initialize_rebuilds_a_bastion_that_has_gone_away(
        self, session, state_store, ec2
    ):
        """Resuming onto a dead bastion must recreate it, not carry on.

        ``initialize()`` returned as soon as state loaded, so the ``bastion_id``
        that ``_verify_resources()`` had just cleared was never acted on. The
        provider came up "initialized" with no bastion, and every submitted job
        went into an SSM parameter path that nothing was reading.
        """
        mode = _mode(DetachedMode, session, state_store, bastion_host_type="ec2")
        mode.bastion_id = "i-gone"
        ec2.describe_instances.side_effect = _client_error(
            "InvalidInstanceID.NotFound", "DescribeInstances"
        )

        with (
            patch.object(mode, "load_state", return_value=True),
            patch.object(mode, "save_state"),
            patch.object(
                mode, "_create_bastion_direct", return_value="i-fresh"
            ) as create,
        ):
            mode.initialize()

        create.assert_called_once()
        assert mode.bastion_id == "i-fresh"
        assert mode.initialized is True

    def test_a_live_bastion_is_left_alone_on_resume(self, session, state_store, ec2):
        """The common resume path must not churn a perfectly good bastion."""
        mode = _mode(DetachedMode, session, state_store, bastion_host_type="ec2")
        mode.bastion_id = "i-alive"
        ec2.describe_instances.return_value = {
            "Reservations": [
                {"Instances": [{"InstanceId": "i-alive", "State": {"Name": "running"}}]}
            ]
        }

        with (
            patch.object(mode, "load_state", return_value=True),
            patch.object(mode, "_create_bastion_direct") as create,
        ):
            mode.initialize()

        create.assert_not_called()
        assert mode.bastion_id == "i-alive"


class TestLoadStateNeverNullsAValidatedId:
    """A stale state document must not undo constructor validation."""

    @pytest.mark.parametrize("mode_class", MODES, ids=lambda c: c.__name__)
    @pytest.mark.parametrize("attribute", list(NETWORK_IDS), ids=str)
    def test_a_null_in_state_does_not_overwrite_the_configured_id(
        self, mode_class, attribute, session, state_store
    ):
        """State written before #69 carries ``None`` for these fields.

        The constructor value was validated; the null was not. Restoring it
        would defer the failure to ``run_instances``, where it reads as a bad
        parameter rather than as stale state.
        """
        mode = _mode(mode_class, session, state_store)
        stale = {
            "provider_id": "test-provider",
            "resources": {},
            "initialized": True,
            **{name: None for name in NETWORK_IDS},
        }
        state_store.load_state.return_value = stale

        assert mode.load_state() is True

        for name, expected in NETWORK_IDS.items():
            assert getattr(mode, name) == expected

    @pytest.mark.parametrize("mode_class", MODES, ids=lambda c: c.__name__)
    def test_a_populated_state_document_still_wins(
        self, mode_class, session, state_store
    ):
        """Guarding against nulls must not stop legitimate restoration."""
        mode = _mode(mode_class, session, state_store)
        state_store.load_state.return_value = {
            "provider_id": "test-provider",
            "resources": {},
            "initialized": True,
            "vpc_id": "vpc-saved",
            "subnet_id": "subnet-saved",
            "security_group_id": "sg-saved",
        }

        assert mode.load_state() is True

        assert mode.vpc_id == "vpc-saved"
        assert mode.subnet_id == "subnet-saved"
        assert mode.security_group_id == "sg-saved"

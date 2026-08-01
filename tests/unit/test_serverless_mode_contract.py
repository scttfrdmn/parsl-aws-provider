"""Unit tests for the ServerlessMode ↔ compute-manager contract and network guard.

These cover the repair of a mode that had never successfully constructed: the
manager contract (#72), the parameters lost between provider and mode (#73), and
the network guard that demanded IDs Lambda has no use for (#74).

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

from unittest.mock import MagicMock

import boto3
import pytest

from parsl_aws_provider.constants import (
    WORKER_TYPE_AUTO,
    WORKER_TYPE_ECS,
    WORKER_TYPE_LAMBDA,
)
from parsl_aws_provider.exceptions import ConfigurationError
from parsl_aws_provider.modes.serverless import ServerlessMode


pytestmark = pytest.mark.unit

NETWORK_IDS = {
    "vpc_id": "vpc-12345",
    "subnet_id": "subnet-12345",
    "security_group_id": "sg-12345",
}


@pytest.fixture
def mock_session():
    """A mock boto3 session."""
    session = MagicMock(spec=boto3.Session)
    session.region_name = "us-east-1"
    return session


@pytest.fixture
def mock_state_store():
    """A mock state store that reports no persisted state."""
    store = MagicMock()
    store.load_state.return_value = None
    return store


def _mode(mock_session, mock_state_store, **overrides):
    """Construct a ServerlessMode with the supplied overrides."""
    params = {
        "provider_id": "test-provider",
        "session": mock_session,
        "state_store": mock_state_store,
        "region": "us-east-1",
    }
    params.update(overrides)
    return ServerlessMode(**params)


class TestManagerContract:
    """LambdaManager and ECSManager are handed the mode as their `provider`."""

    # The attributes both managers read off their `provider`. Missing
    # `workflow_id` made every construction raise AttributeError.
    REQUIRED_ATTRS = (
        "workflow_id",
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
        "aws_profile",
        "security_config",
        "use_spot_instances",
        "subnet_ids",
        "region",
    )

    @pytest.mark.parametrize("attr", REQUIRED_ATTRS)
    def test_manager_contract_attribute_present(
        self, mock_session, mock_state_store, attr
    ):
        """Every attribute the managers read must exist on the mode."""
        mode = _mode(
            mock_session, mock_state_store, worker_type=WORKER_TYPE_AUTO, **NETWORK_IDS
        )
        assert hasattr(mode, attr), f"managers read provider.{attr}"

    def test_workflow_id_matches_provider_id(self, mock_session, mock_state_store):
        """`workflow_id` names IAM roles and clusters; it tracks the provider ID."""
        mode = _mode(
            mock_session, mock_state_store, worker_type=WORKER_TYPE_AUTO, **NETWORK_IDS
        )
        assert mode.workflow_id == mode.provider_id == "test-provider"

    def test_subnet_ids_derived_from_subnet_id(self, mock_session, mock_state_store):
        """ECSManager prefers a `subnet_ids` list over the singular ID."""
        mode = _mode(
            mock_session, mock_state_store, worker_type=WORKER_TYPE_ECS, **NETWORK_IDS
        )
        assert mode.subnet_ids == ["subnet-12345"]

    def test_subnet_ids_is_none_without_a_subnet(self, mock_session, mock_state_store):
        """Lambda-only mode has no subnet, so the list stays unset rather than [None]."""
        mode = _mode(mock_session, mock_state_store, worker_type=WORKER_TYPE_LAMBDA)
        assert mode.subnet_ids is None

    def test_use_spot_instances_tracks_use_spot(self, mock_session, mock_state_store):
        """ECSManager reads `use_spot_instances` to request Fargate Spot."""
        mode = _mode(
            mock_session,
            mock_state_store,
            worker_type=WORKER_TYPE_ECS,
            use_spot=True,
            **NETWORK_IDS,
        )
        assert mode.use_spot_instances is True


class TestConditionalNetworkGuard:
    """Lambda needs no network IDs; ECS and auto do."""

    def test_lambda_constructs_without_network_ids(
        self, mock_session, mock_state_store
    ):
        """Lambda functions run in the Lambda-managed VPC — nothing to supply."""
        mode = _mode(mock_session, mock_state_store, worker_type=WORKER_TYPE_LAMBDA)

        assert mode.require_network_resources is False
        assert mode.vpc_id is None

    @pytest.mark.parametrize("worker_type", [WORKER_TYPE_ECS, WORKER_TYPE_AUTO])
    def test_ecs_and_auto_require_network_ids(
        self, mock_session, mock_state_store, worker_type
    ):
        """Fargate's awsvpcConfiguration is mandatory, so the IDs are too."""
        with pytest.raises(
            ValueError, match="vpc_id, subnet_id, and security_group_id"
        ):
            _mode(mock_session, mock_state_store, worker_type=worker_type)

    def test_invalid_worker_type_reported_before_network_guard(
        self, mock_session, mock_state_store
    ):
        """A bad worker type is the real error even when IDs are also absent."""
        with pytest.raises(ConfigurationError, match="worker_type"):
            _mode(mock_session, mock_state_store, worker_type="bogus")


class TestProviderParameterPlumbing:
    """`compute_type`, `memory_size`, and `timeout` used to vanish into **kwargs."""

    @pytest.mark.parametrize(
        "compute_type,expected",
        [("lambda", WORKER_TYPE_LAMBDA), ("LAMBDA", WORKER_TYPE_LAMBDA)],
    )
    def test_compute_type_maps_to_worker_type(
        self, mock_session, mock_state_store, compute_type, expected
    ):
        """The provider's `compute_type` is the outward name for `worker_type`."""
        mode = _mode(mock_session, mock_state_store, compute_type=compute_type)
        assert mode.worker_type == expected

    def test_compute_type_enum_member_is_unwrapped(
        self, mock_session, mock_state_store
    ):
        """It arrives as a ComputeType, whose str() is the qualified name."""
        from parsl_aws_provider.provider import ComputeType

        mode = _mode(mock_session, mock_state_store, compute_type=ComputeType.LAMBDA)
        assert mode.worker_type == WORKER_TYPE_LAMBDA

    def test_compute_type_ec2_is_treated_as_unset(self, mock_session, mock_state_store):
        """ "ec2" is the provider-wide default and means nothing here."""
        mode = _mode(mock_session, mock_state_store, compute_type="ec2", **NETWORK_IDS)
        assert mode.worker_type == WORKER_TYPE_AUTO

    def test_memory_size_overrides_lambda_memory(self, mock_session, mock_state_store):
        """`memory_size` is the provider-facing name for `lambda_memory`."""
        mode = _mode(
            mock_session,
            mock_state_store,
            worker_type=WORKER_TYPE_LAMBDA,
            lambda_memory=1024,
            memory_size=512,
        )
        assert mode.lambda_memory == 512

    def test_timeout_overrides_lambda_timeout(self, mock_session, mock_state_store):
        """`timeout` is the provider-facing name for `lambda_timeout`."""
        mode = _mode(
            mock_session,
            mock_state_store,
            worker_type=WORKER_TYPE_LAMBDA,
            lambda_timeout=300,
            timeout=120,
        )
        assert mode.lambda_timeout == 120

    def test_native_names_survive_when_no_override_supplied(
        self, mock_session, mock_state_store
    ):
        """Absent the provider-facing aliases, the native values stand."""
        mode = _mode(
            mock_session,
            mock_state_store,
            worker_type=WORKER_TYPE_LAMBDA,
            lambda_memory=1024,
            lambda_timeout=300,
        )
        assert (mode.lambda_memory, mode.lambda_timeout) == (1024, 300)


class TestNetworkResourcesAreNeverCreated:
    """#69 removed VPC creation; serverless kept a CloudFormation copy of it."""

    @pytest.mark.parametrize(
        "helper", ["_create_vpc", "_create_subnet", "_create_security_group"]
    )
    def test_creation_helpers_are_gone(self, helper):
        """The mode must offer no way to create network resources."""
        assert not hasattr(ServerlessMode, helper)

    def test_create_vpc_parameter_is_gone(self, mock_session, mock_state_store):
        """`create_vpc` is no longer a knob, so it must not linger as an attribute."""
        mode = _mode(mock_session, mock_state_store, worker_type=WORKER_TYPE_LAMBDA)
        assert not hasattr(mode, "create_vpc")

    def test_cleanup_infrastructure_leaves_the_security_group_alone(
        self, mock_session, mock_state_store
    ):
        """The SG belongs to the caller; deleting it was destroying user resources."""
        mode = _mode(
            mock_session, mock_state_store, worker_type=WORKER_TYPE_ECS, **NETWORK_IDS
        )
        ec2 = mock_session.client("ec2")
        ec2.reset_mock()

        mode.cleanup_infrastructure()

        ec2.delete_security_group.assert_not_called()
        assert mode.security_group_id == "sg-12345"


class TestInitializedFlag:
    """`initialize()` never set the flag, so every submit re-ran it."""

    def test_initialize_sets_the_flag(self, mock_session, mock_state_store):
        """A successful initialize must be observable, and therefore idempotent."""
        mode = _mode(mock_session, mock_state_store, worker_type=WORKER_TYPE_LAMBDA)
        assert mode.initialized is False

        mode.lambda_manager = MagicMock()
        mode._initialize_compute_managers = MagicMock()

        mode.initialize()

        assert mode.initialized is True
        mode._initialize_compute_managers.assert_called_once()

        # A second call is a no-op rather than a repeat of the work.
        mode.initialize()
        mode._initialize_compute_managers.assert_called_once()

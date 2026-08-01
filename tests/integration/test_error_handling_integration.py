"""Integration tests for robust error handling in compute modules.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import pytest
from unittest.mock import MagicMock, patch
from botocore.exceptions import ClientError

from parsl_ephemeral_aws.compute.ec2 import EC2Manager
from parsl_ephemeral_aws.compute.ecs import ECSManager
from parsl_ephemeral_aws.compute.spot_fleet import SpotFleetManager
from parsl_ephemeral_aws.error_handling import RobustErrorHandler
from parsl_ephemeral_aws.exceptions import (
    ResourceCreationError,
    SpotFleetError,
    SpotFleetThrottlingError,
)
from tests.support import make_manager, mock_provider

# Every test here mocks CredentialManager and the boto3 session, so no emulator is
# needed -- hence `integration` and not `substrate`. Without any marker at all the
# file was skipped by both the Makefile (which then selected
# `-m "integration or localstack"`) and the AWS selection, so it never ran
# anywhere; adding the marker surfaced six failures, all from a `Mock()` provider
# too thin for the managers' __init__.
pytestmark = pytest.mark.integration


class TestErrorHandlingIntegration:
    """Test integration of error handling framework with compute modules."""

    def test_ec2_manager_error_handler_initialization(self):
        """Test EC2Manager initializes error handler correctly."""
        manager = make_manager(EC2Manager, "ec2")

        # Verify error handler is initialized
        assert hasattr(manager, "error_handler")
        assert isinstance(manager.error_handler, RobustErrorHandler)
        assert manager.error_handler.retry_config.max_attempts == 5
        assert manager.error_handler.retry_config.base_delay == 2.0

    def test_ecs_manager_error_handler_initialization(self):
        """Test ECSManager initializes error handler correctly."""
        with patch.object(
            ECSManager, "_get_or_create_cluster", return_value="parsl-ecs-cluster-test"
        ):
            manager = make_manager(ECSManager, "ecs")

        # Verify error handler is initialized
        assert hasattr(manager, "error_handler")
        assert isinstance(manager.error_handler, RobustErrorHandler)
        assert manager.error_handler.retry_config.max_attempts == 5

    def test_spot_fleet_manager_error_handler_initialization(self):
        """Test SpotFleetManager initializes error handler correctly."""
        manager = make_manager(SpotFleetManager, "spot_fleet")

        # Verify error handler is initialized with spot-specific config
        assert hasattr(manager, "error_handler")
        assert isinstance(manager.error_handler, RobustErrorHandler)
        assert (
            manager.error_handler.retry_config.max_attempts == 6
        )  # Extra attempts for spot
        assert manager.error_handler.retry_config.base_delay == 3.0  # Longer delay
        assert manager.error_handler.retry_config.max_delay == 60.0  # Spot-specific cap

    def test_ec2_network_setup_error_handling(self):
        """Test error handling in EC2 network setup."""
        # Mock EC2 client to raise an error
        ec2_client = MagicMock()
        ec2_client.create_vpc.side_effect = ClientError(
            error_response={
                "Error": {"Code": "InternalError", "Message": "Server error"}
            },
            operation_name="CreateVpc",
        )

        manager = make_manager(EC2Manager, "ec2", client=ec2_client)

        # Verify that error is handled and recorded
        with pytest.raises(ResourceCreationError):
            manager._setup_network_resources()

        # Check that error was recorded in the error handler
        assert len(manager.error_handler.error_history) > 0
        error_record = manager.error_handler.error_history[-1]
        assert "InternalError" in str(error_record.exception)

    def test_spot_fleet_throttling_becomes_a_typed_error(self):
        """A throttled request is reported as throttling, not a bare ClientError.

        The caller can only back off if it can tell throttling apart from a
        launch-spec mistake, so the specific type is the contract.

        Note the recognized codes are *not* added to ``error_history`` -- only the
        unrecognized fallthrough is (#120). The original test asserted
        ``len(error_history) > 0`` here and could never have passed; see
        ``test_spot_fleet_unrecognized_error_is_recorded`` for the branch that
        does record.
        """
        ec2_client = MagicMock()
        ec2_client.request_spot_fleet.side_effect = ClientError(
            error_response={
                "Error": {"Code": "Throttling", "Message": "Request rate exceeded"}
            },
            operation_name="RequestSpotFleet",
        )

        manager = make_manager(SpotFleetManager, "spot_fleet", client=ec2_client)

        with pytest.raises(SpotFleetThrottlingError, match="API throttling error"):
            manager._create_spot_fleet_with_retry(
                {"SpotFleetRequestConfig": {}}, MagicMock()
            )

    def test_spot_fleet_unrecognized_error_is_recorded(self):
        """An unmapped error code lands in the handler's history for analysis."""
        ec2_client = MagicMock()
        ec2_client.request_spot_fleet.side_effect = ClientError(
            error_response={
                "Error": {"Code": "InternalError", "Message": "Server error"}
            },
            operation_name="RequestSpotFleet",
        )

        manager = make_manager(SpotFleetManager, "spot_fleet", client=ec2_client)

        with pytest.raises(SpotFleetError):
            manager._create_spot_fleet_with_retry(
                {"SpotFleetRequestConfig": {}}, MagicMock()
            )

        assert len(manager.error_handler.error_history) == 1
        assert "InternalError" in str(manager.error_handler.error_history[-1].exception)

    def test_error_statistics_collection(self):
        """Test that error statistics are properly collected across modules."""
        ec2_manager = make_manager(EC2Manager, "ec2")

        # Simulate some errors
        from parsl_ephemeral_aws.error_handling import ErrorContext

        context = ErrorContext(
            operation="test_operation", resource_type="test_resource"
        )

        # Add some test errors
        error1 = ValueError("Test error 1")
        error2 = ConnectionError("Test error 2")

        ec2_manager.error_handler.handle_error(error1, context)
        ec2_manager.error_handler.handle_error(error2, context)

        # Get statistics
        stats = ec2_manager.error_handler.get_error_statistics()

        assert stats["total_errors"] == 2
        assert stats["error_rate"] > 0
        assert len(stats["most_common_errors"]) > 0

    def test_mock_provider_is_json_serializable_for_audit(self):
        """The shared provider double must survive the audit logger's json.dumps.

        Every manager's ``__init__`` emits a CONFIG_CHANGE audit event whose
        metadata includes ``provider.region``. The audit logger serializes it, so
        an auto-created Mock attribute anywhere in that path raises ``TypeError:
        Object of type Mock is not JSON serializable`` -- which each manager then
        wraps as ``ResourceCreationError: Credential initialization failed``,
        naming neither the attribute nor the real cause. This pins the contract so
        a future field added to the double fails here, with a readable message,
        rather than in six construction tests at once.
        """
        import json

        provider = mock_provider()
        json.dumps({"provider_region": provider.region, "role_arn": provider.role_arn})

"""Integration tests for robust error handling in compute modules.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import pytest
from unittest.mock import MagicMock, patch
from botocore.exceptions import ClientError

from parsl_ephemeral_provider.compute.ec2 import EC2Manager
from parsl_ephemeral_provider.compute.ecs import ECSManager
from parsl_ephemeral_provider.compute.spot_fleet import SpotFleetManager
from parsl_ephemeral_provider.error_handling import ErrorContext, RobustErrorHandler
from parsl_ephemeral_provider.exceptions import (
    ResourceCreationError,
    SpotFleetError,
    SpotFleetRequestError,
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

    @staticmethod
    def _translate(manager, code, message):
        """Put *code* through ``_translate_fleet_error`` and return the exception.

        Retargeted from ``_create_spot_fleet_with_retry``, which went away with
        the legacy ``RequestSpotFleet`` API in #86 -- these two tests had been
        raising ``AttributeError`` from ``patch.object``'s missing-attribute
        check ever since, so neither had exercised anything.

        The method *returns* rather than raises, so the caller keeps its
        ``raise ... from`` chain and the botocore traceback survives.
        """
        exc = ClientError(
            error_response={"Error": {"Code": code, "Message": message}},
            operation_name="CreateFleet",
        )
        context = ErrorContext(
            operation="create_ec2_fleet",
            resource_type="ec2_fleet",
            resource_id="block-1",
        )
        return manager._translate_fleet_error(exc, context)

    def test_spot_fleet_throttling_becomes_a_typed_error(self):
        """A throttled request is reported as throttling, not a bare ClientError.

        The caller can only back off if it can tell throttling apart from a
        launch-spec mistake, so the specific type is the contract, and
        ``retry_after`` is what it carries that the base class does not.
        """
        manager = make_manager(SpotFleetManager, "spot_fleet", client=MagicMock())

        result = self._translate(manager, "Throttling", "Request rate exceeded")

        assert isinstance(result, SpotFleetThrottlingError)
        assert result.retry_after == 60

    @pytest.mark.parametrize(
        "code,expected",
        [
            ("InvalidLaunchTemplateId.NotFound", SpotFleetRequestError),
            ("InsufficientInstanceCapacity", SpotFleetError),
            ("InvalidFleetConfig", SpotFleetRequestError),
            ("MaxSpotInstanceCountExceeded", SpotFleetRequestError),
            ("Throttling", SpotFleetThrottlingError),
            ("InternalError", SpotFleetError),  # the unrecognized fallthrough
        ],
    )
    def test_every_classified_fleet_error_is_recorded(self, code, expected):
        """Recognized error families reach ``error_history``, not just unknown ones.

        Recording used to happen on the fallthrough branch alone (#120), which is
        backwards: an insufficient-capacity or quota rejection is exactly what a
        caller counts in order to decide whether to diversify instance types or
        request a limit increase, whereas an error nobody could classify is the
        least actionable of the set. ``get_error_statistics()`` was therefore
        blind to every failure the code understood.
        """
        manager = make_manager(SpotFleetManager, "spot_fleet", client=MagicMock())

        result = self._translate(manager, code, "boom")

        assert isinstance(result, expected)
        assert len(manager.error_handler.error_history) == 1
        record = manager.error_handler.error_history[-1]
        assert code in str(record.exception)
        assert record.context.operation == "create_ec2_fleet"

    def test_recording_does_not_swallow_the_error(self):
        """The handler records; the caller still gets an exception to raise.

        ``handle_error`` runs a recovery attempt for RETRY/FALLBACK actions, and
        ``InsufficientInstanceCapacity`` maps to FALLBACK -- so this pins that a
        "successful" recovery cannot silently turn a failed fleet into a
        pass. No strategy is registered for ``create_ec2_fleet``, but the point
        is that translation does not depend on that staying true.
        """
        manager = make_manager(SpotFleetManager, "spot_fleet", client=MagicMock())

        with patch.object(
            manager.error_handler.recovery_handler,
            "attempt_recovery",
            return_value=True,
        ):
            result = self._translate(
                manager, "InsufficientInstanceCapacity", "no capacity"
            )

        assert isinstance(result, SpotFleetError)
        assert len(manager.error_handler.error_history) == 1

    def test_error_statistics_collection(self):
        """Test that error statistics are properly collected across modules."""
        ec2_manager = make_manager(EC2Manager, "ec2")

        # Simulate some errors
        from parsl_ephemeral_provider.error_handling import ErrorContext

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

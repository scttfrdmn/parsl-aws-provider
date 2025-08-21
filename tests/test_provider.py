"""Tests for the provider module.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import pytest
from unittest.mock import MagicMock, patch

from parsl_ephemeral_aws import EphemeralAWSProvider
from parsl_ephemeral_aws.constants import MODE_STANDARD, MODE_DETACHED, MODE_SERVERLESS


@patch("parsl_ephemeral_aws.modes.standard.StandardMode")
def test_provider_init_standard_mode(mock_standard_mode):
    """Test provider initialization with standard mode."""
    provider = EphemeralAWSProvider(
        image_id="ami-12345678",
        instance_type="t3.medium",
        region="us-west-2",
        mode=MODE_STANDARD,
    )

    assert provider.mode == MODE_STANDARD
    assert provider.image_id == "ami-12345678"
    assert provider.instance_type == "t3.medium"
    assert provider.region == "us-west-2"
    mock_standard_mode.assert_called_once()


@patch("parsl_ephemeral_aws.modes.detached.DetachedMode")
def test_provider_init_detached_mode(mock_detached_mode):
    """Test provider initialization with detached mode."""
    provider = EphemeralAWSProvider(
        image_id="ami-12345678",
        instance_type="t3.medium",
        region="us-west-2",
        mode=MODE_DETACHED,
    )

    assert provider.mode == MODE_DETACHED
    mock_detached_mode.assert_called_once()


@patch("parsl_ephemeral_aws.modes.serverless.ServerlessMode")
def test_provider_init_serverless_mode(mock_serverless_mode):
    """Test provider initialization with serverless mode."""
    provider = EphemeralAWSProvider(
        image_id="ami-12345678",
        worker_type="lambda",
        region="us-west-2",
        mode=MODE_SERVERLESS,
    )

    assert provider.mode == MODE_SERVERLESS
    mock_serverless_mode.assert_called_once()


def test_provider_init_invalid_mode():
    """Test that initializing with an invalid mode raises an error."""
    with pytest.raises(ValueError) as excinfo:
        EphemeralAWSProvider(
            image_id="ami-12345678",
            instance_type="t3.medium",
            region="us-west-2",
            mode="invalid",
        )

    assert "Invalid mode" in str(excinfo.value)


def test_provider_init_serverless_with_ec2():
    """Test that initializing serverless mode with EC2 worker type raises an error."""
    with pytest.raises(ValueError) as excinfo:
        EphemeralAWSProvider(
            image_id="ami-12345678",
            worker_type="ec2",
            region="us-west-2",
            mode=MODE_SERVERLESS,
        )

    assert "Serverless mode requires worker_type" in str(excinfo.value)


def test_provider_init_invalid_block_counts():
    """Test that initializing with invalid block counts raises an error."""
    with pytest.raises(ValueError) as excinfo:
        EphemeralAWSProvider(
            image_id="ami-12345678",
            instance_type="t3.medium",
            region="us-west-2",
            min_blocks=10,
            max_blocks=5,
        )

    assert "max_blocks" in str(excinfo.value)
    assert "min_blocks" in str(excinfo.value)


@patch("parsl_ephemeral_aws.modes.standard.StandardMode")
def test_provider_submit(mock_standard_mode):
    """Test provider submit method."""
    # Mock the mode handler
    mock_handler = MagicMock()
    mock_standard_mode.return_value = mock_handler

    # Initialize provider
    provider = EphemeralAWSProvider(
        image_id="ami-12345678", instance_type="t3.medium", region="us-west-2"
    )

    # Test submit
    provider.submit("echo hello", 1)
    mock_handler.submit.assert_called_once_with("echo hello", 1, "")


@patch("parsl_ephemeral_aws.modes.standard.StandardMode")
def test_provider_status(mock_standard_mode):
    """Test provider status method."""
    # Mock the mode handler
    mock_handler = MagicMock()
    mock_standard_mode.return_value = mock_handler

    # Initialize provider
    provider = EphemeralAWSProvider(
        image_id="ami-12345678", instance_type="t3.medium", region="us-west-2"
    )

    # Test status
    provider.status(["job-1", "job-2"])
    mock_handler.status.assert_called_once_with(["job-1", "job-2"])


@patch("parsl_ephemeral_aws.modes.standard.StandardMode")
def test_provider_cancel(mock_standard_mode):
    """Test provider cancel method."""
    # Mock the mode handler
    mock_handler = MagicMock()
    mock_standard_mode.return_value = mock_handler

    # Initialize provider
    provider = EphemeralAWSProvider(
        image_id="ami-12345678", instance_type="t3.medium", region="us-west-2"
    )

    # Test cancel
    provider.cancel(["job-1", "job-2"])
    mock_handler.cancel.assert_called_once_with(["job-1", "job-2"])


@patch("parsl_ephemeral_aws.modes.standard.StandardMode")
def test_provider_scale_out(mock_standard_mode):
    """Test provider scale_out method."""
    # Mock the mode handler
    mock_handler = MagicMock()
    mock_standard_mode.return_value = mock_handler

    # Initialize provider
    provider = EphemeralAWSProvider(
        image_id="ami-12345678", instance_type="t3.medium", region="us-west-2"
    )

    # Test scale_out
    provider.scale_out(3)
    mock_handler.scale_out.assert_called_once_with(3)


@patch("parsl_ephemeral_aws.modes.standard.StandardMode")
def test_provider_scale_in(mock_standard_mode):
    """Test provider scale_in method."""
    # Mock the mode handler
    mock_handler = MagicMock()
    mock_standard_mode.return_value = mock_handler

    # Initialize provider
    provider = EphemeralAWSProvider(
        image_id="ami-12345678", instance_type="t3.medium", region="us-west-2"
    )

    # Test scale_in with blocks
    provider.scale_in(2)
    mock_handler.scale_in.assert_called_once_with(2, None)

    # Test scale_in with block_ids
    mock_handler.reset_mock()
    provider.scale_in(block_ids=["block-1", "block-2"])
    mock_handler.scale_in.assert_called_once_with(None, ["block-1", "block-2"])


@patch("parsl_ephemeral_aws.modes.standard.StandardMode")
def test_provider_shutdown(mock_standard_mode):
    """Test provider shutdown method."""
    # Mock the mode handler
    mock_handler = MagicMock()
    mock_standard_mode.return_value = mock_handler

    # Initialize provider
    provider = EphemeralAWSProvider(
        image_id="ami-12345678", instance_type="t3.medium", region="us-west-2"
    )

    # Test shutdown
    provider.shutdown()
    mock_handler.shutdown.assert_called_once()

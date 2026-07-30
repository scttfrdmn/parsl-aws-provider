"""Unit tests for the SpotFleetManager class.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import unittest
from unittest.mock import patch, MagicMock
from botocore.exceptions import ClientError
import logging

import pytest

from parsl_ephemeral_aws.compute.spot_fleet import SpotFleetManager
from parsl_ephemeral_aws.constants import TAG_PREFIX
from parsl_ephemeral_aws.exceptions import SpotFleetThrottlingError

pytestmark = pytest.mark.unit


class _SimpleProvider:
    """A provider stand-in that answers only the attributes it is given.

    Mirrors the anonymous type StandardMode builds for SpotFleetManager
    (``standard.py:220``). Unlike a MagicMock it raises ``AttributeError`` for
    anything it was not given, so the manager's ``getattr(..., default)`` reads
    fall through to their defaults exactly as they do in production.
    """

    def __init__(self, **attrs):
        for name, value in attrs.items():
            setattr(self, name, value)


class TestSpotFleetManager(unittest.TestCase):
    """Test suite for the SpotFleetManager class."""

    def setUp(self):
        """Set up the test environment."""
        # A plain object, not a MagicMock. SpotFleetManager reads its optional
        # security settings with getattr(provider, name, <default>) --
        # `vpc_cidr`, `security_environment`, `admin_cidr_blocks`,
        # `strict_security_mode` -- and a MagicMock answers every one of them
        # with a MagicMock, so the default never applies and SecurityConfig
        # raises `ValueError: Invalid VPC CIDR: <MagicMock ...>` during
        # construction. That was every failure in this file.
        #
        # The single live caller (StandardMode, standard.py:220) builds exactly
        # this shape: a bare namespace carrying only the attributes below, none
        # of them security settings. Modelling it means the getattr defaults are
        # exercised the way they are in production, instead of being masked.
        self.mock_provider = _SimpleProvider(
            workflow_id="test-workflow-id",
            region="us-east-1",
            aws_access_key_id="test_access_key",
            aws_secret_access_key="test_secret_key",
            aws_session_token=None,
            aws_profile=None,
            vpc_id=None,
            subnet_id=None,
            security_group_id=None,
            image_id="ami-12345678",
            instance_type="t2.micro",
            instance_types=[],
            key_name=None,
            use_public_ips=True,
            nodes_per_block=1,
            tags={"ProjectTag": "TestProject"},
            spot_max_price_percentage=100,
            worker_init="echo 'Worker init script'",
        )

        # Disable logging during tests
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        """Clean up after tests."""
        # Re-enable logging
        logging.disable(logging.NOTSET)

    @patch("parsl_ephemeral_aws.compute.spot_fleet.CredentialManager")
    def test_initialization(self, mock_credential_manager_cls):
        """Test SpotFleetManager initialization.

        The session is obtained from ``CredentialManager.create_boto3_session()``,
        not by handing the provider's keys straight to ``boto3.Session`` -- the
        manager derives a ``CredentialConfiguration`` from the provider and lets
        the credential manager resolve it (which is what applies sanitization and
        token refresh). This test used to assert the direct-``boto3.Session``
        construction, a path that no longer exists.
        """
        mock_session = MagicMock()
        mock_ec2_client = MagicMock()
        mock_ec2_resource = MagicMock()

        mock_session.client.return_value = mock_ec2_client
        mock_session.resource.return_value = mock_ec2_resource
        mock_manager = mock_credential_manager_cls.return_value
        mock_manager.create_boto3_session.return_value = mock_session

        # Instantiate SpotFleetManager
        manager = SpotFleetManager(self.mock_provider)

        # The provider's region must reach the session, not a default.
        mock_manager.create_boto3_session.assert_called_once_with(region="us-east-1")

        # And the config handed to CredentialManager must be the one derived from
        # the provider: the provider carries explicit keys, so environment-variable
        # credentials are enabled and the (absent) profile is passed through.
        credential_config = mock_credential_manager_cls.call_args.args[0]
        self.assertTrue(credential_config.use_environment_variables)
        self.assertEqual(credential_config.use_profile, self.mock_provider.aws_profile)

        mock_session.client.assert_called_with("ec2")
        mock_session.resource.assert_called_with("ec2")
        self.assertIs(manager.ec2_client, mock_ec2_client)
        self.assertIs(manager.ec2_resource, mock_ec2_resource)

        # Verify instance variables
        self.assertEqual(manager.provider, self.mock_provider)
        self.assertEqual(manager.vpc_id, None)
        self.assertEqual(manager.subnet_id, None)
        self.assertEqual(manager.security_group_id, None)
        self.assertEqual(manager.iam_fleet_role_arn, None)
        self.assertEqual(manager.fleet_requests, {})
        self.assertEqual(manager.instances, {})
        self.assertEqual(manager.blocks, {})

    @patch("boto3.Session")
    def test_generate_user_data(self, mock_session_cls):
        """Test generation of user data script."""
        # Configure mocks
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_session.client.return_value = MagicMock()
        mock_session.resource.return_value = MagicMock()

        # Instantiate SpotFleetManager
        manager = SpotFleetManager(self.mock_provider)

        # Generate user data
        user_data = manager._generate_user_data()

        # Verify user data contains expected content
        self.assertIn("#!/bin/bash", user_data)
        self.assertIn("Starting Parsl worker setup for test-workflow-id", user_data)
        self.assertIn("Worker init script", user_data)

    @patch("boto3.Session")
    @patch("time.sleep", return_value=None)  # Don't actually sleep during tests
    def test_get_iam_fleet_role_existing(self, mock_sleep, mock_session_cls):
        """Test getting an existing IAM fleet role."""
        # Configure mocks
        mock_session = MagicMock()
        mock_iam_client = MagicMock()

        mock_session_cls.return_value = mock_session
        mock_session.client.side_effect = lambda service: {
            "ec2": MagicMock(),
            "iam": mock_iam_client,
        }[service]

        # Mock get_role to return an existing role
        mock_iam_client.get_role.return_value = {
            "Role": {"Arn": "arn:aws:iam::123456789012:role/test-fleet-role"}
        }

        # Instantiate SpotFleetManager
        manager = SpotFleetManager(self.mock_provider)

        # Get IAM fleet role
        role_arn = manager._get_iam_fleet_role()

        # Verify the role ARN is correct
        self.assertEqual(role_arn, "arn:aws:iam::123456789012:role/test-fleet-role")

        # Verify get_role was called
        # Derived from TAG_PREFIX, not spelled out, so a prefix change does not
        # silently make this assertion stale again.
        role_name = f"{TAG_PREFIX}-spot-fleet-role-{self.mock_provider.workflow_id[:8]}"
        mock_iam_client.get_role.assert_called_with(RoleName=role_name)

        # Verify create_role was not called
        mock_iam_client.create_role.assert_not_called()

    @patch("boto3.Session")
    @patch("time.sleep", return_value=None)  # Don't actually sleep during tests
    def test_get_iam_fleet_role_create_new(self, mock_sleep, mock_session_cls):
        """Test creating a new IAM fleet role when one doesn't exist."""
        # Configure mocks
        mock_session = MagicMock()
        mock_iam_client = MagicMock()

        mock_session_cls.return_value = mock_session
        mock_session.client.side_effect = lambda service: {
            "ec2": MagicMock(),
            "iam": mock_iam_client,
        }[service]

        # Mock get_role to raise NoSuchEntity error
        mock_iam_client.get_role.side_effect = ClientError(
            {
                "Error": {"Code": "NoSuchEntity", "Message": "Role not found"},
                "ResponseMetadata": {},
            },
            "GetRole",
        )

        # Mock create_role response
        mock_iam_client.create_role.return_value = {
            "Role": {"Arn": "arn:aws:iam::123456789012:role/new-fleet-role"}
        }

        # Instantiate SpotFleetManager
        manager = SpotFleetManager(self.mock_provider)

        # Get IAM fleet role (should create a new one)
        role_arn = manager._get_iam_fleet_role()

        # Verify the role ARN is correct
        self.assertEqual(role_arn, "arn:aws:iam::123456789012:role/new-fleet-role")

        # Verify get_role was called
        # Derived from TAG_PREFIX, not spelled out, so a prefix change does not
        # silently make this assertion stale again.
        role_name = f"{TAG_PREFIX}-spot-fleet-role-{self.mock_provider.workflow_id[:8]}"
        mock_iam_client.get_role.assert_called_with(RoleName=role_name)

        # Verify create_role was called
        mock_iam_client.create_role.assert_called_once()

        # Verify attach_role_policy was called
        mock_iam_client.attach_role_policy.assert_called_with(
            RoleName=role_name,
            PolicyArn="arn:aws:iam::aws:policy/service-role/AmazonEC2SpotFleetTaggingRole",
        )

    @patch("boto3.Session")
    def test_throttling_error_handling(self, mock_session_cls):
        """Test handling of throttling errors."""
        # Configure mocks
        mock_session = MagicMock()
        mock_ec2_client = MagicMock()

        mock_session_cls.return_value = mock_session
        mock_session.client.return_value = mock_ec2_client
        mock_session.resource.return_value = MagicMock()

        # Mock the request_spot_fleet to raise a throttling error
        mock_ec2_client.request_spot_fleet.side_effect = ClientError(
            {
                "Error": {
                    "Code": "RequestLimitExceeded",
                    "Message": "Request limit exceeded",
                },
                "ResponseMetadata": {"RetryAfter": 30},
            },
            "RequestSpotFleet",
        )

        # Instantiate SpotFleetManager
        manager = SpotFleetManager(self.mock_provider)

        # Set up network resources (needed for _create_spot_fleet_request)
        manager.vpc_id = "vpc-12345678"
        manager.subnet_id = "subnet-12345678"
        manager.security_group_id = "sg-12345678"
        manager.iam_fleet_role_arn = "arn:aws:iam::123456789012:role/fleet-role"

        # Attempt to create a spot fleet request, which should raise a SpotFleetThrottlingError
        with self.assertRaises(SpotFleetThrottlingError) as context:
            manager._create_spot_fleet_request(
                "block-123",
                {
                    "vpc_id": "vpc-12345678",
                    "subnet_id": "subnet-12345678",
                    "security_group_id": "sg-12345678",
                },
                1,
                "arn:aws:iam::123456789012:role/fleet-role",
            )

        # Verify the error message and attributes
        self.assertIn("AWS throttled Spot Fleet request", str(context.exception))
        self.assertEqual(context.exception.operation, "request_spot_fleet")
        self.assertEqual(context.exception.retry_after, 30)


if __name__ == "__main__":
    unittest.main()

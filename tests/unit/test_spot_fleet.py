"""Unit tests for the SpotFleetManager class.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import unittest
from unittest.mock import patch, MagicMock
import boto3
from botocore.exceptions import ClientError
import logging
import pytest

from parsl_ephemeral_aws.compute.spot_fleet import SpotFleetManager
from parsl_ephemeral_aws.exceptions import (
    SpotFleetError,
    SpotFleetRequestError,
    SpotFleetThrottlingError
)


class TestSpotFleetManager(unittest.TestCase):
    """Test suite for the SpotFleetManager class."""

    def setUp(self):
        """Set up the test environment."""
        # Create a mock provider
        self.mock_provider = MagicMock()
        self.mock_provider.workflow_id = "test-workflow-id"
        self.mock_provider.region = "us-east-1"
        self.mock_provider.aws_access_key_id = "test_access_key"
        self.mock_provider.aws_secret_access_key = "test_secret_key"
        self.mock_provider.aws_session_token = None
        self.mock_provider.aws_profile = None
        self.mock_provider.vpc_id = None
        self.mock_provider.subnet_id = None
        self.mock_provider.security_group_id = None
        self.mock_provider.image_id = "ami-12345678"
        self.mock_provider.instance_type = "t2.micro"
        self.mock_provider.use_public_ips = True
        self.mock_provider.nodes_per_block = 1
        self.mock_provider.tags = {"ProjectTag": "TestProject"}
        self.mock_provider.spot_max_price_percentage = 100
        self.mock_provider.worker_init = "echo 'Worker init script'"

        # Disable logging during tests
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        """Clean up after tests."""
        # Re-enable logging
        logging.disable(logging.NOTSET)

    @patch('boto3.Session')
    def test_initialization(self, mock_session_cls):
        """Test SpotFleetManager initialization."""
        # Configure mocks
        mock_session = MagicMock()
        mock_ec2_client = MagicMock()
        mock_ec2_resource = MagicMock()
        
        mock_session_cls.return_value = mock_session
        mock_session.client.return_value = mock_ec2_client
        mock_session.resource.return_value = mock_ec2_resource
        
        # Instantiate SpotFleetManager
        manager = SpotFleetManager(self.mock_provider)
        
        # Verify initialization
        mock_session_cls.assert_called_once_with(
            region_name="us-east-1",
            aws_access_key_id="test_access_key",
            aws_secret_access_key="test_secret_key"
        )
        
        mock_session.client.assert_called_with('ec2')
        mock_session.resource.assert_called_with('ec2')
        
        # Verify instance variables
        self.assertEqual(manager.provider, self.mock_provider)
        self.assertEqual(manager.vpc_id, None)
        self.assertEqual(manager.subnet_id, None)
        self.assertEqual(manager.security_group_id, None)
        self.assertEqual(manager.iam_fleet_role_arn, None)
        self.assertEqual(manager.fleet_requests, {})
        self.assertEqual(manager.instances, {})
        self.assertEqual(manager.blocks, {})

    @patch('boto3.Session')
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

    @patch('boto3.Session')
    @patch('time.sleep', return_value=None)  # Don't actually sleep during tests
    def test_get_iam_fleet_role_existing(self, mock_sleep, mock_session_cls):
        """Test getting an existing IAM fleet role."""
        # Configure mocks
        mock_session = MagicMock()
        mock_iam_client = MagicMock()
        
        mock_session_cls.return_value = mock_session
        mock_session.client.side_effect = lambda service: {
            'ec2': MagicMock(),
            'iam': mock_iam_client
        }[service]
        
        # Mock get_role to return an existing role
        mock_iam_client.get_role.return_value = {
            'Role': {
                'Arn': 'arn:aws:iam::123456789012:role/test-fleet-role'
            }
        }
        
        # Instantiate SpotFleetManager
        manager = SpotFleetManager(self.mock_provider)
        
        # Get IAM fleet role
        role_arn = manager._get_iam_fleet_role()
        
        # Verify the role ARN is correct
        self.assertEqual(role_arn, 'arn:aws:iam::123456789012:role/test-fleet-role')
        
        # Verify get_role was called
        role_name = f"parsl-aws-spot-fleet-role-{self.mock_provider.workflow_id[:8]}"
        mock_iam_client.get_role.assert_called_with(RoleName=role_name)
        
        # Verify create_role was not called
        mock_iam_client.create_role.assert_not_called()

    @patch('boto3.Session')
    @patch('time.sleep', return_value=None)  # Don't actually sleep during tests
    def test_get_iam_fleet_role_create_new(self, mock_sleep, mock_session_cls):
        """Test creating a new IAM fleet role when one doesn't exist."""
        # Configure mocks
        mock_session = MagicMock()
        mock_iam_client = MagicMock()
        
        mock_session_cls.return_value = mock_session
        mock_session.client.side_effect = lambda service: {
            'ec2': MagicMock(),
            'iam': mock_iam_client
        }[service]
        
        # Mock get_role to raise NoSuchEntity error
        mock_iam_client.get_role.side_effect = ClientError(
            {
                'Error': {
                    'Code': 'NoSuchEntity',
                    'Message': 'Role not found'
                },
                'ResponseMetadata': {}
            },
            'GetRole'
        )
        
        # Mock create_role response
        mock_iam_client.create_role.return_value = {
            'Role': {
                'Arn': 'arn:aws:iam::123456789012:role/new-fleet-role'
            }
        }
        
        # Instantiate SpotFleetManager
        manager = SpotFleetManager(self.mock_provider)
        
        # Get IAM fleet role (should create a new one)
        role_arn = manager._get_iam_fleet_role()
        
        # Verify the role ARN is correct
        self.assertEqual(role_arn, 'arn:aws:iam::123456789012:role/new-fleet-role')
        
        # Verify get_role was called
        role_name = f"parsl-aws-spot-fleet-role-{self.mock_provider.workflow_id[:8]}"
        mock_iam_client.get_role.assert_called_with(RoleName=role_name)
        
        # Verify create_role was called
        mock_iam_client.create_role.assert_called_once()
        
        # Verify attach_role_policy was called
        mock_iam_client.attach_role_policy.assert_called_with(
            RoleName=role_name,
            PolicyArn="arn:aws:iam::aws:policy/service-role/AmazonEC2SpotFleetTaggingRole"
        )

    @patch('boto3.Session')
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
                'Error': {
                    'Code': 'RequestLimitExceeded',
                    'Message': 'Request limit exceeded'
                },
                'ResponseMetadata': {
                    'RetryAfter': 30
                }
            },
            'RequestSpotFleet'
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
            manager._create_spot_fleet_request("block-123", {
                "vpc_id": "vpc-12345678",
                "subnet_id": "subnet-12345678",
                "security_group_id": "sg-12345678"
            }, 1)
        
        # Verify the error message and attributes
        self.assertIn("AWS throttled Spot Fleet request", str(context.exception))
        self.assertEqual(context.exception.operation, "request_spot_fleet")
        self.assertEqual(context.exception.retry_after, 30)


if __name__ == '__main__':
    unittest.main()
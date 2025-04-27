"""Integration tests for the SpotFleetManager class.

These tests use the moto library to mock AWS services for realistic integration testing.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import unittest
from unittest.mock import MagicMock, patch
import boto3
import pytest

try:
    # Check if moto is available
    import moto
    from moto import mock_ec2, mock_iam
    MOTO_AVAILABLE = True
except ImportError:
    MOTO_AVAILABLE = False

from parsl_ephemeral_aws.compute.spot_fleet import SpotFleetManager
from parsl_ephemeral_aws.constants import STATUS_PENDING


# Skip all tests if moto is not installed
pytestmark = pytest.mark.skipif(not MOTO_AVAILABLE, reason="moto not installed")


@mock_ec2
@mock_iam
class TestSpotFleetManagerIntegration(unittest.TestCase):
    """Integration tests for SpotFleetManager using moto."""

    def setUp(self):
        """Set up test environment."""
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
        
        # Create boto3 clients directly with moto
        self.ec2_client = boto3.client('ec2', region_name='us-east-1')
        self.iam_client = boto3.client('iam', region_name='us-east-1')
        
        # Create a VPC for testing
        vpc_response = self.ec2_client.create_vpc(CidrBlock='10.0.0.0/16')
        self.vpc_id = vpc_response['Vpc']['VpcId']
        
        # Create a subnet
        subnet_response = self.ec2_client.create_subnet(
            VpcId=self.vpc_id,
            CidrBlock='10.0.0.0/24'
        )
        self.subnet_id = subnet_response['Subnet']['SubnetId']
        
        # Create a security group
        sg_response = self.ec2_client.create_security_group(
            GroupName='test-sg',
            Description='Test security group',
            VpcId=self.vpc_id
        )
        self.security_group_id = sg_response['GroupId']
        
        # Create an AMI (Note: moto doesn't require a real AMI)
        self.mock_provider.image_id = "ami-12345678"
        
        # Create IAM role for spot fleet
        trust_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "spotfleet.amazonaws.com"},
                    "Action": "sts:AssumeRole"
                }
            ]
        }
        
        self.iam_client.create_role(
            RoleName='SpotFleetRole',
            AssumeRolePolicyDocument=str(trust_policy),
            Description='Role for Spot Fleet'
        )
        
        self.iam_client.attach_role_policy(
            RoleName='SpotFleetRole',
            PolicyArn='arn:aws:iam::aws:policy/service-role/AmazonEC2SpotFleetTaggingRole'
        )

    @patch('time.sleep', return_value=None)  # Don't actually sleep during tests
    def test_setup_network_resources_with_existing(self, mock_sleep):
        """Test setting up network resources using existing resources."""
        # Update provider to use existing network resources
        self.mock_provider.vpc_id = self.vpc_id
        self.mock_provider.subnet_id = self.subnet_id
        self.mock_provider.security_group_id = self.security_group_id
        
        # Create SpotFleetManager
        with patch('boto3.Session') as mock_session:
            # Let Session use the actual boto3 clients created with moto
            mock_session_instance = MagicMock()
            mock_session.return_value = mock_session_instance
            mock_session_instance.client.side_effect = lambda service, **kwargs: {
                'ec2': self.ec2_client,
                'iam': self.iam_client
            }[service]
            mock_session_instance.resource.return_value = boto3.resource('ec2', region_name='us-east-1')
            
            # Create manager
            manager = SpotFleetManager(self.mock_provider)
            
            # Set up network resources
            network = manager._setup_network_resources()
            
            # Verify network resources
            self.assertEqual(network['vpc_id'], self.vpc_id)
            self.assertEqual(network['subnet_id'], self.subnet_id)
            self.assertEqual(network['security_group_id'], self.security_group_id)

    @patch('parsl_ephemeral_aws.compute.spot_fleet.SpotFleetManager._wait_for_fleet_instances')
    @patch('parsl_ephemeral_aws.compute.spot_fleet.SpotFleetManager._create_spot_fleet_request')
    @patch('parsl_ephemeral_aws.compute.spot_fleet.SpotFleetManager._get_iam_fleet_role')
    @patch('parsl_ephemeral_aws.compute.spot_fleet.SpotFleetManager._setup_network_resources')
    def test_create_blocks(self, mock_setup_network, mock_get_iam_role, mock_create_fleet, mock_wait_fleet):
        """Test creating compute blocks."""
        # Configure mocks
        mock_setup_network.return_value = {
            'vpc_id': self.vpc_id,
            'subnet_id': self.subnet_id,
            'security_group_id': self.security_group_id
        }
        mock_get_iam_role.return_value = 'arn:aws:iam::123456789012:role/SpotFleetRole'
        mock_create_fleet.return_value = 'sfr-12345678'
        
        # Create SpotFleetManager
        with patch('boto3.Session') as mock_session:
            mock_session_instance = MagicMock()
            mock_session.return_value = mock_session_instance
            mock_session_instance.client.return_value = MagicMock()
            mock_session_instance.resource.return_value = MagicMock()
            
            # Create manager
            manager = SpotFleetManager(self.mock_provider)
            
            # Create blocks
            blocks = manager.create_blocks(2)
            
            # Verify blocks were created
            self.assertEqual(len(blocks), 2)
            
            # Verify block properties
            for block_id, block_info in blocks.items():
                self.assertEqual(block_info['fleet_request_id'], 'sfr-12345678')
                self.assertEqual(block_info['status'], STATUS_PENDING)
                self.assertIn('created_at', block_info)
            
            # Verify method calls
            mock_setup_network.assert_called_once()
            mock_get_iam_role.assert_called_once()
            self.assertEqual(mock_create_fleet.call_count, 2)
            self.assertEqual(mock_wait_fleet.call_count, 2)


if __name__ == '__main__':
    unittest.main()
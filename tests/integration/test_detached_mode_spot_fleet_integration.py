"""Integration tests for the SpotFleet functionality in DetachedMode.

These tests use the moto library to mock AWS services for realistic integration testing.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import unittest
from unittest.mock import MagicMock, patch
import boto3
import pytest
import json
import time

try:
    # Check if moto is available
    import moto
    from moto import mock_ec2, mock_iam, mock_ssm, mock_cloudformation
    MOTO_AVAILABLE = True
except ImportError:
    MOTO_AVAILABLE = False

from parsl_ephemeral_aws.modes.detached import DetachedMode
from parsl_ephemeral_aws.constants import (
    RESOURCE_TYPE_EC2,
    RESOURCE_TYPE_VPC,
    RESOURCE_TYPE_SUBNET,
    RESOURCE_TYPE_SECURITY_GROUP,
    RESOURCE_TYPE_BASTION,
    RESOURCE_TYPE_CLOUDFORMATION,
    RESOURCE_TYPE_SPOT_FLEET,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_CANCELED,
    STATUS_COMPLETED,
)


# Skip all tests if moto is not installed
pytestmark = pytest.mark.skipif(not MOTO_AVAILABLE, reason="moto not installed")


class MockStateStore:
    """Mock state store implementation for testing."""

    def __init__(self):
        """Initialize with empty state."""
        self.state = None

    def save_state(self, state):
        """Save state."""
        self.state = state
        return True

    def load_state(self):
        """Load state."""
        return self.state


@mock_ec2
@mock_iam
@mock_ssm
@mock_cloudformation
class TestDetachedModeSpotFleetIntegration(unittest.TestCase):
    """Integration tests for DetachedMode SpotFleet functionality using moto."""

    def setUp(self):
        """Set up test environment."""
        # Create boto3 clients directly with moto
        self.ec2_client = boto3.client('ec2', region_name='us-east-1')
        self.iam_client = boto3.client('iam', region_name='us-east-1')
        self.ssm_client = boto3.client('ssm', region_name='us-east-1')
        self.cf_client = boto3.client('cloudformation', region_name='us-east-1')
        
        # Create boto3 session
        self.session = boto3.Session(region_name='us-east-1')
        
        # Create state store
        self.state_store = MockStateStore()
        
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
        
        # Create a dummy AMI
        ami_response = self.ec2_client.register_image(
            Name='test-ami',
            RootDeviceName='/dev/sda1',
            BlockDeviceMappings=[
                {
                    'DeviceName': '/dev/sda1',
                    'Ebs': {'VolumeSize': 8}
                }
            ]
        )
        self.ami_id = ami_response['ImageId']
        
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
        
        # Create role
        self.iam_client.create_role(
            RoleName='SpotFleetRole',
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description='Role for Spot Fleet'
        )
        
        # Create SSM parameters path for workflow
        self.workflow_id = "test-workflow-id"
        self.provider_id = "test-provider-id"
        
        # Create DetachedMode instance with SpotFleet enabled
        self.detached_mode = DetachedMode(
            provider_id=self.provider_id,
            session=self.session,
            state_store=self.state_store,
            workflow_id=self.workflow_id,
            bastion_instance_type="t2.micro",
            instance_type="t2.micro",
            image_id=self.ami_id,
            vpc_id=self.vpc_id,
            subnet_id=self.subnet_id,
            security_group_id=self.security_group_id,
            create_vpc=False,  # Use existing VPC
            use_spot_fleet=True,
            instance_types=["t2.micro", "t2.small", "m5.small"],
            nodes_per_block=2,
            spot_max_price_percentage=80
        )

    @patch('time.sleep', return_value=None)  # Don't actually sleep during tests
    def test_submit_job_with_spot_fleet(self, mock_sleep):
        """Test submitting a job with SpotFleet options."""
        # Submit a job
        job_id = "test-job-1"
        command = "echo 'Hello, world!'"
        tasks_per_node = 1
        
        # Submit the job
        with patch('parsl_ephemeral_aws.modes.detached.get_cf_template', return_value='{}'):
            # First initialize the mode
            self.detached_mode.initialize()
            
            # Then submit the job
            resource_id = self.detached_mode.submit_job(job_id, command, tasks_per_node)
        
        # Verify resource is tracked
        self.assertIn(resource_id, self.detached_mode.resources)
        self.assertEqual(self.detached_mode.resources[resource_id]["job_id"], job_id)
        self.assertEqual(self.detached_mode.resources[resource_id]["status"], STATUS_PENDING)
        
        # Verify SSM parameters were created
        job_param = self.ssm_client.get_parameter(
            Name=f"/parsl/workflows/{self.workflow_id}/jobs/{job_id}"
        )
        status_param = self.ssm_client.get_parameter(
            Name=f"/parsl/workflows/{self.workflow_id}/status/{job_id}"
        )
        
        # Verify job data contains SpotFleet options
        job_data = json.loads(job_param['Parameter']['Value'])
        self.assertTrue(job_data['use_spot_fleet'])
        self.assertEqual(len(job_data['instance_types']), 3)
        self.assertIn('t2.micro', job_data['instance_types'])
        self.assertIn('t2.small', job_data['instance_types'])
        self.assertEqual(job_data['nodes_per_block'], 2)
        self.assertEqual(job_data['spot_max_price_percentage'], 80)
        
        # Verify job status is PENDING
        status_data = json.loads(status_param['Parameter']['Value'])
        self.assertEqual(status_data['status'], STATUS_PENDING)

    @patch('time.sleep', return_value=None)  # Don't actually sleep during tests
    def test_get_job_status_with_spot_fleet(self, mock_sleep):
        """Test getting job status for a SpotFleet job."""
        # Create a job status parameter with SpotFleet information
        job_id = "test-job-2"
        status_data = {
            'status': STATUS_RUNNING,
            'instance_id': 'i-spot1',
            'fleet_request_id': 'sfr-12345',
            'resource_type': RESOURCE_TYPE_SPOT_FLEET,
            'all_instance_ids': ['i-spot1', 'i-spot2']
        }
        
        # Put the status in SSM
        self.ssm_client.put_parameter(
            Name=f"/parsl/workflows/{self.workflow_id}/status/{job_id}",
            Value=json.dumps(status_data),
            Type='String'
        )
        
        # Create resource tracking
        resource_id = f"spot-fleet-{job_id}"
        self.detached_mode.resources = {
            resource_id: {
                "job_id": job_id,
                "status": STATUS_PENDING,
                "type": RESOURCE_TYPE_EC2
            }
        }
        
        # Get job status
        status = self.detached_mode.get_job_status([resource_id])
        
        # Verify status
        self.assertEqual(status[resource_id], STATUS_RUNNING)
        
        # Verify resource was updated with SpotFleet information
        self.assertEqual(self.detached_mode.resources[resource_id]["status"], STATUS_RUNNING)
        self.assertEqual(self.detached_mode.resources[resource_id]["fleet_request_id"], "sfr-12345")
        self.assertEqual(self.detached_mode.resources[resource_id]["resource_type"], RESOURCE_TYPE_SPOT_FLEET)
        self.assertEqual(self.detached_mode.resources[resource_id]["all_instance_ids"], ["i-spot1", "i-spot2"])

    @patch('time.sleep', return_value=None)  # Don't actually sleep during tests
    def test_cancel_job_with_spot_fleet(self, mock_sleep):
        """Test canceling a SpotFleet job."""
        # Create a job parameter for a SpotFleet job
        job_id = "test-job-3"
        job_data = {
            'command': 'echo "test"',
            'image_id': self.ami_id,
            'instance_type': 't2.micro',
            'subnet_id': self.subnet_id,
            'security_group_id': self.security_group_id,
            'use_spot_fleet': True,
            'instance_types': ['t2.micro', 't2.small'],
            'nodes_per_block': 2
        }
        
        # Put the job data in SSM
        self.ssm_client.put_parameter(
            Name=f"/parsl/workflows/{self.workflow_id}/jobs/{job_id}",
            Value=json.dumps(job_data),
            Type='String'
        )
        
        # Create resource tracking with SpotFleet information
        resource_id = f"spot-fleet-{job_id}"
        self.detached_mode.resources = {
            resource_id: {
                "job_id": job_id,
                "status": STATUS_RUNNING,
                "resource_type": RESOURCE_TYPE_SPOT_FLEET,
                "fleet_request_id": "sfr-12345",
                "all_instance_ids": ["i-spot1", "i-spot2"]
            }
        }
        
        # Cancel the job
        status = self.detached_mode.cancel_jobs([resource_id])
        
        # Verify cancel status
        self.assertEqual(status[resource_id], STATUS_CANCELED)
        self.assertEqual(self.detached_mode.resources[resource_id]["status"], STATUS_CANCELED)
        
        # Verify cancel parameter was created in SSM
        cancel_param = self.ssm_client.get_parameter(
            Name=f"/parsl/workflows/{self.workflow_id}/cancel"
        )
        
        # Verify cancel parameter contains SpotFleet information
        cancel_data = json.loads(cancel_param['Parameter']['Value'])
        self.assertIn(job_id, cancel_data["job_ids"])
        self.assertIn("spot_fleet_jobs", cancel_data)
        self.assertEqual(cancel_data["spot_fleet_jobs"][job_id], "sfr-12345")

    @patch('time.sleep', return_value=None)  # Don't actually sleep during tests
    def test_cleanup_spot_fleet_resources(self, mock_sleep):
        """Test cleaning up SpotFleet resources."""
        # Create necessary params in SSM
        job_id = "test-job-4"
        
        # Create job data parameter
        self.ssm_client.put_parameter(
            Name=f"/parsl/workflows/{self.workflow_id}/jobs/{job_id}",
            Value=json.dumps({"command": "echo test"}),
            Type='String'
        )
        
        # Create job status parameter
        self.ssm_client.put_parameter(
            Name=f"/parsl/workflows/{self.workflow_id}/status/{job_id}",
            Value=json.dumps({"status": STATUS_RUNNING}),
            Type='String'
        )
        
        # Mock a SpotFleet resource
        resource_id = f"spot-fleet-{job_id}"
        self.detached_mode.resources = {
            resource_id: {
                "job_id": job_id,
                "status": STATUS_RUNNING,
                "resource_type": RESOURCE_TYPE_SPOT_FLEET,
                "fleet_request_id": "sfr-12345",
                "all_instance_ids": ["i-spot1", "i-spot2"]
            }
        }
        
        # Clean up resources
        with patch('boto3.client') as mock_boto_client:
            # Mock the EC2 client's cancel_spot_fleet_requests method
            mock_ec2_client = MagicMock()
            mock_boto_client.return_value = mock_ec2_client
            mock_ec2_client.cancel_spot_fleet_requests.return_value = {
                'SuccessfulFleetRequests': [
                    {
                        'SpotFleetRequestId': 'sfr-12345',
                        'CurrentSpotFleetRequestState': 'cancelled_terminating',
                        'PreviousSpotFleetRequestState': 'active'
                    }
                ]
            }
            
            # Call cleanup
            self.detached_mode.cleanup_resources([resource_id])
        
        # Verify resource was removed from tracking
        self.assertNotIn(resource_id, self.detached_mode.resources)
        
        # Verify SSM parameters were deleted
        with self.assertRaises(self.ssm_client.exceptions.ParameterNotFound):
            self.ssm_client.get_parameter(
                Name=f"/parsl/workflows/{self.workflow_id}/jobs/{job_id}"
            )
        
        with self.assertRaises(self.ssm_client.exceptions.ParameterNotFound):
            self.ssm_client.get_parameter(
                Name=f"/parsl/workflows/{self.workflow_id}/status/{job_id}"
            )


if __name__ == '__main__':
    unittest.main()
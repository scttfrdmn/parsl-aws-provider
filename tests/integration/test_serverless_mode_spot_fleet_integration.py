"""Integration tests for ServerlessMode with SpotFleet functionality.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import pytest
import boto3
import json
import os
import uuid
import time
from unittest.mock import patch, MagicMock

import moto
from moto import mock_ec2, mock_iam, mock_cloudformation

from parsl_ephemeral_aws.modes.serverless import ServerlessMode
from parsl_ephemeral_aws.exceptions import (
    ResourceCreationError,
    JobSubmissionError,
)
from parsl_ephemeral_aws.constants import (
    RESOURCE_TYPE_SPOT_FLEET,
    WORKER_TYPE_ECS,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_CANCELLED,
)


@mock_ec2
@mock_iam
@mock_cloudformation
class TestServerlessModeSpotFleetIntegration:
    """Integration tests for ServerlessMode with SpotFleet."""

    @pytest.fixture
    def aws_session(self):
        """Create a real boto3 session that will use moto's mocks."""
        return boto3.Session(region_name='us-east-1')

    @pytest.fixture
    def mock_state_store(self):
        """Create a mock state store."""
        state_store = MagicMock()
        state_store.load_state.return_value = None
        state = {}
        state_store.save_state = lambda s: state.update(s)
        return state_store

    @pytest.fixture
    def provider_id(self):
        """Generate a unique provider ID for testing."""
        return f"test-{uuid.uuid4()}"

    @pytest.fixture
    def serverless_mode(self, aws_session, mock_state_store, provider_id):
        """Create a ServerlessMode instance for testing."""
        # Create a custom template for easier mocking with moto
        template_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 
                                   "parsl_ephemeral_aws", "templates", "cloudformation")
        os.makedirs(template_dir, exist_ok=True)
        
        # Create a simple CloudFormation template for testing
        with open(os.path.join(template_dir, "ecs_worker.yml"), "w") as f:
            f.write("""
AWSTemplateFormatVersion: '2010-09-09'
Parameters:
  WorkflowId:
    Type: String
  JobId:
    Type: String
  UseSpotFleet:
    Type: String
    Default: 'false'
  InstanceTypes:
    Type: String
    Default: '[]'
  NodesPerBlock:
    Type: Number
    Default: 1
  SpotMaxPricePercentage:
    Type: String
    Default: ''
  VpcId:
    Type: String
  SubnetIds:
    Type: CommaDelimitedList
  SecurityGroupIds:
    Type: CommaDelimitedList
  Command:
    Type: String
  UseSpot:
    Type: String
    Default: 'false'
Resources:
  # Simplified template for testing
  DummyInstance:
    Type: AWS::EC2::Instance
    Properties:
      ImageId: ami-12345678
      InstanceType: t2.micro
      Tags:
        - Key: Name
          Value: !Sub "parsl-test-${WorkflowId}"
  SpotFleetRole:
    Type: AWS::IAM::Role
    Properties:
      AssumeRolePolicyDocument:
        Version: '2012-10-17'
        Statement:
          - Effect: Allow
            Principal:
              Service: spotfleet.amazonaws.com
            Action: sts:AssumeRole
      ManagedPolicyArns:
        - arn:aws:iam::aws:policy/service-role/AmazonEC2SpotFleetTaggingRole
Outputs:
  SpotFleetRequestId:
    Description: ID of the Spot Fleet request
    Value: sfr-12345678
            """)
        
        # Create the VPC template
        with open(os.path.join(template_dir, "vpc.yml"), "w") as f:
            f.write("""
AWSTemplateFormatVersion: '2010-09-09'
Parameters:
  VpcCidr:
    Type: String
  PublicSubnetCidr:
    Type: String
  WorkflowId:
    Type: String
Resources:
  DummyVPC:
    Type: AWS::EC2::VPC
    Properties:
      CidrBlock: !Ref VpcCidr
      Tags:
        - Key: Name
          Value: !Sub "parsl-vpc-${WorkflowId}"
  DummySubnet:
    Type: AWS::EC2::Subnet
    Properties:
      VpcId: !Ref DummyVPC
      CidrBlock: !Ref PublicSubnetCidr
      Tags:
        - Key: Name
          Value: !Sub "parsl-subnet-${WorkflowId}"
Outputs:
  VpcId:
    Description: VPC ID
    Value: !Ref DummyVPC
  PublicSubnetId:
    Description: Public Subnet ID
    Value: !Ref DummySubnet
            """)
            
        # Create ServerlessMode with SpotFleet
        mode = ServerlessMode(
            provider_id=provider_id,
            session=aws_session,
            state_store=mock_state_store,
            worker_type=WORKER_TYPE_ECS,
            use_spot_fleet=True,
            instance_types=["t3.small", "t3.medium"],
            nodes_per_block=2,
            spot_max_price_percentage=80,
            create_vpc=True,
        )
        
        # Setup mocks
        mode.cf_client = aws_session.client('cloudformation')
        mode.lambda_manager = MagicMock()
        mode.lambda_manager._generate_lambda_code.return_value = b"lambda_code_zip"
        
        # Patch _get_spot_fleet_status to return consistent results in tests
        mode._get_spot_fleet_status = MagicMock(return_value=STATUS_RUNNING)
        
        return mode

    def test_integration_initialize_with_spot_fleet(self, serverless_mode):
        """Test initializing infrastructure with SpotFleet support."""
        # Initialize the mode
        serverless_mode.initialize()
        
        # Verify initialization
        assert serverless_mode.initialized is True
        assert serverless_mode.vpc_id is not None
        assert serverless_mode.subnet_id is not None
        assert serverless_mode.security_group_id is not None
        
        # Verify SpotFleet-specific attributes were preserved
        assert serverless_mode.use_spot_fleet is True
        assert len(serverless_mode.instance_types) == 2
        assert serverless_mode.nodes_per_block == 2
        assert serverless_mode.spot_max_price_percentage == 80

    def test_integration_submit_job_with_spot_fleet(self, serverless_mode):
        """Test job submission with SpotFleet."""
        # Initialize first
        serverless_mode.initialize()
        
        # Submit a job
        job_id = "test-job-1"
        command = "echo hello from spot fleet"
        resource_id = serverless_mode.submit_job(job_id, command, 2)
        
        # Verify resource tracking
        assert resource_id in serverless_mode.resources
        assert serverless_mode.resources[resource_id]["job_id"] == job_id
        assert serverless_mode.resources[resource_id]["command"] == command
        assert serverless_mode.resources[resource_id]["status"] == STATUS_PENDING
        assert serverless_mode.resources[resource_id]["use_spot_fleet"] is True
        
        # Get job status (which should update with fleet_request_id)
        status = serverless_mode.get_job_status([resource_id])
        
        # With our mocked _get_spot_fleet_status, should be RUNNING
        assert status[resource_id] == STATUS_RUNNING
        
        # Should have updated tracking with fleet details
        assert "fleet_request_id" in serverless_mode.resources[resource_id]
        assert serverless_mode.resources[resource_id]["fleet_request_id"] == "sfr-12345678"
        assert serverless_mode.resources[resource_id]["resource_type"] == RESOURCE_TYPE_SPOT_FLEET

    def test_integration_cancel_spot_fleet_job(self, serverless_mode):
        """Test canceling a SpotFleet job."""
        # Initialize and submit a job
        serverless_mode.initialize()
        job_id = "test-job-2"
        resource_id = serverless_mode.submit_job(job_id, "echo hello", 2)
        
        # Get status to set fleet_request_id
        serverless_mode.get_job_status([resource_id])
        
        # Cancel the job
        cancel_results = serverless_mode.cancel_jobs([resource_id])
        
        # Verify cancellation
        assert cancel_results[resource_id] == STATUS_CANCELLED
        assert serverless_mode.resources[resource_id]["status"] == STATUS_CANCELLED

    def test_integration_list_resources_with_spot_fleet(self, serverless_mode):
        """Test listing resources including SpotFleet."""
        # Initialize and submit jobs
        serverless_mode.initialize()
        job_id = "test-job-3"
        resource_id = serverless_mode.submit_job(job_id, "echo hello", 2)
        
        # Get status to set fleet_request_id
        serverless_mode.get_job_status([resource_id])
        
        # List resources
        resources = serverless_mode.list_resources()
        
        # Verify resource listing
        assert "spot_fleet_requests" in resources
        assert len(resources["spot_fleet_requests"]) == 1
        assert resources["spot_fleet_requests"][0]["job_id"] == job_id
        assert resources["spot_fleet_requests"][0]["fleet_request_id"] == "sfr-12345678"

    def test_integration_cleanup_with_spot_fleet(self, serverless_mode):
        """Test cleaning up resources with SpotFleet."""
        # Initialize and submit a job
        serverless_mode.initialize()
        job_id = "test-job-4"
        resource_id = serverless_mode.submit_job(job_id, "echo hello", 2)
        
        # Get status to set fleet_request_id
        serverless_mode.get_job_status([resource_id])
        
        # Clean up the resource
        serverless_mode.cleanup_resources([resource_id])
        
        # Verify resource was removed
        assert resource_id not in serverless_mode.resources
        
        # List resources to verify
        resources = serverless_mode.list_resources()
        assert len(resources["spot_fleet_requests"]) == 0

    @patch('parsl_ephemeral_aws.compute.spot_fleet_cleanup.cleanup_all_spot_fleet_resources')
    def test_integration_cleanup_infrastructure(self, mock_cleanup_spot_fleet, serverless_mode):
        """Test cleaning up all infrastructure with SpotFleet."""
        # Initialize and submit a job
        serverless_mode.initialize()
        job_id = "test-job-5"
        resource_id = serverless_mode.submit_job(job_id, "echo hello", 2)
        
        # Get status to set fleet_request_id
        serverless_mode.get_job_status([resource_id])
        
        # Mock the SpotFleet cleanup utility
        mock_cleanup_spot_fleet.return_value = {
            "cancelled_requests": ["sfr-12345678"],
            "cleaned_roles": ["parsl-aws-spot-fleet-role-test"],
            "errors": []
        }
        
        # Clean up all infrastructure
        serverless_mode.cleanup_infrastructure()
        
        # Verify SpotFleet cleanup was called
        mock_cleanup_spot_fleet.assert_called_once()
        
        # Verify state was reset
        assert serverless_mode.vpc_id is None
        assert serverless_mode.subnet_id is None
        assert serverless_mode.security_group_id is None
        assert not serverless_mode.resources  # Resources should be empty
        assert serverless_mode.initialized is False
"""Unit tests for the SpotFleet functionality in ServerlessMode.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import pytest
from unittest.mock import MagicMock, patch, call
import boto3
import time
import json
import os

from parsl_ephemeral_aws.modes.serverless import ServerlessMode
from parsl_ephemeral_aws.exceptions import (
    OperatingModeError,
    NetworkCreationError,
    ResourceCreationError,
    JobSubmissionError,
)
from parsl_ephemeral_aws.constants import (
    RESOURCE_TYPE_LAMBDA_FUNCTION,
    RESOURCE_TYPE_ECS_TASK,
    RESOURCE_TYPE_SPOT_FLEET,
    RESOURCE_TYPE_VPC,
    RESOURCE_TYPE_SUBNET,
    RESOURCE_TYPE_SECURITY_GROUP,
    WORKER_TYPE_LAMBDA,
    WORKER_TYPE_ECS,
    WORKER_TYPE_AUTO,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    STATUS_FAILED,
    STATUS_CANCELLED,
)


class TestServerlessModeSpotFleet:
    """Tests for the SpotFleet functionality in ServerlessMode class."""

    @pytest.fixture
    def mock_session(self):
        """Create a mock boto3 session."""
        session = MagicMock(spec=boto3.Session)
        session.region_name = "us-east-1"
        return session

    @pytest.fixture
    def mock_state_store(self):
        """Create a mock state store."""
        store = MagicMock()
        store.load_state.return_value = None  # Default to no state
        return store

    @pytest.fixture
    def mock_cf_client(self):
        """Create a mock CloudFormation client."""
        client = MagicMock()
        
        # Mock create_stack
        client.create_stack.return_value = {
            'StackId': 'stack-12345'
        }
        
        # Mock describe_stacks
        client.describe_stacks.return_value = {
            'Stacks': [
                {
                    'StackId': 'stack-12345',
                    'StackName': 'parsl-ecs-12345',
                    'StackStatus': 'CREATE_COMPLETE',
                    'Outputs': [
                        {
                            'OutputKey': 'SpotFleetRequestId',
                            'OutputValue': 'sfr-12345'
                        }
                    ]
                }
            ]
        }
        
        return client

    @pytest.fixture
    def mock_ec2_client(self):
        """Create a mock EC2 client."""
        client = MagicMock()
        
        # Mock create_vpc
        client.create_vpc.return_value = {
            'Vpc': {'VpcId': 'vpc-12345'}
        }
        
        # Mock describe_vpcs
        client.describe_vpcs.return_value = {
            'Vpcs': [{'VpcId': 'vpc-12345'}]
        }
        
        # Mock create_subnet
        client.create_subnet.return_value = {
            'Subnet': {'SubnetId': 'subnet-12345'}
        }
        
        # Mock describe_subnets
        client.describe_subnets.return_value = {
            'Subnets': [{'SubnetId': 'subnet-12345'}]
        }
        
        # Mock create_security_group
        client.create_security_group.return_value = {
            'GroupId': 'sg-12345'
        }
        
        # Mock describe_security_groups
        client.describe_security_groups.return_value = {
            'SecurityGroups': [{'GroupId': 'sg-12345'}]
        }
        
        # Mock describe_spot_fleet_requests
        client.describe_spot_fleet_requests.return_value = {
            'SpotFleetRequestConfigs': [
                {
                    'SpotFleetRequestId': 'sfr-12345',
                    'SpotFleetRequestState': 'active',
                    'SpotFleetRequestConfig': {
                        'TargetCapacity': 2
                    },
                    'FulfilledCapacity': 2
                }
            ]
        }
        
        # Mock cancel_spot_fleet_requests
        client.cancel_spot_fleet_requests.return_value = {
            'SuccessfulFleetRequests': [
                {
                    'SpotFleetRequestId': 'sfr-12345',
                    'CurrentSpotFleetRequestState': 'cancelled_terminating',
                    'PreviousSpotFleetRequestState': 'active'
                }
            ],
            'UnsuccessfulFleetRequests': []
        }
        
        return client

    @pytest.fixture
    def serverless_mode_with_spot_fleet(self, mock_session, mock_state_store, mock_ec2_client, mock_cf_client):
        """Create a ServerlessMode instance with SpotFleet enabled."""
        # Configure session to return mock clients
        def get_client(service_name, **kwargs):
            if service_name == 'ec2':
                return mock_ec2_client
            elif service_name == 'cloudformation':
                return mock_cf_client
            return MagicMock()
            
        mock_session.client.side_effect = get_client
        
        # Create mode instance with SpotFleet enabled
        mode = ServerlessMode(
            provider_id="test-provider",
            session=mock_session,
            state_store=mock_state_store,
            worker_type=WORKER_TYPE_ECS,
            use_spot_fleet=True,
            instance_types=["t3.small", "t3.medium", "m5.small"],
            nodes_per_block=2,
            spot_max_price_percentage=80
        )
        
        return mode

    def test_init_with_spot_fleet(self, serverless_mode_with_spot_fleet):
        """Test initialization of ServerlessMode with SpotFleet options."""
        # Verify SpotFleet specific attributes
        assert serverless_mode_with_spot_fleet.use_spot_fleet is True
        assert len(serverless_mode_with_spot_fleet.instance_types) == 3
        assert "t3.small" in serverless_mode_with_spot_fleet.instance_types
        assert "t3.medium" in serverless_mode_with_spot_fleet.instance_types
        assert "m5.small" in serverless_mode_with_spot_fleet.instance_types
        assert serverless_mode_with_spot_fleet.nodes_per_block == 2
        assert serverless_mode_with_spot_fleet.spot_max_price_percentage == 80

    def test_default_instance_types(self, mock_session, mock_state_store):
        """Test that default instance types are provided if not specified."""
        # Create mode with SpotFleet but no instance types specified
        mode = ServerlessMode(
            provider_id="test-provider",
            session=mock_session,
            state_store=mock_state_store,
            worker_type=WORKER_TYPE_ECS,
            use_spot_fleet=True,
            nodes_per_block=2
        )
        
        # Verify default instance types are set
        assert mode.use_spot_fleet is True
        assert len(mode.instance_types) > 0
        assert "t3.small" in mode.instance_types
        assert "m5.large" in mode.instance_types

    @patch('os.path.join')
    def test_submit_job_with_spot_fleet(self, mock_join, serverless_mode_with_spot_fleet, mock_cf_client):
        """Test job submission with SpotFleet options."""
        # Setup for job submission
        serverless_mode_with_spot_fleet.initialized = True
        serverless_mode_with_spot_fleet.vpc_id = "vpc-12345"
        serverless_mode_with_spot_fleet.subnet_id = "subnet-12345"
        serverless_mode_with_spot_fleet.security_group_id = "sg-12345"
        
        # Setup mock path
        mock_join.return_value = "path/to/template.yml"
        
        # Mock open file
        m = MagicMock()
        m.__enter__.return_value.read.return_value = "template content"
        with patch('builtins.open', return_value=m):
            # Submit job
            resource_id = serverless_mode_with_spot_fleet.submit_job("job-1", "echo hello", 2)
        
        # Verify CloudFormation stack was created
        mock_cf_client.create_stack.assert_called_once()
        args, kwargs = mock_cf_client.create_stack.call_args
        assert kwargs["StackName"].startswith("parsl-ecs-")
        
        # Verify stack parameters include SpotFleet options
        params = {p["ParameterKey"]: p["ParameterValue"] for p in kwargs["Parameters"]}
        assert params["UseSpotFleet"] == "true"
        assert params["UseSpot"] == "false"  # SpotFleet overrides Fargate Spot
        assert params["NodesPerBlock"] == "2"
        assert params["SpotMaxPricePercentage"] == "80"
        assert "t3.small" in params["InstanceTypes"]
        assert "t3.medium" in params["InstanceTypes"]
        assert "m5.small" in params["InstanceTypes"]
        
        # Verify resource tracking
        assert resource_id in serverless_mode_with_spot_fleet.resources
        assert serverless_mode_with_spot_fleet.resources[resource_id]["job_id"] == "job-1"
        assert serverless_mode_with_spot_fleet.resources[resource_id]["worker_type"] == WORKER_TYPE_ECS
        assert serverless_mode_with_spot_fleet.resources[resource_id]["status"] == STATUS_PENDING
        assert serverless_mode_with_spot_fleet.resources[resource_id]["use_spot_fleet"] is True
        
        # Verify state was saved
        serverless_mode_with_spot_fleet.state_store.save_state.assert_called()

    def test_get_spot_fleet_status(self, serverless_mode_with_spot_fleet, mock_ec2_client):
        """Test getting status for a SpotFleet job."""
        # Test with active, fulfilled fleet
        status = serverless_mode_with_spot_fleet._get_spot_fleet_status("sfr-12345")
        assert status == STATUS_RUNNING
        
        # Test with active but not fulfilled fleet
        mock_ec2_client.describe_spot_fleet_requests.return_value = {
            'SpotFleetRequestConfigs': [
                {
                    'SpotFleetRequestId': 'sfr-12345',
                    'SpotFleetRequestState': 'active',
                    'SpotFleetRequestConfig': {
                        'TargetCapacity': 2
                    },
                    'FulfilledCapacity': 1  # Only half fulfilled
                }
            ]
        }
        status = serverless_mode_with_spot_fleet._get_spot_fleet_status("sfr-12345")
        assert status == STATUS_PENDING
        
        # Test with submitted fleet
        mock_ec2_client.describe_spot_fleet_requests.return_value = {
            'SpotFleetRequestConfigs': [
                {
                    'SpotFleetRequestId': 'sfr-12345',
                    'SpotFleetRequestState': 'submitted'
                }
            ]
        }
        status = serverless_mode_with_spot_fleet._get_spot_fleet_status("sfr-12345")
        assert status == STATUS_PENDING
        
        # Test with cancelled fleet
        mock_ec2_client.describe_spot_fleet_requests.return_value = {
            'SpotFleetRequestConfigs': [
                {
                    'SpotFleetRequestId': 'sfr-12345',
                    'SpotFleetRequestState': 'cancelled'
                }
            ]
        }
        status = serverless_mode_with_spot_fleet._get_spot_fleet_status("sfr-12345")
        assert status == STATUS_CANCELLED
        
        # Test with failed fleet
        mock_ec2_client.describe_spot_fleet_requests.return_value = {
            'SpotFleetRequestConfigs': [
                {
                    'SpotFleetRequestId': 'sfr-12345',
                    'SpotFleetRequestState': 'failed'
                }
            ]
        }
        status = serverless_mode_with_spot_fleet._get_spot_fleet_status("sfr-12345")
        assert status == STATUS_FAILED

    def test_get_job_status_for_spot_fleet(self, serverless_mode_with_spot_fleet, mock_cf_client, mock_ec2_client):
        """Test getting job status for a SpotFleet job."""
        # Setup resources with SpotFleet
        resource_id = "serverless-ecs-job-1"
        serverless_mode_with_spot_fleet.resources = {
            resource_id: {
                "job_id": "job-1",
                "worker_type": WORKER_TYPE_ECS,
                "stack_name": "parsl-ecs-12345",
                "status": STATUS_PENDING,
                "created_at": time.time() - 60,
                "use_spot_fleet": True
            }
        }
        
        # Get status (should detect and set fleet_request_id)
        status = serverless_mode_with_spot_fleet.get_job_status([resource_id])
        
        # Verify CloudFormation and EC2 client calls
        mock_cf_client.describe_stacks.assert_called_with(StackName="parsl-ecs-12345")
        mock_ec2_client.describe_spot_fleet_requests.assert_called_with(
            SpotFleetRequestIds=["sfr-12345"]
        )
        
        # Verify status result and resource tracking updates
        assert status[resource_id] == STATUS_RUNNING
        assert serverless_mode_with_spot_fleet.resources[resource_id]["status"] == STATUS_RUNNING
        assert serverless_mode_with_spot_fleet.resources[resource_id]["fleet_request_id"] == "sfr-12345"
        assert "resource_type" in serverless_mode_with_spot_fleet.resources[resource_id]
        assert serverless_mode_with_spot_fleet.resources[resource_id]["resource_type"] == RESOURCE_TYPE_SPOT_FLEET

    def test_cancel_spot_fleet_job(self, serverless_mode_with_spot_fleet, mock_cf_client, mock_ec2_client):
        """Test canceling a SpotFleet job."""
        # Setup resources with SpotFleet
        resource_id = "serverless-ecs-job-1"
        serverless_mode_with_spot_fleet.resources = {
            resource_id: {
                "job_id": "job-1",
                "worker_type": WORKER_TYPE_ECS,
                "stack_name": "parsl-ecs-12345",
                "status": STATUS_RUNNING,
                "use_spot_fleet": True,
                "fleet_request_id": "sfr-12345",
                "resource_type": RESOURCE_TYPE_SPOT_FLEET
            }
        }
        
        # Cancel job
        status = serverless_mode_with_spot_fleet.cancel_jobs([resource_id])
        
        # Verify EC2 client call to cancel SpotFleet
        mock_ec2_client.cancel_spot_fleet_requests.assert_called_with(
            SpotFleetRequestIds=["sfr-12345"],
            TerminateInstances=True
        )
        
        # Verify CloudFormation stack deletion
        mock_cf_client.delete_stack.assert_called_with(
            StackName="parsl-ecs-12345"
        )
        
        # Verify status result
        assert status[resource_id] == STATUS_CANCELLED
        assert serverless_mode_with_spot_fleet.resources[resource_id]["status"] == STATUS_CANCELLED

    @patch('parsl_ephemeral_aws.compute.spot_fleet_cleanup.cleanup_all_spot_fleet_resources')
    def test_cleanup_infrastructure_with_spot_fleet(self, mock_cleanup_spot_fleet, 
                                                  serverless_mode_with_spot_fleet):
        """Test infrastructure cleanup with SpotFleet resources."""
        # Setup resources
        serverless_mode_with_spot_fleet.vpc_id = "vpc-12345"
        serverless_mode_with_spot_fleet.subnet_id = "subnet-12345"
        serverless_mode_with_spot_fleet.security_group_id = "sg-12345"
        serverless_mode_with_spot_fleet.initialized = True
        
        # Mock successful cleanup
        mock_cleanup_spot_fleet.return_value = {
            "cancelled_requests": ["sfr-12345"],
            "cleaned_roles": ["parsl-aws-spot-fleet-role-test"],
            "errors": []
        }
        
        # Cleanup infrastructure
        serverless_mode_with_spot_fleet.cleanup_infrastructure()
        
        # Verify SpotFleet cleanup was called
        mock_cleanup_spot_fleet.assert_called_once_with(
            session=serverless_mode_with_spot_fleet.session,
            workflow_id=serverless_mode_with_spot_fleet.provider_id,
            cancel_active_requests=True,
            cleanup_iam_roles=True
        )
        
        # Verify infrastructure was reset
        assert serverless_mode_with_spot_fleet.vpc_id is None
        assert serverless_mode_with_spot_fleet.subnet_id is None
        assert serverless_mode_with_spot_fleet.security_group_id is None
        assert serverless_mode_with_spot_fleet.initialized is False

    def test_list_resources_with_spot_fleet(self, serverless_mode_with_spot_fleet):
        """Test listing resources including SpotFleet resources."""
        # Setup resources including SpotFleet
        serverless_mode_with_spot_fleet.vpc_id = "vpc-12345"
        serverless_mode_with_spot_fleet.subnet_id = "subnet-12345"
        serverless_mode_with_spot_fleet.security_group_id = "sg-12345"
        
        lambda_id = "serverless-lambda-job-1"
        spot_fleet_id = "serverless-spot-fleet-job-2"
        serverless_mode_with_spot_fleet.resources = {
            lambda_id: {
                "job_id": "job-1",
                "worker_type": WORKER_TYPE_LAMBDA,
                "stack_name": "parsl-lambda-job1",
                "status": STATUS_RUNNING,
                "created_at": time.time()
            },
            spot_fleet_id: {
                "job_id": "job-2",
                "worker_type": WORKER_TYPE_ECS,
                "stack_name": "parsl-ecs-job2",
                "status": STATUS_RUNNING,
                "created_at": time.time(),
                "use_spot_fleet": True,
                "fleet_request_id": "sfr-12345",
                "resource_type": RESOURCE_TYPE_SPOT_FLEET
            }
        }
        
        # List resources
        resources = serverless_mode_with_spot_fleet.list_resources()
        
        # Verify resource categories
        assert "lambda_functions" in resources
        assert "ecs_tasks" in resources  # Standard ECS resources
        assert "spot_fleet_requests" in resources  # SpotFleet resources
        assert "vpc" in resources
        assert "subnet" in resources
        assert "security_group" in resources
        
        # Verify counts
        assert len(resources["lambda_functions"]) == 1
        assert len(resources["ecs_tasks"]) == 0  # No standard ECS tasks, only SpotFleet
        assert len(resources["spot_fleet_requests"]) == 1
        
        # Verify Lambda resource
        assert resources["lambda_functions"][0]["id"] == lambda_id
        assert resources["lambda_functions"][0]["job_id"] == "job-1"
        assert resources["lambda_functions"][0]["status"] == STATUS_RUNNING
        
        # Verify SpotFleet resource
        assert resources["spot_fleet_requests"][0]["id"] == spot_fleet_id
        assert resources["spot_fleet_requests"][0]["job_id"] == "job-2"
        assert resources["spot_fleet_requests"][0]["status"] == STATUS_RUNNING
        assert resources["spot_fleet_requests"][0]["fleet_request_id"] == "sfr-12345"

    def test_load_state_with_spot_fleet(self, serverless_mode_with_spot_fleet, mock_state_store):
        """Test loading state with SpotFleet information."""
        # Setup mock state with SpotFleet data
        mock_state = {
            "resources": {
                "spot-fleet-job-1": {
                    "job_id": "spot-job-1",
                    "status": STATUS_RUNNING,
                    "worker_type": WORKER_TYPE_ECS,
                    "resource_type": RESOURCE_TYPE_SPOT_FLEET,
                    "fleet_request_id": "sfr-12345",
                    "use_spot_fleet": True
                }
            },
            "provider_id": "test-provider",
            "mode": "ServerlessMode",
            "vpc_id": "vpc-12345",
            "subnet_id": "subnet-12345",
            "security_group_id": "sg-12345",
            "initialized": True,
            "worker_type": WORKER_TYPE_ECS,
            "use_spot_fleet": True,
            "instance_types": ["t3.small", "t3.medium", "m5.small"],
            "nodes_per_block": 2,
            "spot_max_price_percentage": 80
        }
        mock_state_store.load_state.return_value = mock_state
        
        # Load state
        result = serverless_mode_with_spot_fleet.load_state()
        
        # Verify state was loaded
        assert result is True
        assert serverless_mode_with_spot_fleet.resources == mock_state["resources"]
        assert serverless_mode_with_spot_fleet.use_spot_fleet == mock_state["use_spot_fleet"]
        assert serverless_mode_with_spot_fleet.instance_types == mock_state["instance_types"]
        assert serverless_mode_with_spot_fleet.nodes_per_block == mock_state["nodes_per_block"]
        assert serverless_mode_with_spot_fleet.spot_max_price_percentage == mock_state["spot_max_price_percentage"]
        
        # Verify SpotFleet resource data is loaded correctly
        spot_resource = serverless_mode_with_spot_fleet.resources.get("spot-fleet-job-1")
        assert spot_resource is not None
        assert spot_resource["resource_type"] == RESOURCE_TYPE_SPOT_FLEET
        assert spot_resource["fleet_request_id"] == "sfr-12345"
        assert spot_resource["use_spot_fleet"] is True
"""Unit tests for the SpotFleet functionality in DetachedMode.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import pytest
from unittest.mock import MagicMock, patch, call
import boto3
import time
import json

from parsl_ephemeral_aws.modes.detached import DetachedMode
from parsl_ephemeral_aws.exceptions import (
    OperatingModeError,
    NetworkCreationError,
    ResourceCreationError,
    ResourceNotFoundError,
    SpotFleetError,
)
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
    STATUS_SUCCEEDED,
    STATUS_FAILED,
    STATUS_CANCELED,
    STATUS_COMPLETED,
)


class TestDetachedModeSpotFleet:
    """Tests for the SpotFleet functionality in DetachedMode class."""

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
    def mock_ec2_client(self):
        """Create a mock EC2 client."""
        client = MagicMock()
        
        # Mock create_vpc
        client.create_vpc.return_value = {
            'Vpc': {'VpcId': 'vpc-12345'}
        }
        
        # Mock create_subnet
        client.create_subnet.return_value = {
            'Subnet': {'SubnetId': 'subnet-12345'}
        }
        
        # Mock create_security_group
        client.create_security_group.return_value = {
            'GroupId': 'sg-12345'
        }
        
        # Mock run_instances (for bastion host)
        client.run_instances.return_value = {
            'Instances': [
                {
                    'InstanceId': 'i-bastion',
                    'State': {'Name': 'pending'},
                    'PrivateIpAddress': '10.0.0.1',
                    'PublicIpAddress': '54.123.456.789'
                }
            ]
        }
        
        # Mock describe_instances
        client.describe_instances.return_value = {
            'Reservations': [
                {
                    'Instances': [
                        {
                            'InstanceId': 'i-bastion',
                            'State': {'Name': 'running'},
                            'PrivateIpAddress': '10.0.0.1',
                            'PublicIpAddress': '54.123.456.789'
                        }
                    ]
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
        
        # Mock describe_spot_fleet_requests
        client.describe_spot_fleet_requests.return_value = {
            'SpotFleetRequestConfigs': [
                {
                    'SpotFleetRequestId': 'sfr-12345',
                    'SpotFleetRequestState': 'active',
                    'ActivityStatus': 'fulfilled'
                }
            ]
        }
        
        # Mock describe_spot_fleet_instances
        client.describe_spot_fleet_instances.return_value = {
            'ActiveInstances': [
                {
                    'InstanceId': 'i-spot1',
                    'InstanceType': 't3.micro',
                    'SpotInstanceRequestId': 'sir-12345'
                },
                {
                    'InstanceId': 'i-spot2',
                    'InstanceType': 't3.micro',
                    'SpotInstanceRequestId': 'sir-67890'
                }
            ]
        }
        
        return client

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
                    'StackName': 'parsl-bastion-12345',
                    'StackStatus': 'CREATE_COMPLETE',
                    'Outputs': [
                        {
                            'OutputKey': 'BastionHostId',
                            'OutputValue': 'i-bastion'
                        }
                    ]
                }
            ]
        }
        
        return client

    @pytest.fixture
    def mock_ssm_client(self):
        """Create a mock SSM client."""
        client = MagicMock()
        
        # Mock get_parameter for regular job
        client.get_parameter.return_value = {
            'Parameter': {
                'Name': '/parsl/workflows/test-workflow/status/job-1',
                'Value': json.dumps({
                    'status': STATUS_RUNNING,
                    'instance_id': 'i-worker'
                })
            }
        }
        
        return client

    @pytest.fixture
    def mock_iam_client(self):
        """Create a mock IAM client."""
        client = MagicMock()
        
        # Mock get_role success
        client.get_role.return_value = {
            'Role': {
                'RoleName': 'parsl-aws-spot-fleet-role-test-work',
                'Arn': 'arn:aws:iam::123456789012:role/parsl-aws-spot-fleet-role-test-work'
            }
        }
        
        return client
        
    @pytest.fixture
    def detached_mode_with_spot_fleet(self, mock_session, mock_state_store, mock_ec2_client, 
                                     mock_cf_client, mock_ssm_client, mock_iam_client):
        """Create a DetachedMode instance with SpotFleet enabled."""
        # Configure session to return mock clients
        def get_client(service_name, **kwargs):
            if service_name == 'ec2':
                return mock_ec2_client
            elif service_name == 'cloudformation':
                return mock_cf_client
            elif service_name == 'ssm':
                return mock_ssm_client
            elif service_name == 'iam':
                return mock_iam_client
            return MagicMock()
            
        mock_session.client.side_effect = get_client
        
        # Create mode instance with SpotFleet enabled
        mode = DetachedMode(
            provider_id="test-provider",
            session=mock_session,
            state_store=mock_state_store,
            workflow_id="test-workflow",
            bastion_instance_type="t3.micro",
            instance_type="t3.small",
            image_id="ami-12345678",
            region="us-east-1",
            use_spot_fleet=True,
            instance_types=["t3.small", "t3.medium", "m5.small"],
            nodes_per_block=2,
            spot_max_price_percentage=80
        )
        
        return mode

    def test_init_with_spot_fleet(self, detached_mode_with_spot_fleet):
        """Test initialization of DetachedMode with SpotFleet options."""
        # Verify SpotFleet specific attributes
        assert detached_mode_with_spot_fleet.use_spot_fleet is True
        assert len(detached_mode_with_spot_fleet.instance_types) == 3
        assert "t3.small" in detached_mode_with_spot_fleet.instance_types
        assert "t3.medium" in detached_mode_with_spot_fleet.instance_types
        assert "m5.small" in detached_mode_with_spot_fleet.instance_types
        assert detached_mode_with_spot_fleet.nodes_per_block == 2
        assert detached_mode_with_spot_fleet.spot_max_price_percentage == 80

    @patch('parsl_ephemeral_aws.modes.detached.get_default_ami')
    @patch('parsl_ephemeral_aws.modes.detached.get_cf_template')
    def test_initialize_with_spot_fleet(self, mock_get_cf_template, mock_get_default_ami, 
                                       detached_mode_with_spot_fleet, mock_ec2_client, mock_cf_client):
        """Test initialize method with SpotFleet options."""
        # Setup mocks
        mock_get_default_ami.return_value = "ami-default"
        mock_get_cf_template.return_value = "CloudFormation Template"
        detached_mode_with_spot_fleet.bastion_host_type = "cloudformation"
        
        # Call initialize
        detached_mode_with_spot_fleet.initialize()
        
        # Verify infrastructure was created
        assert detached_mode_with_spot_fleet.vpc_id == 'vpc-12345'
        assert detached_mode_with_spot_fleet.subnet_id == 'subnet-12345'
        assert detached_mode_with_spot_fleet.security_group_id == 'sg-12345'
        assert detached_mode_with_spot_fleet.bastion_id == 'stack-12345'
        assert detached_mode_with_spot_fleet.initialized is True
        
        # Verify CloudFormation parameters include SpotFleet options
        for call_args in mock_cf_client.create_stack.call_args_list:
            cf_params = call_args[1].get('Parameters', [])
            
            # Convert parameters to a dict for easier checking
            param_dict = {p['ParameterKey']: p['ParameterValue'] for p in cf_params}
            
            # Verify SpotFleet parameters are present
            assert param_dict.get('UseSpotFleet') == 'true'
            assert param_dict.get('NodesPerBlock') == '2'
            assert param_dict.get('SpotMaxPricePercentage') == '80'
            assert 't3.small' in param_dict.get('InstanceTypes', '')
            assert 't3.medium' in param_dict.get('InstanceTypes', '')
            assert 'm5.small' in param_dict.get('InstanceTypes', '')

    def test_submit_job_with_spot_fleet(self, detached_mode_with_spot_fleet, mock_ssm_client):
        """Test job submission with SpotFleet options."""
        # Setup mode as initialized
        detached_mode_with_spot_fleet.initialized = True
        detached_mode_with_spot_fleet.vpc_id = "vpc-12345"
        detached_mode_with_spot_fleet.subnet_id = "subnet-12345"
        detached_mode_with_spot_fleet.security_group_id = "sg-12345"
        detached_mode_with_spot_fleet.bastion_id = "i-bastion"
        
        # Submit a job
        command = "echo hello"
        resource_id = detached_mode_with_spot_fleet.submit_job("job-1", command, 1)
        
        # Verify SSM parameters were created
        assert mock_ssm_client.put_parameter.call_count == 2  # Job command and status
        
        # Verify job data includes SpotFleet options
        for call_args in mock_ssm_client.put_parameter.call_args_list:
            # Check if this is the job data parameter
            if "/parsl/workflows/test-workflow/jobs/job-1" in call_args[1]['Name']:
                job_data = json.loads(call_args[1]['Value'])
                
                # Verify SpotFleet options are included
                assert job_data['use_spot_fleet'] is True
                assert len(job_data['instance_types']) == 3
                assert 't3.small' in job_data['instance_types']
                assert 't3.medium' in job_data['instance_types']
                assert 'm5.small' in job_data['instance_types']
                assert job_data['nodes_per_block'] == 2
                assert job_data['spot_max_price_percentage'] == 80

    def test_get_job_status_for_spot_fleet(self, detached_mode_with_spot_fleet, mock_ssm_client):
        """Test getting job status for a SpotFleet job."""
        # Setup mock resources
        job_id = "spot-job-1"
        resource_id = f"spot-fleet-{job_id}"
        detached_mode_with_spot_fleet.resources = {
            resource_id: {
                "job_id": job_id,
                "status": STATUS_PENDING
            }
        }
        
        # Mock SSM get_parameter response for a SpotFleet job
        mock_ssm_client.get_parameter.return_value = {
            'Parameter': {
                'Value': json.dumps({
                    'status': STATUS_RUNNING,
                    'instance_id': 'i-spot1',
                    'fleet_request_id': 'sfr-12345',
                    'resource_type': RESOURCE_TYPE_SPOT_FLEET,
                    'all_instance_ids': ['i-spot1', 'i-spot2']
                })
            }
        }
        
        # Get status
        status = detached_mode_with_spot_fleet.get_job_status([resource_id])
        
        # Verify SSM call
        mock_ssm_client.get_parameter.assert_called_with(
            Name=f"/parsl/workflows/test-workflow/status/{job_id}"
        )
        
        # Verify status result
        assert status[resource_id] == STATUS_RUNNING
        
        # Verify resource was updated with SpotFleet information
        assert detached_mode_with_spot_fleet.resources[resource_id]["status"] == STATUS_RUNNING
        assert detached_mode_with_spot_fleet.resources[resource_id]["fleet_request_id"] == "sfr-12345"
        assert detached_mode_with_spot_fleet.resources[resource_id]["resource_type"] == RESOURCE_TYPE_SPOT_FLEET
        assert detached_mode_with_spot_fleet.resources[resource_id]["all_instance_ids"] == ["i-spot1", "i-spot2"]

    def test_cancel_spot_fleet_job(self, detached_mode_with_spot_fleet, mock_ssm_client):
        """Test canceling a SpotFleet job."""
        # Setup mock resource with SpotFleet details
        resource_id = "spot-fleet-job-1"
        detached_mode_with_spot_fleet.resources = {
            resource_id: {
                "job_id": "spot-job-1",
                "status": STATUS_RUNNING,
                "resource_type": RESOURCE_TYPE_SPOT_FLEET,
                "fleet_request_id": "sfr-12345",
                "all_instance_ids": ["i-spot1", "i-spot2"]
            }
        }
        
        # Cancel the job
        status = detached_mode_with_spot_fleet.cancel_jobs([resource_id])
        
        # Verify SSM put_parameter was called for the cancel request
        mock_ssm_client.put_parameter.assert_called_once()
        args, kwargs = mock_ssm_client.put_parameter.call_args
        assert kwargs["Name"] == f"/parsl/workflows/test-workflow/cancel"
        
        # Verify the cancel request data contains the SpotFleet information
        cancel_data = json.loads(kwargs["Value"])
        assert "spot-job-1" in cancel_data["job_ids"]
        assert "spot_fleet_jobs" in cancel_data
        assert cancel_data["spot_fleet_jobs"]["spot-job-1"] == "sfr-12345"
        
        # Verify status results
        assert status[resource_id] == STATUS_CANCELED
        
        # Verify resource was updated
        assert detached_mode_with_spot_fleet.resources[resource_id]["status"] == STATUS_CANCELED

    def test_cleanup_spot_fleet_resources(self, detached_mode_with_spot_fleet, mock_ec2_client, mock_ssm_client):
        """Test cleaning up SpotFleet resources."""
        # Setup mock resource with SpotFleet details
        resource_id = "spot-fleet-job-1"
        detached_mode_with_spot_fleet.resources = {
            resource_id: {
                "job_id": "spot-job-1",
                "status": STATUS_RUNNING,
                "resource_type": RESOURCE_TYPE_SPOT_FLEET,
                "fleet_request_id": "sfr-12345",
                "all_instance_ids": ["i-spot1", "i-spot2"]
            }
        }
        
        # Cleanup the resource
        detached_mode_with_spot_fleet.cleanup_resources([resource_id])
        
        # Verify EC2 cancel_spot_fleet_requests was called
        mock_ec2_client.cancel_spot_fleet_requests.assert_called_once_with(
            SpotFleetRequestIds=["sfr-12345"],
            TerminateInstances=True
        )
        
        # Verify SSM parameters were deleted
        mock_ssm_client.delete_parameter.assert_any_call(
            Name=f"/parsl/workflows/test-workflow/jobs/spot-job-1"
        )
        mock_ssm_client.delete_parameter.assert_any_call(
            Name=f"/parsl/workflows/test-workflow/status/spot-job-1"
        )
        
        # Verify resource was removed from tracking
        assert resource_id not in detached_mode_with_spot_fleet.resources
        
        # Verify state was saved
        detached_mode_with_spot_fleet.state_store.save_state.assert_called()

    def test_bastion_script_includes_spot_fleet_support(self, detached_mode_with_spot_fleet):
        """Test that the bastion manager script includes SpotFleet support."""
        # Generate the bastion manager script
        manager_script = detached_mode_with_spot_fleet._get_bastion_manager_script()
        
        # Verify script content includes SpotFleet functionality
        assert "def get_spot_fleet_role()" in manager_script
        assert "def wait_for_fleet_instances(" in manager_script
        assert "def launch_spot_fleet(" in manager_script
        assert "RESOURCE_TYPE_SPOT_FLEET" in manager_script
        assert "USE_SPOT_FLEET" in manager_script

    def test_load_state_with_spot_fleet(self, detached_mode_with_spot_fleet, mock_state_store):
        """Test loading state with SpotFleet information."""
        # Setup mock state with SpotFleet data
        mock_state = {
            "resources": {
                "spot-fleet-job-1": {
                    "job_id": "spot-job-1",
                    "status": STATUS_RUNNING,
                    "resource_type": RESOURCE_TYPE_SPOT_FLEET,
                    "fleet_request_id": "sfr-12345",
                    "all_instance_ids": ["i-spot1", "i-spot2"]
                }
            },
            "provider_id": "test-provider",
            "mode": "DetachedMode",
            "vpc_id": "vpc-12345",
            "subnet_id": "subnet-12345",
            "security_group_id": "sg-12345",
            "bastion_id": "i-bastion",
            "initialized": True,
            "workflow_id": "test-workflow",
            "bastion_host_type": "direct",
            "use_spot_fleet": True,
            "instance_types": ["t3.small", "t3.medium", "m5.small"],
            "nodes_per_block": 2,
            "spot_max_price_percentage": 80
        }
        mock_state_store.load_state.return_value = mock_state
        
        # Load state
        result = detached_mode_with_spot_fleet.load_state()
        
        # Verify state was loaded
        assert result is True
        assert detached_mode_with_spot_fleet.resources == mock_state["resources"]
        assert detached_mode_with_spot_fleet.use_spot_fleet == mock_state["use_spot_fleet"]
        assert detached_mode_with_spot_fleet.instance_types == mock_state["instance_types"]
        assert detached_mode_with_spot_fleet.nodes_per_block == mock_state["nodes_per_block"]
        assert detached_mode_with_spot_fleet.spot_max_price_percentage == mock_state["spot_max_price_percentage"]
        
        # Verify SpotFleet resource data is loaded correctly
        spot_resource = detached_mode_with_spot_fleet.resources.get("spot-fleet-job-1")
        assert spot_resource is not None
        assert spot_resource["resource_type"] == RESOURCE_TYPE_SPOT_FLEET
        assert spot_resource["fleet_request_id"] == "sfr-12345"
        assert spot_resource["all_instance_ids"] == ["i-spot1", "i-spot2"]
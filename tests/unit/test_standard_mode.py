"""Unit tests for the StandardMode class.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import pytest
from unittest.mock import MagicMock, patch, call
import boto3
import time

from parsl_ephemeral_aws.modes.standard import StandardMode
from parsl_ephemeral_aws.exceptions import (
    OperatingModeError,
    NetworkCreationError,
    ResourceCreationError,
    ResourceNotFoundError,
)
from parsl_ephemeral_aws.constants import (
    RESOURCE_TYPE_EC2,
    RESOURCE_TYPE_VPC,
    RESOURCE_TYPE_SUBNET,
    RESOURCE_TYPE_SECURITY_GROUP,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_COMPLETED,
    STATUS_FAILED,
)


class TestStandardMode:
    """Tests for the StandardMode class."""

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
        
        # Mock run_instances
        client.run_instances.return_value = {
            'Instances': [
                {
                    'InstanceId': 'i-12345678',
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
                            'InstanceId': 'i-12345678',
                            'State': {'Name': 'running'},
                            'PrivateIpAddress': '10.0.0.1',
                            'PublicIpAddress': '54.123.456.789'
                        }
                    ]
                }
            ]
        }
        
        return client

    @pytest.fixture
    def standard_mode(self, mock_session, mock_state_store, mock_ec2_client):
        """Create a StandardMode instance with mocked dependencies."""
        # Configure session to return mock EC2 client
        mock_session.client.return_value = mock_ec2_client
        
        # Create mode instance
        mode = StandardMode(
            provider_id="test-provider",
            session=mock_session,
            state_store=mock_state_store,
            instance_type="t3.micro",
            image_id="ami-12345678",
            region="us-east-1"
        )
        
        return mode

    def test_init(self, standard_mode):
        """Test initialization of StandardMode."""
        assert standard_mode.provider_id == "test-provider"
        assert standard_mode.instance_type == "t3.micro"
        assert standard_mode.image_id == "ami-12345678"
        assert standard_mode.region == "us-east-1"
        assert standard_mode.initialized is False
        assert standard_mode.resources == {}

    def test_init_with_predefined_resources(self, mock_session, mock_state_store):
        """Test initialization with predefined VPC, subnet, and security group."""
        mode = StandardMode(
            provider_id="test-provider",
            session=mock_session,
            state_store=mock_state_store,
            instance_type="t3.micro",
            image_id="ami-12345678",
            region="us-east-1",
            vpc_id="vpc-12345",
            subnet_id="subnet-12345",
            security_group_id="sg-12345"
        )
        
        assert mode.vpc_id == "vpc-12345"
        assert mode.subnet_id == "subnet-12345"
        assert mode.security_group_id == "sg-12345"
        assert mode.create_vpc is False

    @patch('parsl_ephemeral_aws.modes.standard.get_default_ami')
    def test_initialize(self, mock_get_default_ami, standard_mode, mock_ec2_client):
        """Test initialize method creates required infrastructure."""
        # Setup mocks
        mock_get_default_ami.return_value = "ami-default"
        
        # Mock VPC creation
        mock_ec2_client.create_vpc.return_value = {
            'Vpc': {'VpcId': 'vpc-12345'}
        }
        
        # Mock subnet creation
        mock_ec2_client.create_subnet.return_value = {
            'Subnet': {'SubnetId': 'subnet-12345'}
        }
        
        # Mock security group creation
        mock_ec2_client.create_security_group.return_value = {
            'GroupId': 'sg-12345'
        }
        
        # Call initialize
        standard_mode.initialize()
        
        # Verify infrastructure was created
        assert standard_mode.vpc_id == 'vpc-12345'
        assert standard_mode.subnet_id == 'subnet-12345'
        assert standard_mode.security_group_id == 'sg-12345'
        assert standard_mode.initialized is True
        
        # Verify EC2 client calls
        mock_ec2_client.create_vpc.assert_called_once()
        mock_ec2_client.create_subnet.assert_called_once()
        mock_ec2_client.create_security_group.assert_called_once()
        
        # Verify state was saved
        standard_mode.state_store.save_state.assert_called()

    @patch('parsl_ephemeral_aws.modes.standard.delete_resource')
    def test_initialize_failure_cleanup(self, mock_delete_resource, standard_mode, mock_ec2_client):
        """Test that resources are cleaned up if initialization fails."""
        # Make subnet creation fail
        mock_ec2_client.create_vpc.return_value = {
            'Vpc': {'VpcId': 'vpc-12345'}
        }
        mock_ec2_client.create_subnet.side_effect = Exception("Subnet creation failed")
        
        # Call initialize and expect exception
        with pytest.raises(ResourceCreationError):
            standard_mode.initialize()
        
        # Verify cleanup was called
        mock_delete_resource.assert_called()

    def test_submit_job(self, standard_mode, mock_ec2_client):
        """Test job submission."""
        # Setup mode as initialized
        standard_mode.initialized = True
        standard_mode.vpc_id = "vpc-12345"
        standard_mode.subnet_id = "subnet-12345"
        standard_mode.security_group_id = "sg-12345"
        
        # Submit a job
        command = "echo hello"
        resource_id = standard_mode.submit_job("job-1", command, 1)
        
        # Verify EC2 instance was launched
        mock_ec2_client.run_instances.assert_called_once()
        
        # Verify resource tracking
        assert resource_id in standard_mode.resources
        assert standard_mode.resources[resource_id]["type"] == RESOURCE_TYPE_EC2
        assert standard_mode.resources[resource_id]["job_id"] == "job-1"
        assert standard_mode.resources[resource_id]["status"] == STATUS_PENDING
        
        # Verify state was saved
        standard_mode.state_store.save_state.assert_called()

    def test_submit_job_not_initialized(self, standard_mode):
        """Test submission when not initialized raises error."""
        standard_mode.initialized = False
        
        with pytest.raises(OperatingModeError):
            standard_mode.submit_job("job-1", "echo hello", 1)

    def test_get_job_status(self, standard_mode, mock_ec2_client):
        """Test getting job status."""
        # Setup mock resources
        job_id = "job-1"
        resource_id = f"resource-{job_id}"
        standard_mode.resources = {
            resource_id: {
                "type": RESOURCE_TYPE_EC2,
                "job_id": job_id,
                "instance_id": "i-12345678",
                "status": STATUS_PENDING
            }
        }
        
        # Get status
        status = standard_mode.get_job_status([resource_id])
        
        # Verify status call and result
        mock_ec2_client.describe_instances.assert_called_once()
        assert status[resource_id] == STATUS_RUNNING  # Status from mocked response
        
        # Verify resource was updated
        assert standard_mode.resources[resource_id]["status"] == STATUS_RUNNING

    def test_cancel_jobs(self, standard_mode, mock_ec2_client):
        """Test canceling jobs."""
        # Setup mock resources
        resource_id1 = "resource-1"
        resource_id2 = "resource-2"
        standard_mode.resources = {
            resource_id1: {
                "type": RESOURCE_TYPE_EC2,
                "job_id": "job-1",
                "instance_id": "i-12345678",
                "status": STATUS_RUNNING
            },
            resource_id2: {
                "type": RESOURCE_TYPE_EC2,
                "job_id": "job-2",
                "instance_id": "i-87654321",
                "status": STATUS_RUNNING
            }
        }
        
        # Cancel jobs
        status = standard_mode.cancel_jobs([resource_id1, resource_id2])
        
        # Verify EC2 calls
        mock_ec2_client.terminate_instances.assert_called_once_with(
            InstanceIds=["i-12345678", "i-87654321"]
        )
        
        # Verify status results
        assert status[resource_id1] == STATUS_COMPLETED  # Assume completed on termination
        assert status[resource_id2] == STATUS_COMPLETED

    @patch('parsl_ephemeral_aws.modes.standard.delete_resource')
    def test_cleanup_infrastructure(self, mock_delete_resource, standard_mode):
        """Test infrastructure cleanup."""
        # Setup infrastructure resources
        standard_mode.vpc_id = "vpc-12345"
        standard_mode.subnet_id = "subnet-12345"
        standard_mode.security_group_id = "sg-12345"
        standard_mode.initialized = True
        
        # Add a resource to be cleaned up
        resource_id = "resource-1"
        standard_mode.resources = {
            resource_id: {
                "type": RESOURCE_TYPE_EC2,
                "job_id": "job-1",
                "instance_id": "i-12345678",
                "status": STATUS_RUNNING
            }
        }
        
        # Call cleanup
        standard_mode.cleanup_infrastructure()
        
        # Verify resource deletion
        expected_calls = [
            call("sg-12345", standard_mode.session, RESOURCE_TYPE_SECURITY_GROUP),
            call("subnet-12345", standard_mode.session, RESOURCE_TYPE_SUBNET),
            call("vpc-12345", standard_mode.session, RESOURCE_TYPE_VPC, force=True)
        ]
        mock_delete_resource.assert_has_calls(expected_calls, any_order=True)
        
        # Verify state was reset
        assert standard_mode.vpc_id is None
        assert standard_mode.subnet_id is None
        assert standard_mode.security_group_id is None
        assert standard_mode.initialized is False
        assert not standard_mode.resources  # Resources should be empty

    def test_cleanup_resources(self, standard_mode, mock_ec2_client):
        """Test resource cleanup."""
        # Setup resources
        resource_id1 = "resource-1"
        resource_id2 = "resource-2"
        standard_mode.resources = {
            resource_id1: {
                "type": RESOURCE_TYPE_EC2,
                "job_id": "job-1",
                "instance_id": "i-12345678",
                "status": STATUS_RUNNING
            },
            resource_id2: {
                "type": RESOURCE_TYPE_EC2,
                "job_id": "job-2",
                "instance_id": "i-87654321",
                "status": STATUS_RUNNING
            }
        }
        
        # Clean up one resource
        standard_mode.cleanup_resources([resource_id1])
        
        # Verify EC2 termination call
        mock_ec2_client.terminate_instances.assert_called_once_with(
            InstanceIds=["i-12345678"]
        )
        
        # Verify resource was removed from tracking
        assert resource_id1 not in standard_mode.resources
        assert resource_id2 in standard_mode.resources
        
        # Verify state was saved
        standard_mode.state_store.save_state.assert_called()

    def test_list_resources(self, standard_mode):
        """Test listing resources."""
        # Setup resources
        standard_mode.vpc_id = "vpc-12345"
        standard_mode.subnet_id = "subnet-12345"
        standard_mode.security_group_id = "sg-12345"
        
        resource_id1 = "resource-1"
        resource_id2 = "resource-2"
        standard_mode.resources = {
            resource_id1: {
                "type": RESOURCE_TYPE_EC2,
                "job_id": "job-1",
                "instance_id": "i-12345678",
                "status": STATUS_RUNNING
            },
            resource_id2: {
                "type": RESOURCE_TYPE_EC2,
                "job_id": "job-2",
                "instance_id": "i-87654321",
                "status": STATUS_FAILED
            }
        }
        
        # List resources
        resources = standard_mode.list_resources()
        
        # Verify resource lists
        assert len(resources["ec2_instances"]) == 2
        assert len(resources["vpc"]) == 1
        assert len(resources["subnet"]) == 1
        assert len(resources["security_group"]) == 1
        
        # Verify resource details
        assert resources["vpc"][0]["id"] == "vpc-12345"
        assert resources["subnet"][0]["id"] == "subnet-12345"
        assert resources["security_group"][0]["id"] == "sg-12345"
        
        # Verify EC2 instances
        ec2_resource_ids = [r["id"] for r in resources["ec2_instances"]]
        assert resource_id1 in ec2_resource_ids
        assert resource_id2 in ec2_resource_ids
        
        # Verify status is included
        for instance in resources["ec2_instances"]:
            if instance["id"] == resource_id1:
                assert instance["status"] == STATUS_RUNNING
            elif instance["id"] == resource_id2:
                assert instance["status"] == STATUS_FAILED

    def test_load_state(self, standard_mode, mock_state_store):
        """Test loading state."""
        # Setup mock state
        mock_state = {
            "resources": {
                "resource-1": {
                    "type": RESOURCE_TYPE_EC2,
                    "job_id": "job-1",
                    "instance_id": "i-12345678",
                    "status": STATUS_RUNNING
                }
            },
            "provider_id": "test-provider",
            "mode": "StandardMode",
            "vpc_id": "vpc-12345",
            "subnet_id": "subnet-12345",
            "security_group_id": "sg-12345",
            "initialized": True
        }
        mock_state_store.load_state.return_value = mock_state
        
        # Load state
        result = standard_mode.load_state()
        
        # Verify state was loaded
        assert result is True
        assert standard_mode.resources == mock_state["resources"]
        assert standard_mode.vpc_id == mock_state["vpc_id"]
        assert standard_mode.subnet_id == mock_state["subnet_id"]
        assert standard_mode.security_group_id == mock_state["security_group_id"]
        assert standard_mode.initialized == mock_state["initialized"]

    def test_save_state(self, standard_mode, mock_state_store):
        """Test saving state."""
        # Setup state
        standard_mode.vpc_id = "vpc-12345"
        standard_mode.subnet_id = "subnet-12345"
        standard_mode.security_group_id = "sg-12345"
        standard_mode.initialized = True
        standard_mode.resources = {
            "resource-1": {
                "type": RESOURCE_TYPE_EC2,
                "job_id": "job-1",
                "instance_id": "i-12345678",
                "status": STATUS_RUNNING
            }
        }
        
        # Save state
        standard_mode.save_state()
        
        # Verify state_store.save_state was called
        mock_state_store.save_state.assert_called_once()
        
        # Verify state content
        state = mock_state_store.save_state.call_args[0][0]
        assert state["provider_id"] == "test-provider"
        assert state["mode"] == "StandardMode"
        assert state["vpc_id"] == "vpc-12345"
        assert state["subnet_id"] == "subnet-12345"
        assert state["security_group_id"] == "sg-12345"
        assert state["initialized"] is True
        assert state["resources"] == standard_mode.resources
"""Unit tests for the DetachedMode class.

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
    ResourceCreationError,
)
from parsl_ephemeral_aws.constants import (
    RESOURCE_TYPE_VPC,
    RESOURCE_TYPE_SUBNET,
    RESOURCE_TYPE_SECURITY_GROUP,
    RESOURCE_TYPE_BASTION,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
)


class TestDetachedMode:
    """Tests for the DetachedMode class."""

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
        client.create_vpc.return_value = {"Vpc": {"VpcId": "vpc-12345"}}

        # Mock create_subnet
        client.create_subnet.return_value = {"Subnet": {"SubnetId": "subnet-12345"}}

        # Mock create_security_group
        client.create_security_group.return_value = {"GroupId": "sg-12345"}

        # Mock run_instances (for bastion host)
        client.run_instances.return_value = {
            "Instances": [
                {
                    "InstanceId": "i-bastion",
                    "State": {"Name": "pending"},
                    "PrivateIpAddress": "10.0.0.1",
                    "PublicIpAddress": "54.123.456.789",
                }
            ]
        }

        # Mock describe_instances
        client.describe_instances.return_value = {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-bastion",
                            "State": {"Name": "running"},
                            "PrivateIpAddress": "10.0.0.1",
                            "PublicIpAddress": "54.123.456.789",
                        }
                    ]
                }
            ]
        }

        return client

    @pytest.fixture
    def mock_cf_client(self):
        """Create a mock CloudFormation client."""
        client = MagicMock()

        # Mock create_stack
        client.create_stack.return_value = {"StackId": "stack-12345"}

        # Mock describe_stacks
        client.describe_stacks.return_value = {
            "Stacks": [
                {
                    "StackId": "stack-12345",
                    "StackName": "parsl-bastion-12345",
                    "StackStatus": "CREATE_COMPLETE",
                    "Outputs": [
                        {"OutputKey": "BastionHostId", "OutputValue": "i-bastion"}
                    ],
                }
            ]
        }

        return client

    @pytest.fixture
    def mock_ssm_client(self):
        """Create a mock SSM client."""
        client = MagicMock()

        # Mock get_parameter
        client.get_parameter.return_value = {
            "Parameter": {
                "Name": "/parsl/workflows/test-workflow/status/job-1",
                "Value": json.dumps(
                    {"status": STATUS_RUNNING, "instance_id": "i-worker"}
                ),
            }
        }

        return client

    @pytest.fixture
    def detached_mode(
        self,
        mock_session,
        mock_state_store,
        mock_ec2_client,
        mock_cf_client,
        mock_ssm_client,
    ):
        """Create a DetachedMode instance with mocked dependencies."""

        # Configure session to return mock clients
        def get_client(service_name, **kwargs):
            if service_name == "ec2":
                return mock_ec2_client
            elif service_name == "cloudformation":
                return mock_cf_client
            elif service_name == "ssm":
                return mock_ssm_client
            return MagicMock()

        mock_session.client.side_effect = get_client

        # Create mode instance
        mode = DetachedMode(
            provider_id="test-provider",
            session=mock_session,
            state_store=mock_state_store,
            workflow_id="test-workflow",
            bastion_instance_type="t3.micro",
            instance_type="t3.small",
            image_id="ami-12345678",
            region="us-east-1",
        )

        return mode

    def test_init(self, detached_mode):
        """Test initialization of DetachedMode."""
        assert detached_mode.provider_id == "test-provider"
        assert detached_mode.workflow_id == "test-workflow"
        assert detached_mode.bastion_instance_type == "t3.micro"
        assert detached_mode.instance_type == "t3.small"
        assert detached_mode.image_id == "ami-12345678"
        assert detached_mode.region == "us-east-1"
        assert detached_mode.initialized is False
        assert detached_mode.resources == {}
        assert detached_mode.bastion_id is None

    def test_init_with_predefined_resources(self, mock_session, mock_state_store):
        """Test initialization with predefined VPC, subnet, security group, and bastion."""
        mode = DetachedMode(
            provider_id="test-provider",
            session=mock_session,
            state_store=mock_state_store,
            workflow_id="test-workflow",
            bastion_instance_type="t3.micro",
            instance_type="t3.small",
            image_id="ami-12345678",
            region="us-east-1",
            vpc_id="vpc-12345",
            subnet_id="subnet-12345",
            security_group_id="sg-12345",
            bastion_id="i-bastion",
        )

        assert mode.vpc_id == "vpc-12345"
        assert mode.subnet_id == "subnet-12345"
        assert mode.security_group_id == "sg-12345"
        assert mode.bastion_id == "i-bastion"
        assert mode.create_vpc is False

    @patch("parsl_ephemeral_aws.modes.detached.get_default_ami")
    @patch("parsl_ephemeral_aws.modes.detached.get_cf_template")
    def test_initialize_cloudformation(
        self,
        mock_get_cf_template,
        mock_get_default_ami,
        detached_mode,
        mock_ec2_client,
        mock_cf_client,
    ):
        """Test initialize method creates infrastructure including bastion via CloudFormation."""
        # Setup mocks
        mock_get_default_ami.return_value = "ami-default"
        mock_get_cf_template.return_value = "CloudFormation Template"
        detached_mode.bastion_host_type = "cloudformation"

        # Call initialize
        detached_mode.initialize()

        # Verify infrastructure was created
        assert detached_mode.vpc_id == "vpc-12345"
        assert detached_mode.subnet_id == "subnet-12345"
        assert detached_mode.security_group_id == "sg-12345"
        assert detached_mode.bastion_id == "stack-12345"
        assert detached_mode.initialized is True

        # Verify EC2 and CF client calls
        mock_ec2_client.create_vpc.assert_called_once()
        mock_ec2_client.create_subnet.assert_called_once()
        mock_ec2_client.create_security_group.assert_called_once()
        mock_cf_client.create_stack.assert_called_once()

        # Verify state was saved
        detached_mode.state_store.save_state.assert_called()

    @patch("parsl_ephemeral_aws.modes.detached.get_default_ami")
    def test_initialize_direct(
        self, mock_get_default_ami, detached_mode, mock_ec2_client
    ):
        """Test initialize method creates infrastructure including direct bastion host."""
        # Setup mocks
        mock_get_default_ami.return_value = "ami-default"
        detached_mode.bastion_host_type = "direct"

        # Call initialize
        detached_mode.initialize()

        # Verify infrastructure was created
        assert detached_mode.vpc_id == "vpc-12345"
        assert detached_mode.subnet_id == "subnet-12345"
        assert detached_mode.security_group_id == "sg-12345"
        assert detached_mode.bastion_id == "i-bastion"
        assert detached_mode.initialized is True

        # Verify EC2 client calls
        mock_ec2_client.create_vpc.assert_called_once()
        mock_ec2_client.create_subnet.assert_called_once()
        mock_ec2_client.create_security_group.assert_called_once()
        mock_ec2_client.run_instances.assert_called_once()

        # Verify state was saved
        detached_mode.state_store.save_state.assert_called()

    @patch("parsl_ephemeral_aws.modes.detached.delete_resource")
    def test_initialize_failure_cleanup(
        self, mock_delete_resource, detached_mode, mock_ec2_client
    ):
        """Test that resources are cleaned up if initialization fails."""
        # Make subnet creation fail
        mock_ec2_client.create_vpc.return_value = {"Vpc": {"VpcId": "vpc-12345"}}
        mock_ec2_client.create_subnet.side_effect = Exception("Subnet creation failed")

        # Call initialize and expect exception
        with pytest.raises(ResourceCreationError):
            detached_mode.initialize()

        # Verify cleanup was called
        mock_delete_resource.assert_called()

    def test_submit_job(self, detached_mode, mock_ssm_client):
        """Test job submission via SSM Parameter Store."""
        # Setup mode as initialized
        detached_mode.initialized = True
        detached_mode.vpc_id = "vpc-12345"
        detached_mode.subnet_id = "subnet-12345"
        detached_mode.security_group_id = "sg-12345"
        detached_mode.bastion_id = "i-bastion"

        # Submit a job
        command = "echo hello"
        resource_id = detached_mode.submit_job("job-1", command, 1)

        # Verify SSM parameters were created
        assert mock_ssm_client.put_parameter.call_count == 2  # Job command and status

        # Verify first call for job command
        first_call = mock_ssm_client.put_parameter.call_args_list[0]
        assert "/parsl/workflows/test-workflow/jobs/job-1" in first_call[1]["Name"]

        # Verify second call for job status
        second_call = mock_ssm_client.put_parameter.call_args_list[1]
        assert "/parsl/workflows/test-workflow/status/job-1" in second_call[1]["Name"]
        assert STATUS_PENDING in second_call[1]["Value"]

        # Verify resource tracking
        assert resource_id in detached_mode.resources
        assert detached_mode.resources[resource_id]["job_id"] == "job-1"
        assert detached_mode.resources[resource_id]["status"] == STATUS_PENDING

        # Verify state was saved
        detached_mode.state_store.save_state.assert_called()

    def test_submit_job_not_initialized(self, detached_mode):
        """Test submission when not initialized raises error."""
        detached_mode.initialized = False

        with pytest.raises(OperatingModeError):
            detached_mode.submit_job("job-1", "echo hello", 1)

    def test_get_job_status(self, detached_mode, mock_ssm_client):
        """Test getting job status via SSM."""
        # Setup mock resources
        job_id = "job-1"
        resource_id = f"serverless-{job_id}"
        detached_mode.resources = {
            resource_id: {"job_id": job_id, "status": STATUS_PENDING}
        }

        # Mock SSM get_parameter response for the job status
        mock_ssm_client.get_parameter.return_value = {
            "Parameter": {
                "Value": json.dumps(
                    {"status": STATUS_RUNNING, "instance_id": "i-worker"}
                )
            }
        }

        # Get status
        status = detached_mode.get_job_status([resource_id])

        # Verify SSM call
        mock_ssm_client.get_parameter.assert_called_with(
            Name=f"/parsl/workflows/test-workflow/status/{job_id}"
        )

        # Verify status result
        assert status[resource_id] == STATUS_RUNNING

        # Verify resource was updated
        assert detached_mode.resources[resource_id]["status"] == STATUS_RUNNING

    def test_cancel_jobs(self, detached_mode, mock_ssm_client):
        """Test canceling jobs via bastion host."""
        # Setup mock resources
        resource_id1 = "job-resource-1"
        resource_id2 = "job-resource-2"
        detached_mode.resources = {
            resource_id1: {"job_id": "job-1", "status": STATUS_RUNNING},
            resource_id2: {"job_id": "job-2", "status": STATUS_RUNNING},
        }

        # Cancel jobs
        status = detached_mode.cancel_jobs([resource_id1, resource_id2])

        # Verify SSM put_parameter was called for the cancel request
        mock_ssm_client.put_parameter.assert_called_once()
        args, kwargs = mock_ssm_client.put_parameter.call_args
        assert kwargs["Name"] == "/parsl/workflows/test-workflow/cancel"
        assert "job-1" in kwargs["Value"]
        assert "job-2" in kwargs["Value"]

        # Verify status results
        assert status[resource_id1] == STATUS_CANCELLED
        assert status[resource_id2] == STATUS_CANCELLED

        # Verify resources were updated
        assert detached_mode.resources[resource_id1]["status"] == STATUS_CANCELLED
        assert detached_mode.resources[resource_id2]["status"] == STATUS_CANCELLED

    def test_cleanup_resources(self, detached_mode, mock_ssm_client):
        """Test resource cleanup."""
        # Setup resources
        resource_id1 = "job-resource-1"
        resource_id2 = "job-resource-2"
        detached_mode.resources = {
            resource_id1: {"job_id": "job-1", "status": STATUS_RUNNING},
            resource_id2: {"job_id": "job-2", "status": STATUS_COMPLETED},
        }

        # Clean up one resource
        detached_mode.cleanup_resources([resource_id1])

        # Verify SSM delete_parameter was called for job data
        mock_ssm_client.delete_parameter.assert_any_call(
            Name="/parsl/workflows/test-workflow/jobs/job-1"
        )

        # Verify SSM delete_parameter was called for job status
        mock_ssm_client.delete_parameter.assert_any_call(
            Name="/parsl/workflows/test-workflow/status/job-1"
        )

        # Verify resource was removed from tracking
        assert resource_id1 not in detached_mode.resources
        assert resource_id2 in detached_mode.resources

        # Verify state was saved
        detached_mode.state_store.save_state.assert_called()

    @patch("parsl_ephemeral_aws.modes.detached.delete_resource")
    def test_cleanup_infrastructure(
        self, mock_delete_resource, detached_mode, mock_ec2_client, mock_cf_client
    ):
        """Test infrastructure cleanup including bastion host."""
        # Setup infrastructure resources
        detached_mode.vpc_id = "vpc-12345"
        detached_mode.subnet_id = "subnet-12345"
        detached_mode.security_group_id = "sg-12345"
        detached_mode.bastion_id = "i-bastion"
        detached_mode.bastion_host_type = "direct"
        detached_mode.initialized = True
        detached_mode.preserve_bastion = False  # Don't preserve bastion

        # Add a resource to be cleaned up
        resource_id = "job-resource-1"
        detached_mode.resources = {
            resource_id: {"job_id": "job-1", "status": STATUS_RUNNING}
        }

        # Call cleanup
        detached_mode.cleanup_infrastructure()

        # Verify EC2 termination call for bastion
        mock_ec2_client.terminate_instances.assert_called_once_with(
            InstanceIds=["i-bastion"]
        )

        # Verify resource deletion for networking
        expected_calls = [
            call("sg-12345", detached_mode.session, RESOURCE_TYPE_SECURITY_GROUP),
            call("subnet-12345", detached_mode.session, RESOURCE_TYPE_SUBNET),
            call("vpc-12345", detached_mode.session, RESOURCE_TYPE_VPC, force=True),
        ]
        mock_delete_resource.assert_has_calls(expected_calls, any_order=True)

        # Verify state was reset
        assert detached_mode.vpc_id is None
        assert detached_mode.subnet_id is None
        assert detached_mode.security_group_id is None
        assert detached_mode.bastion_id is None
        assert detached_mode.initialized is False
        assert not detached_mode.resources  # Resources should be empty

    def test_cleanup_infrastructure_preserve_bastion(
        self, detached_mode, mock_ec2_client
    ):
        """Test infrastructure cleanup when preserve_bastion is True."""
        # Setup infrastructure resources
        detached_mode.vpc_id = "vpc-12345"
        detached_mode.subnet_id = "subnet-12345"
        detached_mode.security_group_id = "sg-12345"
        detached_mode.bastion_id = "i-bastion"
        detached_mode.bastion_host_type = "direct"
        detached_mode.initialized = True
        detached_mode.preserve_bastion = True  # Preserve bastion

        # Call cleanup
        detached_mode.cleanup_infrastructure()

        # Verify EC2 termination was NOT called for bastion
        mock_ec2_client.terminate_instances.assert_not_called()

        # Verify bastion ID is preserved
        assert detached_mode.bastion_id == "i-bastion"

    def test_cleanup_cloudformation_bastion(self, detached_mode, mock_cf_client):
        """Test cleanup when bastion host is deployed via CloudFormation."""
        # Setup infrastructure resources
        detached_mode.vpc_id = "vpc-12345"
        detached_mode.subnet_id = "subnet-12345"
        detached_mode.security_group_id = "sg-12345"
        detached_mode.bastion_id = "stack-12345"
        detached_mode.bastion_host_type = "cloudformation"
        detached_mode.initialized = True
        detached_mode.preserve_bastion = False  # Don't preserve bastion

        # Call cleanup
        detached_mode.cleanup_infrastructure()

        # Verify CloudFormation delete_stack was called
        mock_cf_client.delete_stack.assert_called_once_with(StackName="stack-12345")

    def test_list_resources(self, detached_mode):
        """Test listing resources."""
        # Setup resources
        detached_mode.vpc_id = "vpc-12345"
        detached_mode.subnet_id = "subnet-12345"
        detached_mode.security_group_id = "sg-12345"
        detached_mode.bastion_id = "i-bastion"

        resource_id1 = "job-resource-1"
        resource_id2 = "job-resource-2"
        detached_mode.resources = {
            resource_id1: {"job_id": "job-1", "status": STATUS_RUNNING},
            resource_id2: {"job_id": "job-2", "status": STATUS_COMPLETED},
            "i-bastion": {
                "type": RESOURCE_TYPE_BASTION,
                "created_at": time.time(),
                "workflow_id": "test-workflow",
            },
        }

        # List resources
        resources = detached_mode.list_resources()

        # Verify resource categories
        assert "ec2_instances" in resources
        assert "bastion_host" in resources
        assert "vpc" in resources
        assert "subnet" in resources
        assert "security_group" in resources

        # Verify counts
        assert len(resources["ec2_instances"]) == 2
        assert len(resources["bastion_host"]) == 1
        assert len(resources["vpc"]) == 1
        assert len(resources["subnet"]) == 1
        assert len(resources["security_group"]) == 1

        # Verify details
        assert resources["vpc"][0]["id"] == "vpc-12345"
        assert resources["subnet"][0]["id"] == "subnet-12345"
        assert resources["security_group"][0]["id"] == "sg-12345"

        # Verify bastion host
        assert resources["bastion_host"][0]["id"] == "i-bastion"
        assert resources["bastion_host"][0]["type"] == RESOURCE_TYPE_BASTION

        # Verify job instances
        ec2_resource_ids = [r["id"] for r in resources["ec2_instances"]]
        assert resource_id1 in ec2_resource_ids
        assert resource_id2 in ec2_resource_ids

    def test_load_state(self, detached_mode, mock_state_store):
        """Test loading state."""
        # Setup mock state
        mock_state = {
            "resources": {
                "job-resource-1": {"job_id": "job-1", "status": STATUS_RUNNING}
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
        }
        mock_state_store.load_state.return_value = mock_state

        # Load state
        result = detached_mode.load_state()

        # Verify state was loaded
        assert result is True
        assert detached_mode.resources == mock_state["resources"]
        assert detached_mode.vpc_id == mock_state["vpc_id"]
        assert detached_mode.subnet_id == mock_state["subnet_id"]
        assert detached_mode.security_group_id == mock_state["security_group_id"]
        assert detached_mode.bastion_id == mock_state["bastion_id"]
        assert detached_mode.initialized == mock_state["initialized"]
        assert detached_mode.workflow_id == mock_state["workflow_id"]
        assert detached_mode.bastion_host_type == mock_state["bastion_host_type"]

    def test_save_state(self, detached_mode, mock_state_store):
        """Test saving state."""
        # Setup state
        detached_mode.vpc_id = "vpc-12345"
        detached_mode.subnet_id = "subnet-12345"
        detached_mode.security_group_id = "sg-12345"
        detached_mode.bastion_id = "i-bastion"
        detached_mode.initialized = True
        detached_mode.bastion_host_type = "direct"
        detached_mode.workflow_id = "test-workflow"
        detached_mode.resources = {
            "job-resource-1": {"job_id": "job-1", "status": STATUS_RUNNING}
        }

        # Save state
        detached_mode.save_state()

        # Verify state_store.save_state was called
        mock_state_store.save_state.assert_called_once()

        # Verify state content
        state = mock_state_store.save_state.call_args[0][0]
        assert state["provider_id"] == "test-provider"
        assert state["mode"] == "DetachedMode"
        assert state["vpc_id"] == "vpc-12345"
        assert state["subnet_id"] == "subnet-12345"
        assert state["security_group_id"] == "sg-12345"
        assert state["bastion_id"] == "i-bastion"
        assert state["initialized"] is True
        assert state["resources"] == detached_mode.resources
        assert state["workflow_id"] == "test-workflow"
        assert state["bastion_host_type"] == "direct"

    def test_prepare_bastion_init_script(self, detached_mode):
        """Test bastion host initialization script generation."""
        # Generate init script
        init_script = detached_mode._prepare_bastion_init_script()

        # Verify script content
        assert "#!/bin/bash" in init_script
        assert "parsl-bastion-manager.py" in init_script
        assert "parsl-idle-shutdown" in init_script
        assert "systemd" in init_script  # Should set up systemd service
        assert f"export PARSL_WORKFLOW_ID={detached_mode.workflow_id}" in init_script

    def test_get_bastion_manager_script(self, detached_mode):
        """Test bastion manager script generation."""
        # Generate manager script
        manager_script = detached_mode._get_bastion_manager_script()

        # Verify script content
        assert "#!/usr/bin/env python3" in manager_script
        assert "WORKFLOW_ID" in manager_script
        assert "get_pending_jobs" in manager_script
        assert "update_job_status" in manager_script
        assert "launch_instance" in manager_script
        assert "main()" in manager_script  # Has entry point

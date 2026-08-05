"""Unit tests for the DetachedMode class.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import pytest
from unittest.mock import MagicMock, patch
import base64
import boto3
import time
import json
from botocore.exceptions import ClientError

from parsl_ephemeral_provider.modes.detached import DetachedMode
from parsl_ephemeral_provider.state.base import STATE_KEY_MODE
from parsl_ephemeral_provider.exceptions import (
    OperatingModeError,
    ResourceCreationError,
    ResourceNotFoundError,
)
from parsl_ephemeral_provider.constants import (
    MAX_CFN_PARAMETER_BYTES,
    MAX_EC2_USER_DATA_B64_BYTES,
    MAX_EC2_USER_DATA_BYTES,
    RESOURCE_TYPE_BASTION,
    STATUS_INTERRUPTED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
)

pytestmark = pytest.mark.unit


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
            vpc_id="vpc-12345",
            subnet_id="subnet-12345",
            security_group_id="sg-12345",
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

        # #69 made all three network IDs caller-supplied and removed the
        # create-on-demand switch entirely. The bastion is still this mode's own
        # to create — that asymmetry is the point of the assertions above.
        assert not hasattr(mode, "create_vpc")

    @patch("parsl_ephemeral_provider.modes.detached.get_default_ami")
    @patch("parsl_ephemeral_provider.modes.detached.get_cf_template")
    def test_initialize_cloudformation(
        self,
        mock_get_cf_template,
        mock_get_default_ami,
        detached_mode,
        mock_ec2_client,
        mock_cf_client,
    ):
        """Initialize creates only the bastion stack; the network is the caller's.

        The three ``create_vpc``/``create_subnet``/``create_security_group``
        assertions this test used to make were removed by #69. The bastion is
        still created here, so that half stands.
        """
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

        # The bastion stack is this mode's to create; the network is not.
        mock_cf_client.create_stack.assert_called_once()
        mock_ec2_client.create_vpc.assert_not_called()
        mock_ec2_client.create_subnet.assert_not_called()
        mock_ec2_client.create_security_group.assert_not_called()

        # It is verified instead.
        mock_ec2_client.describe_vpcs.assert_called_once_with(VpcIds=["vpc-12345"])

        # Verify state was saved
        detached_mode.state_store.save_state.assert_called()

    @patch("parsl_ephemeral_provider.modes.detached.get_default_ami")
    def test_initialize_direct(
        self, mock_get_default_ami, detached_mode, mock_ec2_client
    ):
        """Initialize launches only the bastion instance; the network is the caller's."""
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

        # The bastion instance is this mode's to launch; the network is not (#69).
        mock_ec2_client.run_instances.assert_called_once()
        mock_ec2_client.create_vpc.assert_not_called()
        mock_ec2_client.create_subnet.assert_not_called()
        mock_ec2_client.create_security_group.assert_not_called()

        # Verify state was saved
        detached_mode.state_store.save_state.assert_called()

    def test_initialize_reports_a_missing_network_resource(
        self, detached_mode, mock_ec2_client
    ):
        """A missing VPC is named, and no bastion is launched into it.

        Replaces ``test_initialize_failure_cleanup``, which failed
        ``create_subnet`` and asserted a ``delete_resource`` rollback — neither
        function is reachable from this mode any more, and patching the latter
        raised ``AttributeError`` outright.
        """
        mock_ec2_client.describe_vpcs.side_effect = ClientError(
            {"Error": {"Code": "InvalidVpcID.NotFound", "Message": "not found"}},
            "DescribeVpcs",
        )

        with pytest.raises(ResourceNotFoundError) as exc_info:
            detached_mode.initialize()

        assert "vpc-12345" in str(exc_info.value)

        # Verification precedes creation, so nothing was launched, and the ID
        # survives rather than being nulled out (#77).
        mock_ec2_client.run_instances.assert_not_called()
        assert detached_mode.vpc_id == "vpc-12345"
        assert detached_mode.initialized is False

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

    def test_get_job_status_keeps_an_interruption(self, detached_mode, mock_ssm_client):
        """A reclaim marked by the monitor survives the next poll (#137).

        The status document this mode reads is written by the worker itself, so a
        reclaimed instance cannot report its own reclaim — it simply stops
        updating, leaving the last value it wrote. Re-reading would therefore
        overwrite ``STATUS_INTERRUPTED`` with a stale RUNNING.
        """
        resource_id = "serverless-job-1"
        detached_mode.resources = {
            resource_id: {"job_id": "job-1", "status": STATUS_INTERRUPTED}
        }

        status = detached_mode.get_job_status([resource_id])

        assert status[resource_id] == STATUS_INTERRUPTED
        assert detached_mode.resources[resource_id]["status"] == STATUS_INTERRUPTED
        mock_ssm_client.get_parameter.assert_not_called()

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

    def test_cleanup_infrastructure(
        self, detached_mode, mock_ec2_client, mock_cf_client
    ):
        """Cleanup terminates the bastion this mode created, and nothing else.

        The ``delete_resource`` patch and the SG/subnet/VPC deletion assertions
        this test carried were removed by #69 — the module no longer imports that
        function, so the patch raised ``AttributeError``.
        """
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

        # The caller's network is left intact and still configured (#69).
        mock_ec2_client.delete_security_group.assert_not_called()
        mock_ec2_client.delete_subnet.assert_not_called()
        mock_ec2_client.delete_vpc.assert_not_called()
        assert detached_mode.vpc_id == "vpc-12345"
        assert detached_mode.subnet_id == "subnet-12345"
        assert detached_mode.security_group_id == "sg-12345"

        # The bastion was this mode's, so its ID is cleared along with the rest.
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

        # The mode writes its own state key, so the provider's document survives
        state_key, state = mock_state_store.save_state.call_args[0]
        assert state_key == STATE_KEY_MODE

        # Verify state content
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


class TestBastionUserDataSize:
    """The bastion UserData must fit the mechanism that delivers it (#227).

    Detached mode was wholly non-functional because the init script — a ~32 KB
    program — was passed as UserData: 10.4x CloudFormation's 4,096 B parameter
    limit and 2.0x EC2's 16,384 B limit. Nothing in-tree rendered the script and
    measured it, and substrate enforces neither limit, so the failure only ever
    appeared against live AWS, at provider *construction*.

    These tests are that missing measurement, and they are **arithmetic on
    rendered strings** — no session, no emulator, nothing faked. Whether staging
    actually works is a different question, answered against substrate by
    ``tests/integration/test_substrate_modes.py::TestDetachedModeSubstrate``,
    which uploads the script and fetches the URL over HTTP. Asserting
    ``put_object`` call args against a mock here would duplicate that while
    proving only that this code calls a method.
    """

    # Longest presigned URL measured against live AWS: 175 B with static keys,
    # 1,064 B under a session token (the token alone is ~664 B). The URL is the
    # one part of the shim whose size is not ours to control, so the budget is
    # set against the worst case.
    LONGEST_PRESIGNED_URL = 1064

    @pytest.fixture
    def mode(self):
        """A DetachedMode used only to render strings; no AWS call is made."""
        session = MagicMock(spec=boto3.Session)
        session.region_name = "us-east-1"
        return DetachedMode(
            provider_id="test-provider",
            session=session,
            state_store=MagicMock(),
            workflow_id="test-workflow",
            vpc_id="vpc-12345",
            subnet_id="subnet-12345",
            security_group_id="sg-12345",
        )

    def test_the_shim_fits_every_delivery_limit(self, mode):
        """The shim clears all three limits even with the longest URL possible.

        The URL is substituted at its worst-case measured length rather than
        left to a fixture's short example: a caller using assume-role or
        session-token credentials gets a URL ~6x longer, and a budget calibrated
        on static keys would pass here and fail for them.
        """
        shim = mode._render_bastion_shim(
            "https://x/" + "u" * self.LONGEST_PRESIGNED_URL
        )

        raw = len(shim.encode())
        encoded = len(base64.b64encode(shim.encode()))

        assert raw < MAX_EC2_USER_DATA_BYTES
        assert encoded < MAX_EC2_USER_DATA_B64_BYTES
        # The tightest limit by far, and the one that made the default
        # bastion_host_type impossible.
        assert encoded < MAX_CFN_PARAMETER_BYTES
        assert encoded < MAX_CFN_PARAMETER_BYTES * 3 // 4, (
            f"shim base64 is {encoded} B against a {MAX_CFN_PARAMETER_BYTES} B "
            "CloudFormation parameter limit — it is meant to stay a fetch, not "
            "accumulate logic"
        )

    def test_the_init_script_itself_would_not_have_fit(self, mode):
        """Guard the premise: the script really is too big to send inline.

        Without this, the test above could keep passing for the wrong reason —
        if the script ever shrank below the limits, staging would look like
        unnecessary machinery and the assertions above would prove nothing.
        """
        init_script = mode._prepare_bastion_init_script()

        assert len(init_script.encode()) > MAX_EC2_USER_DATA_BYTES
        assert len(base64.b64encode(init_script.encode())) > MAX_CFN_PARAMETER_BYTES

    def test_the_shim_fetches_rather_than_carries(self, mode):
        """UserData downloads the script; it does not contain it."""
        shim = mode._render_bastion_shim("https://example.test/staged.sh?sig=abc")

        assert shim.startswith("#!/bin/bash")
        assert "curl" in shim
        assert "https://example.test/staged.sh?sig=abc" in shim
        # The script's own content must not have travelled along with it.
        assert "parsl-bastion-manager.py" not in shim
        assert "systemd" not in shim

    def test_the_shim_records_failure_instead_of_aborting_silently(self, mode):
        """A failed fetch must leave evidence, not a mystery.

        The lesson of #225: an instance whose UserData died still reaches
        ``running`` and still reports CREATE_COMPLETE, so failure has to be
        written down somewhere to be noticed at all.
        """
        shim = mode._render_bastion_shim("https://example.test/staged.sh")

        assert "parsl-bastion-bootstrap.status" in shim
        assert "FAILED: could not download bastion init script" in shim
        assert "--retry" in shim  # a transient 5xx should not strand the bastion

    def test_an_oversized_shim_is_refused_at_render_time(self, mode):
        """The size check fails loudly rather than deferring to AWS.

        This is the regression gate: #227 was a live-only failure precisely
        because nothing checked before the API call.
        """
        with (
            patch.object(
                mode,
                "_stage_bastion_init_script",
                return_value="https://example.test/staged.sh",
            ),
            patch.object(
                mode,
                "_render_bastion_shim",
                return_value="x" * (MAX_EC2_USER_DATA_BYTES + 1),
            ),
        ):
            with pytest.raises(ResourceCreationError, match="over the"):
                mode._prepare_bastion_user_data()


class TestBastionInitScriptShell:
    """The init script's shell must survive first boot on Amazon Linux 2023.

    #225: the script ran `set -e` and then installed `awscli`, for which AL2023
    — the default AMI family — has no package. UserData aborted at that line, so
    the manager script, its service, and the idle-shutdown cron were never
    installed. The instance still reached `running` and the stack still reported
    CREATE_COMPLETE, which is why a bastion that orchestrated nothing looked
    healthy from every angle.

    Every assertion below pins a fact established by booting a real AL2023
    bastion and reading its logs, not by reasoning about the shell. Each one
    corresponds to a defect that a passing unit suite did not catch: the package
    set is the third revision.
    """

    @pytest.fixture
    def init_script(self):
        session = MagicMock(spec=boto3.Session)
        session.region_name = "us-east-1"
        session.client.return_value = MagicMock()
        mode = DetachedMode(
            provider_id="test-provider",
            session=session,
            state_store=MagicMock(),
            workflow_id="test-workflow",
            vpc_id="vpc-12345",
            subnet_id="subnet-12345",
            security_group_id="sg-12345",
        )
        return mode._prepare_bastion_init_script()

    def test_no_awscli_package_install(self, init_script):
        """AL2023 has no `awscli` package; CLI v2 is preinstalled."""
        assert "awscli" not in init_script

    def test_package_install_dispatches_on_the_available_manager(self, init_script):
        """`apt-get || yum` failed on AL2023, where apt-get does not exist."""
        assert "command -v dnf" in init_script
        assert "apt-get install -y python3 python3-pip jq awscli" not in init_script

    def test_boto3_comes_from_the_distro_not_pip(self, init_script):
        """boto3 must be the system package, and pip must not be installed.

        Verified live: `dnf install python3-pip` pulls in **python3.11** and
        repoints /usr/bin/python3 at it, which breaks `dnf` itself and AWS CLI v2
        — both python3.9 scripts — with `ModuleNotFoundError: No module named
        'dnf'`. AL2023 packages boto3 for the system 3.9, so no pip is needed.

        `python3` is likewise absent from the install list: it is already there,
        and naming it invites the same substitution.
        """
        assert "python3-boto3" in init_script
        assert "python3-pip" not in init_script
        assert "pip install" not in init_script
        assert 'python3 -c "import boto3"' in init_script

    def test_a_cron_daemon_is_installed(self, init_script):
        """AL2023 ships no cron daemon, so `crontab -` was a no-op.

        The idle-shutdown timer is the only thing that reads `idle_timeout`, so
        without this the option cannot work — a bastion would run until someone
        reclaimed it, and with `preserve_bastion=True` (the default) nothing
        ever does.
        """
        assert "cronie" in init_script
        assert "crond" in init_script
        # The prerequisite check must catch a daemon that is not *running*.
        # `crontab` being on PATH proved nothing: it is the client, and the
        # /etc/cron.d drop-in is read only by the daemon.
        prerequisites = init_script.split("# Verify prerequisites")[1]
        assert "systemctl is-active crond" in prerequisites

    def test_the_idle_timer_is_registered_somewhere_cron_will_read(self, init_script):
        """The timer goes in /etc/cron.d, not through `crontab -`.

        Verified live: `(crontab -l 2>/dev/null; echo '...') | crontab -` left
        /var/spool/cron/root at **0 bytes** on a fresh bastion. `crontab -l` has
        nothing to list and exits non-zero there, so the subshell contributed
        nothing and the append landed nowhere — while every command in the
        pipeline exited 0. So `idle_timeout` was registered on no schedule even
        after #225 installed cronie.

        A /etc/cron.d file has no read-modify-write step and so no empty-input
        case. cron ignores such a file unless it is mode 0644, root-owned, and
        has no dot in its name, and its lines carry a user field.

        Confirmed on the fixed bastion, which is the only evidence that counts
        here — the drop-in existing proves nothing about cron reading it::

            CROND[25601]: (root) CMD (/usr/local/bin/parsl-idle-shutdown.sh)
            CROND[25600]: (root) CMDEND (/usr/local/bin/parsl-idle-shutdown.sh)
            -rw-r--r--. 1 root root 11 /var/run/parsl-last-activity

        The activity file is the script's own output, so it ran, not merely got
        scheduled.
        """
        assert "| crontab -" not in init_script

        assert "/etc/cron.d/parsl-idle-shutdown" in init_script
        assert "." not in "parsl-idle-shutdown"
        assert (
            "*/5 * * * * root /usr/local/bin/parsl-idle-shutdown.sh" in init_script
        ), "system crontab format requires a user field"
        assert "chmod 0644 /etc/cron.d/parsl-idle-shutdown" in init_script
        assert "chown root:root /etc/cron.d/parsl-idle-shutdown" in init_script

        # Written after the script it schedules, or the first firing finds
        # nothing to run.
        assert init_script.index(
            "chmod +x /usr/local/bin/parsl-idle-shutdown.sh"
        ) < init_script.index("/etc/cron.d/parsl-idle-shutdown")

    def test_the_script_signals_completion(self, init_script):
        """A sentinel is the only positive evidence UserData ran to the end.

        Instance state, status checks, and stack status are all indifferent to
        UserData, which is how #225 stayed invisible.
        """
        assert "touch /var/run/parsl_bastion_ready" in init_script
        # After the parts that would have been skipped by the #225 abort.
        assert init_script.index("parsl-bastion-manager.service") < init_script.index(
            "parsl_bastion_ready"
        )
        assert init_script.index("parsl-idle-shutdown.sh") < init_script.index(
            "parsl_bastion_ready"
        )

"""Integration tests for state persistence in workflow scenarios.

These tests verify that state persistence works correctly across different
operating modes and scenarios, including interruptions and resumption.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import os
import json
import time
import uuid
import pytest
import tempfile
from unittest.mock import patch

from parsl_ephemeral_aws.modes.standard import StandardMode
from parsl_ephemeral_aws.modes.detached import DetachedMode
from parsl_ephemeral_aws.modes.serverless import ServerlessMode
from parsl_ephemeral_aws.state.file import FileStateStore
from tests.substrate_support import (
    get_substrate_session,
    is_substrate_available,
)


# Skip all tests if the substrate emulator is not available
pytestmark = pytest.mark.skipif(
    not is_substrate_available(),
    reason="substrate not available - start with 'make substrate-up'",
)


@pytest.mark.integration
class TestWorkflowStatePersistence:
    """Integration tests for workflow state persistence."""

    @pytest.fixture(scope="class")
    def substrate_session(self):
        """Create a session connected to substrate."""
        return get_substrate_session()

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for state files."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            yield tmp_dir

    @pytest.fixture
    def state_file_path(self, temp_dir):
        """Create a path for the state file."""
        return os.path.join(temp_dir, f"test-state-{uuid.uuid4().hex[:8]}.json")

    @pytest.fixture
    def file_state_store(self, state_file_path):
        """Create a FileStateStore instance."""
        provider_id = f"test-provider-{uuid.uuid4().hex[:8]}"
        return FileStateStore(file_path=state_file_path, provider_id=provider_id)

    @pytest.fixture
    def mock_ec2_client(self, substrate_session):
        """Create a EC2 client connected to substrate."""
        return substrate_session.client("ec2")

    @pytest.fixture
    def mock_ssm_client(self, substrate_session):
        """Create a SSM client connected to substrate."""
        return substrate_session.client("ssm")

    @pytest.fixture
    def mock_s3_client(self, substrate_session):
        """Create a S3 client connected to substrate."""
        return substrate_session.client("s3")

    @pytest.mark.substrate
    def test_standard_mode_state_persistence(
        self, substrate_session, file_state_store, mock_ec2_client
    ):
        """Test state persistence with StandardMode."""
        # Create a StandardMode instance
        provider_id = f"test-provider-{uuid.uuid4().hex[:8]}"

        # Set up mocks for AWS services using substrate
        with patch("boto3.Session", return_value=substrate_session):
            # Create a standard mode
            mode = StandardMode(
                provider_id=provider_id,
                session=substrate_session,
                state_store=file_state_store,
                region="us-east-1",
                instance_type="t2.micro",
                image_id="ami-12345678",  # Dummy AMI
            )

            # Initialize mode to create basic infrastructure (VPC, etc.)
            # Mock the actual infrastructure creation
            with patch.object(mode, "_create_vpc", return_value="vpc-12345"):
                with patch.object(mode, "_create_subnet", return_value="subnet-12345"):
                    with patch.object(
                        mode, "_create_security_group", return_value="sg-12345"
                    ):
                        mode.initialize()

            # Verify mode is initialized
            assert mode.initialized
            assert mode.vpc_id == "vpc-12345"
            assert mode.subnet_id == "subnet-12345"
            assert mode.security_group_id == "sg-12345"

            # Mock job submission
            with patch.object(mode, "_create_ec2_instance") as mock_create_instance:
                mock_create_instance.return_value = {
                    "instance_id": f"i-{uuid.uuid4().hex[:12]}",
                    "private_ip": "10.0.0.1",
                    "public_ip": "54.123.456.789",
                    "dns_name": "ec2-54-123-456-789.compute-1.amazonaws.com",
                }

                # Submit some jobs
                job_ids = []
                resource_ids = []
                for i in range(3):
                    job_id = f"test-job-{i}-{uuid.uuid4().hex[:8]}"
                    resource_id = mode.submit_job(job_id, f"echo 'Job {i}'", 1)
                    job_ids.append(job_id)
                    resource_ids.append(resource_id)

            # Verify resources were created
            assert len(mode.resources) == 3
            for resource_id in resource_ids:
                assert resource_id in mode.resources

            # Save state
            mode.save_state()

            # Verify state was saved to file
            assert os.path.exists(file_state_store.file_path)

            # Create a new mode instance
            mode2 = StandardMode(
                provider_id=provider_id,
                session=substrate_session,
                state_store=file_state_store,
                region="us-east-1",
                instance_type="t2.micro",
                image_id="ami-12345678",  # Dummy AMI
            )

            # Load state
            mode2.load_state()

            # Verify state was loaded correctly
            assert mode2.initialized
            assert mode2.vpc_id == "vpc-12345"
            assert mode2.subnet_id == "subnet-12345"
            assert mode2.security_group_id == "sg-12345"
            assert len(mode2.resources) == 3

            # Check that resources were loaded correctly
            for resource_id in resource_ids:
                assert resource_id in mode2.resources
                assert mode2.resources[resource_id]["job_id"] in job_ids

            # Clean up - do not actually try to delete AWS resources in the emulator
            with patch.object(mode2, "_delete_ec2_instance"):
                with patch.object(mode2, "_delete_security_group"):
                    with patch.object(mode2, "_delete_subnet"):
                        with patch.object(mode2, "_delete_vpc"):
                            mode2.cleanup_infrastructure()

            # Verify cleanup logic updated the state
            assert not mode2.initialized
            assert mode2.vpc_id is None
            assert mode2.subnet_id is None
            assert mode2.security_group_id is None

            # State file should be empty or reflect cleaned state
            assert (
                not os.path.exists(file_state_store.file_path)
                or os.path.getsize(file_state_store.file_path) == 0
            )

    @pytest.mark.substrate
    def test_detached_mode_state_persistence(self, substrate_session, file_state_store):
        """Test state persistence with DetachedMode."""
        # Create a DetachedMode instance
        provider_id = f"test-provider-{uuid.uuid4().hex[:8]}"
        workflow_id = f"test-workflow-{uuid.uuid4().hex[:8]}"

        # Set up mocks for AWS services using substrate
        with patch("boto3.Session", return_value=substrate_session):
            # Create a detached mode
            mode = DetachedMode(
                provider_id=provider_id,
                session=substrate_session,
                state_store=file_state_store,
                region="us-east-1",
                instance_type="t2.micro",
                image_id="ami-12345678",  # Dummy AMI
                workflow_id=workflow_id,
                bastion_instance_type="t2.micro",
                bastion_host_type="direct",
            )

            # Initialize mode - mock infrastructure creation
            with patch.object(mode, "_create_vpc", return_value="vpc-12345"):
                with patch.object(mode, "_create_subnet", return_value="subnet-12345"):
                    with patch.object(
                        mode, "_create_security_group", return_value="sg-12345"
                    ):
                        with patch.object(
                            mode, "_create_bastion_host"
                        ) as mock_create_bastion:
                            mock_create_bastion.return_value = {
                                "instance_id": f"i-bastion-{uuid.uuid4().hex[:8]}",
                                "private_ip": "10.0.0.2",
                                "public_ip": "54.123.456.789",
                                "dns_name": "ec2-54-123-456-789.compute-1.amazonaws.com",
                            }
                            mode.initialize()

            # Verify mode is initialized with bastion
            assert mode.initialized
            assert mode.vpc_id == "vpc-12345"
            assert mode.subnet_id == "subnet-12345"
            assert mode.security_group_id == "sg-12345"
            assert mode.bastion_id is not None

            # Mock job submission
            with patch.object(mode, "_create_ec2_instance") as mock_create_instance:
                mock_create_instance.return_value = {
                    "instance_id": f"i-{uuid.uuid4().hex[:12]}",
                    "private_ip": "10.0.0.5",
                    "public_ip": None,  # No public IP in detached mode
                    "dns_name": None,
                }

                # Mock SSM parameter creation
                with patch.object(mode, "_create_ssm_parameter"):
                    # Submit some jobs
                    job_ids = []
                    resource_ids = []
                    for i in range(3):
                        job_id = f"test-job-{i}-{uuid.uuid4().hex[:8]}"
                        resource_id = mode.submit_job(job_id, f"echo 'Job {i}'", 1)
                        job_ids.append(job_id)
                        resource_ids.append(resource_id)

            # Verify resources were created
            assert len(mode.resources) == 3
            for resource_id in resource_ids:
                assert resource_id in mode.resources

            # Save state
            mode.save_state()

            # Verify state was saved to file
            assert os.path.exists(file_state_store.file_path)

            # Create a new mode instance
            mode2 = DetachedMode(
                provider_id=provider_id,
                session=substrate_session,
                state_store=file_state_store,
                region="us-east-1",
                instance_type="t2.micro",
                image_id="ami-12345678",  # Dummy AMI
                workflow_id=workflow_id,
                bastion_instance_type="t2.micro",
                bastion_host_type="direct",
            )

            # Load state
            mode2.load_state()

            # Verify state was loaded correctly
            assert mode2.initialized
            assert mode2.vpc_id == "vpc-12345"
            assert mode2.subnet_id == "subnet-12345"
            assert mode2.security_group_id == "sg-12345"
            assert mode2.bastion_id is not None
            assert len(mode2.resources) == 3

            # Check that resources were loaded correctly
            for resource_id in resource_ids:
                assert resource_id in mode2.resources
                assert mode2.resources[resource_id]["job_id"] in job_ids

            # Clean up
            with patch.object(mode2, "_delete_ec2_instance"):
                with patch.object(mode2, "_delete_ssm_parameter"):
                    with patch.object(mode2, "_delete_security_group"):
                        with patch.object(mode2, "_delete_subnet"):
                            with patch.object(mode2, "_delete_vpc"):
                                mode2.preserve_bastion = (
                                    False  # Ensure bastion is cleaned up
                                )
                                mode2.cleanup_infrastructure()

            # Verify cleanup logic updated the state
            assert not mode2.initialized
            assert mode2.vpc_id is None
            assert mode2.subnet_id is None
            assert mode2.security_group_id is None
            assert mode2.bastion_id is None

    @pytest.mark.substrate
    def test_serverless_mode_state_persistence(
        self, substrate_session, file_state_store
    ):
        """Test state persistence with ServerlessMode."""
        # Create a ServerlessMode instance
        provider_id = f"test-provider-{uuid.uuid4().hex[:8]}"

        # Set up mocks for AWS services using substrate
        with patch("boto3.Session", return_value=substrate_session):
            # Create a serverless mode
            mode = ServerlessMode(
                provider_id=provider_id,
                session=substrate_session,
                state_store=file_state_store,
                region="us-east-1",
                worker_type="lambda",  # Use Lambda for simplicity
                lambda_memory=128,
                lambda_timeout=30,
            )

            # Initialize mode - Lambda-only mode won't create network resources
            mode.initialize()

            # Verify mode is initialized
            assert mode.initialized

            # Lambda mode should not create VPC resources
            assert mode.vpc_id is None
            assert mode.subnet_id is None
            assert mode.security_group_id is None

            # Mock Lambda function creation and invocation
            with patch.object(
                mode.lambda_manager, "_create_lambda_function"
            ) as mock_create_lambda:
                mock_create_lambda.return_value = {
                    "FunctionName": f"lambda-{uuid.uuid4().hex[:8]}",
                    "FunctionArn": f"arn:aws:lambda:us-east-1:123456789012:function:lambda-{uuid.uuid4().hex[:8]}",
                }

                with patch.object(mode.lambda_manager, "_invoke_lambda") as mock_invoke:
                    mock_invoke.return_value = {
                        "StatusCode": 200,
                        "Payload": json.dumps({"statusCode": 200, "body": "Success"}),
                    }

                    # Submit some jobs
                    job_ids = []
                    resource_ids = []
                    for i in range(3):
                        job_id = f"test-job-{i}-{uuid.uuid4().hex[:8]}"

                        # Mock writing to temp file for Lambda code
                        with patch("builtins.open", mock_open()):
                            resource_id = mode.submit_job(job_id, f"echo 'Job {i}'", 1)
                            job_ids.append(job_id)
                            resource_ids.append(resource_id)

            # Verify resources were created
            assert len(mode.resources) == 3
            for resource_id in resource_ids:
                assert resource_id in mode.resources

            # Save state
            mode.save_state()

            # Verify state was saved to file
            assert os.path.exists(file_state_store.file_path)

            # Create a new mode instance
            mode2 = ServerlessMode(
                provider_id=provider_id,
                session=substrate_session,
                state_store=file_state_store,
                region="us-east-1",
                worker_type="lambda",
                lambda_memory=128,
                lambda_timeout=30,
            )

            # Load state
            mode2.load_state()

            # Verify state was loaded correctly
            assert mode2.initialized
            assert len(mode2.resources) == 3

            # Check that resources were loaded correctly
            for resource_id in resource_ids:
                assert resource_id in mode2.resources
                assert mode2.resources[resource_id]["job_id"] in job_ids
                assert mode2.resources[resource_id]["worker_type"] == "lambda"

            # Clean up
            with patch.object(mode2.lambda_manager, "_delete_lambda_function"):
                mode2.cleanup_infrastructure()

            # Verify cleanup logic updated the state
            assert not mode2.initialized
            assert len(mode2.resources) == 0

    @pytest.mark.substrate
    def test_an_interruption_survives_a_restart(
        self, substrate_session, file_state_store
    ):
        """An interrupted block must still read as interrupted after a restart.

        A reclaim is precisely the event most likely to be followed by the driver
        going away, so a marker held only in memory would be lost exactly when it
        matters. If the restored block came back RUNNING, the provider would keep
        waiting on capacity AWS already took, and if it came back COMPLETED --
        which is what ``EC2_STATUS_MAPPING`` makes of a ``shutting-down``
        instance -- its tasks would be silently dropped instead of re-run (#137).

        This used to test a checkpoint/recovery API that #137 deleted: its entry
        point was ``register_task(task_id, instance_id)``, and a Parsl provider is
        never told a task ID. What replaces it is the response that *is*
        implementable at this layer -- mark the block, let Parsl's own ``retries``
        re-run the tasks -- checked across the persistence boundary this file
        exists to cover.
        """
        provider_id = f"test-provider-{uuid.uuid4().hex[:8]}"
        instance_id = f"i-spot-{uuid.uuid4().hex[:12]}"

        def build_mode():
            return StandardMode(
                provider_id=provider_id,
                session=substrate_session,
                state_store=file_state_store,
                region="us-east-1",
                instance_type="t3.micro",
                image_id="ami-12345678",
                vpc_id="vpc-12345",
                subnet_id="subnet-12345",
                security_group_id="sg-12345",
                use_spot=True,
                spot_interruption_handling=True,
            )

        mode = build_mode()
        try:
            mode.resources[instance_id] = {
                "type": "ec2",
                "job_id": f"test-job-{uuid.uuid4().hex[:8]}",
                "status": "RUNNING",
                "is_spot": True,
                "created_at": time.time(),
            }
            # The real registration path, as used by every spot launch.
            mode._register_spot_instance(instance_id)

            # Drive the reclaim through the queue the monitoring thread drains,
            # rather than reaching for the handler directly -- substrate emulates
            # neither EventBridge nor a spot ``InstanceLifecycle``, so this is the
            # only seam that can originate one here.
            mode.spot_interruption_monitor.event_queue.put(
                (
                    "instance",
                    instance_id,
                    {"InstanceId": instance_id, "InstanceAction": "terminate"},
                )
            )
            mode.spot_interruption_monitor._process_interruption_events()

            assert mode.resources[instance_id]["status"] == "INTERRUPTED"
            mode.save_state()
        finally:
            if mode.spot_interruption_monitor:
                mode.spot_interruption_monitor.stop_monitoring()

        mode2 = build_mode()
        try:
            assert mode2.load_state() is True

            assert mode2.resources[instance_id]["status"] == "INTERRUPTED"
            # And the reason came back with it, so a FAILED block is diagnosable
            # without going to CloudTrail.
            assert mode2.resources[instance_id]["interruption_event"] == {
                "InstanceId": instance_id,
                "InstanceAction": "terminate",
            }
            # Still sticky on the far side: get_job_status must not re-derive a
            # healthy-looking status from the instance EC2 is tearing down.
            assert mode2.get_job_status([instance_id])[instance_id] == "INTERRUPTED"
        finally:
            if mode2.spot_interruption_monitor:
                mode2.spot_interruption_monitor.stop_monitoring()

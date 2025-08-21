"""Integration tests for operating modes using LocalStack.

These tests run against LocalStack, which provides a local simulation of AWS services.
They verify that the operating modes can create resources, submit jobs, and clean up
properly in an environment that mimics AWS APIs.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import os
import pytest
import uuid
from unittest.mock import patch

from parsl_ephemeral_aws.modes.standard import StandardMode
from parsl_ephemeral_aws.modes.detached import DetachedMode
from parsl_ephemeral_aws.modes.serverless import ServerlessMode
from parsl_ephemeral_aws.state.file import FileStateStore
from parsl_ephemeral_aws.utils.localstack import (
    is_localstack_available,
    get_localstack_session,
)


@pytest.fixture(scope="session")
def localstack_available():
    """Check if LocalStack is available for testing."""
    if not is_localstack_available():
        pytest.skip("LocalStack is not available. Make sure it's running on port 4566.")
    return True


@pytest.fixture(scope="session")
def localstack_session(localstack_available):
    """Create a session connected to LocalStack."""
    return get_localstack_session()


@pytest.fixture
def temp_state_store(tmpdir):
    """Create a temporary state store for testing."""
    state_file = tmpdir.join("state.json")
    return FileStateStore(file_path=str(state_file))


@pytest.fixture
def provider_id():
    """Generate a unique provider ID for tests."""
    return f"test-provider-{uuid.uuid4().hex[:8]}"


@pytest.mark.integration
class TestStandardModeLocalstack:
    """Integration tests for StandardMode using LocalStack."""

    @pytest.mark.localstack
    def test_initialize_and_cleanup(
        self, localstack_session, temp_state_store, provider_id
    ):
        """Test that StandardMode can initialize resources and clean them up."""
        # Create StandardMode instance
        mode = StandardMode(
            provider_id=provider_id,
            session=localstack_session,
            state_store=temp_state_store,
            region="us-east-1",
            instance_type="t2.micro",
            image_id="ami-12345678",  # Dummy AMI ID for testing
        )

        try:
            # Initialize mode - this should create VPC, subnet, security group
            mode.initialize()

            # Verify resources were created
            assert mode.vpc_id is not None
            assert mode.subnet_id is not None
            assert mode.security_group_id is not None
            assert mode.initialized is True

            # Verify resources exist in LocalStack
            ec2 = localstack_session.client("ec2")
            vpcs = ec2.describe_vpcs(VpcIds=[mode.vpc_id])
            assert len(vpcs["Vpcs"]) == 1

            subnets = ec2.describe_subnets(SubnetIds=[mode.subnet_id])
            assert len(subnets["Subnets"]) == 1

            security_groups = ec2.describe_security_groups(
                GroupIds=[mode.security_group_id]
            )
            assert len(security_groups["SecurityGroups"]) == 1

            # Verify state was saved
            assert os.path.exists(temp_state_store.file_path)

        finally:
            # Clean up resources
            mode.cleanup_infrastructure()

            # Verify resources were cleaned up
            assert mode.vpc_id is None
            assert mode.subnet_id is None
            assert mode.security_group_id is None
            assert mode.initialized is False

    @pytest.mark.localstack
    def test_submit_job_and_status(
        self, localstack_session, temp_state_store, provider_id
    ):
        """Test job submission and status checking."""
        # Create StandardMode instance
        mode = StandardMode(
            provider_id=provider_id,
            session=localstack_session,
            state_store=temp_state_store,
            region="us-east-1",
            instance_type="t2.micro",
            image_id="ami-12345678",  # Dummy AMI ID for testing
        )

        try:
            # Initialize mode
            mode.initialize()

            # Submit a job
            job_id = f"test-job-{uuid.uuid4().hex[:8]}"
            command = "echo hello"
            resource_id = mode.submit_job(job_id, command, 1)

            # Verify job was tracked
            assert resource_id in mode.resources
            assert mode.resources[resource_id]["job_id"] == job_id

            # Check job status
            status = mode.get_job_status([resource_id])
            assert resource_id in status

            # Cancel job
            cancel_status = mode.cancel_jobs([resource_id])
            assert resource_id in cancel_status

            # Cleanup specific resource
            mode.cleanup_resources([resource_id])
            assert resource_id not in mode.resources

        finally:
            # Clean up all resources
            mode.cleanup_infrastructure()


@pytest.mark.integration
class TestDetachedModeLocalstack:
    """Integration tests for DetachedMode using LocalStack."""

    @pytest.mark.localstack
    def test_initialize_and_cleanup(
        self, localstack_session, temp_state_store, provider_id
    ):
        """Test that DetachedMode can initialize resources and clean them up."""
        # Create DetachedMode instance
        mode = DetachedMode(
            provider_id=provider_id,
            session=localstack_session,
            state_store=temp_state_store,
            region="us-east-1",
            instance_type="t2.micro",
            workflow_id=f"test-workflow-{uuid.uuid4().hex[:8]}",
            bastion_instance_type="t2.micro",
            image_id="ami-12345678",  # Dummy AMI ID for testing
            bastion_host_type="direct",  # Use direct mode for simpler testing
        )

        try:
            # Initialize mode
            mode.initialize()

            # Verify resources were created
            assert mode.vpc_id is not None
            assert mode.subnet_id is not None
            assert mode.security_group_id is not None
            assert mode.bastion_id is not None
            assert mode.initialized is True

            # Verify resources exist in LocalStack
            ec2 = localstack_session.client("ec2")
            vpcs = ec2.describe_vpcs(VpcIds=[mode.vpc_id])
            assert len(vpcs["Vpcs"]) == 1

            subnets = ec2.describe_subnets(SubnetIds=[mode.subnet_id])
            assert len(subnets["Subnets"]) == 1

            security_groups = ec2.describe_security_groups(
                GroupIds=[mode.security_group_id]
            )
            assert len(security_groups["SecurityGroups"]) == 1

            # Check bastion instance
            instances = ec2.describe_instances(InstanceIds=[mode.bastion_id])
            assert len(instances["Reservations"]) > 0

        finally:
            # Clean up resources
            # Note: Set preserve_bastion to False to ensure bastion cleanup
            mode.preserve_bastion = False
            mode.cleanup_infrastructure()

            # Verify resources were cleaned up
            assert mode.vpc_id is None
            assert mode.subnet_id is None
            assert mode.security_group_id is None
            assert mode.bastion_id is None
            assert mode.initialized is False

    @pytest.mark.localstack
    def test_submit_job_and_status(
        self, localstack_session, temp_state_store, provider_id
    ):
        """Test job submission and status checking in detached mode."""
        # Create DetachedMode instance
        workflow_id = f"test-workflow-{uuid.uuid4().hex[:8]}"
        mode = DetachedMode(
            provider_id=provider_id,
            session=localstack_session,
            state_store=temp_state_store,
            region="us-east-1",
            instance_type="t2.micro",
            workflow_id=workflow_id,
            bastion_instance_type="t2.micro",
            image_id="ami-12345678",  # Dummy AMI ID for testing
            bastion_host_type="direct",  # Use direct mode for simpler testing
        )

        try:
            # Initialize mode
            mode.initialize()

            # Submit a job
            job_id = f"test-job-{uuid.uuid4().hex[:8]}"
            command = "echo hello"
            resource_id = mode.submit_job(job_id, command, 1)

            # Verify job was tracked
            assert resource_id in mode.resources
            assert mode.resources[resource_id]["job_id"] == job_id

            # Verify SSM parameters were created
            ssm = localstack_session.client("ssm")
            try:
                job_param = ssm.get_parameter(
                    Name=f"/parsl/workflows/{workflow_id}/jobs/{job_id}"
                )
                assert (
                    job_param["Parameter"]["Name"]
                    == f"/parsl/workflows/{workflow_id}/jobs/{job_id}"
                )
            except ssm.exceptions.ParameterNotFound:
                pytest.fail("SSM parameter for job was not created")

            # Check job status
            status = mode.get_job_status([resource_id])
            assert resource_id in status

            # Cancel job
            cancel_status = mode.cancel_jobs([resource_id])
            assert resource_id in cancel_status

            # Cleanup specific resource
            mode.cleanup_resources([resource_id])
            assert resource_id not in mode.resources

        finally:
            # Clean up all resources
            mode.preserve_bastion = False  # Ensure bastion cleanup
            mode.cleanup_infrastructure()


@pytest.mark.integration
class TestServerlessModeLocalstack:
    """Integration tests for ServerlessMode using LocalStack."""

    @pytest.mark.localstack
    def test_initialize_and_cleanup(
        self, localstack_session, temp_state_store, provider_id
    ):
        """Test that ServerlessMode can initialize resources and clean them up."""
        # Create ServerlessMode instance
        mode = ServerlessMode(
            provider_id=provider_id,
            session=localstack_session,
            state_store=temp_state_store,
            region="us-east-1",
            worker_type="lambda",  # Use Lambda for simplicity
            lambda_memory=128,
            lambda_timeout=30,
        )

        try:
            # Initialize mode - serverless Lambda-only mode won't create network resources
            mode.initialize()

            # Verify initialization
            assert mode.initialized is True

            # Lambda mode should not create VPC resources
            assert mode.vpc_id is None
            assert mode.subnet_id is None
            assert mode.security_group_id is None

            # For ECS mode, test with network resources
            mode_ecs = ServerlessMode(
                provider_id=f"{provider_id}-ecs",
                session=localstack_session,
                state_store=temp_state_store,
                region="us-east-1",
                worker_type="ecs",
                ecs_task_cpu=256,
                ecs_task_memory=512,
                ecs_container_image="amazon/amazon-ecs-sample",
            )

            mode_ecs.initialize()
            assert mode_ecs.vpc_id is not None
            assert mode_ecs.subnet_id is not None
            assert mode_ecs.security_group_id is not None

            # Clean up ECS mode resources
            mode_ecs.cleanup_infrastructure()

        finally:
            # Clean up resources
            mode.cleanup_infrastructure()

            # Verify resources were cleaned up
            assert mode.initialized is False

    @pytest.mark.localstack
    def test_submit_lambda_job(self, localstack_session, temp_state_store, provider_id):
        """Test Lambda job submission with CloudFormation stacks."""
        # Create ServerlessMode instance for Lambda
        mode = ServerlessMode(
            provider_id=provider_id,
            session=localstack_session,
            state_store=temp_state_store,
            region="us-east-1",
            worker_type="lambda",
            lambda_memory=128,
            lambda_timeout=30,
        )

        # Mock the Lambda code generation since we can't actually create it in tests
        mock_lambda_manager = mode.lambda_manager
        mock_lambda_manager._generate_lambda_code.return_value = b"mock_lambda_code"

        try:
            # Initialize mode
            mode.initialize()

            # Submit a job
            job_id = f"test-job-{uuid.uuid4().hex[:8]}"
            command = "echo hello"

            # Mock writing to temp file
            with patch("builtins.open", create=True) as mock_open:
                resource_id = mode.submit_job(job_id, command, 1)

            # Verify job was tracked
            assert resource_id in mode.resources
            assert mode.resources[resource_id]["job_id"] == job_id
            assert mode.resources[resource_id]["worker_type"] == "lambda"

            # Check job status
            status = mode.get_job_status([resource_id])
            assert resource_id in status

            # Cancel job
            cancel_status = mode.cancel_jobs([resource_id])
            assert resource_id in cancel_status

            # Cleanup specific resource
            mode.cleanup_resources([resource_id])
            assert resource_id not in mode.resources

        finally:
            # Clean up all resources
            mode.cleanup_infrastructure()

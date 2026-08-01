"""Unit tests for the ServerlessMode class.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import pytest
from unittest.mock import MagicMock, patch
import boto3
import time

from parsl_ephemeral_aws.modes.serverless import ServerlessMode
from parsl_ephemeral_aws.state.base import STATE_KEY_MODE
from parsl_ephemeral_aws.utils.aws import get_cf_template
from parsl_ephemeral_aws.exceptions import (
    JobSubmissionError,
    OperatingModeError,
)
from parsl_ephemeral_aws.constants import (
    RESOURCE_TYPE_LAMBDA_FUNCTION,
    RESOURCE_TYPE_ECS_TASK,
    WORKER_TYPE_LAMBDA,
    WORKER_TYPE_ECS,
    WORKER_TYPE_AUTO,
    STATUS_INTERRUPTED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    STATUS_FAILED,
    STATUS_CANCELLED,
)

pytestmark = pytest.mark.unit


class TestServerlessMode:
    """Tests for the ServerlessMode class."""

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
        client.create_stack.return_value = {"StackId": "stack-12345"}

        # Mock describe_stacks
        client.describe_stacks.return_value = {
            "Stacks": [
                {
                    "StackId": "stack-12345",
                    "StackName": "parsl-lambda-12345",
                    "StackStatus": "CREATE_COMPLETE",
                    "Outputs": [
                        {
                            "OutputKey": "LambdaFunctionName",
                            "OutputValue": "parsl-lambda-function",
                        },
                        {"OutputKey": "ClusterName", "OutputValue": "parsl-cluster"},
                        {"OutputKey": "ServiceName", "OutputValue": "parsl-service"},
                    ],
                }
            ]
        }

        return client

    @pytest.fixture
    def mock_ec2_client(self):
        """Create a mock EC2 client."""
        client = MagicMock()

        # Mock create_vpc
        client.create_vpc.return_value = {"Vpc": {"VpcId": "vpc-12345"}}

        # Mock describe_vpcs
        client.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-12345"}]}

        # Mock create_subnet
        client.create_subnet.return_value = {"Subnet": {"SubnetId": "subnet-12345"}}

        # Mock describe_subnets
        client.describe_subnets.return_value = {
            "Subnets": [{"SubnetId": "subnet-12345"}]
        }

        # Mock create_security_group
        client.create_security_group.return_value = {"GroupId": "sg-12345"}

        # Mock describe_security_groups
        client.describe_security_groups.return_value = {
            "SecurityGroups": [{"GroupId": "sg-12345"}]
        }

        return client

    @pytest.fixture
    def mock_lambda_client(self):
        """Create a mock Lambda client."""
        client = MagicMock()

        # Mock get_function
        client.get_function.return_value = {
            "Configuration": {
                "FunctionName": "parsl-lambda-function",
                "FunctionArn": "arn:aws:lambda:us-east-1:123456789012:function:parsl-lambda-function",
            }
        }

        # Mock invoke
        client.invoke.return_value = {
            "StatusCode": 200,
            "Payload": MagicMock(
                read=lambda: '{"statusCode": 200, "body": "Success"}'.encode()
            ),
        }

        return client

    @pytest.fixture
    def mock_ecs_client(self):
        """Create a mock ECS client."""
        client = MagicMock()

        # Mock describe_services
        client.describe_services.return_value = {
            "services": [
                {
                    "serviceName": "parsl-service",
                    "desiredCount": 1,
                    "runningCount": 1,
                    "deployments": [{"status": "PRIMARY", "id": "deployment-12345"}],
                    "events": [{"message": "service has reached a steady state"}],
                }
            ]
        }

        # Mock list_tasks
        client.list_tasks.return_value = {"taskArns": ["task-arn-12345"]}

        # Mock describe_tasks
        client.describe_tasks.return_value = {
            "tasks": [
                {
                    "taskArn": "task-arn-12345",
                    "lastStatus": "RUNNING",
                    "containers": [{"name": "worker", "lastStatus": "RUNNING"}],
                }
            ]
        }

        return client

    @pytest.fixture
    def serverless_mode(
        self,
        mock_session,
        mock_state_store,
        mock_cf_client,
        mock_ec2_client,
        mock_lambda_client,
        mock_ecs_client,
        mock_s3_client,
    ):
        """Create a ServerlessMode instance with mocked dependencies."""

        # Configure session to return mock clients
        def get_client(service_name, **kwargs):
            if service_name == "cloudformation":
                return mock_cf_client
            elif service_name == "ec2":
                return mock_ec2_client
            elif service_name == "lambda":
                return mock_lambda_client
            elif service_name == "ecs":
                return mock_ecs_client
            elif service_name == "s3":
                # Lambda submits stage their zip here (#116).
                return mock_s3_client
            return MagicMock()

        mock_session.client.side_effect = get_client

        # Create mode instance with auto worker type
        mode = ServerlessMode(
            provider_id="test-provider",
            session=mock_session,
            state_store=mock_state_store,
            worker_type=WORKER_TYPE_AUTO,
            lambda_timeout=300,
            lambda_memory=1024,
            ecs_task_cpu=1024,
            ecs_task_memory=2048,
            region="us-east-1",
            vpc_id="vpc-12345",
            subnet_id="subnet-12345",
            security_group_id="sg-12345",
        )

        # Mock LambdaManager and ECSManager
        mode.lambda_manager = MagicMock()
        mode.lambda_manager._generate_lambda_code.return_value = b"lambda_code_zip"

        mode.ecs_manager = MagicMock()

        return mode

    def test_init(self, serverless_mode):
        """Test initialization of ServerlessMode."""
        assert serverless_mode.provider_id == "test-provider"
        assert serverless_mode.worker_type == WORKER_TYPE_AUTO
        assert serverless_mode.lambda_timeout == 300
        assert serverless_mode.lambda_memory == 1024
        assert serverless_mode.ecs_task_cpu == 1024
        assert serverless_mode.ecs_task_memory == 2048
        assert serverless_mode.region == "us-east-1"
        assert serverless_mode.initialized is False
        assert serverless_mode.resources == {}

    def test_init_with_invalid_worker_type(self, mock_session, mock_state_store):
        """Test that initializing with an invalid worker type raises an error."""
        with pytest.raises(Exception) as excinfo:
            ServerlessMode(
                provider_id="test-provider",
                session=mock_session,
                state_store=mock_state_store,
                worker_type="invalid",
                region="us-east-1",
            )

        assert "worker_type" in str(excinfo.value).lower()

    def test_init_with_predefined_resources(self, mock_session, mock_state_store):
        """Test initialization with predefined VPC, subnet, and security group."""
        mode = ServerlessMode(
            provider_id="test-provider",
            session=mock_session,
            state_store=mock_state_store,
            worker_type=WORKER_TYPE_LAMBDA,
            vpc_id="vpc-12345",
            subnet_id="subnet-12345",
            security_group_id="sg-12345",
            region="us-east-1",
        )

        assert mode.vpc_id == "vpc-12345"
        assert mode.subnet_id == "subnet-12345"
        assert mode.security_group_id == "sg-12345"
        # ECSManager reads subnet_ids in preference to subnet_id
        assert mode.subnet_ids == ["subnet-12345"]

    def test_initialize_lambda_only(self, mock_session, mock_state_store):
        """A Lambda-only mode needs no network IDs at all.

        Functions run in the Lambda-managed VPC — ``create_function`` is passed no
        ``VpcConfig`` — so the base class's network requirement is waived for this
        worker type. Constructing with none of the three IDs is the case that
        proves it; the previous version of this test set ``worker_type`` on the
        fixture (which supplies IDs) and then asserted ``vpc_id is None``, an
        assertion that only made sense when Lambda-only meant "no VPC was
        created" rather than "no VPC is needed".
        """
        mode = ServerlessMode(
            provider_id="test-provider",
            session=mock_session,
            state_store=mock_state_store,
            worker_type=WORKER_TYPE_LAMBDA,
            region="us-east-1",
        )
        mode.initialize()

        assert mode.initialized is True

        # Only the Lambda manager, and no network verification was attempted.
        assert mode.lambda_manager is not None
        assert mode.ecs_manager is None
        mock_session.client("ec2").describe_vpcs.assert_not_called()

    def test_initialize_ecs(self, serverless_mode, mock_ec2_client, mock_cf_client):
        """Test initialize method creates resources for ECS."""
        # Configure for ECS
        serverless_mode.worker_type = WORKER_TYPE_ECS

        # Call initialize
        serverless_mode.initialize()

        # Verify VPC resources were created
        assert serverless_mode.vpc_id == "vpc-12345"
        assert serverless_mode.subnet_id == "subnet-12345"
        assert serverless_mode.security_group_id == "sg-12345"
        assert serverless_mode.initialized is True

        # Verify ECS manager was initialized
        assert serverless_mode.ecs_manager is not None

        # Verify state was saved
        serverless_mode.state_store.save_state.assert_called()

    def test_the_mode_creates_no_network_resources(self, serverless_mode):
        """The mode must not be able to create a VPC, subnet, or security group.

        This replaces ``test_create_vpc_with_cloudformation``. #69 made all three
        network IDs caller-supplied and removed the creation helpers, so a test
        calling ``_create_vpc()`` is asserting behaviour that must not come back:
        creating a VPC the caller did not ask for is precisely what #69 removed,
        and #70 was the matching hazard of deleting a security group the caller
        owns.
        """
        for removed in ("_create_vpc", "_create_subnet", "_create_security_group"):
            assert not hasattr(serverless_mode, removed), (
                f"{removed} came back; the mode must use caller-supplied network IDs"
            )

    def test_worker_type_selection(self, serverless_mode):
        """Test worker type selection logic."""
        # Short command should use Lambda
        short_cmd = "echo hello"
        assert serverless_mode._select_worker_type(short_cmd, 1) == WORKER_TYPE_LAMBDA

        # Long command should use ECS
        long_cmd = "x" * 6000  # Over 5000 chars
        assert serverless_mode._select_worker_type(long_cmd, 1) == WORKER_TYPE_ECS

        # Multiple tasks should use ECS
        assert serverless_mode._select_worker_type(short_cmd, 4) == WORKER_TYPE_ECS

    def test_submit_job_lambda(self, serverless_mode, mock_cf_client, mock_s3_client):
        """Test job submission via Lambda.

        No tempfile patching: #116 replaced the local-file-and-unlink dance with
        an S3-staged zip referenced by ``CodeS3Bucket``/``CodeS3Key``, because the
        archive bytes cannot travel through a CloudFormation string parameter.
        The old ``mock_unlink.assert_called_once()`` was asserting a code path
        that no longer exists.
        """
        # Configure for Lambda
        serverless_mode.worker_type = WORKER_TYPE_LAMBDA
        serverless_mode.initialized = True

        resource_id = serverless_mode.submit_job("job-1", "echo hello", 1)

        # Verify CloudFormation stack was created
        mock_cf_client.create_stack.assert_called_once()
        args, kwargs = mock_cf_client.create_stack.call_args
        assert kwargs["StackName"].startswith("parsl-lambda-")

        # The right one of the two templates was deployed. That the loader is
        # what produced it is asserted separately, in
        # ``test_templates_are_deployed_through_the_package_loader`` -- here both
        # a packaged read and a ``__file__``-relative one yield the same bytes.
        assert kwargs["TemplateBody"] == get_cf_template("lambda_worker.yml")

        # The function code is staged in S3, and the stack points at it rather
        # than carrying it inline.
        mock_s3_client.put_object.assert_called_once()
        put_kwargs = mock_s3_client.put_object.call_args[1]
        assert put_kwargs["Body"] == b"lambda_code_zip"
        parameters = {
            p["ParameterKey"]: p["ParameterValue"] for p in kwargs["Parameters"]
        }
        assert parameters["CodeS3Bucket"] == put_kwargs["Bucket"]
        assert parameters["CodeS3Key"] == put_kwargs["Key"]

        # Verify resource tracking
        assert resource_id in serverless_mode.resources
        assert serverless_mode.resources[resource_id]["job_id"] == "job-1"
        assert (
            serverless_mode.resources[resource_id]["worker_type"] == WORKER_TYPE_LAMBDA
        )
        assert serverless_mode.resources[resource_id]["status"] == STATUS_PENDING

        # Verify state was saved
        serverless_mode.state_store.save_state.assert_called()

    def test_submit_job_ecs(self, serverless_mode, mock_cf_client):
        """Test job submission via ECS.

        The template is loaded for real. This used to patch ``os.path.join``
        *globally* and stub ``builtins.open`` to feed in "template content",
        because the mode built the template path by hand. It now goes through
        ``get_cf_template()`` (#113), so the packaged ``ecs_worker.yml`` is what
        gets deployed — and patching every path join in the interpreter was
        never safe.
        """
        # Guard the premise: if the two templates were interchangeable, the
        # TemplateBody assertion below would pass for either one.
        assert get_cf_template("ecs_worker.yml") != get_cf_template("lambda_worker.yml")

        # Configure for ECS
        serverless_mode.worker_type = WORKER_TYPE_ECS
        serverless_mode.initialized = True
        serverless_mode.vpc_id = "vpc-12345"
        serverless_mode.subnet_id = "subnet-12345"
        serverless_mode.security_group_id = "sg-12345"

        resource_id = serverless_mode.submit_job("job-1", "echo hello", 2)

        # Verify CloudFormation stack was created
        mock_cf_client.create_stack.assert_called_once()
        args, kwargs = mock_cf_client.create_stack.call_args
        assert kwargs["StackName"].startswith("parsl-ecs-")

        # The right one of the two templates was deployed; see
        # ``test_templates_are_deployed_through_the_package_loader`` for the
        # assertion that the loader is what produced it.
        assert kwargs["TemplateBody"] == get_cf_template("ecs_worker.yml")

        # Verify stack parameters
        params = {p["ParameterKey"]: p["ParameterValue"] for p in kwargs["Parameters"]}
        assert params["VpcId"] == "vpc-12345"
        assert params["SubnetIds"] == "subnet-12345"
        assert params["SecurityGroupIds"] == "sg-12345"
        assert params["TaskCount"] == "2"  # tasks_per_node

        # Verify resource tracking
        assert resource_id in serverless_mode.resources
        assert serverless_mode.resources[resource_id]["job_id"] == "job-1"
        assert serverless_mode.resources[resource_id]["worker_type"] == WORKER_TYPE_ECS
        assert serverless_mode.resources[resource_id]["status"] == STATUS_PENDING

    @pytest.mark.parametrize(
        ("worker_type", "template_name"),
        [
            (WORKER_TYPE_LAMBDA, "lambda_worker.yml"),
            (WORKER_TYPE_ECS, "ecs_worker.yml"),
        ],
    )
    def test_templates_are_deployed_through_the_package_loader(
        self, serverless_mode, mock_cf_client, worker_type, template_name
    ):
        """Both submit paths obtain their template from ``get_cf_template()``.

        Comparing ``TemplateBody`` to the loader's output cannot establish this on
        its own: in a source checkout, reading the file by a path built from
        ``__file__`` -- which is what both paths did before #113 -- produces
        identical bytes. That equivalence is exactly why #112 stayed invisible in
        development and only failed from an installed wheel, where the
        ``__file__``-relative path resolves to a file that is not there. So this
        asserts on the *call*, which is the part that differs.
        """
        serverless_mode.worker_type = worker_type
        serverless_mode.initialized = True

        with patch(
            "parsl_ephemeral_aws.modes.serverless.get_cf_template",
            wraps=get_cf_template,
        ) as mock_loader:
            serverless_mode.submit_job("job-1", "echo hello", 1)

        mock_loader.assert_called_once_with(template_name)
        # ``wraps`` means the real loader still ran, so the body is genuine
        # rather than a sentinel that would pass regardless of what was read.
        assert mock_cf_client.create_stack.call_args[1][
            "TemplateBody"
        ] == get_cf_template(template_name)

    def test_submit_job_initializes_lazily(self, serverless_mode):
        """An un-initialized mode initializes itself rather than refusing.

        This replaces a test that asserted ``initialized = False`` makes
        ``submit_job`` raise. It never could: ``submit_job`` opens with
        ``ensure_initialized()`` (``modes/base.py:325``), whose whole purpose is
        to initialize on demand. The old test only ever passed because the
        fixture raised before reaching the assertion.
        """
        serverless_mode.worker_type = WORKER_TYPE_LAMBDA
        serverless_mode.initialized = False

        resource_id = serverless_mode.submit_job("job-1", "echo hello", 1)

        assert serverless_mode.initialized is True
        assert resource_id in serverless_mode.resources

    def test_submit_job_ecs_missing_network(self, serverless_mode):
        """Test ECS submission fails when network resources are missing.

        ``OperatingModeError``, not ``JobSubmissionError``: ``submit_job`` wraps
        every failure, which is the contract ``modes/base.py`` documents and all
        three modes implement. The internal ``JobSubmissionError`` is the
        ``__cause__``.
        """
        # Configure for ECS but without network resources
        serverless_mode.worker_type = WORKER_TYPE_ECS
        serverless_mode.initialized = True
        serverless_mode.vpc_id = None

        with pytest.raises(OperatingModeError) as exc_info:
            serverless_mode.submit_job("job-1", "echo hello", 1)

        assert isinstance(exc_info.value.__cause__, JobSubmissionError)
        assert "Missing required network resources" in str(exc_info.value)

        # The failure path must not leave a phantom tracking record behind, or
        # the provider would poll a job that was never dispatched (#115).
        assert serverless_mode.resources == {}

    def test_get_job_status_lambda(self, serverless_mode, mock_cf_client):
        """Test getting job status for Lambda jobs."""
        # Setup Lambda job resources
        resource_id = "serverless-lambda-job-1"
        job_id = "job-1"
        stack_name = "parsl-lambda-12345"
        serverless_mode.resources = {
            resource_id: {
                "job_id": job_id,
                "worker_type": WORKER_TYPE_LAMBDA,
                "stack_name": stack_name,
                "status": STATUS_PENDING,
                "created_at": time.time() - 10,  # Created 10 seconds ago
                "resource_type": RESOURCE_TYPE_LAMBDA_FUNCTION,
            }
        }

        # Get status
        status = serverless_mode.get_job_status([resource_id])

        # Verify CF client was called
        mock_cf_client.describe_stacks.assert_called_with(StackName=stack_name)

        # Verify status result - should be RUNNING since we mocked CREATE_COMPLETE stack
        assert status[resource_id] == STATUS_RUNNING

        # Verify resource was updated
        assert serverless_mode.resources[resource_id]["status"] == STATUS_RUNNING

    def test_get_job_status_ecs(self, serverless_mode, mock_cf_client, mock_ecs_client):
        """Test getting job status for ECS jobs."""
        # Setup ECS job resources
        resource_id = "serverless-ecs-job-1"
        job_id = "job-1"
        stack_name = "parsl-ecs-12345"
        serverless_mode.resources = {
            resource_id: {
                "job_id": job_id,
                "worker_type": WORKER_TYPE_ECS,
                "stack_name": stack_name,
                "status": STATUS_PENDING,
                "created_at": time.time() - 60,
                "resource_type": RESOURCE_TYPE_ECS_TASK,
            }
        }

        # Get status
        status = serverless_mode.get_job_status([resource_id])

        # Verify CF and ECS client calls
        mock_cf_client.describe_stacks.assert_called_with(StackName=stack_name)
        mock_ecs_client.describe_services.assert_called()

        # Verify status result - should be running since we mocked running task
        assert status[resource_id] == STATUS_RUNNING

        # Verify resource was updated
        assert serverless_mode.resources[resource_id]["status"] == STATUS_RUNNING

    def test_get_job_status_keeps_an_interruption(
        self, serverless_mode, mock_cf_client
    ):
        """A reclaim marked by the monitor survives the next poll (#137).

        Neither route below can see a reclaim: a fleet whose instances AWS is
        taking back still reports itself active, and the CloudFormation stack
        stays CREATE_COMPLETE. Re-deriving would overwrite the marker with a
        healthy-looking RUNNING on the very next poll.
        """
        resource_id = "serverless-lambda-job-1"
        serverless_mode.resources = {
            resource_id: {
                "job_id": "job-1",
                "worker_type": WORKER_TYPE_LAMBDA,
                "stack_name": "parsl-lambda-12345",
                "status": STATUS_INTERRUPTED,
                "created_at": time.time() - 10,
                "resource_type": RESOURCE_TYPE_LAMBDA_FUNCTION,
            }
        }

        status = serverless_mode.get_job_status([resource_id])

        assert status[resource_id] == STATUS_INTERRUPTED
        assert serverless_mode.resources[resource_id]["status"] == STATUS_INTERRUPTED
        mock_cf_client.describe_stacks.assert_not_called()

    def test_get_lambda_status(self, serverless_mode):
        """Test Lambda job status calculation."""
        resource_id = "serverless-lambda-job-1"

        # Test pending status (very recent job)
        serverless_mode.resources = {
            resource_id: {
                "created_at": time.time() - 2  # 2 seconds ago
            }
        }
        assert (
            serverless_mode._get_lambda_status("function-name", resource_id)
            == STATUS_PENDING
        )

        # Test running status (job in progress)
        serverless_mode.resources = {
            resource_id: {
                "created_at": time.time() - 30  # 30 seconds ago
            }
        }
        assert (
            serverless_mode._get_lambda_status("function-name", resource_id)
            == STATUS_RUNNING
        )

        # Test succeeded status (job completed)
        serverless_mode.resources = {
            resource_id: {
                "created_at": time.time()
                - 600  # 10 minutes ago (longer than lambda_timeout)
            }
        }
        assert (
            serverless_mode._get_lambda_status("function-name", resource_id)
            == STATUS_SUCCEEDED
        )

    def test_get_ecs_status(self, serverless_mode, mock_ecs_client):
        """Test ECS job status calculation."""
        # Test with running tasks
        status = serverless_mode._get_ecs_status("parsl-cluster", "parsl-service")
        assert status == STATUS_RUNNING

        # Test with no tasks but steady state message.
        #
        # `deployments` is present because ECS always returns one for an ACTIVE
        # service, and the no-tasks branch short-circuits to COMPLETED when the
        # list is empty -- so omitting it (as this test used to) skips the event
        # scan entirely and asserts nothing about it.
        mock_ecs_client.list_tasks.return_value = {"taskArns": []}
        mock_ecs_client.describe_services.return_value = {
            "services": [
                {
                    "serviceName": "parsl-service",
                    "desiredCount": 0,
                    "runningCount": 0,
                    "deployments": [{"status": "PRIMARY", "id": "deployment-12345"}],
                    "events": [{"message": "service has reached a steady state"}],
                }
            ]
        }
        status = serverless_mode._get_ecs_status("parsl-cluster", "parsl-service")
        assert status == STATUS_SUCCEEDED

        # Test with failure message
        mock_ecs_client.describe_services.return_value = {
            "services": [
                {
                    "serviceName": "parsl-service",
                    "desiredCount": 1,
                    "runningCount": 0,
                    "deployments": [{"status": "PRIMARY", "id": "deployment-12345"}],
                    "events": [{"message": "was unable to place a task"}],
                }
            ]
        }
        status = serverless_mode._get_ecs_status("parsl-cluster", "parsl-service")
        assert status == STATUS_FAILED

    def test_cancel_jobs(self, serverless_mode, mock_cf_client):
        """Test canceling jobs by deleting CloudFormation stacks."""
        # Setup resources
        resource_id1 = "serverless-lambda-job-1"
        resource_id2 = "serverless-ecs-job-2"
        serverless_mode.resources = {
            resource_id1: {
                "job_id": "job-1",
                "worker_type": WORKER_TYPE_LAMBDA,
                "stack_name": "parsl-lambda-job1",
                "status": STATUS_RUNNING,
            },
            resource_id2: {
                "job_id": "job-2",
                "worker_type": WORKER_TYPE_ECS,
                "stack_name": "parsl-ecs-job2",
                "status": STATUS_RUNNING,
            },
        }

        # Cancel jobs
        status = serverless_mode.cancel_jobs([resource_id1, resource_id2])

        # Verify CF delete_stack was called for both stacks
        assert mock_cf_client.delete_stack.call_count == 2

        # Verify status results
        assert status[resource_id1] == STATUS_CANCELLED
        assert status[resource_id2] == STATUS_CANCELLED

        # Verify resources were updated
        assert serverless_mode.resources[resource_id1]["status"] == STATUS_CANCELLED
        assert serverless_mode.resources[resource_id2]["status"] == STATUS_CANCELLED

    def test_cleanup_resources(self, serverless_mode, mock_cf_client):
        """Test resource cleanup."""
        # Setup resources
        resource_id1 = "serverless-lambda-job-1"
        resource_id2 = "serverless-ecs-job-2"
        serverless_mode.resources = {
            resource_id1: {
                "job_id": "job-1",
                "worker_type": WORKER_TYPE_LAMBDA,
                "stack_name": "parsl-lambda-job1",
                "status": STATUS_RUNNING,
            },
            resource_id2: {
                "job_id": "job-2",
                "worker_type": WORKER_TYPE_ECS,
                "stack_name": "parsl-ecs-job2",
                "status": STATUS_RUNNING,
            },
        }

        # Clean up one resource
        serverless_mode.cleanup_resources([resource_id1])

        # Verify CF delete_stack was called for first stack
        mock_cf_client.delete_stack.assert_called_once_with(
            StackName="parsl-lambda-job1"
        )

        # Verify resource was removed from tracking
        assert resource_id1 not in serverless_mode.resources
        assert resource_id2 in serverless_mode.resources

        # Verify state was saved
        serverless_mode.state_store.save_state.assert_called()

    @patch("time.sleep")
    def test_cleanup_infrastructure(
        self, mock_sleep, serverless_mode, mock_cf_client, mock_ec2_client
    ):
        """Cleanup removes this mode's own resources and spares the caller's network.

        The previous version of this test asserted the opposite: that
        ``cleanup_infrastructure()`` deletes a ``parsl-vpc-{id[:8]}`` stack and
        nulls all three network IDs. #69 made the network caller-supplied and #70
        removed that deletion — it was an unguarded ``delete_security_group`` plus
        a ``delete_stack`` on an 8-character-truncated name, reachable from the
        ``except`` handler in ``initialize()``.
        """
        # Setup infrastructure resources
        serverless_mode.vpc_id = "vpc-12345"
        serverless_mode.subnet_id = "subnet-12345"
        serverless_mode.security_group_id = "sg-12345"

        # Add resources to be cleaned up
        serverless_mode.resources = {
            "resource-1": {
                "job_id": "job-1",
                "worker_type": WORKER_TYPE_LAMBDA,
                "stack_name": "parsl-lambda-job1",
                "status": STATUS_RUNNING,
            }
        }

        # Call cleanup
        serverless_mode.cleanup_infrastructure()

        # This mode's own resources are gone, and the managers were asked to
        # release theirs.
        assert not serverless_mode.resources
        mock_cf_client.delete_stack.assert_any_call(StackName="parsl-lambda-job1")
        serverless_mode.lambda_manager.cleanup_all_resources.assert_called_once()
        serverless_mode.ecs_manager.cleanup_all_resources.assert_called_once()

        # The caller's network is untouched: not deleted, and still configured so
        # a subsequent initialize() can reuse it.
        mock_ec2_client.delete_security_group.assert_not_called()
        mock_ec2_client.delete_subnet.assert_not_called()
        mock_ec2_client.delete_vpc.assert_not_called()
        for call in mock_cf_client.delete_stack.call_args_list:
            assert not call[1]["StackName"].startswith("parsl-vpc-")
        assert serverless_mode.vpc_id == "vpc-12345"
        assert serverless_mode.subnet_id == "subnet-12345"
        assert serverless_mode.security_group_id == "sg-12345"

        assert serverless_mode.initialized is False

    def test_list_resources(self, serverless_mode):
        """Test listing resources."""
        # Setup resources
        serverless_mode.vpc_id = "vpc-12345"
        serverless_mode.subnet_id = "subnet-12345"
        serverless_mode.security_group_id = "sg-12345"

        lambda_id = "serverless-lambda-job-1"
        ecs_id = "serverless-ecs-job-2"
        serverless_mode.resources = {
            lambda_id: {
                "job_id": "job-1",
                "worker_type": WORKER_TYPE_LAMBDA,
                "stack_name": "parsl-lambda-job1",
                "status": STATUS_RUNNING,
                "created_at": time.time(),
            },
            ecs_id: {
                "job_id": "job-2",
                "worker_type": WORKER_TYPE_ECS,
                "stack_name": "parsl-ecs-job2",
                "status": STATUS_SUCCEEDED,
                "created_at": time.time(),
            },
        }

        # List resources
        resources = serverless_mode.list_resources()

        # Verify resource categories
        assert "lambda_functions" in resources
        assert "ecs_tasks" in resources
        assert "vpc" in resources
        assert "subnet" in resources
        assert "security_group" in resources

        # Verify counts
        assert len(resources["lambda_functions"]) == 1
        assert len(resources["ecs_tasks"]) == 1
        assert len(resources["vpc"]) == 1
        assert len(resources["subnet"]) == 1
        assert len(resources["security_group"]) == 1

        # Verify resource details
        assert resources["vpc"][0]["id"] == "vpc-12345"
        assert resources["subnet"][0]["id"] == "subnet-12345"
        assert resources["security_group"][0]["id"] == "sg-12345"

        # Verify Lambda resource
        assert resources["lambda_functions"][0]["id"] == lambda_id
        assert resources["lambda_functions"][0]["job_id"] == "job-1"
        assert resources["lambda_functions"][0]["status"] == STATUS_RUNNING

        # Verify ECS resource
        assert resources["ecs_tasks"][0]["id"] == ecs_id
        assert resources["ecs_tasks"][0]["job_id"] == "job-2"
        assert resources["ecs_tasks"][0]["status"] == STATUS_SUCCEEDED

    def test_load_state(self, serverless_mode, mock_state_store):
        """Test loading state."""
        # Setup mock state
        mock_state = {
            "resources": {
                "serverless-lambda-job-1": {
                    "job_id": "job-1",
                    "worker_type": WORKER_TYPE_LAMBDA,
                    "stack_name": "parsl-lambda-job1",
                    "status": STATUS_RUNNING,
                }
            },
            "provider_id": "test-provider",
            "mode": "ServerlessMode",
            "vpc_id": "vpc-12345",
            "subnet_id": "subnet-12345",
            "security_group_id": "sg-12345",
            "initialized": True,
            # A stale routing decision, to prove the constructor's value wins.
            "worker_type": WORKER_TYPE_ECS,
        }
        mock_state_store.load_state.return_value = mock_state

        # Load state
        result = serverless_mode.load_state()

        # Verify state was loaded
        assert result is True
        assert serverless_mode.resources == mock_state["resources"]
        assert serverless_mode.vpc_id == mock_state["vpc_id"]
        assert serverless_mode.subnet_id == mock_state["subnet_id"]
        assert serverless_mode.security_group_id == mock_state["security_group_id"]
        assert serverless_mode.initialized == mock_state["initialized"]

        # Configuration is not restored from state. The provider re-supplies
        # compute_type on every construction, so a saved value would either agree
        # (no-op) or silently override the caller's current choice. The old
        # assertion here read `== mock_state["worker_type"]` and passed only
        # because both sides happened to be AUTO (#118).
        assert serverless_mode.worker_type == WORKER_TYPE_AUTO

    def test_save_state(self, serverless_mode, mock_state_store):
        """Test saving state."""
        # Setup state
        serverless_mode.vpc_id = "vpc-12345"
        serverless_mode.subnet_id = "subnet-12345"
        serverless_mode.security_group_id = "sg-12345"
        serverless_mode.initialized = True
        serverless_mode.worker_type = WORKER_TYPE_AUTO
        serverless_mode.resources = {
            "serverless-lambda-job-1": {
                "job_id": "job-1",
                "worker_type": WORKER_TYPE_LAMBDA,
                "stack_name": "parsl-lambda-job1",
                "status": STATUS_RUNNING,
            }
        }

        # Save state
        serverless_mode.save_state()

        # Verify state_store.save_state was called
        mock_state_store.save_state.assert_called_once()

        # The mode writes its own state key, so the provider's document survives
        state_key, state = mock_state_store.save_state.call_args[0]
        assert state_key == STATE_KEY_MODE

        # Verify state content
        assert state["provider_id"] == "test-provider"
        assert state["mode"] == "ServerlessMode"
        assert state["vpc_id"] == "vpc-12345"
        assert state["subnet_id"] == "subnet-12345"
        assert state["security_group_id"] == "sg-12345"
        assert state["initialized"] is True
        assert state["resources"] == serverless_mode.resources
        assert state["worker_type"] == WORKER_TYPE_AUTO

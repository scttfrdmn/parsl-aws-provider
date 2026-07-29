"""Unit tests for error handling in critical components.

These tests verify that exceptions are properly raised, caught, and handled
throughout the codebase, ensuring robust error handling and reporting.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import pytest
import uuid
from unittest.mock import MagicMock, patch
from botocore.exceptions import ClientError, NoCredentialsError

from parsl_ephemeral_aws.provider import EphemeralAWSProvider
from parsl_ephemeral_aws.compute.ec2 import EC2Manager
from parsl_ephemeral_aws.modes.standard import StandardMode
from parsl_ephemeral_aws.modes.detached import DetachedMode
from parsl_ephemeral_aws.modes.serverless import ServerlessMode
from parsl_ephemeral_aws.compute.spot_fleet import SpotFleetManager
from parsl_ephemeral_aws.compute.lambda_func import LambdaManager
from parsl_ephemeral_aws.compute.ecs import ECSManager
from parsl_ephemeral_aws.constants import STATUS_CANCELLED
from parsl_ephemeral_aws.exceptions import (
    AWSAuthenticationError,
    AWSConnectionError,
    JobSubmissionError,
    ResourceCreationError,
    ProviderConfigurationError,
    NetworkCreationError,
    SpotFleetError,
    SpotFleetRequestError,
    SpotFleetThrottlingError,
    LambdaFunctionError,
    CloudFormationError,
    BastionHostError,
)


def _mock_provider(**overrides):
    """Build a mock provider that satisfies every compute manager's ``__init__``.

    All four managers are ``__init__(self, provider)`` and read a fixed set of
    attributes off it -- ``workflow_id``/``region`` for audit events, the four
    ``aws_*`` credential fields, and the security fields that
    ``SecurityConfig``/``CredentialConfig`` require. A bare ``MagicMock`` is not
    enough: those two read the attributes as *values*, so a MagicMock where a
    CIDR string or a bool belongs raises during construction.
    """
    provider = MagicMock()
    provider.workflow_id = "test-workflow"
    provider.region = "us-east-1"
    provider.aws_access_key_id = None
    provider.aws_secret_access_key = None
    provider.aws_session_token = None
    provider.aws_profile = None
    # Read as values by SecurityConfig / CredentialConfig, not just passed along.
    provider.security_config = None
    provider.security_environment = "dev"
    provider.vpc_cidr = "10.0.0.0/16"
    provider.admin_cidr_blocks = None
    provider.strict_security_mode = None
    provider.role_arn = None
    provider.vpc_id = "vpc-12345"
    provider.subnet_id = "subnet-12345"
    provider.security_group_id = "sg-12345"
    provider.subnet_ids = ["subnet-12345"]
    provider.use_spot_instances = False
    provider.nodes_per_block = 1
    provider.tags = {}
    for name, value in overrides.items():
        setattr(provider, name, value)
    return provider


def _make_manager(manager_cls, module, **provider_overrides):
    """Construct *manager_cls* against a mocked session, returning it.

    Patches ``CredentialManager`` in the manager's own module so no real
    credential resolution or boto3 session is attempted; every ``client()`` and
    ``resource()`` call on the session returns the same MagicMock, so the tests
    can set ``side_effect`` on whichever client attribute they name.
    """
    provider = _mock_provider(**provider_overrides)
    client = MagicMock()
    session = MagicMock()
    session.client.return_value = client
    session.resource.return_value = MagicMock()
    session.region_name = "us-east-1"

    with patch(f"parsl_ephemeral_aws.compute.{module}.CredentialManager") as mock_cm:
        mock_cm.return_value.create_boto3_session.return_value = session
        return manager_cls(provider=provider)


class TestAWSConnectionErrors:
    """Tests for AWS connection and authentication errors."""

    @pytest.fixture
    def provider_config(self):
        """Create a basic provider configuration."""
        return {
            "region": "us-east-1",
            "instance_type": "t3.micro",
            "image_id": "ami-12345678",
            "max_blocks": 1,
        }

    def test_no_credentials_error(self, provider_config):
        """Test handling of missing AWS credentials."""
        # Simulate boto3 raising NoCredentialsError
        with patch("boto3.Session", side_effect=NoCredentialsError()):
            with pytest.raises(AWSAuthenticationError):
                provider = EphemeralAWSProvider(**provider_config)

    def test_invalid_credentials_error(self, provider_config):
        """Test handling of invalid AWS credentials."""
        # Simulate boto3 client raising unauthorized error
        mock_session = MagicMock()
        error_response = {
            "Error": {
                "Code": "AuthFailure",
                "Message": "AWS was not able to validate the provided credentials",
            }
        }
        mock_session.client.side_effect = ClientError(error_response, "AssumeRole")

        with patch("boto3.Session", return_value=mock_session):
            with pytest.raises(AWSAuthenticationError):
                provider = EphemeralAWSProvider(**provider_config)

    def test_service_unavailable_error(self, provider_config):
        """Test handling of AWS service unavailability."""
        # Simulate boto3 client raising service unavailable error
        mock_session = MagicMock()
        error_response = {
            "Error": {
                "Code": "ServiceUnavailable",
                "Message": "Service is currently unavailable",
            }
        }
        mock_session.client.side_effect = ClientError(
            error_response, "DescribeInstances"
        )

        with patch("boto3.Session", return_value=mock_session):
            provider = EphemeralAWSProvider(**provider_config)
            with pytest.raises(AWSConnectionError):
                # Try to use the provider
                with patch.object(provider, "_initialize_operating_mode"):
                    provider.status([])

    def test_throttling_error(self, provider_config):
        """Test handling of AWS API throttling."""
        # Simulate boto3 client raising throttling error
        mock_session = MagicMock()
        mock_client = MagicMock()
        throttle_error = {
            "Error": {
                "Code": "RequestLimitExceeded",
                "Message": "Request limit exceeded",
            }
        }
        mock_client.describe_instances.side_effect = ClientError(
            throttle_error, "DescribeInstances"
        )
        mock_session.client.return_value = mock_client

        with patch("boto3.Session", return_value=mock_session):
            provider = EphemeralAWSProvider(**provider_config)
            # Initialize with our mock session
            provider._session = mock_session

            # The provider should handle the throttling error (log it, potentially retry, but not crash)
            with patch.object(provider, "_initialize_operating_mode"):
                with pytest.raises(AWSConnectionError):
                    provider.status([])


class TestModeInitializationErrors:
    """Tests for errors during operating mode initialization."""

    @pytest.fixture
    def mock_provider(self):
        """Create a mock provider."""
        provider = MagicMock()
        provider.region = "us-east-1"
        provider.aws_access_key_id = None
        provider.aws_secret_access_key = None
        provider.aws_session_token = None
        provider.aws_profile = None
        provider.max_blocks = 1
        return provider

    @pytest.fixture
    def mock_session(self):
        """Create a mock boto3 session."""
        session = MagicMock()
        return session

    def test_standard_mode_vpc_creation_error(self, mock_provider, mock_session):
        """Test error handling during VPC creation in StandardMode."""
        mock_ec2_client = MagicMock()
        mock_session.client.return_value = mock_ec2_client

        # Simulate error creating VPC
        error_response = {
            "Error": {
                "Code": "VpcLimitExceeded",
                "Message": "The maximum number of VPCs has been reached",
            }
        }
        mock_ec2_client.create_vpc.side_effect = ClientError(
            error_response, "CreateVpc"
        )

        mode = StandardMode(
            provider_id=str(uuid.uuid4()),
            session=mock_session,
            state_store=MagicMock(),
            region="us-east-1",
            instance_type="t3.micro",
            image_id="ami-12345678",
        )

        with pytest.raises(NetworkCreationError):
            mode.initialize()

    def test_detached_mode_bastion_error(self, mock_provider, mock_session):
        """Test error handling during bastion host creation in DetachedMode."""
        mock_ec2_client = MagicMock()
        mock_session.client.return_value = mock_ec2_client

        # Mock successful VPC and subnet creation
        mock_ec2_client.create_vpc.return_value = {"Vpc": {"VpcId": "vpc-12345"}}
        mock_ec2_client.create_subnet.return_value = {
            "Subnet": {"SubnetId": "subnet-12345"}
        }
        mock_ec2_client.create_security_group.return_value = {"GroupId": "sg-12345"}

        # Simulate error creating bastion instance
        error_response = {
            "Error": {
                "Code": "InsufficientInstanceCapacity",
                "Message": "Insufficient capacity",
            }
        }
        mock_ec2_client.run_instances.side_effect = ClientError(
            error_response, "RunInstances"
        )

        workflow_id = f"test-workflow-{uuid.uuid4().hex[:8]}"
        mode = DetachedMode(
            provider_id=str(uuid.uuid4()),
            session=mock_session,
            state_store=MagicMock(),
            region="us-east-1",
            instance_type="t3.micro",
            image_id="ami-12345678",
            workflow_id=workflow_id,
            bastion_instance_type="t3.micro",
            bastion_host_type="direct",
        )

        with pytest.raises(BastionHostError):
            # Mock tag creation to avoid errors
            with patch.object(mode, "_create_tags"):
                mode.initialize()

    def test_serverless_mode_lambda_error(self, mock_provider, mock_session):
        """Test error handling during Lambda function creation in ServerlessMode."""
        mock_lambda_client = MagicMock()
        mock_session.client.return_value = mock_lambda_client

        # Simulate error creating Lambda function
        error_response = {
            "Error": {
                "Code": "ResourceConflictException",
                "Message": "Function already exists",
            }
        }
        mock_lambda_client.create_function.side_effect = ClientError(
            error_response, "CreateFunction"
        )

        mode = ServerlessMode(
            provider_id=str(uuid.uuid4()),
            session=mock_session,
            state_store=MagicMock(),
            region="us-east-1",
            worker_type="lambda",
            lambda_memory=128,
            lambda_timeout=30,
        )

        # Mock Lambda manager to simulate error
        mode.lambda_manager = MagicMock()
        mode.lambda_manager._create_lambda_function.side_effect = LambdaFunctionError(
            "Failed to create Lambda function"
        )

        with pytest.raises(LambdaFunctionError):
            mode.initialize()


class TestEC2ManagerErrors:
    """Tests for error handling in EC2Manager.

    These exercise ``create_blocks``/``terminate_instance``/``get_instance_status``
    -- the manager's actual public surface. The suite previously called
    ``create_instance(image_id=..., min_count=..., ...)``, which no commit in the
    project's history has ever defined; the tests could only ever have raised
    ``AttributeError``, and did so from setup once the fixture was repaired.
    """

    @pytest.fixture
    def ec2_manager(self):
        """Create an EC2Manager with mock session."""
        return _make_manager(EC2Manager, "ec2")

    def _create_blocks(self, manager, count=1):
        """Call create_blocks with the network lookup stubbed out."""
        network = {
            "vpc_id": "vpc-12345",
            "subnet_id": "subnet-12345",
            "security_group_id": "sg-12345",
        }
        with patch.object(manager, "_setup_network_resources", return_value=network):
            return manager.create_blocks(count)

    def test_ami_not_found_error(self, ec2_manager):
        """An unusable AMI surfaces as ResourceCreationError from create_blocks.

        ``AMINotFoundError`` is raised only by ``utils.aws.get_default_ami()``
        for a region absent from the AMI table -- never by the manager, which
        wraps every launch failure as ResourceCreationError. Both derive from
        ResourceCreationError, so that is what a caller can actually catch.
        """
        ec2_manager.ec2_client.run_instances.side_effect = ClientError(
            {
                "Error": {
                    "Code": "InvalidAMIID.NotFound",
                    "Message": "The image id ami-12345 does not exist",
                }
            },
            "RunInstances",
        )

        with pytest.raises(ResourceCreationError):
            self._create_blocks(ec2_manager)

    def test_insufficient_capacity_error(self, ec2_manager):
        """Test handling of insufficient capacity errors."""
        ec2_manager.ec2_client.run_instances.side_effect = ClientError(
            {
                "Error": {
                    "Code": "InsufficientInstanceCapacity",
                    "Message": "Insufficient capacity",
                }
            },
            "RunInstances",
        )

        with pytest.raises(ResourceCreationError):
            self._create_blocks(ec2_manager)

    def test_instance_limit_exceeded_error(self, ec2_manager):
        """Test handling of instance limit exceeded errors."""
        ec2_manager.ec2_client.run_instances.side_effect = ClientError(
            {
                "Error": {
                    "Code": "InstanceLimitExceeded",
                    "Message": "You have requested more instances than your limit",
                }
            },
            "RunInstances",
        )

        with pytest.raises(ResourceCreationError):
            self._create_blocks(ec2_manager)

    def test_instance_not_found_is_reported_as_terminated(self, ec2_manager):
        """A vanished instance reads as 'terminated' rather than raising.

        This is deliberate, not a gap: an instance EC2 no longer knows about has
        finished, and ``get_instance_status`` special-cases
        ``InvalidInstanceID.NotFound`` to say so (compute/ec2.py). The old test
        expected ``ResourceNotFoundError``, which would make every completed
        one-shot job look like a failure.
        """
        ec2_manager.ec2_client.describe_instances.side_effect = ClientError(
            {
                "Error": {
                    "Code": "InvalidInstanceID.NotFound",
                    "Message": "The instance ID i-12345 does not exist",
                }
            },
            "DescribeInstances",
        )

        assert ec2_manager.get_instance_status("i-12345") == "terminated"

    def test_instance_status_other_client_error_propagates(self, ec2_manager):
        """Any error code other than NotFound is re-raised, not swallowed."""
        ec2_manager.ec2_client.describe_instances.side_effect = ClientError(
            {"Error": {"Code": "UnauthorizedOperation", "Message": "Denied"}},
            "DescribeInstances",
        )

        with pytest.raises(ClientError):
            ec2_manager.get_instance_status("i-12345")

    def test_termination_error(self, ec2_manager):
        """A refused termination propagates rather than being reported as done."""
        ec2_manager.ec2_client.terminate_instances.side_effect = ClientError(
            {
                "Error": {
                    "Code": "OperationNotPermitted",
                    "Message": "You are not authorized to terminate instances",
                }
            },
            "TerminateInstances",
        )

        with pytest.raises(ClientError):
            ec2_manager.terminate_instance("i-12345")

    def test_failed_termination_does_not_mark_the_instance_terminated(
        self, ec2_manager
    ):
        """The tracked status must not say 'terminated' when the call failed.

        Otherwise cleanup drops the record and the instance bills on untracked.
        """
        ec2_manager.instances["i-12345"] = {"id": "i-12345", "status": "running"}
        ec2_manager.ec2_client.terminate_instances.side_effect = ClientError(
            {"Error": {"Code": "OperationNotPermitted", "Message": "Denied"}},
            "TerminateInstances",
        )

        with pytest.raises(ClientError):
            ec2_manager.terminate_instance("i-12345")

        assert ec2_manager.instances["i-12345"]["status"] == "running"


class TestSpotFleetManagerErrors:
    """Tests for error handling in SpotFleetManager.

    Driven through ``create_blocks``/``terminate_block``, the manager's real
    surface. ``create_spot_fleet``/``cancel_spot_fleet``/
    ``update_spot_fleet_capacity`` have never existed on this class.
    """

    @pytest.fixture
    def spot_fleet_manager(self):
        """Create a SpotFleetManager with mock session."""
        return _make_manager(
            SpotFleetManager,
            "spot_fleet",
            instance_types=["t3.micro", "t3.small"],
            spot_max_price_percentage=80,
        )

    def _request_fleet(self, manager):
        """Call ``_create_spot_fleet_request`` -- the layer that classifies errors.

        ``create_blocks`` deliberately wraps everything it catches as
        ``ResourceCreationError``, and because ``SpotFleetError`` descends from
        it (via ``EC2InstanceError``) the subclass is *erased* at that boundary.
        So the subclass assertions have to run one level down; the wrapping
        itself is asserted separately in
        ``test_create_blocks_wraps_fleet_errors``.
        """
        return manager._create_spot_fleet_request(
            block_id="block-1",
            network={
                "vpc_id": "vpc-12345",
                "subnet_id": "subnet-12345",
                "security_group_id": "sg-12345",
            },
            target_capacity=1,
            fleet_role_arn="arn:aws:iam::123456789012:role/fleet",
        )

    def test_spot_fleet_request_error(self, spot_fleet_manager):
        """An invalid fleet configuration surfaces as SpotFleetError."""
        spot_fleet_manager.ec2_client.request_spot_fleet.side_effect = ClientError(
            {
                "Error": {
                    "Code": "InvalidSpotFleetRequestConfig",
                    "Message": "Invalid Spot Fleet request configuration",
                }
            },
            "RequestSpotFleet",
        )

        with pytest.raises(SpotFleetError):
            self._request_fleet(spot_fleet_manager)

    def test_spot_fleet_throttling_error(self, spot_fleet_manager):
        """RequestLimitExceeded surfaces as the throttling subclass.

        The subclass carries ``retry_after``, which is the whole point of
        distinguishing it -- a caller can back off for the interval AWS named
        instead of retrying blind.
        """
        spot_fleet_manager.ec2_client.request_spot_fleet.side_effect = ClientError(
            {
                "Error": {
                    "Code": "RequestLimitExceeded",
                    "Message": "Request limit exceeded",
                }
            },
            "RequestSpotFleet",
        )

        with pytest.raises(SpotFleetThrottlingError):
            self._request_fleet(spot_fleet_manager)

    def test_spot_fleet_instance_limit_error(self, spot_fleet_manager):
        """InstanceLimitExceeded surfaces as the request subclass."""
        spot_fleet_manager.ec2_client.request_spot_fleet.side_effect = ClientError(
            {
                "Error": {
                    "Code": "InstanceLimitExceeded",
                    "Message": "You have reached your instance limit",
                }
            },
            "RequestSpotFleet",
        )

        with pytest.raises(SpotFleetRequestError):
            self._request_fleet(spot_fleet_manager)

    def test_create_blocks_wraps_fleet_errors(self, spot_fleet_manager):
        """create_blocks reports a fleet failure as ResourceCreationError.

        Pinning the wrapping is what makes the subclass tests above meaningful:
        callers of ``create_blocks`` get the generic type, callers of
        ``_create_spot_fleet_request`` get the specific one.
        """
        spot_fleet_manager.ec2_client.request_spot_fleet.side_effect = ClientError(
            {
                "Error": {
                    "Code": "InvalidSpotFleetRequestConfig",
                    "Message": "Invalid Spot Fleet request configuration",
                }
            },
            "RequestSpotFleet",
        )
        network = {
            "vpc_id": "vpc-12345",
            "subnet_id": "subnet-12345",
            "security_group_id": "sg-12345",
        }

        with (
            patch.object(
                spot_fleet_manager, "_setup_network_resources", return_value=network
            ),
            patch.object(
                spot_fleet_manager,
                "_get_iam_fleet_role",
                return_value="arn:aws:iam::123456789012:role/fleet",
            ),
        ):
            with pytest.raises(ResourceCreationError):
                spot_fleet_manager.create_blocks(1)

    def test_spot_fleet_cancellation_error(self, spot_fleet_manager):
        """A refused cancellation surfaces as SpotFleetError."""
        spot_fleet_manager.blocks["block-1"] = {
            "id": "block-1",
            "fleet_request_id": "sfr-12345",
            "status": "running",
            "instance_ids": [],
        }
        spot_fleet_manager.ec2_client.cancel_spot_fleet_requests.side_effect = (
            ClientError(
                {
                    "Error": {
                        "Code": "UnauthorizedOperation",
                        "Message": "You are not authorized to cancel this request",
                    }
                },
                "CancelSpotFleetRequests",
            )
        )

        with pytest.raises(SpotFleetError):
            spot_fleet_manager.terminate_block("block-1")

    def test_cancelling_an_absent_fleet_is_not_an_error(self, spot_fleet_manager):
        """A fleet AWS has already forgotten counts as cancelled, not as failure.

        Termination has to be idempotent -- cleanup runs it on paths that may
        have already run it.
        """
        spot_fleet_manager.blocks["block-1"] = {
            "id": "block-1",
            "fleet_request_id": "sfr-12345",
            "status": "running",
            "instance_ids": [],
        }
        spot_fleet_manager.ec2_client.cancel_spot_fleet_requests.side_effect = (
            ClientError(
                {
                    "Error": {
                        "Code": "InvalidSpotFleetRequestId.NotFound",
                        "Message": "The spot fleet request does not exist",
                    }
                },
                "CancelSpotFleetRequests",
            )
        )

        spot_fleet_manager.terminate_block("block-1")

        assert spot_fleet_manager.blocks["block-1"]["status"] == STATUS_CANCELLED


class TestLambdaManagerErrors:
    """Tests for error handling in LambdaManager.

    Driven through ``submit_job``/``get_job_status``/``cleanup_all_resources``.
    ``create_lambda_function``/``invoke_lambda_function``/
    ``delete_lambda_function`` have never existed on this class -- the private
    ``_create_lambda_function`` does.
    """

    @pytest.fixture
    def lambda_manager(self):
        """Create a LambdaManager with mock session."""
        return _make_manager(
            LambdaManager, "lambda_func", lambda_timeout=300, lambda_memory=512
        )

    def test_lambda_creation_error(self, lambda_manager):
        """A rejected create_function surfaces as JobSubmissionError.

        ``_create_lambda_function`` raises ResourceCreationError, which
        ``submit_job`` wraps as JobSubmissionError -- that is what a caller sees.
        """
        lambda_manager.lambda_client.create_function.side_effect = ClientError(
            {
                "Error": {
                    "Code": "InvalidParameterValueException",
                    "Message": "Memory size must be between 128 and 10240 MB",
                }
            },
            "CreateFunction",
        )

        with patch.object(
            lambda_manager, "_create_lambda_execution_role", return_value="arn:role"
        ):
            with pytest.raises(JobSubmissionError):
                lambda_manager.submit_job("job-1", "echo hello")

    def test_lambda_invocation_error(self, lambda_manager):
        """A rejected invoke surfaces as JobSubmissionError."""
        lambda_manager.lambda_client.invoke.side_effect = ClientError(
            {
                "Error": {
                    "Code": "TooManyRequestsException",
                    "Message": "Rate exceeded",
                }
            },
            "Invoke",
        )

        with patch.object(
            lambda_manager, "_create_lambda_function", return_value="parsl-lambda-job-1"
        ):
            with pytest.raises(JobSubmissionError):
                lambda_manager.submit_job("job-1", "echo hello")

    def test_non_accepted_async_invocation_is_a_failure(self, lambda_manager):
        """An async invoke that is not 202 must not be reported as submitted.

        ``invoke`` returns rather than raising when the request is rejected, so
        without the status-code check the job would be tracked as PENDING
        forever against a function that never ran.
        """
        lambda_manager.lambda_client.invoke.return_value = {"StatusCode": 400}

        with patch.object(
            lambda_manager, "_create_lambda_function", return_value="parsl-lambda-job-1"
        ):
            with pytest.raises(JobSubmissionError):
                lambda_manager.submit_job("job-1", "echo hello")

        assert "job-1" not in lambda_manager.jobs

    def test_lambda_status_error_does_not_raise(self, lambda_manager):
        """A failed status lookup degrades to UNKNOWN rather than raising.

        ``status()`` is polled on every Parsl iteration; raising there would
        abort the run over a transient throttle.
        """
        lambda_manager.lambda_client.get_function.side_effect = ClientError(
            {"Error": {"Code": "TooManyRequestsException", "Message": "Rate exceeded"}},
            "GetFunction",
        )

        assert lambda_manager.get_job_status("parsl-lambda-job-1", "req-1") == "UNKNOWN"

    def test_lambda_deletion_error_is_tolerated_during_cleanup(self, lambda_manager):
        """One undeletable function must not abort the rest of the cleanup.

        Cleanup is best-effort by design: aborting on the first failure would
        strand every remaining function and IAM role.
        """
        lambda_manager.function_names.update({"fn-a", "fn-b"})
        lambda_manager.lambda_client.delete_function.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "Denied"}},
            "DeleteFunction",
        )

        lambda_manager.cleanup_all_resources()

        assert lambda_manager.lambda_client.delete_function.call_count == 2


class TestECSManagerErrors:
    """Tests for error handling in ECSManager.

    Driven through construction, ``submit_job`` and ``cancel_job``.
    ``create_cluster``/``run_task``/``stop_task``/``register_task_definition``
    have never existed on this class -- ``_get_or_create_cluster`` and
    ``_register_task_definition`` do, and the cluster one runs from
    ``__init__``.
    """

    @pytest.fixture
    def ecs_manager(self):
        """Create an ECSManager with mock session and cluster creation stubbed."""
        with patch.object(
            ECSManager, "_get_or_create_cluster", return_value="parsl-ecs-cluster-test"
        ):
            return _make_manager(ECSManager, "ecs", use_public_ips=False)

    def test_cluster_creation_error(self):
        """A rejected create_cluster surfaces as ResourceCreationError.

        ``__init__`` calls ``_get_or_create_cluster``, so this fails
        construction -- the manager is never handed back half-built.
        """
        provider = _mock_provider()
        ecs_client = MagicMock()
        ecs_client.describe_clusters.return_value = {"clusters": []}
        ecs_client.create_cluster.side_effect = ClientError(
            {
                "Error": {
                    "Code": "InvalidParameterException",
                    "Message": "Invalid parameter in request",
                }
            },
            "CreateCluster",
        )
        session = MagicMock()
        session.client.return_value = ecs_client
        session.region_name = "us-east-1"

        with patch("parsl_ephemeral_aws.compute.ecs.CredentialManager") as mock_cm:
            mock_cm.return_value.create_boto3_session.return_value = session
            with pytest.raises(ResourceCreationError):
                ECSManager(provider=provider)

    def test_task_definition_error(self, ecs_manager):
        """A rejected register_task_definition surfaces as JobSubmissionError."""
        ecs_manager.ecs_client.register_task_definition.side_effect = ClientError(
            {
                "Error": {
                    "Code": "ClientException",
                    "Message": "Invalid task definition",
                }
            },
            "RegisterTaskDefinition",
        )

        with pytest.raises(JobSubmissionError):
            ecs_manager.submit_job("job-1", "echo hello", tasks_per_node=1)

    def test_run_task_error(self, ecs_manager):
        """A rejected run_task surfaces as JobSubmissionError."""
        ecs_manager.ecs_client.run_task.side_effect = ClientError(
            {
                "Error": {
                    "Code": "InvalidParameterException",
                    "Message": "Invalid parameter in request",
                }
            },
            "RunTask",
        )

        with (
            patch.object(
                ecs_manager, "_register_task_definition", return_value="arn:task-def"
            ),
            patch.object(
                ecs_manager,
                "_get_or_create_network_resources",
                return_value={
                    "subnet_ids": ["subnet-12345"],
                    "security_group_id": "sg-12345",
                },
            ),
        ):
            with pytest.raises(JobSubmissionError):
                ecs_manager.submit_job("job-1", "echo hello", tasks_per_node=1)

    def test_stop_task_error(self, ecs_manager):
        """A refused stop_task propagates rather than reporting success."""
        ecs_manager.ecs_client.stop_task.side_effect = ClientError(
            {
                "Error": {
                    "Code": "InvalidParameterException",
                    "Message": "Task not found",
                }
            },
            "StopTask",
        )

        with pytest.raises(ClientError):
            ecs_manager.cancel_job("parsl-ecs-cluster-test", "task-1")


class TestProviderConfigurationErrors:
    """Tests for provider configuration errors."""

    def test_invalid_mode(self):
        """Test handling of invalid operating mode."""
        with pytest.raises(ProviderConfigurationError):
            provider = EphemeralAWSProvider(
                region="us-east-1",
                instance_type="t3.micro",
                image_id="ami-12345678",
                mode="invalid_mode",
            )

    def test_missing_required_parameter(self):
        """Test handling of missing required parameters."""
        # Missing image_id in standard mode
        with pytest.raises(ProviderConfigurationError):
            provider = EphemeralAWSProvider(
                region="us-east-1",
                instance_type="t3.micro",
                mode="standard",
                # Missing image_id
            )

    def test_incompatible_parameters(self):
        """Test handling of incompatible parameter combinations."""
        # Spot Fleet without instance types
        with pytest.raises(ProviderConfigurationError):
            provider = EphemeralAWSProvider(
                region="us-east-1",
                instance_type="t3.micro",
                image_id="ami-12345678",
                mode="standard",
                use_spot_fleet=True,
                # Missing instance_types
            )

    def test_invalid_parameter_values(self):
        """Test handling of invalid parameter values."""
        # Invalid region
        with pytest.raises(ProviderConfigurationError):
            provider = EphemeralAWSProvider(
                region="invalid-region",
                instance_type="t3.micro",
                image_id="ami-12345678",
                mode="standard",
            )

        # Invalid instance type format
        with pytest.raises(ProviderConfigurationError):
            provider = EphemeralAWSProvider(
                region="us-east-1",
                instance_type="invalid_instance_type",
                image_id="ami-12345678",
                mode="standard",
            )

    def test_spot_without_interruption_warning(self):
        """Test warning when using spot instances without interruption handling."""
        with patch("logging.Logger.warning") as mock_warning:
            provider = EphemeralAWSProvider(
                region="us-east-1",
                instance_type="t3.micro",
                image_id="ami-12345678",
                mode="standard",
                use_spot=True,
                spot_interruption_handling=False,
            )

            # Verify warning was logged
            mock_warning.assert_called_with(
                "Spot instances are enabled but spot interruption handling is disabled. Tasks may be lost if instances are interrupted."
            )


class TestCloudFormationErrors:
    """Tests for CloudFormation error handling in Serverless mode."""

    @pytest.fixture
    def serverless_mode(self):
        """Create a ServerlessMode with mock session."""
        session = MagicMock()
        cf_client = MagicMock()
        session.client.return_value = cf_client

        return ServerlessMode(
            provider_id=str(uuid.uuid4()),
            session=session,
            state_store=MagicMock(),
            region="us-east-1",
            worker_type="ecs",
            ecs_task_cpu=256,
            ecs_task_memory=512,
            ecs_container_image="amazon/amazon-ecs-sample",
            vpc_id="vpc-12345",
            subnet_id="subnet-12345",
            security_group_id="sg-12345",
        )

    def test_stack_creation_error(self, serverless_mode):
        """Test handling of CloudFormation stack creation errors."""
        # Simulate stack creation error
        error_response = {
            "Error": {
                "Code": "LimitExceededException",
                "Message": "Stack limit exceeded",
            }
        }
        serverless_mode.cf_client.create_stack.side_effect = ClientError(
            error_response, "CreateStack"
        )

        with pytest.raises(CloudFormationError):
            serverless_mode._create_cloudformation_stack(
                stack_name="test-stack", template_body="{}", parameters=[], tags={}
            )

    def test_stack_deletion_error(self, serverless_mode):
        """Test handling of CloudFormation stack deletion errors."""
        # Simulate stack deletion error
        error_response = {
            "Error": {"Code": "ValidationError", "Message": "Stack does not exist"}
        }
        serverless_mode.cf_client.delete_stack.side_effect = ClientError(
            error_response, "DeleteStack"
        )

        # This should log the error but not raise an exception since we're trying to delete something that doesn't exist
        serverless_mode._delete_cloudformation_stack("test-stack")

        # Verify delete_stack was called with the right stack name
        serverless_mode.cf_client.delete_stack.assert_called_with(
            StackName="test-stack"
        )

    def test_stack_waiting_error(self, serverless_mode):
        """Test handling of errors while waiting for CloudFormation stack."""
        # Mock describe_stacks to return a failed stack
        serverless_mode.cf_client.describe_stacks.return_value = {
            "Stacks": [
                {
                    "StackName": "test-stack",
                    "StackStatus": "CREATE_FAILED",
                    "StackStatusReason": "Resource creation failed",
                }
            ]
        }

        with pytest.raises(CloudFormationError):
            serverless_mode._wait_for_stack("test-stack", "CREATE_COMPLETE", 10)

    def test_stack_timeout_error(self, serverless_mode):
        """Test handling of timeout while waiting for CloudFormation stack."""
        # Mock describe_stacks to always return an in-progress stack
        serverless_mode.cf_client.describe_stacks.return_value = {
            "Stacks": [{"StackName": "test-stack", "StackStatus": "CREATE_IN_PROGRESS"}]
        }

        # Set a short timeout to trigger the timeout error
        with pytest.raises(CloudFormationError):
            # Pass a very short timeout (1 second)
            serverless_mode._wait_for_stack("test-stack", "CREATE_COMPLETE", 1)


@pytest.mark.unit
class TestEC2ManagerQuotaErrors:
    """Tests for EC2 quota, capacity, and instance-type error handling (closes #43)."""

    @pytest.fixture
    def ec2_manager_and_client(self):
        """EC2Manager wired with a fully-mocked provider and boto3 session."""
        mock_provider = MagicMock()
        mock_provider.workflow_id = "test-workflow-quota"
        mock_provider.region = "us-east-1"
        mock_provider.image_id = "ami-12345678"
        mock_provider.instance_type = "t3.micro"
        mock_provider.vpc_id = "vpc-test"
        mock_provider.subnet_id = "subnet-test"
        mock_provider.security_group_id = "sg-test"
        mock_provider.tags = {}
        mock_provider.aws_access_key_id = None
        mock_provider.aws_secret_access_key = None
        mock_provider.aws_session_token = None
        mock_provider.aws_profile = None
        mock_provider.iam_instance_profile_arn = None
        mock_provider.auto_create_instance_profile = False
        # Required by SecurityConfig and CredentialConfig
        mock_provider.vpc_cidr = "10.0.0.0/16"
        mock_provider.security_environment = "dev"
        mock_provider.admin_cidr_blocks = None
        mock_provider.strict_security_mode = None
        mock_provider.role_arn = None
        # Control block creation behavior
        mock_provider.use_spot_instances = False
        mock_provider.nodes_per_block = 1

        mock_ec2 = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ec2
        mock_session.resource.return_value = MagicMock()
        mock_session.region_name = "us-east-1"

        with patch("parsl_ephemeral_aws.compute.ec2.CredentialManager") as mock_cm:
            mock_cm.return_value.create_boto3_session.return_value = mock_session
            manager = EC2Manager(provider=mock_provider)
        manager.ec2_client = mock_ec2
        return manager, mock_ec2

    def _client_error(self, code, message="error"):
        return ClientError(
            {"Error": {"Code": code, "Message": message}}, "RunInstances"
        )

    def _run_create_blocks(self, manager, ec2):
        """Call create_blocks with network resources pre-mocked."""
        network = {
            "vpc_id": "vpc-test",
            "subnet_id": "subnet-test",
            "security_group_id": "sg-test",
        }
        with patch.object(manager, "_setup_network_resources", return_value=network):
            manager.create_blocks(1)

    def test_quota_exceeded_error(self, ec2_manager_and_client):
        """VcpuLimitExceeded on run_instances surfaces as ResourceCreationError."""
        manager, ec2 = ec2_manager_and_client
        ec2.run_instances.side_effect = self._client_error(
            "VcpuLimitExceeded", "vCPU quota exceeded"
        )

        with pytest.raises(ResourceCreationError):
            self._run_create_blocks(manager, ec2)

    def test_invalid_instance_type_error(self, ec2_manager_and_client):
        """InvalidInstanceType on run_instances surfaces as ResourceCreationError."""
        manager, ec2 = ec2_manager_and_client
        ec2.run_instances.side_effect = self._client_error(
            "InvalidInstanceType", "The instance type is invalid"
        )

        with pytest.raises(ResourceCreationError):
            self._run_create_blocks(manager, ec2)

    def test_insufficient_spot_capacity_error(self, ec2_manager_and_client):
        """InsufficientInstanceCapacity on run_instances surfaces as ResourceCreationError."""
        manager, ec2 = ec2_manager_and_client
        ec2.run_instances.side_effect = self._client_error(
            "InsufficientInstanceCapacity", "Insufficient spot capacity"
        )

        with pytest.raises(ResourceCreationError):
            self._run_create_blocks(manager, ec2)

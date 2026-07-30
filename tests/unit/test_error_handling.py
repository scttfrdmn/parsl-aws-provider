"""Unit tests for error handling in critical components.

These tests verify that exceptions are properly raised, caught, and handled
throughout the codebase, ensuring robust error handling and reporting.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import pytest
import uuid
from unittest.mock import MagicMock, patch
from botocore.exceptions import ClientError, NoCredentialsError, ProfileNotFound
from parsl.jobs.states import JobState

from parsl_ephemeral_aws.provider import EphemeralAWSProvider
from parsl_ephemeral_aws.compute.ec2 import EC2Manager
from parsl_ephemeral_aws.modes.standard import StandardMode
from parsl_ephemeral_aws.modes.detached import DetachedMode
from parsl_ephemeral_aws.modes.serverless import ServerlessMode
from parsl_ephemeral_aws.compute.spot_fleet import SpotFleetManager
from parsl_ephemeral_aws.compute.lambda_func import LambdaManager
from parsl_ephemeral_aws.compute.ecs import ECSManager
from parsl_ephemeral_aws.constants import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SUCCEEDED,
    STATUS_UNKNOWN,
)
from parsl_ephemeral_aws.exceptions import (
    AWSAuthenticationError,
    AWSConnectionError,
    JobSubmissionError,
    ResourceCreationError,
    ProviderConfigurationError,
    ResourceNotFoundError,
    SpotFleetError,
    SpotFleetRequestError,
    SpotFleetThrottlingError,
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
            # Required since #69 -- the provider no longer creates a network.
            "vpc_id": "vpc-12345",
            "subnet_id": "subnet-12345",
            "security_group_id": "sg-12345",
        }

    def _provider_with_failing_sts(self, provider_config, error_code):
        """Construct a provider whose STS verification call raises *error_code*."""
        mock_session = MagicMock()
        mock_session.client.side_effect = ClientError(
            {"Error": {"Code": error_code, "Message": "m"}}, "GetCallerIdentity"
        )
        with patch("boto3.Session", return_value=mock_session):
            return EphemeralAWSProvider(**provider_config)

    def test_no_credentials_error(self, provider_config):
        """Absent credentials are an authentication failure, not a connection one.

        ``NoCredentialsError`` used to fall through to the bare ``except
        Exception`` in ``create_session`` and surface as ``AWSConnectionError``,
        telling the caller to retry a network fault when the fix is to configure
        credentials (#104).
        """
        with patch("boto3.Session", side_effect=NoCredentialsError()):
            with pytest.raises(AWSAuthenticationError):
                EphemeralAWSProvider(**provider_config)

    def test_unknown_profile_is_an_authentication_error(self, provider_config):
        """A mistyped profile name must not read as a connectivity fault."""
        with patch("boto3.Session", side_effect=ProfileNotFound(profile="nope")):
            with pytest.raises(AWSAuthenticationError):
                EphemeralAWSProvider(**provider_config)

    @pytest.mark.parametrize(
        "error_code",
        [
            "AuthFailure",
            "InvalidClientTokenId",
            "AccessDenied",
            "ExpiredToken",
            "SignatureDoesNotMatch",
            "UnrecognizedClientException",
        ],
    )
    def test_invalid_credentials_error(self, provider_config, error_code):
        """Every STS auth-failure code maps to AWSAuthenticationError.

        ``create_session`` classified on ``str(e)`` and matched only
        ``InvalidClientTokenId``/``AccessDenied``, so the other four arrived as
        the generic ``AWSConnectionError`` (#104).
        """
        with pytest.raises(AWSAuthenticationError):
            self._provider_with_failing_sts(provider_config, error_code)

    def test_service_unavailable_error(self, provider_config):
        """A genuine service outage stays AWSConnectionError, not the auth subclass.

        This is the negative half of the #104 fix: widening the auth set must not
        swallow retryable failures. Asserted via ``type(...) is`` because
        ``AWSAuthenticationError`` *subclasses* ``AWSConnectionError``, so
        ``pytest.raises(AWSConnectionError)`` would pass either way.
        """
        with pytest.raises(AWSConnectionError) as exc_info:
            self._provider_with_failing_sts(provider_config, "ServiceUnavailable")

        assert type(exc_info.value) is AWSConnectionError

    def test_throttling_error(self, provider_config):
        """Throttling during status() degrades to UNKNOWN rather than raising.

        ``status()`` is polled on every Parsl iteration and catches broadly on
        purpose (provider.py) -- raising would abort the whole run over a
        transient throttle. The old test asserted ``AWSConnectionError`` here,
        which is the opposite of the contract.

        The mode's ``get_job_status`` is stubbed to raise rather than letting the
        throttle come from a mocked ``describe_instances``: registering a
        resource well enough for the mode to actually reach that call takes more
        setup than the assertion is worth, and without it the test passes
        vacuously on the "resource not tracked" path instead of exercising the
        handler.
        """
        mock_session = MagicMock()
        mock_session.client.return_value = MagicMock()

        with patch("boto3.Session", return_value=mock_session):
            provider = EphemeralAWSProvider(**provider_config)

        provider.job_map["job-1"] = {"resource_id": "resource-1", "status": "RUNNING"}
        throttle = ClientError(
            {
                "Error": {
                    "Code": "RequestLimitExceeded",
                    "Message": "Request limit exceeded",
                }
            },
            "DescribeInstances",
        )

        with patch.object(
            provider.operating_mode, "get_job_status", side_effect=throttle
        ):
            statuses = provider.status(["job-1"])

        assert [s.state for s in statuses] == [JobState.UNKNOWN]

    def test_status_of_an_unknown_job_is_reported_not_raised(self, provider_config):
        """A job ID the provider has never seen reads as UNKNOWN."""
        mock_session = MagicMock()
        mock_session.client.return_value = MagicMock()

        with patch("boto3.Session", return_value=mock_session):
            provider = EphemeralAWSProvider(**provider_config)

        statuses = provider.status(["never-submitted"])

        assert [s.state for s in statuses] == [JobState.UNKNOWN]


class TestModeInitializationErrors:
    """Tests for errors during operating mode initialization.

    These used to assert ``NetworkCreationError`` from ``StandardMode`` and
    ``BastionHostError`` from ``DetachedMode`` while the modes created their own
    VPC/subnet/SG. #69 removed that creation, and neither exception is raised
    anywhere in the package any more. The successor contract is
    ``_verify_resources()``: every mode now confirms the caller-supplied IDs
    exist and raises ``ResourceNotFoundError`` naming the bad one.

    That is worth covering in all three modes rather than deleting, because the
    failure it replaced was the opaque one -- before #103 the modes silently
    nulled the attribute out so ``initialize()`` could recreate the resource, and
    since nothing recreates it the ``None`` surfaced much later as an
    ``InvalidParameterValue`` from inside ``run_instances``.
    """

    @pytest.fixture
    def mock_session(self):
        """Create a mock boto3 session whose clients are all one MagicMock."""
        session = MagicMock()
        session.client.return_value = MagicMock()
        return session

    @staticmethod
    def _network_ids():
        return {
            "vpc_id": "vpc-12345",
            "subnet_id": "subnet-12345",
            "security_group_id": "sg-12345",
        }

    @pytest.mark.parametrize(
        "describe_call,error_code,expected_in_message",
        [
            ("describe_vpcs", "InvalidVpcID.NotFound", "vpc-12345"),
            ("describe_vpcs", "InvalidVpcID.Malformed", "vpc-12345"),
            ("describe_subnets", "InvalidSubnetID.NotFound", "subnet-12345"),
            ("describe_security_groups", "InvalidGroup.NotFound", "sg-12345"),
        ],
    )
    def test_standard_mode_missing_network_resource(
        self, mock_session, describe_call, error_code, expected_in_message
    ):
        """A missing or malformed network ID fails initialize(), naming the ID."""
        ec2 = mock_session.client.return_value
        getattr(ec2, describe_call).side_effect = ClientError(
            {"Error": {"Code": error_code, "Message": "m"}}, describe_call
        )

        mode = StandardMode(
            provider_id=str(uuid.uuid4()),
            session=mock_session,
            state_store=MagicMock(),
            region="us-east-1",
            instance_type="t3.micro",
            image_id="ami-12345678",
            **self._network_ids(),
        )

        with pytest.raises(ResourceNotFoundError) as exc_info:
            mode.initialize()

        assert expected_in_message in str(exc_info.value)

    def test_standard_mode_unrelated_client_error_propagates(self, mock_session):
        """An error code outside the not-found set is re-raised, not reinterpreted.

        Reporting a throttle or an authorization failure as "this VPC does not
        exist" would send the caller looking for the wrong problem.
        """
        ec2 = mock_session.client.return_value
        ec2.describe_vpcs.side_effect = ClientError(
            {"Error": {"Code": "RequestLimitExceeded", "Message": "m"}}, "DescribeVpcs"
        )

        mode = StandardMode(
            provider_id=str(uuid.uuid4()),
            session=mock_session,
            state_store=MagicMock(),
            region="us-east-1",
            instance_type="t3.micro",
            image_id="ami-12345678",
            **self._network_ids(),
        )

        with pytest.raises(ClientError):
            mode.initialize()

    def test_detached_mode_missing_network_resource(self, mock_session):
        """DetachedMode verifies the network before it touches the bastion.

        Its ``_verify_resources`` override calls ``super()`` first for exactly
        this reason: a missing VPC must not be reported as a bastion problem.
        """
        ec2 = mock_session.client.return_value
        ec2.describe_vpcs.side_effect = ClientError(
            {"Error": {"Code": "InvalidVpcID.NotFound", "Message": "m"}},
            "DescribeVpcs",
        )

        mode = DetachedMode(
            provider_id=str(uuid.uuid4()),
            session=mock_session,
            state_store=MagicMock(),
            region="us-east-1",
            instance_type="t3.micro",
            image_id="ami-12345678",
            workflow_id=f"test-workflow-{uuid.uuid4().hex[:8]}",
            bastion_instance_type="t3.micro",
            bastion_host_type="direct",
            **self._network_ids(),
        )

        with pytest.raises(ResourceNotFoundError):
            mode.initialize()

    def test_serverless_ecs_missing_network_resource(self, mock_session):
        """An ECS-backed serverless mode verifies its subnet and SG.

        Fargate's ``awsvpcConfiguration`` is mandatory, so unlike the Lambda-only
        case below these IDs genuinely have to resolve.
        """
        ec2 = mock_session.client.return_value
        ec2.describe_subnets.side_effect = ClientError(
            {"Error": {"Code": "InvalidSubnetID.NotFound", "Message": "m"}},
            "DescribeSubnets",
        )

        mode = ServerlessMode(
            provider_id=str(uuid.uuid4()),
            session=mock_session,
            state_store=MagicMock(),
            region="us-east-1",
            worker_type="ecs",
            **self._network_ids(),
        )

        with pytest.raises(ResourceNotFoundError):
            mode.initialize()

    def test_serverless_lambda_only_needs_no_network(self, mock_session):
        """Lambda-only serverless mode initializes with no network IDs at all.

        Functions run in the Lambda-managed VPC -- ``lambda_func.py`` passes no
        ``VpcConfig`` -- so there is nothing for the caller to pre-provision and
        nothing to verify. Guarding this makes sure the #69 requirement is not
        re-tightened onto the one worker type that does not need it.
        """
        mode = ServerlessMode(
            provider_id=str(uuid.uuid4()),
            session=mock_session,
            state_store=MagicMock(),
            region="us-east-1",
            worker_type="lambda",
            lambda_memory=128,
            lambda_timeout=30,
        )

        with patch.object(mode, "_initialize_compute_managers"):
            mode.initialize()

        assert mode.initialized is True


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
    """Tests for provider configuration errors.

    What the provider validates, and what it deliberately does not, is a
    contract in itself. Two things it *should* reject were found missing here
    and fixed: an unknown region (#107) and an unrecognised keyword (#105).
    Two more it is right to leave alone -- see
    ``test_unvalidated_by_design`` for why.
    """

    @pytest.fixture
    def provider_config(self):
        """Minimum config that constructs, for tests to perturb one field of."""
        return {
            "region": "us-east-1",
            "instance_type": "t3.micro",
            "image_id": "ami-12345678",
            "mode": "standard",
            "vpc_id": "vpc-12345",
            "subnet_id": "subnet-12345",
            "security_group_id": "sg-12345",
        }

    @staticmethod
    def _mock_session():
        session = MagicMock()
        session.client.return_value = MagicMock()
        session.region_name = "us-east-1"
        return session

    def _construct(self, config):
        """Construct a provider with boto3 fully mocked out."""
        with patch("boto3.Session", return_value=self._mock_session()):
            return EphemeralAWSProvider(**config)

    def test_invalid_mode(self, provider_config):
        """An unknown operating mode is rejected by name."""
        with pytest.raises(ProviderConfigurationError, match="invalid_mode"):
            self._construct({**provider_config, "mode": "invalid_mode"})

    def test_invalid_compute_type(self, provider_config):
        """An unknown compute type is rejected by name."""
        with pytest.raises(ProviderConfigurationError, match="quantum"):
            self._construct({**provider_config, "compute_type": "quantum"})

    def test_invalid_state_store_type(self, provider_config):
        """An unknown state store type is rejected by name."""
        with pytest.raises(ProviderConfigurationError, match="carrier-pigeon"):
            self._construct({**provider_config, "state_store_type": "carrier-pigeon"})

    def test_s3_store_without_bucket(self, provider_config):
        """The s3 store needs a bucket; without one there is nowhere to write."""
        with pytest.raises(ProviderConfigurationError, match="s3_bucket"):
            self._construct({**provider_config, "state_store_type": "s3"})

    def test_invalid_region(self, provider_config):
        """An unknown region is rejected at construction, not at first API call.

        Left unchecked it surfaced much later as an opaque
        ``EndpointConnectionError`` from whichever AWS call ran first -- in
        standard mode from inside ``initialize()``, after the state store had
        already been built (#107).
        """
        with pytest.raises(ProviderConfigurationError, match="invalid-region"):
            self._construct({**provider_config, "region": "invalid-region"})

    @pytest.mark.parametrize(
        "region",
        [
            "us-east-1",
            # Added to AWS well after this package was written: the check has to
            # read botocore's shipped table, not a list maintained in-tree.
            "ap-southeast-5",
            # Non-commercial partitions are usable and must not be rejected.
            "us-gov-west-1",
            "cn-north-1",
        ],
    )
    def test_valid_regions_accepted(self, provider_config, region):
        """Every partition botocore knows about is accepted."""
        provider = self._construct({**provider_config, "region": region})

        assert provider.region == region

    def test_unknown_option_is_rejected(self, provider_config):
        """A misspelled option raises instead of vanishing into **kwargs.

        ``self.kwargs = kwargs`` was write-only, so an unrecognised option was
        accepted in silence and then ignored -- which is exactly how
        ``use_spot_fleet`` went unnoticed (#105).
        """
        with pytest.raises(ProviderConfigurationError, match="use_sp0t_fleet"):
            self._construct({**provider_config, "use_sp0t_fleet": True})

    def test_spot_fleet_options_reach_the_mode(self, provider_config):
        """use_spot_fleet and friends must actually configure the mode.

        This is the regression test for #105: all four of these were accepted by
        the provider signature-less ``**kwargs``, stored on the unread
        ``self.kwargs``, and never forwarded, so ``StandardMode`` kept its
        defaults and ``spot_fleet_manager`` stayed ``None``. Spot Fleet was
        documented in a dozen places and unreachable in all of them.
        """
        provider = self._construct(
            {
                **provider_config,
                "use_spot": True,
                "use_spot_fleet": True,
                "instance_types": ["t3.micro", "t3.small"],
                "spot_max_price_percentage": 80,
                "nodes_per_block": 2,
            }
        )
        mode = provider.operating_mode

        assert mode.use_spot_fleet is True
        assert mode.instance_types == ["t3.micro", "t3.small"]
        assert mode.spot_max_price_percentage == 80
        assert mode.nodes_per_block == 2
        # The manager is gated on use_spot and use_spot_fleet both being set;
        # its presence is what proves the forwarding took effect.
        assert mode.spot_fleet_manager is not None

    @pytest.mark.parametrize(
        "override, expected",
        [
            # Parsl's strategy reads min/max straight off the provider and
            # validates neither, so an unreachable range pins the executor: it
            # cannot scale out to min_blocks and will not scale in because it
            # believes it is already there (#108).
            ({"min_blocks": 10, "max_blocks": 5}, "cannot be less than min_blocks"),
            ({"min_blocks": 0, "max_blocks": 5, "init_blocks": 9}, "init_blocks"),
            ({"min_blocks": 3, "max_blocks": 5, "init_blocks": 1}, "init_blocks"),
            ({"min_blocks": -3}, "min_blocks"),
            ({"max_blocks": -1}, "max_blocks"),
            ({"init_blocks": -1}, "init_blocks"),
        ],
    )
    def test_unreachable_block_counts_are_rejected(
        self, provider_config, override, expected
    ):
        """The three block counts have to describe a reachable range.

        This check existed at ``f32eb23:232`` and was lost in the ``cc4a240``
        rewrite; the only surviving record was a test in a file that had failed
        at collection since ``MODE_STANDARD`` was removed from ``constants.py``,
        so it had not run in a long time.
        """
        with pytest.raises(ProviderConfigurationError, match=expected):
            self._construct({**provider_config, **override})

    @pytest.mark.parametrize(
        "blocks",
        [
            {"min_blocks": 0, "max_blocks": 1, "init_blocks": 1},
            {"min_blocks": 0, "max_blocks": 0, "init_blocks": 0},
            {"min_blocks": 2, "max_blocks": 2, "init_blocks": 2},
            {"min_blocks": 1, "max_blocks": 10, "init_blocks": 4},
        ],
    )
    def test_reachable_block_counts_accepted(self, provider_config, blocks):
        """Equal bounds and a zero floor are all legitimate configurations."""
        provider = self._construct({**provider_config, **blocks})

        assert provider.min_blocks == blocks["min_blocks"]
        assert provider.max_blocks == blocks["max_blocks"]
        assert provider.init_blocks == blocks["init_blocks"]

    @pytest.mark.parametrize(
        "override",
        [
            # Thousands of names, new families constantly; a local pattern check
            # would reject valid types or wave through typos. RunInstances is the
            # authority.
            {"instance_type": "invalid_instance_type"},
            # Omitting it is a supported feature, not an error: the provider
            # resolves a per-region Amazon Linux 2023 default.
            {"image_id": None},
        ],
    )
    def test_unvalidated_by_design(self, provider_config, override):
        """Two fields are deliberately not validated locally.

        Pinned so a future "add more validation" pass has to argue with the
        reasoning rather than quietly reverse it.
        """
        provider = self._construct({**provider_config, **override})

        assert provider is not None


class TestCloudFormationErrors:
    """Tests for CloudFormation error handling in Serverless mode.

    ``ServerlessMode`` drives CloudFormation through inline ``self.cf_client``
    calls in ``_submit_ecs_job``/``_submit_lambda_job`` (create), ``cancel_jobs``
    and ``cleanup_resources`` (delete), and ``get_job_status`` (describe). These
    tests exercise those.

    The suite previously called ``_create_cloudformation_stack``,
    ``_delete_cloudformation_stack``, and ``_wait_for_stack`` and expected
    ``CloudFormationError``. No commit has ever defined any of the three, and
    ``CloudFormationError`` (``exceptions.py:185``) is raised nowhere in the
    package -- so all four tests could only ever have raised ``AttributeError``.
    The failures they describe are real, though, and are asserted below against
    the code that actually runs.
    """

    @pytest.fixture
    def serverless_mode(self):
        """Create an ECS-mode ServerlessMode with its cf_client mocked."""
        session = MagicMock()
        session.client.return_value = MagicMock()
        session.region_name = "us-east-1"

        mode = ServerlessMode(
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
        mode.cf_client = MagicMock()
        return mode

    @staticmethod
    def _track(mode, resource_id="res-1", **extra):
        """Register a resource the way a successful submit would have."""
        mode.resources[resource_id] = {
            "job_id": "job-1",
            "stack_name": "test-stack",
            "worker_type": "ecs",
            "status": "PENDING",
            **extra,
        }
        return resource_id

    def test_stack_creation_error(self, serverless_mode):
        """A rejected create_stack surfaces as JobSubmissionError.

        ``_submit_ecs_job`` wraps everything it catches, so JobSubmissionError --
        not CloudFormationError -- is what a caller can actually catch.
        """
        resource_id = self._track(serverless_mode)
        serverless_mode.cf_client.create_stack.side_effect = ClientError(
            {
                "Error": {
                    "Code": "LimitExceededException",
                    "Message": "Stack limit exceeded",
                }
            },
            "CreateStack",
        )

        with pytest.raises(JobSubmissionError):
            serverless_mode._submit_ecs_job(
                job_id="job-1",
                command="echo hello",
                tasks_per_node=1,
                job_name=None,
                resource_id=resource_id,
            )

    def test_stack_deletion_of_absent_stack_is_not_an_error(self, serverless_mode):
        """Deleting a stack AWS has already forgotten still clears tracking.

        Cleanup has to be idempotent -- it runs on paths that may have already
        run it -- so a "does not exist" ValidationError must not strand the
        resource record.
        """
        resource_id = self._track(serverless_mode)
        serverless_mode.cf_client.delete_stack.side_effect = ClientError(
            {"Error": {"Code": "ValidationError", "Message": "Stack does not exist"}},
            "DeleteStack",
        )

        serverless_mode.cleanup_resources([resource_id])

        serverless_mode.cf_client.delete_stack.assert_called_with(
            StackName="test-stack"
        )
        assert resource_id not in serverless_mode.resources

    def test_failed_stack_is_reported_failed(self, serverless_mode):
        """A CREATE_FAILED stack reports FAILED rather than raising.

        ``get_job_status`` is polled on every Parsl iteration; raising there
        would abort the run instead of letting Parsl see the task failed.
        """
        resource_id = self._track(serverless_mode)
        serverless_mode.cf_client.describe_stacks.return_value = {
            "Stacks": [
                {
                    "StackName": "test-stack",
                    "StackStatus": "CREATE_FAILED",
                    "StackStatusReason": "Resource creation failed",
                }
            ]
        }

        assert serverless_mode.get_job_status([resource_id])[resource_id] == (
            STATUS_FAILED
        )

    @pytest.mark.parametrize(
        "stack_status",
        [
            "ROLLBACK_COMPLETE",
            "ROLLBACK_IN_PROGRESS",
            "UPDATE_ROLLBACK_COMPLETE",
            "UPDATE_ROLLBACK_IN_PROGRESS",
        ],
    )
    def test_rolled_back_stack_is_reported_failed(self, serverless_mode, stack_status):
        """Any rollback is a failure, including the ones that end in COMPLETE.

        The mapping tested affixes -- ``endswith("FAILED")`` then
        ``startswith("DELETE")`` -- and ``ROLLBACK_COMPLETE`` matches neither, so
        it fell through to RUNNING (#106). That is the *usual* CloudFormation
        failure state, since automatic rollback on CREATE_FAILED is the default.
        RUNNING is not terminal, so the job was polled forever: Parsl never
        learned the task had failed and never retried it, while the stack sat in
        a state that can only be deleted.
        """
        resource_id = self._track(serverless_mode)
        serverless_mode.cf_client.describe_stacks.return_value = {
            "Stacks": [{"StackName": "test-stack", "StackStatus": stack_status}]
        }

        assert serverless_mode.get_job_status([resource_id])[resource_id] == (
            STATUS_FAILED
        )

    def test_in_progress_stack_is_reported_pending(self, serverless_mode):
        """A stack still being created is PENDING, not FAILED.

        Guards the #106 fix against over-reaching: only rollbacks became
        failures.
        """
        resource_id = self._track(serverless_mode)
        serverless_mode.cf_client.describe_stacks.return_value = {
            "Stacks": [{"StackName": "test-stack", "StackStatus": "CREATE_IN_PROGRESS"}]
        }

        assert serverless_mode.get_job_status([resource_id])[resource_id] == (
            STATUS_PENDING
        )

    def test_deleting_stack_is_reported_cancelled(self, serverless_mode):
        """A stack being deleted reads as cancelled, not failed."""
        resource_id = self._track(serverless_mode)
        serverless_mode.cf_client.describe_stacks.return_value = {
            "Stacks": [{"StackName": "test-stack", "StackStatus": "DELETE_IN_PROGRESS"}]
        }

        assert serverless_mode.get_job_status([resource_id])[resource_id] == (
            STATUS_CANCELLED
        )

    def test_vanished_stack_is_reported_succeeded(self, serverless_mode):
        """A stack AWS no longer knows about has finished, so report success.

        ``cleanup_resources`` deletes the stack once a job is done, so a missing
        stack is the normal end state -- reporting UNKNOWN or FAILED would make
        every completed serverless job look broken.
        """
        resource_id = self._track(serverless_mode)
        serverless_mode.cf_client.describe_stacks.side_effect = ClientError(
            {
                "Error": {
                    "Code": "ValidationError",
                    "Message": "Stack with id test-stack does not exist",
                }
            },
            "DescribeStacks",
        )

        assert serverless_mode.get_job_status([resource_id])[resource_id] == (
            STATUS_SUCCEEDED
        )

    def test_throttled_describe_degrades_to_unknown(self, serverless_mode):
        """A transient describe failure yields UNKNOWN rather than raising.

        UNKNOWN keeps the job pollable; a raise would abort the whole run over a
        throttle.
        """
        resource_id = self._track(serverless_mode)
        serverless_mode.cf_client.describe_stacks.side_effect = ClientError(
            {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}},
            "DescribeStacks",
        )

        assert serverless_mode.get_job_status([resource_id])[resource_id] == (
            STATUS_UNKNOWN
        )


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

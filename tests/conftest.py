"""Pytest configuration for Parsl Ephemeral AWS Provider.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import os
import pytest
import boto3
import logging
import requests
from unittest.mock import MagicMock
from typing import Generator

from tests.substrate_support import (
    create_substrate_session,
    get_substrate_endpoint,
    is_substrate_running,
)

# Configure logging for tests
logging.basicConfig(level=logging.INFO)

# Test configuration
AWS_TEST_REGION = os.environ.get("AWS_TEST_REGION", "us-west-2")
AWS_TEST_PROFILE = os.environ.get("AWS_TEST_PROFILE", "aws")


@pytest.fixture(scope="session", autouse=True)
def moto_managed_policies():
    """Make moto serve the real AWS managed-policy set.

    Without ``MOTO_IAM_LOAD_MANAGED_POLICIES`` moto's IAM backend has *zero*
    managed policies, so every ``attach_role_policy`` for an
    ``arn:aws:iam::aws:policy/...`` ARN fails with ``NoSuchEntity``. That covers
    the IAM half of Lambda (``AWSLambdaBasicExecutionRole``), ECS
    (``AmazonECSTaskExecutionRolePolicy``), and Spot Fleet
    (``AmazonEC2SpotFleetTaggingRole``) setup — i.e. the roles all three of those
    managers create before they can do anything.

    It is set here rather than per-module because moto reads it lazily, on first
    IAM use, so a single session-wide value covers both the unit and integration
    moto suites without ordering constraints.
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("MOTO_IAM_LOAD_MANAGED_POLICIES", "true")
        yield


def pytest_collection_modifyitems(config, items):
    """Neutralize real AWS credentials for every test that is not AWS-marked.

    A ``@mock_aws``-decorated *class* only wraps its ``test_*`` methods; pytest
    fixtures defined on that class run outside the mock. A fixture that calls
    ``create_vpc`` therefore reaches real AWS with whatever ambient credentials
    the developer has — which is exactly what happened: a moto integration test
    hit the live account and failed ``VpcLimitExceeded`` against production VPCs.

    Marking the fixture bug fixed is not enough, because nothing stops the next
    one. Any test without ``@pytest.mark.aws`` gets synthetic credentials and a
    fixed region, so an un-mocked call fails as an auth error against a fake
    account instead of mutating a real one.
    """
    fake_env = {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    }
    for item in items:
        if item.get_closest_marker("aws") is None:
            for name, value in fake_env.items():
                item.add_marker(pytest.mark.setenv_aws(name, value))


@pytest.fixture(autouse=True)
def _neutralize_aws_credentials(request, monkeypatch):
    """Apply the synthetic credentials chosen in ``pytest_collection_modifyitems``.

    Autouse and function-scoped so it is torn down per test, and applied before
    any test-local fixture that builds a boto3 client. ``AWS_PROFILE`` is dropped
    too: a profile in ``~/.aws/credentials`` outranks these variables in
    botocore's chain, so leaving it set would defeat the guard entirely.
    """
    if request.node.get_closest_marker("aws") is not None:
        return
    for marker in request.node.iter_markers("setenv_aws"):
        monkeypatch.setenv(*marker.args)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)


@pytest.fixture
def aws_credentials():
    """Mocked AWS Credentials for boto3."""
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
    yield
    # Clean up
    del os.environ["AWS_ACCESS_KEY_ID"]
    del os.environ["AWS_SECRET_ACCESS_KEY"]
    del os.environ["AWS_SECURITY_TOKEN"]
    del os.environ["AWS_SESSION_TOKEN"]
    del os.environ["AWS_DEFAULT_REGION"]


@pytest.fixture
def mock_provider():
    """Create a mock provider for testing."""
    provider = MagicMock()
    provider.workflow_id = "test-workflow-id"
    provider.region = "us-east-1"
    provider.image_id = "ami-12345678"
    provider.instance_type = "t3.micro"
    provider.vpc_id = "vpc-12345678"
    provider.subnet_id = "subnet-12345678"
    provider.security_group_id = "sg-12345678"
    provider.tags = {"TestTag": "TestValue"}
    provider.aws_access_key_id = None
    provider.aws_secret_access_key = None
    provider.aws_session_token = None
    provider.aws_profile = None
    return provider


@pytest.fixture
def mock_ec2_client():
    """Create a mock EC2 client."""
    client = MagicMock()

    # Mock run_instances
    client.run_instances.return_value = {
        "Instances": [
            {
                "InstanceId": "i-12345678",
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
                        "InstanceId": "i-12345678",
                        "State": {"Name": "running"},
                        "PrivateIpAddress": "10.0.0.1",
                        "PublicIpAddress": "54.123.456.789",
                    }
                ]
            }
        ]
    }

    # Mock create_vpc
    client.create_vpc.return_value = {
        "Vpc": {
            "VpcId": "vpc-12345678",
            "CidrBlock": "10.0.0.0/16",
            "State": "available",
        }
    }

    # Mock create_subnet
    client.create_subnet.return_value = {
        "Subnet": {
            "SubnetId": "subnet-12345678",
            "VpcId": "vpc-12345678",
            "CidrBlock": "10.0.0.0/24",
            "State": "available",
        }
    }

    # Mock create_security_group
    client.create_security_group.return_value = {"GroupId": "sg-12345678"}

    return client


@pytest.fixture
def mock_s3_client():
    """Create a mock S3 client."""
    client = MagicMock()

    # Mock get_object
    client.get_object.return_value = {
        "Body": MagicMock(read=lambda: b'{"key": "value"}')
    }

    return client


@pytest.fixture
def mock_ssm_client():
    """Create a mock SSM client."""
    client = MagicMock()

    # Mock get_parameter
    client.get_parameter.return_value = {
        "Parameter": {
            "Name": "/parsl/workflows/test",
            "Value": '{"key": "value"}',
            "Version": 1,
        }
    }

    return client


@pytest.fixture
def mock_lambda_client():
    """Create a mock Lambda client."""
    client = MagicMock()

    # Mock create_function
    client.create_function.return_value = {
        "FunctionName": "test-function",
        "FunctionArn": "arn:aws:lambda:us-east-1:123456789012:function:test-function",
    }

    # Mock invoke
    client.invoke.return_value = {
        "StatusCode": 200,
        "Payload": MagicMock(read=lambda: b'{"statusCode": 200, "body": "Success"}'),
    }

    return client


@pytest.fixture
def mock_ecs_client():
    """Create a mock ECS client."""
    client = MagicMock()

    # Mock create_cluster
    client.create_cluster.return_value = {
        "cluster": {
            "clusterName": "test-cluster",
            "clusterArn": "arn:aws:ecs:us-east-1:123456789012:cluster/test-cluster",
        }
    }

    # Mock register_task_definition
    client.register_task_definition.return_value = {
        "taskDefinition": {
            "taskDefinitionArn": "arn:aws:ecs:us-east-1:123456789012:task-definition/test-task:1",
            "family": "test-task",
            "revision": 1,
        }
    }

    # Mock run_task
    client.run_task.return_value = {
        "tasks": [
            {
                "taskArn": "arn:aws:ecs:us-east-1:123456789012:task/test-cluster/abcdef12345",
                "lastStatus": "PENDING",
            }
        ]
    }

    return client


@pytest.fixture
def mock_iam_client():
    """Create a mock IAM client that simulates missing resources."""
    from botocore.exceptions import ClientError

    client = MagicMock()
    client.get_role.side_effect = ClientError(
        {"Error": {"Code": "NoSuchEntityException", "Message": "Role does not exist"}},
        "GetRole",
    )
    client.create_role.return_value = {
        "Role": {"Arn": "arn:aws:iam::123456789012:role/test-role"}
    }
    client.get_instance_profile.side_effect = ClientError(
        {
            "Error": {
                "Code": "NoSuchEntityException",
                "Message": "Instance profile does not exist",
            }
        },
        "GetInstanceProfile",
    )
    client.create_instance_profile.return_value = {
        "InstanceProfile": {
            "Arn": "arn:aws:iam::123456789012:instance-profile/test-profile"
        }
    }
    return client


@pytest.fixture
def mock_boto3_session(
    aws_credentials,
    mock_ec2_client,
    mock_s3_client,
    mock_ssm_client,
    mock_lambda_client,
    mock_ecs_client,
    mock_iam_client,
):
    """Create a mock boto3 session with all needed clients."""
    session = MagicMock()

    # Configure clients
    def get_client(service_name, **kwargs):
        if service_name == "ec2":
            return mock_ec2_client
        elif service_name == "s3":
            return mock_s3_client
        elif service_name == "ssm":
            return mock_ssm_client
        elif service_name == "lambda":
            return mock_lambda_client
        elif service_name == "ecs":
            return mock_ecs_client
        elif service_name == "iam":
            return mock_iam_client
        else:
            return MagicMock()

    session.client = get_client

    # Configure resources
    session.resource = MagicMock(return_value=MagicMock())

    return session


@pytest.fixture
def substrate_endpoint() -> str:
    """Return the substrate emulator endpoint URL."""
    return get_substrate_endpoint()


@pytest.fixture
def substrate_running(substrate_endpoint) -> bool:
    """Return whether a substrate server answers at the endpoint."""
    return is_substrate_running(substrate_endpoint)


@pytest.fixture
def boto3_substrate_session(
    aws_credentials, substrate_endpoint, substrate_running
) -> boto3.Session:
    """Boto3 session for substrate, skipping the test if it is not running."""
    if not substrate_running:
        pytest.skip(f"substrate is not running at {substrate_endpoint}")

    return create_substrate_session(endpoint=substrate_endpoint)


@pytest.fixture(scope="session")
def substrate_available() -> bool:
    """Return whether substrate is up with the services this suite needs.

    Session-scoped, so the probe runs once per run rather than per test.

    ``/_localstack/health`` rather than ``/health``: the latter reports only
    liveness, while this returns the per-service map. Substrate serves it
    deliberately, so tools that poll LocalStack's health route work unchanged.
    """
    endpoint = get_substrate_endpoint()
    try:
        response = requests.get(f"{endpoint}/_localstack/health", timeout=5)
        if response.status_code == 200:
            services = response.json().get("services", {})
            return all(
                services.get(name) == "available"
                for name in ("ec2", "lambda", "s3", "ssm")
            )
    except Exception as exc:
        logging.warning("substrate health check failed: %s", exc)
    return False


@pytest.fixture
def substrate_session(substrate_available) -> Generator[boto3.Session, None, None]:
    """Boto3 session whose clients are pre-bound to the substrate endpoint.

    ``session.client`` is wrapped rather than each call site passing
    ``endpoint_url``, so code under test that builds its own clients from this
    session still reaches the emulator.
    """
    if not substrate_available:
        pytest.skip("substrate not available - start with 'make substrate-up'")

    endpoint = get_substrate_endpoint()
    session = create_substrate_session(region=AWS_TEST_REGION, endpoint=endpoint)

    original_client = session.client

    def substrate_client(service_name, **kwargs):
        kwargs.setdefault("endpoint_url", endpoint)
        return original_client(service_name, **kwargs)

    session.client = substrate_client
    yield session


@pytest.fixture
def aws_session() -> Generator[boto3.Session, None, None]:
    """Boto3 session configured for real AWS with 'aws' profile."""
    try:
        session = boto3.Session(
            profile_name=AWS_TEST_PROFILE, region_name=AWS_TEST_REGION
        )
        # Test that the session works
        sts = session.client("sts")
        sts.get_caller_identity()
        yield session
    except Exception as e:
        pytest.skip(
            f"AWS profile '{AWS_TEST_PROFILE}' not available or not configured: {e}"
        )


@pytest.fixture
def test_session(request, substrate_session, aws_session) -> boto3.Session:
    """Choose the substrate emulator or real AWS based on test markers.

    ``@pytest.mark.aws`` selects real AWS; anything else gets substrate, so a
    test that forgets its marker cannot reach a live account by accident.
    """
    if request.node.get_closest_marker("aws"):
        return aws_session
    return substrate_session


@pytest.fixture
def ephemeral_provider_config():
    """Base configuration for EphemeralAWSProvider testing."""
    return {
        "image_id": "ami-0abcdef1234567890",  # Amazon Linux 2 AMI (example)
        "instance_type": "t3.micro",
        "region": AWS_TEST_REGION,
        "min_blocks": 0,
        "max_blocks": 2,
        "debug": True,
        "auto_shutdown": True,
        "max_idle_time": 60,  # 1 minute for faster tests
    }

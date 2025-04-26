"""Pytest configuration for Parsl Ephemeral AWS Provider.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import os
import pytest
import boto3
import logging
from unittest.mock import MagicMock

# Configure logging for tests
logging.basicConfig(level=logging.INFO)


@pytest.fixture
def aws_credentials():
    """Mocked AWS Credentials for boto3."""
    os.environ['AWS_ACCESS_KEY_ID'] = 'testing'
    os.environ['AWS_SECRET_ACCESS_KEY'] = 'testing'
    os.environ['AWS_SECURITY_TOKEN'] = 'testing'
    os.environ['AWS_SESSION_TOKEN'] = 'testing'
    os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
    yield
    # Clean up
    del os.environ['AWS_ACCESS_KEY_ID']
    del os.environ['AWS_SECRET_ACCESS_KEY']
    del os.environ['AWS_SECURITY_TOKEN']
    del os.environ['AWS_SESSION_TOKEN']
    del os.environ['AWS_DEFAULT_REGION']


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
    
    # Mock create_vpc
    client.create_vpc.return_value = {
        'Vpc': {
            'VpcId': 'vpc-12345678',
            'CidrBlock': '10.0.0.0/16',
            'State': 'available'
        }
    }
    
    # Mock create_subnet
    client.create_subnet.return_value = {
        'Subnet': {
            'SubnetId': 'subnet-12345678',
            'VpcId': 'vpc-12345678',
            'CidrBlock': '10.0.0.0/24',
            'State': 'available'
        }
    }
    
    # Mock create_security_group
    client.create_security_group.return_value = {
        'GroupId': 'sg-12345678'
    }
    
    return client


@pytest.fixture
def mock_s3_client():
    """Create a mock S3 client."""
    client = MagicMock()
    
    # Mock get_object
    client.get_object.return_value = {
        'Body': MagicMock(
            read=lambda: b'{"key": "value"}'
        )
    }
    
    return client


@pytest.fixture
def mock_ssm_client():
    """Create a mock SSM client."""
    client = MagicMock()
    
    # Mock get_parameter
    client.get_parameter.return_value = {
        'Parameter': {
            'Name': '/parsl/workflows/test',
            'Value': '{"key": "value"}',
            'Version': 1
        }
    }
    
    return client


@pytest.fixture
def mock_lambda_client():
    """Create a mock Lambda client."""
    client = MagicMock()
    
    # Mock create_function
    client.create_function.return_value = {
        'FunctionName': 'test-function',
        'FunctionArn': 'arn:aws:lambda:us-east-1:123456789012:function:test-function'
    }
    
    # Mock invoke
    client.invoke.return_value = {
        'StatusCode': 200,
        'Payload': MagicMock(
            read=lambda: b'{"statusCode": 200, "body": "Success"}'
        )
    }
    
    return client


@pytest.fixture
def mock_ecs_client():
    """Create a mock ECS client."""
    client = MagicMock()
    
    # Mock create_cluster
    client.create_cluster.return_value = {
        'cluster': {
            'clusterName': 'test-cluster',
            'clusterArn': 'arn:aws:ecs:us-east-1:123456789012:cluster/test-cluster'
        }
    }
    
    # Mock register_task_definition
    client.register_task_definition.return_value = {
        'taskDefinition': {
            'taskDefinitionArn': 'arn:aws:ecs:us-east-1:123456789012:task-definition/test-task:1',
            'family': 'test-task',
            'revision': 1
        }
    }
    
    # Mock run_task
    client.run_task.return_value = {
        'tasks': [
            {
                'taskArn': 'arn:aws:ecs:us-east-1:123456789012:task/test-cluster/abcdef12345',
                'lastStatus': 'PENDING'
            }
        ]
    }
    
    return client


@pytest.fixture
def mock_boto3_session(aws_credentials, mock_ec2_client, mock_s3_client, mock_ssm_client, mock_lambda_client, mock_ecs_client):
    """Create a mock boto3 session with all needed clients."""
    session = MagicMock()
    
    # Configure clients
    def get_client(service_name, **kwargs):
        if service_name == 'ec2':
            return mock_ec2_client
        elif service_name == 's3':
            return mock_s3_client
        elif service_name == 'ssm':
            return mock_ssm_client
        elif service_name == 'lambda':
            return mock_lambda_client
        elif service_name == 'ecs':
            return mock_ecs_client
        else:
            return MagicMock()
    
    session.client = get_client
    
    # Configure resources
    session.resource = MagicMock(return_value=MagicMock())
    
    return session


@pytest.fixture
def localstack_endpoint():
    """Get the LocalStack endpoint URL."""
    # Default LocalStack endpoint
    endpoint = "http://localhost:4566"
    
    # Override with environment variable if specified
    if 'LOCALSTACK_ENDPOINT' in os.environ:
        endpoint = os.environ['LOCALSTACK_ENDPOINT']
    
    return endpoint


@pytest.fixture
def is_localstack_running(localstack_endpoint):
    """Check if LocalStack is running."""
    try:
        # Try to connect to the health endpoint
        import requests
        response = requests.get(f"{localstack_endpoint}/health", timeout=1)
        return response.status_code == 200
    except Exception:
        return False


@pytest.fixture
def boto3_localstack_session(aws_credentials, localstack_endpoint, is_localstack_running):
    """Create a boto3 session that connects to LocalStack."""
    if not is_localstack_running:
        pytest.skip("LocalStack is not running")
    
    # Return session configured for LocalStack
    return boto3.Session(
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name="us-east-1"
    )
# Testing with LocalStack

This guide explains how to use LocalStack for testing the Parsl Ephemeral AWS Provider without using real AWS resources.

## What is LocalStack?

[LocalStack](https://github.com/localstack/localstack) is a fully functional local AWS cloud stack that provides an environment for testing and developing cloud applications offline. It provides local versions of many AWS services including EC2, S3, Lambda, and more.

## Benefits of LocalStack Testing

Using LocalStack for testing the Parsl Ephemeral AWS Provider offers several advantages:

- **Cost-free testing**: No real AWS resources are created, so you don't incur any AWS charges
- **Faster tests**: Operations against LocalStack are typically faster than real AWS
- **Offline development**: You can develop and test without an internet connection
- **Consistent environment**: Tests run in a clean, isolated environment

## Setting Up LocalStack

### Installation

1. Install LocalStack:

```bash
pip install localstack
```

2. (Optional) Install the AWS CLI local wrapper:

```bash
pip install awscli-local
```

### Starting LocalStack

Start the LocalStack Docker container:

```bash
# Start LocalStack with the services we need
localstack start -d

# Verify that LocalStack is running
localstack status services
```

## Configuring the Provider for LocalStack

The Parsl Ephemeral AWS Provider can be configured to use LocalStack by setting the appropriate endpoint URLs:

```python
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
from parsl_ephemeral_aws import EphemeralAWSProvider

# Configure the ephemeral AWS provider with LocalStack
provider = EphemeralAWSProvider(
    image_id='ami-12345678',  # Can be any value in LocalStack
    instance_type='t3.medium',
    region='us-east-1',
    
    # LocalStack configuration
    use_localstack=True,
    localstack_endpoint='http://localhost:4566'
)

# Create Parsl configuration
config = Config(
    executors=[
        HighThroughputExecutor(
            label='aws_executor',
            provider=provider,
        )
    ]
)

# Load the configuration
parsl.load(config)
```

## Mocking AWS Resources in LocalStack

When testing with LocalStack, you need to set up some mock resources before running your tests:

### Setting Up Mock AMIs

```bash
# Create a mock AMI in LocalStack
awslocal ec2 register-image \
  --name "test-ami" \
  --root-device-name "/dev/xvda" \
  --block-device-mappings "DeviceName=/dev/xvda,Ebs={VolumeSize=8}" \
  --architecture x86_64
```

### Setting Up VPC and Networking

```bash
# Create a VPC
VPC_ID=$(awslocal ec2 create-vpc --cidr-block 10.0.0.0/16 --query 'Vpc.VpcId' --output text)

# Create a subnet
SUBNET_ID=$(awslocal ec2 create-subnet --vpc-id $VPC_ID --cidr-block 10.0.0.0/24 --query 'Subnet.SubnetId' --output text)

# Create an internet gateway
IGW_ID=$(awslocal ec2 create-internet-gateway --query 'InternetGateway.InternetGatewayId' --output text)

# Attach the internet gateway to the VPC
awslocal ec2 attach-internet-gateway --internet-gateway-id $IGW_ID --vpc-id $VPC_ID
```

### Setting Up Security Groups

```bash
# Create a security group
SG_ID=$(awslocal ec2 create-security-group --group-name test-sg --description "Test security group" --vpc-id $VPC_ID --query 'GroupId' --output text)

# Add inbound rules
awslocal ec2 authorize-security-group-ingress --group-id $SG_ID --protocol tcp --port 22 --cidr 0.0.0.0/0
awslocal ec2 authorize-security-group-ingress --group-id $SG_ID --protocol tcp --port 54000-55000 --cidr 0.0.0.0/0
```

## Integration Testing with LocalStack

Here's an example of a pytest fixture for testing with LocalStack:

```python
import pytest
import boto3
import os
from moto import mock_ec2, mock_iam, mock_s3, mock_ssm

@pytest.fixture
def aws_credentials():
    """Mocked AWS Credentials for boto3."""
    os.environ['AWS_ACCESS_KEY_ID'] = 'testing'
    os.environ['AWS_SECRET_ACCESS_KEY'] = 'testing'
    os.environ['AWS_SECURITY_TOKEN'] = 'testing'
    os.environ['AWS_SESSION_TOKEN'] = 'testing'
    os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'

@pytest.fixture
def localstack_endpoint():
    """LocalStack endpoint URL."""
    return "http://localhost:4566"

@pytest.fixture
def ec2_resource(aws_credentials, localstack_endpoint):
    """Mock EC2 resource using LocalStack."""
    return boto3.resource('ec2', endpoint_url=localstack_endpoint)

@pytest.fixture
def setup_mock_aws(ec2_resource, localstack_endpoint):
    """Set up mock AWS resources in LocalStack."""
    # Create a VPC
    vpc = ec2_resource.create_vpc(CidrBlock='10.0.0.0/16')
    vpc.create_tags(Tags=[{'Key': 'Name', 'Value': 'test-vpc'}])
    
    # Create a subnet
    subnet = ec2_resource.create_subnet(
        VpcId=vpc.id,
        CidrBlock='10.0.0.0/24',
        AvailabilityZone='us-east-1a'
    )
    
    # Create and attach an internet gateway
    igw = ec2_resource.create_internet_gateway()
    vpc.attach_internet_gateway(InternetGatewayId=igw.id)
    
    # Create a security group
    sg = ec2_resource.create_security_group(
        GroupName='test-sg',
        Description='Test security group',
        VpcId=vpc.id
    )
    sg.authorize_ingress(
        IpPermissions=[
            {
                'IpProtocol': 'tcp',
                'FromPort': 22,
                'ToPort': 22,
                'IpRanges': [{'CidrIp': '0.0.0.0/0'}]
            },
            {
                'IpProtocol': 'tcp',
                'FromPort': 54000,
                'ToPort': 55000,
                'IpRanges': [{'CidrIp': '0.0.0.0/0'}]
            }
        ]
    )
    
    # Return the created resources
    return {
        'vpc_id': vpc.id,
        'subnet_id': subnet.id,
        'security_group_id': sg.id,
        'internet_gateway_id': igw.id
    }

def test_ephemeral_aws_provider(setup_mock_aws, localstack_endpoint):
    """Test the EphemeralAWSProvider with LocalStack."""
    from parsl_ephemeral_aws import EphemeralAWSProvider
    
    # Create provider using LocalStack
    provider = EphemeralAWSProvider(
        image_id='ami-12345678',  # Any value works in LocalStack
        instance_type='t3.medium',
        region='us-east-1',
        init_blocks=1,
        
        # Use existing network resources
        vpc_id=setup_mock_aws['vpc_id'],
        subnet_id=setup_mock_aws['subnet_id'],
        security_group_id=setup_mock_aws['security_group_id'],
        
        # LocalStack configuration
        use_localstack=True,
        localstack_endpoint=localstack_endpoint
    )
    
    # Test provider operations
    # ...
```

## Simulating AWS Service Responses

LocalStack doesn't fully implement all AWS service behaviors. For more specific testing, you can combine LocalStack with moto or custom mocks:

```python
import boto3
from unittest.mock import patch, MagicMock

def test_ec2_instance_creation():
    # Use moto to mock EC2 API for specific responses
    with patch('boto3.client') as mock_client:
        ec2_mock = MagicMock()
        mock_client.return_value = ec2_mock
        
        # Mock the run_instances response
        ec2_mock.run_instances.return_value = {
            'Instances': [
                {
                    'InstanceId': 'i-12345678',
                    'State': {'Name': 'pending'},
                    'PrivateIpAddress': '10.0.0.1',
                    'PublicIpAddress': '54.123.456.789'
                }
            ]
        }
        
        # Test your code that calls run_instances
        # ...
```

## Known Limitations

When testing with LocalStack, be aware of these limitations:

1. **Limited EC2 functionality**: LocalStack doesn't fully simulate EC2 instance behavior
2. **No actual compute resources**: Instances don't run code; you need to mock their behavior
3. **Feature gaps**: Not all AWS API features are implemented in LocalStack
4. **Version sensitivity**: Different versions of LocalStack may have different feature sets

## Best Practices

1. **Combined approach**: Use LocalStack for integration tests and unittest mocks for unit tests
2. **Resource cleanup**: Always make sure tests clean up resources, even after failures
3. **Separate test configurations**: Keep LocalStack test configurations separate from production ones
4. **Minimal permissions**: Test with minimal IAM permissions to ensure your app works correctly

## Troubleshooting

### LocalStack Connection Issues

If you're having trouble connecting to LocalStack:

```bash
# Check if LocalStack is running
localstack status services

# Check the logs for any errors
localstack logs
```

### Missing AWS Features in LocalStack

If you're missing AWS features in LocalStack:

1. Check the LocalStack documentation for supported features
2. Consider mocking those specific API calls
3. Use the pro version of LocalStack for more features

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
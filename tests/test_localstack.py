"""Tests using LocalStack for AWS service simulation.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import pytest
import boto3
import os
import uuid
import time
from unittest.mock import patch

# Mark the entire module as requiring LocalStack
pytestmark = pytest.mark.skipif(
    os.environ.get('SKIP_LOCALSTACK_TESTS', 'False').lower() == 'true',
    reason="Skipping LocalStack tests"
)


@pytest.fixture
def ec2_client(boto3_localstack_session, localstack_endpoint):
    """Create an EC2 client connected to LocalStack."""
    return boto3_localstack_session.client(
        'ec2',
        endpoint_url=localstack_endpoint
    )


@pytest.fixture
def s3_client(boto3_localstack_session, localstack_endpoint):
    """Create an S3 client connected to LocalStack."""
    return boto3_localstack_session.client(
        's3',
        endpoint_url=localstack_endpoint
    )


@pytest.fixture
def ssm_client(boto3_localstack_session, localstack_endpoint):
    """Create an SSM client connected to LocalStack."""
    return boto3_localstack_session.client(
        'ssm',
        endpoint_url=localstack_endpoint
    )


@pytest.fixture
def lambda_client(boto3_localstack_session, localstack_endpoint):
    """Create a Lambda client connected to LocalStack."""
    return boto3_localstack_session.client(
        'lambda',
        endpoint_url=localstack_endpoint
    )


@pytest.fixture
def setup_vpc(ec2_client):
    """Set up a VPC in LocalStack for testing."""
    # Create a VPC
    vpc_response = ec2_client.create_vpc(CidrBlock='10.0.0.0/16')
    vpc_id = vpc_response['Vpc']['VpcId']
    
    # Tag the VPC
    ec2_client.create_tags(
        Resources=[vpc_id],
        Tags=[
            {'Key': 'Name', 'Value': 'test-vpc'},
            {'Key': 'ParslResource', 'Value': 'true'},
            {'Key': 'ParslWorkflowId', 'Value': 'test-workflow'}
        ]
    )
    
    # Create a subnet
    subnet_response = ec2_client.create_subnet(
        VpcId=vpc_id,
        CidrBlock='10.0.0.0/24'
    )
    subnet_id = subnet_response['Subnet']['SubnetId']
    
    # Create an internet gateway
    igw_response = ec2_client.create_internet_gateway()
    igw_id = igw_response['InternetGateway']['InternetGatewayId']
    
    # Attach the internet gateway to the VPC
    ec2_client.attach_internet_gateway(
        InternetGatewayId=igw_id,
        VpcId=vpc_id
    )
    
    # Create a security group
    sg_response = ec2_client.create_security_group(
        GroupName='test-sg',
        Description='Test security group',
        VpcId=vpc_id
    )
    sg_id = sg_response['GroupId']
    
    # Add inbound rules
    ec2_client.authorize_security_group_ingress(
        GroupId=sg_id,
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
    
    yield {
        'vpc_id': vpc_id,
        'subnet_id': subnet_id,
        'security_group_id': sg_id,
        'internet_gateway_id': igw_id
    }
    
    # Clean up resources (optional in LocalStack)
    try:
        ec2_client.delete_security_group(GroupId=sg_id)
        ec2_client.detach_internet_gateway(
            InternetGatewayId=igw_id,
            VpcId=vpc_id
        )
        ec2_client.delete_internet_gateway(InternetGatewayId=igw_id)
        ec2_client.delete_subnet(SubnetId=subnet_id)
        ec2_client.delete_vpc(VpcId=vpc_id)
    except Exception as e:
        print(f"Error cleaning up resources: {e}")


@pytest.fixture
def setup_s3_bucket(s3_client):
    """Set up an S3 bucket in LocalStack for testing."""
    # Create a unique bucket name
    bucket_name = f"test-bucket-{uuid.uuid4().hex[:8]}"
    
    # Create the bucket
    s3_client.create_bucket(Bucket=bucket_name)
    
    yield bucket_name
    
    # Clean up the bucket
    try:
        # Delete all objects in the bucket
        response = s3_client.list_objects_v2(Bucket=bucket_name)
        if 'Contents' in response:
            for obj in response['Contents']:
                s3_client.delete_object(Bucket=bucket_name, Key=obj['Key'])
        
        # Delete the bucket
        s3_client.delete_bucket(Bucket=bucket_name)
    except Exception as e:
        print(f"Error cleaning up S3 bucket: {e}")


@pytest.mark.localstack
def test_vpc_creation(ec2_client):
    """Test VPC creation in LocalStack."""
    # Create a VPC
    vpc_response = ec2_client.create_vpc(CidrBlock='10.0.0.0/16')
    vpc_id = vpc_response['Vpc']['VpcId']
    
    # Verify VPC was created
    describe_response = ec2_client.describe_vpcs(VpcIds=[vpc_id])
    assert len(describe_response['Vpcs']) == 1
    assert describe_response['Vpcs'][0]['VpcId'] == vpc_id
    assert describe_response['Vpcs'][0]['CidrBlock'] == '10.0.0.0/16'


@pytest.mark.localstack
def test_s3_state_storage(s3_client, setup_s3_bucket):
    """Test S3 state storage using LocalStack."""
    bucket_name = setup_s3_bucket
    test_key = 'test/state.json'
    test_data = '{"workflow_id": "test", "status": "running"}'
    
    # Upload test data
    s3_client.put_object(
        Bucket=bucket_name,
        Key=test_key,
        Body=test_data,
        ContentType='application/json'
    )
    
    # Get the object
    response = s3_client.get_object(
        Bucket=bucket_name,
        Key=test_key
    )
    
    # Read the data
    data = response['Body'].read().decode('utf-8')
    
    # Verify data
    assert data == test_data


@pytest.mark.localstack
def test_ssm_parameter_store(ssm_client):
    """Test Parameter Store using LocalStack."""
    parameter_name = '/parsl/workflows/test-workflow/state'
    parameter_value = '{"status": "running", "blocks": 2}'
    
    # Put parameter
    ssm_client.put_parameter(
        Name=parameter_name,
        Value=parameter_value,
        Type='String'
    )
    
    # Get parameter
    response = ssm_client.get_parameter(
        Name=parameter_name
    )
    
    # Verify parameter
    assert response['Parameter']['Name'] == parameter_name
    assert response['Parameter']['Value'] == parameter_value
    
    # Delete parameter
    ssm_client.delete_parameter(
        Name=parameter_name
    )
    
    # Verify deletion
    with pytest.raises(Exception) as e:
        ssm_client.get_parameter(
            Name=parameter_name
        )
    assert 'ParameterNotFound' in str(e)


@pytest.mark.localstack
def test_ec2_instance_lifecycle(ec2_client, setup_vpc):
    """Test EC2 instance lifecycle using LocalStack."""
    # This test will need a lot of mocking in LocalStack
    # since it doesn't fully implement EC2
    vpc_resources = setup_vpc
    
    # Register an AMI (mock)
    ami_response = ec2_client.register_image(
        Name='test-ami',
        RootDeviceName='/dev/xvda',
        BlockDeviceMappings=[
            {
                'DeviceName': '/dev/xvda',
                'Ebs': {
                    'VolumeSize': 8
                }
            }
        ],
        Architecture='x86_64'
    )
    ami_id = ami_response['ImageId']
    
    # Launch an instance
    instance_response = ec2_client.run_instances(
        ImageId=ami_id,
        InstanceType='t3.micro',
        MinCount=1,
        MaxCount=1,
        SubnetId=vpc_resources['subnet_id'],
        SecurityGroupIds=[vpc_resources['security_group_id']],
        TagSpecifications=[
            {
                'ResourceType': 'instance',
                'Tags': [
                    {'Key': 'Name', 'Value': 'test-instance'},
                    {'Key': 'ParslResource', 'Value': 'true'},
                    {'Key': 'ParslWorkflowId', 'Value': 'test-workflow'}
                ]
            }
        ]
    )
    
    instance_id = instance_response['Instances'][0]['InstanceId']
    
    # Verify instance was created
    describe_response = ec2_client.describe_instances(InstanceIds=[instance_id])
    assert len(describe_response['Reservations']) > 0
    assert len(describe_response['Reservations'][0]['Instances']) > 0
    assert describe_response['Reservations'][0]['Instances'][0]['InstanceId'] == instance_id
    
    # Terminate instance
    ec2_client.terminate_instances(InstanceIds=[instance_id])
    
    # LocalStack doesn't fully simulate instance state changes,
    # so we can't reliably check termination status


@pytest.mark.localstack
@pytest.mark.skip(reason="LocalStack doesn't fully support Lambda function creation and invocation")
def test_lambda_function(lambda_client):
    """Test Lambda function in LocalStack."""
    # Create a basic Lambda function
    function_name = 'test-lambda-function'
    
    # Create function ZIP file with simple handler
    import io
    import zipfile
    
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w') as zip_file:
        zip_file.writestr('index.py', """
def handler(event, context):
    return {
        'statusCode': 200,
        'body': 'Hello from Lambda!'
    }
""")
    
    # Create the Lambda function
    lambda_client.create_function(
        FunctionName=function_name,
        Runtime='python3.9',
        Role='arn:aws:iam::123456789012:role/test-role',
        Handler='index.handler',
        Code={
            'ZipFile': zip_buffer.getvalue()
        },
        Description='Test Lambda function',
        Timeout=30,
        MemorySize=128
    )
    
    # Invoke the function
    response = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType='RequestResponse'
    )
    
    # LocalStack doesn't fully implement Lambda invocation,
    # so the actual response might not be meaningful


@pytest.mark.integration
@pytest.mark.skipif(True, reason="Requires provider implementation with LocalStack")
def test_provider_with_localstack(is_localstack_running, localstack_endpoint):
    """Integration test with Parsl Ephemeral AWS Provider and LocalStack."""
    if not is_localstack_running:
        pytest.skip("LocalStack is not running")
    
    try:
        # This will be implemented once the provider has LocalStack support
        from parsl_ephemeral_aws import EphemeralAWSProvider
        
        provider = EphemeralAWSProvider(
            image_id='ami-12345678',  # Any value works in LocalStack
            instance_type='t3.medium',
            region='us-east-1',
            init_blocks=1,
            
            # LocalStack configuration
            use_localstack=True,
            localstack_endpoint=localstack_endpoint
        )
        
        # Test provider operations
        # ...
        
        # Clean up
        provider.shutdown()
        
    except ImportError:
        pytest.skip("Provider not available")
    except Exception as e:
        pytest.fail(f"Test failed: {e}")
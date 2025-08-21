"""Unit tests with AWS mocking using Moto.

These tests use the Moto library to mock AWS services, providing isolated and
comprehensive testing of AWS interactions without requiring LocalStack or AWS.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import os
import uuid
import json
import pytest
from unittest.mock import patch, MagicMock
import boto3

try:
    # Import moto decorators
    from moto import (
        mock_ec2,
        mock_s3,
        mock_ssm,
        mock_iam,
        mock_lambda,
        mock_ecs,
        mock_cloudformation,
    )

    MOTO_AVAILABLE = True
except ImportError:
    MOTO_AVAILABLE = False

    # Create placeholder decorators if moto is not available
    def mock_decorator(func):
        return pytest.mark.skip(reason="Moto library not available")(func)

    mock_ec2 = (
        mock_s3
    ) = (
        mock_ssm
    ) = mock_iam = mock_lambda = mock_ecs = mock_cloudformation = mock_decorator

# Mark tests as requiring moto
pytestmark = pytest.mark.skipif(
    not MOTO_AVAILABLE,
    reason="Moto library not available. Install with: pip install moto",
)

from parsl_ephemeral_aws.compute.ec2 import EC2Manager
from parsl_ephemeral_aws.compute.spot_fleet import SpotFleetManager
from parsl_ephemeral_aws.compute.lambda_func import LambdaManager
from parsl_ephemeral_aws.compute.ecs import ECSManager
from parsl_ephemeral_aws.network.vpc import VPCManager
from parsl_ephemeral_aws.network.security import SecurityGroupManager
from parsl_ephemeral_aws.state.parameter_store import ParameterStoreState
from parsl_ephemeral_aws.state.s3 import S3State
from parsl_ephemeral_aws.modes.serverless import ServerlessMode


@pytest.fixture
def region():
    """AWS region for testing."""
    return "us-east-1"


@pytest.fixture
def aws_credentials():
    """Mocked AWS credentials for testing."""
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
def moto_session(aws_credentials, region):
    """Create a boto3 session for moto tests."""
    return boto3.Session(region_name=region)


class TestVPCWithMoto:
    """Test VPCManager with moto mocked AWS."""

    @mock_ec2
    def test_vpc_creation_and_deletion(self, moto_session):
        """Test creating and deleting a VPC."""
        vpc_manager = VPCManager(session=moto_session, region="us-east-1")

        # Create a VPC
        vpc_id = vpc_manager.create_vpc(
            cidr_block="10.0.0.0/16", tags={"Name": "test-vpc", "TestKey": "TestValue"}
        )

        # Verify VPC was created
        ec2_client = moto_session.client("ec2")
        response = ec2_client.describe_vpcs(VpcIds=[vpc_id])

        assert len(response["Vpcs"]) == 1
        assert response["Vpcs"][0]["VpcId"] == vpc_id
        assert response["Vpcs"][0]["CidrBlock"] == "10.0.0.0/16"

        # Create a subnet
        subnet_id = vpc_manager.create_subnet(
            vpc_id=vpc_id,
            cidr_block="10.0.0.0/24",
            availability_zone=f"{moto_session.region_name}a",
            tags={"Name": "test-subnet"},
        )

        # Verify subnet was created
        response = ec2_client.describe_subnets(SubnetIds=[subnet_id])
        assert len(response["Subnets"]) == 1
        assert response["Subnets"][0]["SubnetId"] == subnet_id
        assert response["Subnets"][0]["VpcId"] == vpc_id

        # Delete the subnet
        vpc_manager.delete_subnet(subnet_id)

        # Try to describe the subnet (should fail)
        try:
            ec2_client.describe_subnets(SubnetIds=[subnet_id])
            assert False, "Subnet should be deleted"
        except ec2_client.exceptions.ClientError as e:
            # Expected error because subnet is deleted
            assert "InvalidSubnetID.NotFound" in str(e)

        # Delete the VPC
        vpc_manager.delete_vpc(vpc_id)

        # Try to describe the VPC (should fail)
        try:
            ec2_client.describe_vpcs(VpcIds=[vpc_id])
            assert False, "VPC should be deleted"
        except ec2_client.exceptions.ClientError as e:
            # Expected error because VPC is deleted
            assert "InvalidVpcID.NotFound" in str(e)


class TestSecurityGroupWithMoto:
    """Test SecurityGroupManager with moto mocked AWS."""

    @mock_ec2
    def test_security_group_creation_and_deletion(self, moto_session):
        """Test creating and deleting a security group."""
        # First create a VPC
        vpc_manager = VPCManager(session=moto_session, region="us-east-1")
        vpc_id = vpc_manager.create_vpc(
            cidr_block="10.0.0.0/16", tags={"Name": "test-vpc"}
        )

        # Create a security group manager
        sg_manager = SecurityGroupManager(session=moto_session, region="us-east-1")

        # Create a security group
        sg_id = sg_manager.create_security_group(
            name="test-sg",
            description="Test security group",
            vpc_id=vpc_id,
            tags={"Name": "test-sg"},
        )

        # Verify security group was created
        ec2_client = moto_session.client("ec2")
        response = ec2_client.describe_security_groups(GroupIds=[sg_id])

        assert len(response["SecurityGroups"]) == 1
        assert response["SecurityGroups"][0]["GroupId"] == sg_id
        assert response["SecurityGroups"][0]["GroupName"] == "test-sg"
        assert response["SecurityGroups"][0]["VpcId"] == vpc_id

        # Add ingress rules
        sg_manager.add_ingress_rule(
            security_group_id=sg_id,
            ip_protocol="tcp",
            from_port=22,
            to_port=22,
            cidr_ip="0.0.0.0/0",
        )

        # Verify ingress rule was added
        response = ec2_client.describe_security_groups(GroupIds=[sg_id])
        assert len(response["SecurityGroups"][0]["IpPermissions"]) == 1

        # Delete the security group
        sg_manager.delete_security_group(sg_id)

        # Try to describe the security group (should fail)
        try:
            ec2_client.describe_security_groups(GroupIds=[sg_id])
            assert False, "Security group should be deleted"
        except ec2_client.exceptions.ClientError as e:
            # Expected error because security group is deleted
            assert "InvalidGroup.NotFound" in str(e)

        # Clean up VPC
        vpc_manager.delete_vpc(vpc_id)


class TestEC2WithMoto:
    """Test EC2Manager with moto mocked AWS."""

    @mock_ec2
    def test_ec2_instance_lifecycle(self, moto_session):
        """Test EC2 instance creation, status, and termination."""
        # First create a VPC and subnet
        vpc_manager = VPCManager(session=moto_session, region="us-east-1")
        vpc_id = vpc_manager.create_vpc(
            cidr_block="10.0.0.0/16", tags={"Name": "test-vpc"}
        )

        subnet_id = vpc_manager.create_subnet(
            vpc_id=vpc_id,
            cidr_block="10.0.0.0/24",
            availability_zone=f"{moto_session.region_name}a",
            tags={"Name": "test-subnet"},
        )

        # Create a security group
        sg_manager = SecurityGroupManager(session=moto_session, region="us-east-1")
        sg_id = sg_manager.create_security_group(
            name="test-sg",
            description="Test security group",
            vpc_id=vpc_id,
            tags={"Name": "test-sg"},
        )

        # Create an EC2 manager
        ec2_manager = EC2Manager(
            session=moto_session,
            region="us-east-1",
            vpc_id=vpc_id,
            subnet_id=subnet_id,
            security_group_id=sg_id,
        )

        # Create test AMI
        ec2_client = moto_session.client("ec2")
        ami_response = ec2_client.run_instances(
            ImageId="ami-12345678",  # This doesn't matter in moto
            MinCount=1,
            MaxCount=1,
            InstanceType="t2.micro",
        )
        instance_id = ami_response["Instances"][0]["InstanceId"]

        # Create AMI from the instance
        image_response = ec2_client.create_image(
            InstanceId=instance_id, Name="test-ami"
        )
        ami_id = image_response["ImageId"]

        # Create an EC2 instance
        instance = ec2_manager.create_instance(
            image_id=ami_id,
            instance_type="t2.micro",
            min_count=1,
            max_count=1,
            tags={"Name": "test-instance"},
        )

        # Verify instance was created
        assert "instance_id" in instance
        instance_id = instance["instance_id"]

        # Get instance status
        status = ec2_manager.get_instance_status(instance_id)

        # Moto doesn't perfectly simulate all AWS behavior, but we should get a status
        assert status in [
            "pending",
            "running",
            "shutting-down",
            "terminated",
            "stopping",
            "stopped",
        ]

        # Terminate the instance
        ec2_manager.terminate_instance(instance_id)

        # Clean up
        sg_manager.delete_security_group(sg_id)
        vpc_manager.delete_subnet(subnet_id)
        vpc_manager.delete_vpc(vpc_id)

    @mock_ec2
    def test_ec2_instance_block(self, moto_session):
        """Test creating multiple EC2 instances as a block."""
        # First create a VPC and subnet
        vpc_manager = VPCManager(session=moto_session, region="us-east-1")
        vpc_id = vpc_manager.create_vpc(
            cidr_block="10.0.0.0/16", tags={"Name": "test-vpc"}
        )

        subnet_id = vpc_manager.create_subnet(
            vpc_id=vpc_id,
            cidr_block="10.0.0.0/24",
            availability_zone=f"{moto_session.region_name}a",
            tags={"Name": "test-subnet"},
        )

        # Create a security group
        sg_manager = SecurityGroupManager(session=moto_session, region="us-east-1")
        sg_id = sg_manager.create_security_group(
            name="test-sg",
            description="Test security group",
            vpc_id=vpc_id,
            tags={"Name": "test-sg"},
        )

        # Create an EC2 manager
        ec2_manager = EC2Manager(
            session=moto_session,
            region="us-east-1",
            vpc_id=vpc_id,
            subnet_id=subnet_id,
            security_group_id=sg_id,
        )

        # Create test AMI
        ec2_client = moto_session.client("ec2")
        ami_response = ec2_client.run_instances(
            ImageId="ami-12345678",  # This doesn't matter in moto
            MinCount=1,
            MaxCount=1,
            InstanceType="t2.micro",
        )
        instance_id = ami_response["Instances"][0]["InstanceId"]

        # Create AMI from the instance
        image_response = ec2_client.create_image(
            InstanceId=instance_id, Name="test-ami"
        )
        ami_id = image_response["ImageId"]

        # Create a block of EC2 instances (2 instances)
        instances = ec2_manager.create_instances(
            image_id=ami_id,
            instance_type="t2.micro",
            count=2,
            tags={"Name": "test-instance-block"},
        )

        # Verify instances were created
        assert len(instances) == 2
        for instance in instances:
            assert "instance_id" in instance

        # Clean up
        for instance in instances:
            ec2_manager.terminate_instance(instance["instance_id"])

        sg_manager.delete_security_group(sg_id)
        vpc_manager.delete_subnet(subnet_id)
        vpc_manager.delete_vpc(vpc_id)


class TestParameterStoreWithMoto:
    """Test ParameterStoreState with moto mocked AWS."""

    @mock_ssm
    def test_parameter_store_lifecycle(self, moto_session):
        """Test saving, loading, and deleting state in Parameter Store."""
        # Create a mock provider
        mock_provider = type(
            "MockProvider",
            (),
            {
                "workflow_id": f"test-workflow-{uuid.uuid4().hex[:8]}",
                "region": "us-east-1",
                "aws_access_key_id": None,
                "aws_secret_access_key": None,
                "aws_session_token": None,
                "aws_profile": None,
            },
        )

        # Create a parameter store state
        prefix = f"/parsl/test/{uuid.uuid4().hex[:8]}"
        param_store = ParameterStoreState(provider=mock_provider, prefix=prefix)

        # Save a test state
        test_state_key = "test-state"
        test_state = {
            "provider_info": {"id": "test-provider", "region": "us-east-1"},
            "resources": {"resource-1": {"id": "r-1", "status": "running"}},
        }

        param_store.save_state(test_state_key, test_state)

        # Load the state
        loaded_state = param_store.load_state(test_state_key)

        # Verify state was saved and loaded correctly
        assert loaded_state is not None
        assert loaded_state["provider_info"]["id"] == test_state["provider_info"]["id"]
        assert (
            loaded_state["resources"]["resource-1"]["id"]
            == test_state["resources"]["resource-1"]["id"]
        )

        # List states
        states = param_store.list_states("")
        assert len(states) >= 1
        assert any(k.endswith(test_state_key) for k in states.keys())

        # Delete the state
        param_store.delete_state(test_state_key)

        # Verify state was deleted
        loaded_state = param_store.load_state(test_state_key)
        assert loaded_state is None


class TestS3StateWithMoto:
    """Test S3State with moto mocked AWS."""

    @mock_s3
    def test_s3_state_lifecycle(self, moto_session):
        """Test saving, loading, and deleting state in S3."""
        # Create a mock provider
        mock_provider = type(
            "MockProvider",
            (),
            {
                "workflow_id": f"test-workflow-{uuid.uuid4().hex[:8]}",
                "region": "us-east-1",
                "aws_access_key_id": None,
                "aws_secret_access_key": None,
                "aws_session_token": None,
                "aws_profile": None,
            },
        )

        # Create a bucket
        bucket_name = f"test-bucket-{uuid.uuid4().hex[:8]}"
        s3_client = moto_session.client("s3")
        s3_client.create_bucket(Bucket=bucket_name)

        # Create an S3 state
        key_prefix = f"parsl/test/{uuid.uuid4().hex[:8]}"
        s3_state = S3State(
            provider=mock_provider, bucket_name=bucket_name, key_prefix=key_prefix
        )

        # Save a test state
        test_state_key = "test-state"
        test_state = {
            "provider_info": {"id": "test-provider", "region": "us-east-1"},
            "resources": {"resource-1": {"id": "r-1", "status": "running"}},
        }

        s3_state.save_state(test_state_key, test_state)

        # Load the state
        loaded_state = s3_state.load_state(test_state_key)

        # Verify state was saved and loaded correctly
        assert loaded_state is not None
        assert loaded_state["provider_info"]["id"] == test_state["provider_info"]["id"]
        assert (
            loaded_state["resources"]["resource-1"]["id"]
            == test_state["resources"]["resource-1"]["id"]
        )

        # List states
        states = s3_state.list_states("")
        assert len(states) >= 1
        assert any(k.endswith(test_state_key) for k in states.keys())

        # Delete the state
        s3_state.delete_state(test_state_key)

        # Verify state was deleted
        loaded_state = s3_state.load_state(test_state_key)
        assert loaded_state is None


class TestLambdaWithMoto:
    """Test LambdaManager with moto mocked AWS."""

    @mock_lambda
    @mock_iam
    def test_lambda_function_lifecycle(self, moto_session):
        """Test Lambda function creation, invocation, and deletion."""
        # Create IAM role for Lambda
        iam_client = moto_session.client("iam")
        role_name = f"lambda-test-role-{uuid.uuid4().hex[:8]}"

        assume_role_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "lambda.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }

        role_response = iam_client.create_role(
            RoleName=role_name, AssumeRolePolicyDocument=json.dumps(assume_role_policy)
        )

        role_arn = role_response["Role"]["Arn"]

        # Create a Lambda manager
        lambda_manager = LambdaManager(session=moto_session, region="us-east-1")

        # Prepare Lambda function
        function_name = f"test-function-{uuid.uuid4().hex[:8]}"
        handler = "index.handler"
        runtime = "python3.8"
        memory_size = 128
        timeout = 30
        code_content = """
def handler(event, context):
    return {
        'statusCode': 200,
        'body': 'Hello from Lambda!'
    }
"""

        # Mock lambda code generation
        with patch.object(
            lambda_manager, "_generate_lambda_code", return_value=b"dummy_code"
        ):
            # Create Lambda function
            function = lambda_manager.create_lambda_function(
                function_name=function_name,
                handler=handler,
                runtime=runtime,
                role=role_arn,
                memory_size=memory_size,
                timeout=timeout,
                code_content=code_content,
            )

        # Verify function was created
        assert function["FunctionName"] == function_name

        # Invoke Lambda function
        with patch.object(lambda_manager.lambda_client, "invoke") as mock_invoke:
            mock_invoke.return_value = {
                "StatusCode": 200,
                "Payload": MagicMock(
                    read=lambda: json.dumps(
                        {"statusCode": 200, "body": "Hello from Lambda!"}
                    ).encode()
                ),
            }

            response = lambda_manager.invoke_lambda_function(
                function_name=function_name, payload={"key": "value"}
            )

        # Verify invocation response
        assert response["statusCode"] == 200
        assert "Hello from Lambda!" in response["body"]

        # Delete Lambda function
        lambda_manager.delete_lambda_function(function_name)

        # Verify function was deleted
        try:
            lambda_client = moto_session.client("lambda")
            lambda_client.get_function(FunctionName=function_name)
            assert False, "Lambda function should be deleted"
        except lambda_client.exceptions.ResourceNotFoundException:
            # Expected error because function is deleted
            pass

        # Clean up IAM role
        iam_client.delete_role(RoleName=role_name)


class TestECSWithMoto:
    """Test ECSManager with moto mocked AWS."""

    @mock_ecs
    def test_ecs_cluster_lifecycle(self, moto_session):
        """Test ECS cluster creation and deletion."""
        # Create an ECS manager
        ecs_manager = ECSManager(
            session=moto_session,
            region="us-east-1",
            vpc_id="vpc-12345",  # Dummy values for network resources
            subnet_id="subnet-12345",
            security_group_id="sg-12345",
        )

        # Create an ECS cluster
        cluster_name = f"test-cluster-{uuid.uuid4().hex[:8]}"
        cluster_arn = ecs_manager.create_cluster(cluster_name)

        # Verify cluster was created
        ecs_client = moto_session.client("ecs")
        response = ecs_client.describe_clusters(clusters=[cluster_name])

        assert len(response["clusters"]) == 1
        assert response["clusters"][0]["clusterName"] == cluster_name
        assert response["clusters"][0]["status"] == "ACTIVE"

        # Delete the cluster
        ecs_manager.delete_cluster(cluster_name)

        # Verify cluster was deleted
        response = ecs_client.describe_clusters(clusters=[cluster_name])
        assert (
            len(response["clusters"]) == 0
            or response["clusters"][0]["status"] == "INACTIVE"
        )

    @mock_ecs
    def test_ecs_task_definition(self, moto_session):
        """Test ECS task definition registration and deregistration."""
        # Create an ECS manager
        ecs_manager = ECSManager(
            session=moto_session,
            region="us-east-1",
            vpc_id="vpc-12345",  # Dummy values for network resources
            subnet_id="subnet-12345",
            security_group_id="sg-12345",
        )

        # Create a task definition
        family = f"test-task-{uuid.uuid4().hex[:8]}"
        container_definitions = [
            {
                "name": "test-container",
                "image": "amazon/amazon-ecs-sample",
                "cpu": 256,
                "memory": 512,
                "essential": True,
            }
        ]

        task_definition_arn = ecs_manager.register_task_definition(
            family=family,
            container_definitions=container_definitions,
            cpu="256",
            memory="512",
        )

        # Verify task definition was registered
        ecs_client = moto_session.client("ecs")
        response = ecs_client.describe_task_definition(taskDefinition=family)

        assert response["taskDefinition"]["family"] == family
        assert len(response["taskDefinition"]["containerDefinitions"]) == 1
        assert (
            response["taskDefinition"]["containerDefinitions"][0]["name"]
            == "test-container"
        )

        # Deregister task definition
        ecs_manager.deregister_task_definition(task_definition_arn)

        # Moto doesn't properly support deregistering task definitions, so we can't verify deletion


class TestSpotFleetWithMoto:
    """Test SpotFleetManager with moto mocked AWS."""

    @mock_ec2
    @mock_iam
    def test_spot_fleet_request_lifecycle(self, moto_session):
        """Test spot fleet request creation and cancellation."""
        # First create a VPC and subnet
        vpc_manager = VPCManager(session=moto_session, region="us-east-1")
        vpc_id = vpc_manager.create_vpc(
            cidr_block="10.0.0.0/16", tags={"Name": "test-vpc"}
        )

        subnet_id = vpc_manager.create_subnet(
            vpc_id=vpc_id,
            cidr_block="10.0.0.0/24",
            availability_zone=f"{moto_session.region_name}a",
            tags={"Name": "test-subnet"},
        )

        # Create a security group
        sg_manager = SecurityGroupManager(session=moto_session, region="us-east-1")
        sg_id = sg_manager.create_security_group(
            name="test-sg",
            description="Test security group",
            vpc_id=vpc_id,
            tags={"Name": "test-sg"},
        )

        # Create IAM role for Spot Fleet
        iam_client = moto_session.client("iam")
        role_name = f"spot-fleet-role-{uuid.uuid4().hex[:8]}"

        assume_role_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "spotfleet.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }

        role_response = iam_client.create_role(
            RoleName=role_name, AssumeRolePolicyDocument=json.dumps(assume_role_policy)
        )

        role_arn = role_response["Role"]["Arn"]

        # Attach necessary policies
        policy_arn = (
            "arn:aws:iam::aws:policy/service-role/AmazonEC2SpotFleetTaggingRole"
        )
        try:
            iam_client.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
        except iam_client.exceptions.NoSuchEntityException:
            # Moto might not have this policy, so we'll create it
            policy_document = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "ec2:CreateTags",
                            "ec2:DescribeTags",
                            "ec2:DescribeInstances",
                            "ec2:TerminateInstances",
                            "ec2:RequestSpotInstances",
                            "ec2:CancelSpotInstanceRequests",
                            "ec2:DescribeSpotInstanceRequests",
                        ],
                        "Resource": "*",
                    }
                ],
            }

            iam_client.put_role_policy(
                RoleName=role_name,
                PolicyName="SpotFleetTaggingPolicy",
                PolicyDocument=json.dumps(policy_document),
            )

        # Create test AMI
        ec2_client = moto_session.client("ec2")
        ami_response = ec2_client.run_instances(
            ImageId="ami-12345678",  # This doesn't matter in moto
            MinCount=1,
            MaxCount=1,
            InstanceType="t2.micro",
        )
        instance_id = ami_response["Instances"][0]["InstanceId"]

        # Create AMI from the instance
        image_response = ec2_client.create_image(
            InstanceId=instance_id, Name="test-ami"
        )
        ami_id = image_response["ImageId"]

        # Create a SpotFleetManager
        spot_fleet_manager = SpotFleetManager(
            session=moto_session,
            region="us-east-1",
            vpc_id=vpc_id,
            subnet_id=subnet_id,
            security_group_id=sg_id,
            iam_fleet_role=role_arn,
        )

        # Define launch specification
        launch_specification = {
            "ImageId": ami_id,
            "InstanceType": "t2.micro",
            "SubnetId": subnet_id,
            "SecurityGroupIds": [sg_id],
            "TagSpecifications": [
                {
                    "ResourceType": "instance",
                    "Tags": [{"Key": "Name", "Value": "test-spot-instance"}],
                }
            ],
        }

        # Mock the spot fleet request creation since Moto's implementation is limited
        with patch.object(
            spot_fleet_manager.ec2_client, "request_spot_fleet"
        ) as mock_request:
            request_id = f"sfr-{uuid.uuid4().hex[:8]}"
            mock_request.return_value = {"SpotFleetRequestId": request_id}

            # Create spot fleet request
            spot_fleet_request_id = spot_fleet_manager.request_spot_fleet(
                target_capacity=2,
                valid_until=None,  # Use default future time
                allocation_strategy="lowestPrice",
                instance_interruption_behavior="terminate",
                launch_specifications=[launch_specification],
            )

        assert spot_fleet_request_id is not None

        # Mock describe spot fleet requests
        with patch.object(
            spot_fleet_manager.ec2_client, "describe_spot_fleet_requests"
        ) as mock_describe:
            mock_describe.return_value = {
                "SpotFleetRequestConfigs": [
                    {
                        "SpotFleetRequestId": spot_fleet_request_id,
                        "SpotFleetRequestState": "active",
                        "ActivityStatus": "fulfilled",
                    }
                ]
            }

            # Check status
            status = spot_fleet_manager.get_spot_fleet_request_status(
                spot_fleet_request_id
            )
            assert status == "active"

        # Mock cancel spot fleet request
        with patch.object(
            spot_fleet_manager.ec2_client, "cancel_spot_fleet_requests"
        ) as mock_cancel:
            mock_cancel.return_value = {
                "SuccessfulFleetRequests": [
                    {
                        "SpotFleetRequestId": spot_fleet_request_id,
                        "CurrentSpotFleetRequestState": "cancelled_terminating",
                        "PreviousSpotFleetRequestState": "active",
                    }
                ],
                "UnsuccessfulFleetRequests": [],
            }

            # Cancel spot fleet request
            cancelled = spot_fleet_manager.cancel_spot_fleet_request(
                spot_fleet_request_id, terminate_instances=True
            )
            assert cancelled is True

        # Clean up
        iam_client.delete_role_policy(
            RoleName=role_name, PolicyName="SpotFleetTaggingPolicy"
        )
        iam_client.delete_role(RoleName=role_name)
        sg_manager.delete_security_group(sg_id)
        vpc_manager.delete_subnet(subnet_id)
        vpc_manager.delete_vpc(vpc_id)


class TestServerlessModeWithMoto:
    """Test ServerlessMode with moto mocked AWS."""

    @pytest.mark.xfail(reason="ServerlessMode requires more complex mocking")
    @mock_lambda
    @mock_ecs
    @mock_ec2
    @mock_iam
    def test_serverless_mode_initialization(self, moto_session):
        """Test ServerlessMode initialization."""
        # Mock the provider
        mock_provider = type(
            "MockProvider",
            (),
            {
                "workflow_id": f"test-workflow-{uuid.uuid4().hex[:8]}",
                "region": "us-east-1",
                "aws_access_key_id": None,
                "aws_secret_access_key": None,
                "aws_session_token": None,
                "aws_profile": None,
                "label": "test-provider",
                "engine": MagicMock(),
                "session": moto_session,
            },
        )

        # Mock Lambda and ECS managers with patches
        with patch(
            "parsl_ephemeral_aws.compute.lambda_func.LambdaManager"
        ) as mock_lambda_manager, patch(
            "parsl_ephemeral_aws.compute.ecs.ECSManager"
        ) as mock_ecs_manager, patch(
            "parsl_ephemeral_aws.network.vpc.VPCManager"
        ) as mock_vpc_manager, patch(
            "parsl_ephemeral_aws.network.security.SecurityGroupManager"
        ) as mock_sg_manager:
            # Create mock manager instances
            mock_lambda_manager.return_value.create_lambda_function.return_value = {
                "FunctionName": "test-function"
            }
            mock_ecs_manager.return_value.create_cluster.return_value = (
                "test-cluster-arn"
            )
            mock_vpc_manager.return_value.create_vpc.return_value = "vpc-12345"
            mock_vpc_manager.return_value.create_subnet.return_value = "subnet-12345"
            mock_sg_manager.return_value.create_security_group.return_value = "sg-12345"

            # Initialize ServerlessMode
            config = {
                "region": "us-east-1",
                "serverless": {
                    "lambda": {"memory_size": 128, "timeout": 30},
                    "ecs": {"cpu": "256", "memory": "512"},
                },
            }

            mode = ServerlessMode(
                provider=mock_provider, label="test-serverless", config=config
            )

            # Verify managers were created
            assert mode.lambda_manager is not None
            assert mode.ecs_manager is not None


class TestCloudFormationWithMoto:
    """Test CloudFormation operations with moto mocked AWS."""

    @mock_cloudformation
    def test_cloudformation_stack_lifecycle(self, moto_session):
        """Test CloudFormation stack creation and deletion."""
        # Create CloudFormation client
        cf_client = moto_session.client("cloudformation")

        # Define a simple CloudFormation template
        template = {
            "AWSTemplateFormatVersion": "2010-09-09",
            "Resources": {
                "TestBucket": {
                    "Type": "AWS::S3::Bucket",
                    "Properties": {"BucketName": f"test-bucket-{uuid.uuid4().hex[:8]}"},
                }
            },
            "Outputs": {"BucketName": {"Value": {"Ref": "TestBucket"}}},
        }

        # Create stack
        stack_name = f"test-stack-{uuid.uuid4().hex[:8]}"
        response = cf_client.create_stack(
            StackName=stack_name,
            TemplateBody=json.dumps(template),
            Capabilities=["CAPABILITY_IAM"],
        )

        # Verify stack was created
        assert "StackId" in response

        # Describe stack
        response = cf_client.describe_stacks(StackName=stack_name)

        assert len(response["Stacks"]) == 1
        assert response["Stacks"][0]["StackName"] == stack_name

        # Due to limitations in moto, we won't be able to fully test outputs
        # But we can test basic functionality

        # Delete stack
        cf_client.delete_stack(StackName=stack_name)

        # Verify stack was deleted
        # Note: Moto's CloudFormation implementation doesn't fully support stack deletion
        # so we can't verify deletion here

"""Unit tests for error handling in critical components.

These tests verify that exceptions are properly raised, caught, and handled
throughout the codebase, ensuring robust error handling and reporting.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import boto3
import pytest
import uuid
from unittest.mock import MagicMock, patch, PropertyMock
from botocore.exceptions import ClientError, NoCredentialsError

from parsl_ephemeral_aws.provider import EphemeralAWSProvider
from parsl_ephemeral_aws.modes.standard import StandardMode
from parsl_ephemeral_aws.modes.detached import DetachedMode
from parsl_ephemeral_aws.modes.serverless import ServerlessMode
from parsl_ephemeral_aws.compute.ec2 import EC2Manager
from parsl_ephemeral_aws.compute.spot_fleet import SpotFleetManager
from parsl_ephemeral_aws.compute.lambda_func import LambdaManager
from parsl_ephemeral_aws.compute.ecs import ECSManager
from parsl_ephemeral_aws.network.vpc import VPCManager
from parsl_ephemeral_aws.network.security import SecurityGroupManager
from parsl_ephemeral_aws.exceptions import (
    AWSAuthenticationError,
    AWSConnectionError,
    ResourceCreationError,
    ResourceDeletionError,
    ResourceNotFoundError,
    JobExecutionError,
    ProviderConfigurationError,
    NetworkCreationError,
    SpotFleetError,
    LambdaFunctionError,
    ECSTaskError,
    CloudFormationError,
    SpotInstanceError,
    BastionHostError,
    TaskTimeoutError,
    SpotInterruptionError,
    AMINotFoundError,
)


class TestAWSConnectionErrors:
    """Tests for AWS connection and authentication errors."""
    
    @pytest.fixture
    def provider_config(self):
        """Create a basic provider configuration."""
        return {
            "region": "us-east-1",
            "instance_type": "t3.micro",
            "image_id": "ami-12345678",
            "max_blocks": 1
        }
    
    def test_no_credentials_error(self, provider_config):
        """Test handling of missing AWS credentials."""
        # Simulate boto3 raising NoCredentialsError
        with patch('boto3.Session', side_effect=NoCredentialsError()):
            with pytest.raises(AWSAuthenticationError):
                provider = EphemeralAWSProvider(**provider_config)
    
    def test_invalid_credentials_error(self, provider_config):
        """Test handling of invalid AWS credentials."""
        # Simulate boto3 client raising unauthorized error
        mock_session = MagicMock()
        error_response = {
            'Error': {
                'Code': 'AuthFailure',
                'Message': 'AWS was not able to validate the provided credentials'
            }
        }
        mock_session.client.side_effect = ClientError(error_response, 'AssumeRole')
        
        with patch('boto3.Session', return_value=mock_session):
            with pytest.raises(AWSAuthenticationError):
                provider = EphemeralAWSProvider(**provider_config)
    
    def test_service_unavailable_error(self, provider_config):
        """Test handling of AWS service unavailability."""
        # Simulate boto3 client raising service unavailable error
        mock_session = MagicMock()
        error_response = {
            'Error': {
                'Code': 'ServiceUnavailable',
                'Message': 'Service is currently unavailable'
            }
        }
        mock_session.client.side_effect = ClientError(error_response, 'DescribeInstances')
        
        with patch('boto3.Session', return_value=mock_session):
            provider = EphemeralAWSProvider(**provider_config)
            with pytest.raises(AWSConnectionError):
                # Try to use the provider
                with patch.object(provider, '_initialize_operating_mode'):
                    provider.status([])
    
    def test_throttling_error(self, provider_config):
        """Test handling of AWS API throttling."""
        # Simulate boto3 client raising throttling error
        mock_session = MagicMock()
        mock_client = MagicMock()
        throttle_error = {
            'Error': {
                'Code': 'RequestLimitExceeded',
                'Message': 'Request limit exceeded'
            }
        }
        mock_client.describe_instances.side_effect = ClientError(throttle_error, 'DescribeInstances')
        mock_session.client.return_value = mock_client
        
        with patch('boto3.Session', return_value=mock_session):
            provider = EphemeralAWSProvider(**provider_config)
            # Initialize with our mock session
            provider._session = mock_session
            
            # The provider should handle the throttling error (log it, potentially retry, but not crash)
            with patch.object(provider, '_initialize_operating_mode'):
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
            'Error': {
                'Code': 'VpcLimitExceeded',
                'Message': 'The maximum number of VPCs has been reached'
            }
        }
        mock_ec2_client.create_vpc.side_effect = ClientError(error_response, 'CreateVpc')
        
        mode = StandardMode(
            provider_id=str(uuid.uuid4()),
            session=mock_session,
            state_store=MagicMock(),
            region="us-east-1",
            instance_type="t3.micro",
            image_id="ami-12345678"
        )
        
        with pytest.raises(NetworkCreationError):
            mode.initialize()
    
    def test_detached_mode_bastion_error(self, mock_provider, mock_session):
        """Test error handling during bastion host creation in DetachedMode."""
        mock_ec2_client = MagicMock()
        mock_session.client.return_value = mock_ec2_client
        
        # Mock successful VPC and subnet creation
        mock_ec2_client.create_vpc.return_value = {'Vpc': {'VpcId': 'vpc-12345'}}
        mock_ec2_client.create_subnet.return_value = {'Subnet': {'SubnetId': 'subnet-12345'}}
        mock_ec2_client.create_security_group.return_value = {'GroupId': 'sg-12345'}
        
        # Simulate error creating bastion instance
        error_response = {
            'Error': {
                'Code': 'InsufficientInstanceCapacity',
                'Message': 'Insufficient capacity'
            }
        }
        mock_ec2_client.run_instances.side_effect = ClientError(error_response, 'RunInstances')
        
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
            bastion_host_type="direct"
        )
        
        with pytest.raises(BastionHostError):
            # Mock tag creation to avoid errors
            with patch.object(mode, '_create_tags'):
                mode.initialize()
    
    def test_serverless_mode_lambda_error(self, mock_provider, mock_session):
        """Test error handling during Lambda function creation in ServerlessMode."""
        mock_lambda_client = MagicMock()
        mock_session.client.return_value = mock_lambda_client
        
        # Simulate error creating Lambda function
        error_response = {
            'Error': {
                'Code': 'ResourceConflictException',
                'Message': 'Function already exists'
            }
        }
        mock_lambda_client.create_function.side_effect = ClientError(error_response, 'CreateFunction')
        
        mode = ServerlessMode(
            provider_id=str(uuid.uuid4()),
            session=mock_session,
            state_store=MagicMock(),
            region="us-east-1",
            worker_type="lambda",
            lambda_memory=128,
            lambda_timeout=30
        )
        
        # Mock Lambda manager to simulate error
        mode.lambda_manager = MagicMock()
        mode.lambda_manager._create_lambda_function.side_effect = LambdaFunctionError("Failed to create Lambda function")
        
        with pytest.raises(LambdaFunctionError):
            mode.initialize()


class TestEC2ManagerErrors:
    """Tests for error handling in EC2Manager."""
    
    @pytest.fixture
    def ec2_manager(self):
        """Create an EC2Manager with mock session."""
        session = MagicMock()
        ec2_client = MagicMock()
        session.client.return_value = ec2_client
        
        return EC2Manager(
            session=session,
            region="us-east-1",
            vpc_id="vpc-12345",
            subnet_id="subnet-12345",
            security_group_id="sg-12345"
        )
    
    def test_ami_not_found_error(self, ec2_manager):
        """Test handling of AMI not found errors."""
        # Simulate AMI not found
        error_response = {
            'Error': {
                'Code': 'InvalidAMIID.NotFound',
                'Message': 'The image id ami-12345 does not exist'
            }
        }
        ec2_manager.ec2_client.describe_images.side_effect = ClientError(error_response, 'DescribeImages')
        
        with pytest.raises(AMINotFoundError):
            ec2_manager.create_instance(
                image_id="ami-12345",
                instance_type="t3.micro",
                min_count=1,
                max_count=1,
                key_name=None,
                user_data=None,
                tags={}
            )
    
    def test_insufficient_capacity_error(self, ec2_manager):
        """Test handling of insufficient capacity errors."""
        # Simulate insufficient capacity
        error_response = {
            'Error': {
                'Code': 'InsufficientInstanceCapacity',
                'Message': 'Insufficient capacity'
            }
        }
        ec2_manager.ec2_client.run_instances.side_effect = ClientError(error_response, 'RunInstances')
        
        with pytest.raises(EC2InstanceError):
            ec2_manager.create_instance(
                image_id="ami-12345",
                instance_type="t3.micro",
                min_count=1,
                max_count=1,
                key_name=None,
                user_data=None,
                tags={}
            )
    
    def test_instance_limit_exceeded_error(self, ec2_manager):
        """Test handling of instance limit exceeded errors."""
        # Simulate instance limit exceeded
        error_response = {
            'Error': {
                'Code': 'InstanceLimitExceeded',
                'Message': 'You have requested more instances than your current instance limit'
            }
        }
        ec2_manager.ec2_client.run_instances.side_effect = ClientError(error_response, 'RunInstances')
        
        with pytest.raises(EC2InstanceError):
            ec2_manager.create_instance(
                image_id="ami-12345",
                instance_type="t3.micro",
                min_count=1,
                max_count=1,
                key_name=None,
                user_data=None,
                tags={}
            )
    
    def test_instance_not_found_error(self, ec2_manager):
        """Test handling of instance not found errors."""
        # Simulate instance not found
        error_response = {
            'Error': {
                'Code': 'InvalidInstanceID.NotFound',
                'Message': 'The instance ID i-12345 does not exist'
            }
        }
        ec2_manager.ec2_client.describe_instances.side_effect = ClientError(error_response, 'DescribeInstances')
        
        with pytest.raises(ResourceNotFoundError):
            ec2_manager.get_instance_status("i-12345")
    
    def test_termination_error(self, ec2_manager):
        """Test handling of instance termination errors."""
        # Simulate termination error
        error_response = {
            'Error': {
                'Code': 'OperationNotPermitted',
                'Message': 'You are not authorized to terminate instances'
            }
        }
        ec2_manager.ec2_client.terminate_instances.side_effect = ClientError(error_response, 'TerminateInstances')
        
        with pytest.raises(ResourceDeletionError):
            ec2_manager.terminate_instance("i-12345")


class TestSpotFleetManagerErrors:
    """Tests for error handling in SpotFleetManager."""
    
    @pytest.fixture
    def spot_fleet_manager(self):
        """Create a SpotFleetManager with mock session."""
        session = MagicMock()
        ec2_client = MagicMock()
        session.client.return_value = ec2_client
        
        return SpotFleetManager(
            session=session,
            region="us-east-1",
            vpc_id="vpc-12345",
            subnet_id="subnet-12345",
            security_group_id="sg-12345"
        )
    
    def test_spot_fleet_request_error(self, spot_fleet_manager):
        """Test handling of spot fleet request errors."""
        # Simulate spot fleet request error
        error_response = {
            'Error': {
                'Code': 'InvalidSpotFleetRequestConfig',
                'Message': 'Invalid Spot Fleet request configuration'
            }
        }
        spot_fleet_manager.ec2_client.request_spot_fleet.side_effect = ClientError(error_response, 'RequestSpotFleet')
        
        with pytest.raises(SpotFleetError):
            spot_fleet_manager.create_spot_fleet(
                image_id="ami-12345",
                instance_types=["t3.micro", "t3.small"],
                target_capacity=1,
                iam_fleet_role="arn:aws:iam::123456789012:role/aws-service-role/spotfleet.amazonaws.com/AWSServiceRoleForEC2SpotFleet",
                allocation_strategy="lowestPrice",
                user_data=None,
                tags={}
            )
    
    def test_spot_fleet_throttling_error(self, spot_fleet_manager):
        """Test handling of spot fleet throttling errors."""
        # Simulate throttling error
        error_response = {
            'Error': {
                'Code': 'RequestLimitExceeded',
                'Message': 'Request limit exceeded'
            }
        }
        spot_fleet_manager.ec2_client.request_spot_fleet.side_effect = ClientError(error_response, 'RequestSpotFleet')
        
        with pytest.raises(SpotFleetError):
            spot_fleet_manager.create_spot_fleet(
                image_id="ami-12345",
                instance_types=["t3.micro", "t3.small"],
                target_capacity=1,
                iam_fleet_role="arn:aws:iam::123456789012:role/aws-service-role/spotfleet.amazonaws.com/AWSServiceRoleForEC2SpotFleet",
                allocation_strategy="lowestPrice",
                user_data=None,
                tags={}
            )
    
    def test_spot_fleet_modification_error(self, spot_fleet_manager):
        """Test handling of spot fleet modification errors."""
        # Simulate modification error
        error_response = {
            'Error': {
                'Code': 'InvalidSpotFleetRequestId',
                'Message': 'The spot fleet request ID does not exist'
            }
        }
        spot_fleet_manager.ec2_client.modify_spot_fleet_request.side_effect = ClientError(error_response, 'ModifySpotFleetRequest')
        
        with pytest.raises(SpotFleetError):
            spot_fleet_manager.update_spot_fleet_capacity("sfr-12345", 2)
    
    def test_spot_fleet_cancellation_error(self, spot_fleet_manager):
        """Test handling of spot fleet cancellation errors."""
        # Simulate cancellation error
        error_response = {
            'Error': {
                'Code': 'InvalidSpotFleetRequestId',
                'Message': 'The spot fleet request ID does not exist'
            }
        }
        spot_fleet_manager.ec2_client.cancel_spot_fleet_requests.side_effect = ClientError(error_response, 'CancelSpotFleetRequests')
        
        with pytest.raises(SpotFleetError):
            spot_fleet_manager.cancel_spot_fleet("sfr-12345")


class TestLambdaManagerErrors:
    """Tests for error handling in LambdaManager."""
    
    @pytest.fixture
    def lambda_manager(self):
        """Create a LambdaManager with mock session."""
        session = MagicMock()
        lambda_client = MagicMock()
        session.client.return_value = lambda_client
        
        return LambdaManager(
            session=session,
            region="us-east-1"
        )
    
    def test_lambda_creation_error(self, lambda_manager):
        """Test handling of Lambda function creation errors."""
        # Simulate creation error
        error_response = {
            'Error': {
                'Code': 'InvalidParameterValueException',
                'Message': 'Memory size must be between 128 and 10240 MB'
            }
        }
        lambda_manager.lambda_client.create_function.side_effect = ClientError(error_response, 'CreateFunction')
        
        with pytest.raises(LambdaFunctionError):
            # Mock code generation to return bytes
            with patch.object(lambda_manager, '_generate_lambda_code', return_value=b'mock_code'):
                lambda_manager.create_lambda_function(
                    function_name="test-function",
                    handler="index.handler",
                    runtime="python3.8",
                    role="arn:aws:iam::123456789012:role/lambda-role",
                    memory_size=128,
                    timeout=30,
                    code_content="def handler(event, context): return {'statusCode': 200}",
                    environment_variables={}
                )
    
    def test_lambda_invocation_error(self, lambda_manager):
        """Test handling of Lambda function invocation errors."""
        # Simulate invocation error
        error_response = {
            'Error': {
                'Code': 'ResourceNotFoundException',
                'Message': 'Function not found'
            }
        }
        lambda_manager.lambda_client.invoke.side_effect = ClientError(error_response, 'Invoke')
        
        with pytest.raises(LambdaFunctionError):
            lambda_manager.invoke_lambda_function(
                function_name="test-function",
                payload={"key": "value"}
            )
    
    def test_lambda_timeout_error(self, lambda_manager):
        """Test handling of Lambda function timeout errors."""
        # Simulate timeout error response from Lambda
        mock_response = {
            'StatusCode': 200,
            'FunctionError': 'Unhandled',
            'Payload': MagicMock()
        }
        mock_response['Payload'].read.return_value = b'{"errorType": "TimeoutError", "errorMessage": "Task timed out"}'
        lambda_manager.lambda_client.invoke.return_value = mock_response
        
        with pytest.raises(TaskTimeoutError):
            lambda_manager.invoke_lambda_function(
                function_name="test-function",
                payload={"key": "value"}
            )
    
    def test_lambda_deletion_error(self, lambda_manager):
        """Test handling of Lambda function deletion errors."""
        # Simulate deletion error
        error_response = {
            'Error': {
                'Code': 'ResourceNotFoundException',
                'Message': 'Function not found'
            }
        }
        lambda_manager.lambda_client.delete_function.side_effect = ClientError(error_response, 'DeleteFunction')
        
        # This should log the error but not raise an exception since we're trying to delete something that doesn't exist
        lambda_manager.delete_lambda_function("test-function")
        
        # Verify delete_function was called with the right function name
        lambda_manager.lambda_client.delete_function.assert_called_with(FunctionName="test-function")


class TestECSManagerErrors:
    """Tests for error handling in ECSManager."""
    
    @pytest.fixture
    def ecs_manager(self):
        """Create an ECSManager with mock session."""
        session = MagicMock()
        ecs_client = MagicMock()
        session.client.return_value = ecs_client
        
        return ECSManager(
            session=session,
            region="us-east-1",
            vpc_id="vpc-12345",
            subnet_id="subnet-12345",
            security_group_id="sg-12345"
        )
    
    def test_cluster_creation_error(self, ecs_manager):
        """Test handling of ECS cluster creation errors."""
        # Simulate creation error
        error_response = {
            'Error': {
                'Code': 'InvalidParameterException',
                'Message': 'Invalid parameter in request'
            }
        }
        ecs_manager.ecs_client.create_cluster.side_effect = ClientError(error_response, 'CreateCluster')
        
        with pytest.raises(ECSTaskError):
            ecs_manager.create_cluster("test-cluster")
    
    def test_task_definition_error(self, ecs_manager):
        """Test handling of ECS task definition errors."""
        # Simulate definition error
        error_response = {
            'Error': {
                'Code': 'InvalidParameterException',
                'Message': 'Invalid task definition parameters'
            }
        }
        ecs_manager.ecs_client.register_task_definition.side_effect = ClientError(error_response, 'RegisterTaskDefinition')
        
        with pytest.raises(ECSTaskError):
            ecs_manager.register_task_definition(
                family="test-task",
                container_definitions=[
                    {
                        "name": "test-container",
                        "image": "amazon/amazon-ecs-sample",
                        "cpu": 256,
                        "memory": 512,
                        "essential": True
                    }
                ],
                requires_compatibilities=["FARGATE"],
                network_mode="awsvpc",
                cpu="256",
                memory="512",
                execution_role_arn="arn:aws:iam::123456789012:role/ecsTaskExecutionRole"
            )
    
    def test_run_task_error(self, ecs_manager):
        """Test handling of ECS run task errors."""
        # Simulate run task error
        error_response = {
            'Error': {
                'Code': 'ClientException',
                'Message': 'No container instances found'
            }
        }
        ecs_manager.ecs_client.run_task.side_effect = ClientError(error_response, 'RunTask')
        
        with pytest.raises(ECSTaskError):
            ecs_manager.run_task(
                cluster="test-cluster",
                task_definition="test-task",
                count=1,
                launch_type="FARGATE",
                network_configuration={
                    "awsvpcConfiguration": {
                        "subnets": ["subnet-12345"],
                        "securityGroups": ["sg-12345"],
                        "assignPublicIp": "ENABLED"
                    }
                }
            )
    
    def test_stop_task_error(self, ecs_manager):
        """Test handling of ECS stop task errors."""
        # Simulate stop task error
        error_response = {
            'Error': {
                'Code': 'ClientException',
                'Message': 'Task not found'
            }
        }
        ecs_manager.ecs_client.stop_task.side_effect = ClientError(error_response, 'StopTask')
        
        with pytest.raises(ECSTaskError):
            ecs_manager.stop_task("test-cluster", "task-12345")


class TestProviderConfigurationErrors:
    """Tests for provider configuration errors."""
    
    def test_invalid_mode(self):
        """Test handling of invalid operating mode."""
        with pytest.raises(ProviderConfigurationError):
            provider = EphemeralAWSProvider(
                region="us-east-1",
                instance_type="t3.micro",
                image_id="ami-12345678",
                mode="invalid_mode"
            )
    
    def test_missing_required_parameter(self):
        """Test handling of missing required parameters."""
        # Missing image_id in standard mode
        with pytest.raises(ProviderConfigurationError):
            provider = EphemeralAWSProvider(
                region="us-east-1",
                instance_type="t3.micro",
                mode="standard"
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
                use_spot_fleet=True
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
                mode="standard"
            )
        
        # Invalid instance type format
        with pytest.raises(ProviderConfigurationError):
            provider = EphemeralAWSProvider(
                region="us-east-1",
                instance_type="invalid_instance_type",
                image_id="ami-12345678",
                mode="standard"
            )
    
    def test_spot_without_interruption_warning(self):
        """Test warning when using spot instances without interruption handling."""
        with patch('logging.Logger.warning') as mock_warning:
            provider = EphemeralAWSProvider(
                region="us-east-1",
                instance_type="t3.micro",
                image_id="ami-12345678",
                mode="standard",
                use_spot=True,
                spot_interruption_handling=False
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
            ecs_container_image="amazon/amazon-ecs-sample"
        )
    
    def test_stack_creation_error(self, serverless_mode):
        """Test handling of CloudFormation stack creation errors."""
        # Simulate stack creation error
        error_response = {
            'Error': {
                'Code': 'LimitExceededException',
                'Message': 'Stack limit exceeded'
            }
        }
        serverless_mode.cf_client.create_stack.side_effect = ClientError(error_response, 'CreateStack')
        
        with pytest.raises(CloudFormationError):
            serverless_mode._create_cloudformation_stack(
                stack_name="test-stack",
                template_body="{}",
                parameters=[],
                tags={}
            )
    
    def test_stack_deletion_error(self, serverless_mode):
        """Test handling of CloudFormation stack deletion errors."""
        # Simulate stack deletion error
        error_response = {
            'Error': {
                'Code': 'ValidationError',
                'Message': 'Stack does not exist'
            }
        }
        serverless_mode.cf_client.delete_stack.side_effect = ClientError(error_response, 'DeleteStack')
        
        # This should log the error but not raise an exception since we're trying to delete something that doesn't exist
        serverless_mode._delete_cloudformation_stack("test-stack")
        
        # Verify delete_stack was called with the right stack name
        serverless_mode.cf_client.delete_stack.assert_called_with(StackName="test-stack")
    
    def test_stack_waiting_error(self, serverless_mode):
        """Test handling of errors while waiting for CloudFormation stack."""
        # Mock describe_stacks to return a failed stack
        serverless_mode.cf_client.describe_stacks.return_value = {
            'Stacks': [
                {
                    'StackName': 'test-stack',
                    'StackStatus': 'CREATE_FAILED',
                    'StackStatusReason': 'Resource creation failed'
                }
            ]
        }
        
        with pytest.raises(CloudFormationError):
            serverless_mode._wait_for_stack("test-stack", "CREATE_COMPLETE", 10)
    
    def test_stack_timeout_error(self, serverless_mode):
        """Test handling of timeout while waiting for CloudFormation stack."""
        # Mock describe_stacks to always return an in-progress stack
        serverless_mode.cf_client.describe_stacks.return_value = {
            'Stacks': [
                {
                    'StackName': 'test-stack',
                    'StackStatus': 'CREATE_IN_PROGRESS'
                }
            ]
        }
        
        # Set a short timeout to trigger the timeout error
        with pytest.raises(CloudFormationError):
            # Pass a very short timeout (1 second)
            serverless_mode._wait_for_stack("test-stack", "CREATE_COMPLETE", 1)
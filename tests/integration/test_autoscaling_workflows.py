"""Integration tests for autoscaling workflows.

These tests verify that the provider correctly scales resources up and down
based on workload demands in various operating modes.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import os
import time
import uuid
import pytest
import tempfile
from unittest.mock import MagicMock, patch

from parsl_ephemeral_aws.provider import EphemeralAWSProvider
from parsl_ephemeral_aws.modes.standard import StandardMode
from parsl_ephemeral_aws.modes.detached import DetachedMode
from parsl_ephemeral_aws.modes.serverless import ServerlessMode
from parsl_ephemeral_aws.state.file import FileStateStore
from parsl_ephemeral_aws.utils.localstack import is_localstack_available, get_localstack_session


# Skip all tests if LocalStack is not available
pytestmark = pytest.mark.skipif(
    not is_localstack_available(),
    reason="LocalStack is not available. Make sure it's running on port 4566."
)


@pytest.mark.integration
class TestAutoscalingWorkflows:
    """Integration tests for autoscaling workflow scenarios."""
    
    @pytest.fixture(scope="class")
    def localstack_session(self):
        """Create a session connected to LocalStack."""
        return get_localstack_session()
    
    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for state files."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            yield tmp_dir
    
    @pytest.fixture
    def state_file_path(self, temp_dir):
        """Create a path for the state file."""
        return os.path.join(temp_dir, f"test-state-{uuid.uuid4().hex[:8]}.json")
    
    @pytest.fixture
    def file_state_store(self, state_file_path):
        """Create a FileStateStore instance."""
        provider_id = f"test-provider-{uuid.uuid4().hex[:8]}"
        return FileStateStore(file_path=state_file_path, provider_id=provider_id)
    
    @pytest.mark.localstack
    def test_standard_mode_scaling(self, localstack_session, file_state_store):
        """Test autoscaling in StandardMode."""
        # Create a StandardMode instance with scaling configuration
        provider_id = f"test-provider-{uuid.uuid4().hex[:8]}"
        
        # Set up mocks for AWS services using LocalStack
        with patch('boto3.Session', return_value=localstack_session):
            # Create standard mode with scaling configuration
            mode = StandardMode(
                provider_id=provider_id,
                session=localstack_session,
                state_store=file_state_store,
                region="us-east-1",
                instance_type="t2.micro",
                image_id="ami-12345678",  # Dummy AMI
                max_blocks=3,
                min_blocks=0,
                init_blocks=1
            )
            
            # Initialize mode
            with patch.object(mode, '_create_vpc', return_value="vpc-12345"):
                with patch.object(mode, '_create_subnet', return_value="subnet-12345"):
                    with patch.object(mode, '_create_security_group', return_value="sg-12345"):
                        mode.initialize()
            
            # Mock instance creation
            instance_id_counter = 0
            
            def create_mock_instance(*args, **kwargs):
                nonlocal instance_id_counter
                instance_id_counter += 1
                return {
                    'instance_id': f"i-{instance_id_counter:05d}",
                    'private_ip': f"10.0.0.{instance_id_counter}",
                    'public_ip': f"54.123.456.{instance_id_counter}",
                    'dns_name': f"ec2-54-123-456-{instance_id_counter}.compute-1.amazonaws.com"
                }
            
            # Test initial block creation
            with patch.object(mode, '_create_ec2_instance', side_effect=create_mock_instance):
                # Should create one instance for init_blocks=1
                mode._init_blocks()
                
                # Verify one instance was created
                assert len(mode.resources) == 1
            
            # Test scaling out (adding instances)
            with patch.object(mode, '_create_ec2_instance', side_effect=create_mock_instance):
                # Try to scale out by 2 more instances
                new_blocks = mode.scale_out(2)
                
                # Verify scaling was successful
                assert new_blocks == 2
                
                # Verify we now have 3 instances total
                assert len(mode.resources) == 3
                
                # Try to scale beyond max_blocks
                new_blocks = mode.scale_out(1)
                
                # Should not scale further since max_blocks=3
                assert new_blocks == 0
                assert len(mode.resources) == 3
            
            # Mock job submission and setting status to running
            job_ids = []
            resource_ids = list(mode.resources.keys())
            
            for i, resource_id in enumerate(resource_ids):
                job_id = f"job-{i+1}"
                # Set resource job mapping
                mode.resources[resource_id]["job_id"] = job_id
                # Set to running status
                mode.resources[resource_id]["status"] = "running"
                job_ids.append(job_id)
            
            # Test scaling in (removing instances)
            with patch.object(mode, '_delete_ec2_instance'):
                # Mark one job as complete
                complete_resource_id = resource_ids[0]
                mode.resources[complete_resource_id]["status"] = "completed"
                
                # Scale in one block
                num_released = mode.scale_in(1)
                
                # Should remove the completed job's instance
                assert num_released == 1
                assert complete_resource_id not in mode.resources
                assert len(mode.resources) == 2
                
                # Try to scale in more than min_blocks
                all_resource_ids = list(mode.resources.keys())
                for resource_id in all_resource_ids:
                    mode.resources[resource_id]["status"] = "completed"
                
                # Scale in remaining 2 instances
                num_released = mode.scale_in(2)
                
                # Should remove both instances
                assert num_released == 2
                assert len(mode.resources) == 0
            
            # Clean up
            with patch.object(mode, '_delete_ec2_instance'):
                with patch.object(mode, '_delete_security_group'):
                    with patch.object(mode, '_delete_subnet'):
                        with patch.object(mode, '_delete_vpc'):
                            mode.cleanup_infrastructure()
    
    @pytest.mark.localstack
    def test_detached_mode_scaling(self, localstack_session, file_state_store):
        """Test autoscaling in DetachedMode."""
        # Create a DetachedMode instance with scaling configuration
        provider_id = f"test-provider-{uuid.uuid4().hex[:8]}"
        workflow_id = f"test-workflow-{uuid.uuid4().hex[:8]}"
        
        # Set up mocks for AWS services using LocalStack
        with patch('boto3.Session', return_value=localstack_session):
            # Create detached mode with scaling configuration
            mode = DetachedMode(
                provider_id=provider_id,
                session=localstack_session,
                state_store=file_state_store,
                region="us-east-1",
                instance_type="t2.micro",
                image_id="ami-12345678",  # Dummy AMI
                workflow_id=workflow_id,
                bastion_instance_type="t2.micro",
                bastion_host_type="direct",
                max_blocks=3,
                min_blocks=0,
                init_blocks=1
            )
            
            # Initialize mode
            with patch.object(mode, '_create_vpc', return_value="vpc-12345"):
                with patch.object(mode, '_create_subnet', return_value="subnet-12345"):
                    with patch.object(mode, '_create_security_group', return_value="sg-12345"):
                        with patch.object(mode, '_create_bastion_host') as mock_create_bastion:
                            mock_create_bastion.return_value = {
                                'instance_id': f"i-bastion-{uuid.uuid4().hex[:8]}",
                                'private_ip': '10.0.0.2',
                                'public_ip': '54.123.456.789',
                                'dns_name': 'ec2-54-123-456-789.compute-1.amazonaws.com'
                            }
                            with patch.object(mode, '_create_tags'):
                                mode.initialize()
            
            # Verify bastion was created
            assert mode.bastion_id is not None
            
            # Mock instance creation
            instance_id_counter = 0
            
            def create_mock_instance(*args, **kwargs):
                nonlocal instance_id_counter
                instance_id_counter += 1
                return {
                    'instance_id': f"i-{instance_id_counter:05d}",
                    'private_ip': f"10.0.0.{instance_id_counter + 10}",
                    'public_ip': None,  # No public IP in detached mode
                    'dns_name': None
                }
            
            # Test initial block creation
            with patch.object(mode, '_create_ec2_instance', side_effect=create_mock_instance):
                with patch.object(mode, '_create_ssm_parameter'):
                    # Should create one instance for init_blocks=1
                    mode._init_blocks()
                    
                    # Verify one instance was created
                    assert len(mode.resources) == 1
            
            # Test scaling out (adding instances)
            with patch.object(mode, '_create_ec2_instance', side_effect=create_mock_instance):
                with patch.object(mode, '_create_ssm_parameter'):
                    # Try to scale out by 2 more instances
                    new_blocks = mode.scale_out(2)
                    
                    # Verify scaling was successful
                    assert new_blocks == 2
                    
                    # Verify we now have 3 instances total
                    assert len(mode.resources) == 3
                    
                    # Try to scale beyond max_blocks
                    new_blocks = mode.scale_out(1)
                    
                    # Should not scale further since max_blocks=3
                    assert new_blocks == 0
                    assert len(mode.resources) == 3
            
            # Mock job submission and setting status to running
            job_ids = []
            resource_ids = list(mode.resources.keys())
            
            for i, resource_id in enumerate(resource_ids):
                job_id = f"job-{i+1}"
                # Set resource job mapping
                mode.resources[resource_id]["job_id"] = job_id
                # Set to running status
                mode.resources[resource_id]["status"] = "running"
                job_ids.append(job_id)
            
            # Test scaling in (removing instances)
            with patch.object(mode, '_delete_ec2_instance'):
                with patch.object(mode, '_delete_ssm_parameter'):
                    # Mark one job as complete
                    complete_resource_id = resource_ids[0]
                    mode.resources[complete_resource_id]["status"] = "completed"
                    
                    # Scale in one block
                    num_released = mode.scale_in(1)
                    
                    # Should remove the completed job's instance
                    assert num_released == 1
                    assert complete_resource_id not in mode.resources
                    assert len(mode.resources) == 2
                    
                    # Try to scale in more than min_blocks
                    all_resource_ids = list(mode.resources.keys())
                    for resource_id in all_resource_ids:
                        mode.resources[resource_id]["status"] = "completed"
                    
                    # Scale in remaining 2 instances
                    num_released = mode.scale_in(2)
                    
                    # Should remove both instances
                    assert num_released == 2
                    assert len(mode.resources) == 0
            
            # Clean up
            with patch.object(mode, '_delete_ec2_instance'):
                with patch.object(mode, '_delete_ssm_parameter'):
                    with patch.object(mode, '_delete_security_group'):
                        with patch.object(mode, '_delete_subnet'):
                            with patch.object(mode, '_delete_vpc'):
                                mode.preserve_bastion = False  # Ensure bastion is cleaned up
                                mode.cleanup_infrastructure()
    
    @pytest.mark.localstack
    def test_serverless_lambda_scaling(self, localstack_session, file_state_store):
        """Test autoscaling with Lambda functions in ServerlessMode."""
        # Create a ServerlessMode instance with Lambda and scaling configuration
        provider_id = f"test-provider-{uuid.uuid4().hex[:8]}"
        
        # Set up mocks for AWS services using LocalStack
        with patch('boto3.Session', return_value=localstack_session):
            # Create serverless mode with scaling configuration
            mode = ServerlessMode(
                provider_id=provider_id,
                session=localstack_session,
                state_store=file_state_store,
                region="us-east-1",
                worker_type="lambda",
                lambda_memory=128,
                lambda_timeout=30,
                max_blocks=10,
                min_blocks=0,
                init_blocks=0  # Serverless starts with 0 resources
            )
            
            # Initialize mode
            mode.initialize()
            
            # Mock Lambda function creation
            function_counter = 0
            
            def create_mock_function(*args, **kwargs):
                nonlocal function_counter
                function_counter += 1
                function_name = f"lambda-function-{function_counter}"
                return {
                    'FunctionName': function_name,
                    'FunctionArn': f"arn:aws:lambda:us-east-1:123456789012:function:{function_name}"
                }
            
            # Mock Lambda invocation for tasks
            mock_response = {
                'StatusCode': 200,
                'Payload': MagicMock(read=lambda: b'{"statusCode": 200, "body": "Success"}')
            }
            
            # Set up Lambda mocking
            mode.lambda_manager = MagicMock()
            mode.lambda_manager._create_lambda_function.side_effect = create_mock_function
            mode.lambda_manager.invoke_lambda_function.return_value = mock_response
            
            # Submit multiple jobs in parallel
            job_ids = []
            resource_ids = []
            
            # Lambda functions are created on demand, no initial blocks
            assert len(mode.resources) == 0
            
            # Submit 5 jobs - should create 5 Lambda functions
            for i in range(5):
                with patch("builtins.open", MagicMock()):  # Mock writing Lambda code to file
                    job_id = f"job-{i+1}"
                    resource_id = mode.submit_job(job_id, f"echo 'Job {i+1}'", 1)
                    job_ids.append(job_id)
                    resource_ids.append(resource_id)
            
            # Verify Lambda functions were created
            assert len(mode.resources) == 5
            assert mode.lambda_manager._create_lambda_function.call_count == 5
            
            # Test Lambda function invocation
            with patch("builtins.open", MagicMock()):  # Mock writing Lambda code to file
                for resource_id in resource_ids:
                    status = mode.get_job_status([resource_id])
                    assert resource_id in status
            
            # Lambda functions are automatically cleaned up after execution
            # This is a key difference from EC2-based modes
            
            # Clean up remaining resources
            mode.lambda_manager.delete_lambda_function.return_value = None
            mode.cleanup_infrastructure()
    
    @pytest.mark.localstack
    def test_auto_worker_type_selection(self, localstack_session, file_state_store):
        """Test automatic worker type selection in ServerlessMode."""
        # Create a ServerlessMode instance with auto worker type selection
        provider_id = f"test-provider-{uuid.uuid4().hex[:8]}"
        
        # Set up mocks for AWS services using LocalStack
        with patch('boto3.Session', return_value=localstack_session):
            # Create serverless mode with auto worker type
            mode = ServerlessMode(
                provider_id=provider_id,
                session=localstack_session,
                state_store=file_state_store,
                region="us-east-1",
                worker_type="auto",  # Auto selection based on job characteristics
                lambda_memory=128,
                lambda_timeout=30,
                ecs_task_cpu=256,
                ecs_task_memory=512,
                ecs_container_image="amazon/amazon-ecs-sample",
                max_blocks=10,
                min_blocks=0
            )
            
            # Initialize mode
            mode.initialize()
            
            # Mock Lambda function creation
            mode.lambda_manager = MagicMock()
            mode.lambda_manager._create_lambda_function.return_value = {
                'FunctionName': 'lambda-function',
                'FunctionArn': 'arn:aws:lambda:us-east-1:123456789012:function:lambda-function'
            }
            mode.lambda_manager.invoke_lambda_function.return_value = {
                'StatusCode': 200,
                'Payload': MagicMock(read=lambda: b'{"statusCode": 200, "body": "Success"}')
            }
            
            # Mock ECS task creation
            mode.ecs_manager = MagicMock()
            mode.ecs_manager.create_cluster.return_value = 'test-cluster'
            mode.ecs_manager.register_task_definition.return_value = 'test-task:1'
            mode.ecs_manager.run_task.return_value = {
                'taskArn': 'arn:aws:ecs:us-east-1:123456789012:task/test-cluster/abcdef12345',
                'clusterArn': 'arn:aws:ecs:us-east-1:123456789012:cluster/test-cluster',
                'taskDefinitionArn': 'arn:aws:ecs:us-east-1:123456789012:task-definition/test-task:1'
            }
            
            # Patch the worker type selection method
            with patch.object(mode, '_select_worker_type') as mock_select_worker:
                # First job is small, should use Lambda
                mock_select_worker.side_effect = ["lambda", "ecs", "lambda"]
                
                # Submit jobs with different characteristics
                with patch("builtins.open", MagicMock()):  # Mock writing Lambda code to file
                    # First job - small, should use Lambda
                    job1_id = "small-job"
                    resource1_id = mode.submit_job(job1_id, "echo 'Small job'", 1)
                    
                    # Lambda function should be created
                    assert mode.resources[resource1_id]["worker_type"] == "lambda"
                    assert mode.lambda_manager._create_lambda_function.call_count == 1
                    assert mode.ecs_manager.run_task.call_count == 0
                    
                    # Second job - larger, should use ECS
                    job2_id = "large-job"
                    resource2_id = mode.submit_job(job2_id, "echo 'Large job'", 4, job_name="cpu-intensive")
                    
                    # ECS task should be created
                    assert mode.resources[resource2_id]["worker_type"] == "ecs"
                    assert mode.lambda_manager._create_lambda_function.call_count == 1
                    assert mode.ecs_manager.run_task.call_count == 1
                    
                    # Third job - another small job, back to Lambda
                    job3_id = "another-small-job"
                    resource3_id = mode.submit_job(job3_id, "echo 'Another small job'", 1)
                    
                    # Lambda function should be created
                    assert mode.resources[resource3_id]["worker_type"] == "lambda"
                    assert mode.lambda_manager._create_lambda_function.call_count == 2
                    assert mode.ecs_manager.run_task.call_count == 1
            
            # Clean up
            mode.lambda_manager.delete_lambda_function.return_value = None
            mode.ecs_manager.stop_task.return_value = None
            mode.ecs_manager.deregister_task_definition.return_value = None
            mode.ecs_manager.delete_cluster.return_value = None
            mode.cleanup_infrastructure()
    
    @pytest.mark.localstack
    def test_hybrid_scaling_strategy(self, localstack_session, file_state_store):
        """Test hybrid scaling strategy using mixed resource types."""
        # Create a provider that uses a hybrid approach
        provider_id = f"test-provider-{uuid.uuid4().hex[:8]}"
        
        # Create a provider to handle both the test and implementation
        provider = EphemeralAWSProvider(
            provider_id=provider_id,
            region="us-east-1",
            mode="serverless",
            worker_type="auto",
            lambda_memory=128,
            lambda_timeout=30,
            ecs_task_cpu=256,
            ecs_task_memory=512,
            ecs_container_image="amazon/amazon-ecs-sample",
            max_blocks=10,
            min_blocks=0,
            init_blocks=0,
            # Inject our session and state store for testing
            _test_session=localstack_session,
            _test_state_store=file_state_store
        )
        
        # Initialize the internal operating mode directly
        with patch.object(provider, '_initialize_operating_mode'):
            # Mock the operating mode
            mode = ServerlessMode(
                provider_id=provider_id,
                session=localstack_session,
                state_store=file_state_store,
                region="us-east-1",
                worker_type="auto",
                lambda_memory=128,
                lambda_timeout=30,
                ecs_task_cpu=256,
                ecs_task_memory=512,
                ecs_container_image="amazon/amazon-ecs-sample",
                max_blocks=10,
                min_blocks=0,
                init_blocks=0
            )
            
            # Initialize mode
            mode.initialize()
            
            # Replace provider's operating mode with our mocked one
            provider._operating_mode = mode
            
            # Mock Lambda function and ECS task creation
            mode.lambda_manager = MagicMock()
            mode.ecs_manager = MagicMock()
            
            # Configure mocks
            mode.lambda_manager._create_lambda_function.return_value = {
                'FunctionName': 'lambda-function',
                'FunctionArn': 'arn:aws:lambda:us-east-1:123456789012:function:lambda-function'
            }
            mode.lambda_manager.invoke_lambda_function.return_value = {
                'StatusCode': 200,
                'Payload': MagicMock(read=lambda: b'{"statusCode": 200, "body": "Success"}')
            }
            
            mode.ecs_manager.create_cluster.return_value = 'test-cluster'
            mode.ecs_manager.register_task_definition.return_value = 'test-task:1'
            mode.ecs_manager.run_task.return_value = {
                'taskArn': 'arn:aws:ecs:us-east-1:123456789012:task/test-cluster/abcdef12345',
                'clusterArn': 'arn:aws:ecs:us-east-1:123456789012:cluster/test-cluster',
                'taskDefinitionArn': 'arn:aws:ecs:us-east-1:123456789012:task-definition/test-task:1'
            }
            
            # Test the provider's submit method with different job types
            with patch.object(mode, '_select_worker_type') as mock_select_worker:
                mock_select_worker.side_effect = ["lambda", "ecs", "lambda", "ecs", "lambda"]
                
                # Submit a mix of jobs with provider interface
                with patch("builtins.open", MagicMock()):  # Mock writing Lambda code to file
                    # Submit 5 mixed jobs
                    job_ids = []
                    for i in range(5):
                        job_id = f"job-{i+1}"
                        command = f"echo 'Job {i+1}'"
                        
                        # Submit through provider interface
                        with patch.object(provider, 'status'):  # Mock status calls
                            resource_id = provider.submit(job_id, command)
                            job_ids.append(job_id)
                    
                    # Verify jobs were submitted and tracked
                    assert len(mode.resources) == 5
                    
                    # Should have 3 Lambda functions and 2 ECS tasks based on our mock
                    lambda_count = sum(1 for r in mode.resources.values() if r.get("worker_type") == "lambda")
                    ecs_count = sum(1 for r in mode.resources.values() if r.get("worker_type") == "ecs")
                    
                    assert lambda_count == 3
                    assert ecs_count == 2
                    
                    # Check status through provider interface
                    all_resource_ids = list(mode.resources.keys())
                    
                    # Patch status to return running for all jobs
                    with patch.object(mode, 'get_job_status') as mock_status:
                        mock_status.return_value = {rid: "running" for rid in all_resource_ids}
                        status = provider.status(job_ids)
                        
                        # All jobs should be running
                        for job_id in job_ids:
                            assert status[job_id] == "running"
                    
                    # Test cancellation through provider interface
                    with patch.object(mode, 'cancel_jobs') as mock_cancel:
                        mock_cancel.return_value = {rid: "cancelled" for rid in all_resource_ids}
                        cancel_status = provider.cancel(job_ids)
                        
                        # All jobs should be cancelled
                        for job_id in job_ids:
                            assert cancel_status[job_id] == "cancelled"
            
            # Clean up - use provider interface
            with patch.object(mode, 'cleanup_infrastructure'):
                provider.shutdown()
                # Verify cleanup was called
                mode.cleanup_infrastructure.assert_called_once()
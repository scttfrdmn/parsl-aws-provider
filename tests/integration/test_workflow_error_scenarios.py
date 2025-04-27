"""Integration tests for error handling in workflow scenarios.

These tests simulate error conditions that might occur during real-world
workflows to verify proper recovery mechanisms and error handling.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import os
import json
import time
import uuid
import pytest
import tempfile
from unittest.mock import MagicMock, patch
from botocore.exceptions import ClientError

from parsl_ephemeral_aws.provider import EphemeralAWSProvider
from parsl_ephemeral_aws.modes.standard import StandardMode
from parsl_ephemeral_aws.modes.detached import DetachedMode
from parsl_ephemeral_aws.modes.serverless import ServerlessMode
from parsl_ephemeral_aws.state.file import FileStateStore
from parsl_ephemeral_aws.exceptions import (
    ResourceCreationError,
    JobExecutionError,
    SpotInstanceError,
    BastionHostError,
    LambdaFunctionError,
    EC2InstanceError,
)
from parsl_ephemeral_aws.utils.localstack import is_localstack_available, get_localstack_session


# Skip all tests if LocalStack is not available
pytestmark = pytest.mark.skipif(
    not is_localstack_available(),
    reason="LocalStack is not available. Make sure it's running on port 4566."
)


@pytest.mark.integration
class TestWorkflowErrorScenarios:
    """Integration tests for workflow error scenarios."""
    
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
    def test_infrastructure_failure_recovery(self, localstack_session, file_state_store):
        """Test recovery after infrastructure creation failure."""
        # Create a StandardMode instance
        provider_id = f"test-provider-{uuid.uuid4().hex[:8]}"
        
        # Set up mocks for AWS services using LocalStack
        with patch('boto3.Session', return_value=localstack_session):
            # Create a standard mode
            mode = StandardMode(
                provider_id=provider_id,
                session=localstack_session,
                state_store=file_state_store,
                region="us-east-1",
                instance_type="t2.micro",
                image_id="ami-12345678"  # Dummy AMI
            )
            
            # Set up a mock VPC creation that fails
            with patch.object(mode, '_create_vpc') as mock_create_vpc:
                mock_create_vpc.side_effect = ResourceCreationError("VPC creation failed")
                
                # Initialize should fail
                with pytest.raises(ResourceCreationError):
                    mode.initialize()
                
                # Verify mode is not initialized
                assert not mode.initialized
                assert mode.vpc_id is None
            
            # Now let's make it succeed
            with patch.object(mode, '_create_vpc', return_value="vpc-12345"):
                with patch.object(mode, '_create_subnet', return_value="subnet-12345"):
                    with patch.object(mode, '_create_security_group', return_value="sg-12345"):
                        mode.initialize()
            
            # Verify mode is now initialized
            assert mode.initialized
            assert mode.vpc_id == "vpc-12345"
            assert mode.subnet_id == "subnet-12345"
            assert mode.security_group_id == "sg-12345"
            
            # Clean up
            with patch.object(mode, '_delete_security_group'):
                with patch.object(mode, '_delete_subnet'):
                    with patch.object(mode, '_delete_vpc'):
                        mode.cleanup_infrastructure()
    
    @pytest.mark.localstack
    def test_instance_creation_failure_recovery(self, localstack_session, file_state_store):
        """Test recovery after instance creation failures."""
        # Create a StandardMode instance
        provider_id = f"test-provider-{uuid.uuid4().hex[:8]}"
        
        # Set up mocks for AWS services using LocalStack
        with patch('boto3.Session', return_value=localstack_session):
            # Create a standard mode
            mode = StandardMode(
                provider_id=provider_id,
                session=localstack_session,
                state_store=file_state_store,
                region="us-east-1",
                instance_type="t2.micro",
                image_id="ami-12345678"  # Dummy AMI
            )
            
            # Initialize mode to create basic infrastructure (VPC, etc.)
            with patch.object(mode, '_create_vpc', return_value="vpc-12345"):
                with patch.object(mode, '_create_subnet', return_value="subnet-12345"):
                    with patch.object(mode, '_create_security_group', return_value="sg-12345"):
                        mode.initialize()
            
            # Mock instance creation that fails
            with patch.object(mode, '_create_ec2_instance') as mock_create_instance:
                # Simulate instance creation failure
                mock_create_instance.side_effect = EC2InstanceError("Instance creation failed")
                
                # Submit job should fail
                job_id = f"test-job-{uuid.uuid4().hex[:8]}"
                with pytest.raises(JobExecutionError):
                    mode.submit_job(job_id, "echo 'Test job'", 1)
                
                # Verify no resources were created
                assert len(mode.resources) == 0
            
            # Now let's make it succeed
            with patch.object(mode, '_create_ec2_instance') as mock_create_instance:
                mock_create_instance.return_value = {
                    'instance_id': f"i-{uuid.uuid4().hex[:12]}",
                    'private_ip': '10.0.0.1',
                    'public_ip': '54.123.456.789',
                    'dns_name': 'ec2-54-123-456-789.compute-1.amazonaws.com'
                }
                
                # Submit should succeed
                job_id = f"test-job-{uuid.uuid4().hex[:8]}"
                resource_id = mode.submit_job(job_id, "echo 'Test job'", 1)
                
                # Verify resource was created
                assert resource_id in mode.resources
                assert mode.resources[resource_id]["job_id"] == job_id
            
            # Clean up
            with patch.object(mode, '_delete_ec2_instance'):
                with patch.object(mode, '_delete_security_group'):
                    with patch.object(mode, '_delete_subnet'):
                        with patch.object(mode, '_delete_vpc'):
                            mode.cleanup_infrastructure()
    
    @pytest.mark.localstack
    def test_bastion_failure_recovery(self, localstack_session, file_state_store):
        """Test recovery after bastion host creation failure."""
        # Create a DetachedMode instance
        provider_id = f"test-provider-{uuid.uuid4().hex[:8]}"
        workflow_id = f"test-workflow-{uuid.uuid4().hex[:8]}"
        
        # Set up mocks for AWS services using LocalStack
        with patch('boto3.Session', return_value=localstack_session):
            # Create a detached mode
            mode = DetachedMode(
                provider_id=provider_id,
                session=localstack_session,
                state_store=file_state_store,
                region="us-east-1",
                instance_type="t2.micro",
                image_id="ami-12345678",  # Dummy AMI
                workflow_id=workflow_id,
                bastion_instance_type="t2.micro",
                bastion_host_type="direct"
            )
            
            # Set up mocks for network resources
            with patch.object(mode, '_create_vpc', return_value="vpc-12345"):
                with patch.object(mode, '_create_subnet', return_value="subnet-12345"):
                    with patch.object(mode, '_create_security_group', return_value="sg-12345"):
                        # Mock bastion creation that fails
                        with patch.object(mode, '_create_bastion_host') as mock_create_bastion:
                            mock_create_bastion.side_effect = BastionHostError("Bastion host creation failed")
                            
                            # Initialize should fail
                            with pytest.raises(BastionHostError):
                                with patch.object(mode, '_create_tags'):
                                    mode.initialize()
                            
                            # Verify mode is not fully initialized
                            assert not mode.initialized
                            assert mode.vpc_id == "vpc-12345"  # These were created before the failure
                            assert mode.subnet_id == "subnet-12345"
                            assert mode.security_group_id == "sg-12345"
                            assert mode.bastion_id is None  # This failed
                        
                        # Now let's make bastion creation succeed
                        with patch.object(mode, '_create_bastion_host') as mock_create_bastion:
                            mock_create_bastion.return_value = {
                                'instance_id': f"i-bastion-{uuid.uuid4().hex[:8]}",
                                'private_ip': '10.0.0.2',
                                'public_ip': '54.123.456.789',
                                'dns_name': 'ec2-54-123-456-789.compute-1.amazonaws.com'
                            }
                            
                            # Initialize should succeed
                            with patch.object(mode, '_create_tags'):
                                mode.initialize()
                            
                            # Verify mode is now initialized
                            assert mode.initialized
                            assert mode.bastion_id is not None
            
            # Clean up
            with patch.object(mode, '_delete_ec2_instance'):
                with patch.object(mode, '_delete_security_group'):
                    with patch.object(mode, '_delete_subnet'):
                        with patch.object(mode, '_delete_vpc'):
                            mode.preserve_bastion = False  # Ensure bastion is cleaned up
                            mode.cleanup_infrastructure()
    
    @pytest.mark.localstack
    def test_lambda_failure_recovery(self, localstack_session, file_state_store):
        """Test recovery after Lambda function creation failure."""
        # Create a ServerlessMode instance
        provider_id = f"test-provider-{uuid.uuid4().hex[:8]}"
        
        # Set up mocks for AWS services using LocalStack
        with patch('boto3.Session', return_value=localstack_session):
            # Create a serverless mode
            mode = ServerlessMode(
                provider_id=provider_id,
                session=localstack_session,
                state_store=file_state_store,
                region="us-east-1",
                worker_type="lambda",  # Use Lambda for simplicity
                lambda_memory=128,
                lambda_timeout=30
            )
            
            # Initialize mode - Lambda-only mode won't create network resources
            mode.initialize()
            
            # Verify mode is initialized
            assert mode.initialized
            
            # Mock Lambda function creation that fails
            mode.lambda_manager = MagicMock()
            mode.lambda_manager._create_lambda_function.side_effect = LambdaFunctionError("Lambda function creation failed")
            
            # Submit job should fail
            job_id = f"test-job-{uuid.uuid4().hex[:8]}"
            with pytest.raises(JobExecutionError):
                # Mock writing to temp file for Lambda code
                with patch("builtins.open", MagicMock()):
                    mode.submit_job(job_id, "echo 'Test job'", 1)
            
            # Verify no resources were created
            assert len(mode.resources) == 0
            
            # Now let's make it succeed
            mode.lambda_manager._create_lambda_function.side_effect = None
            mode.lambda_manager._create_lambda_function.return_value = {
                'FunctionName': f"lambda-{uuid.uuid4().hex[:8]}",
                'FunctionArn': f"arn:aws:lambda:us-east-1:123456789012:function:lambda-{uuid.uuid4().hex[:8]}"
            }
            
            mode.lambda_manager.invoke_lambda_function.return_value = {
                'StatusCode': 200,
                'Payload': {'statusCode': 200, 'body': 'Success'}
            }
            
            # Submit should succeed
            job_id = f"test-job-{uuid.uuid4().hex[:8]}"
            # Mock writing to temp file for Lambda code
            with patch("builtins.open", MagicMock()):
                resource_id = mode.submit_job(job_id, "echo 'Test job'", 1)
            
            # Verify resource was created
            assert resource_id in mode.resources
            assert mode.resources[resource_id]["job_id"] == job_id
            
            # Clean up
            mode.lambda_manager.delete_lambda_function.return_value = None
            mode.cleanup_infrastructure()
    
    @pytest.mark.localstack
    def test_retry_after_throttling(self, localstack_session, file_state_store):
        """Test retry logic after AWS API throttling."""
        # Create a StandardMode instance
        provider_id = f"test-provider-{uuid.uuid4().hex[:8]}"
        
        # Set up mocks for AWS services using LocalStack
        with patch('boto3.Session', return_value=localstack_session):
            # Create a standard mode
            mode = StandardMode(
                provider_id=provider_id,
                session=localstack_session,
                state_store=file_state_store,
                region="us-east-1",
                instance_type="t2.micro",
                image_id="ami-12345678"  # Dummy AMI
            )
            
            # Initialize mode to create infrastructure
            with patch.object(mode, '_create_vpc', return_value="vpc-12345"):
                with patch.object(mode, '_create_subnet', return_value="subnet-12345"):
                    with patch.object(mode, '_create_security_group', return_value="sg-12345"):
                        mode.initialize()
            
            # Mock EC2 instance creation that gets throttled first, then succeeds
            with patch.object(mode, '_create_ec2_instance') as mock_create_instance:
                # First call gets throttled
                throttle_error = ClientError(
                    {
                        'Error': {
                            'Code': 'RequestLimitExceeded',
                            'Message': 'Request limit exceeded'
                        }
                    },
                    'RunInstances'
                )
                # Second call succeeds
                mock_create_instance.side_effect = [
                    throttle_error,
                    {
                        'instance_id': f"i-{uuid.uuid4().hex[:12]}",
                        'private_ip': '10.0.0.1',
                        'public_ip': '54.123.456.789',
                        'dns_name': 'ec2-54-123-456-789.compute-1.amazonaws.com'
                    }
                ]
                
                # Mock sleep to avoid actual waiting
                with patch('time.sleep'):
                    # Set up retries in the EC2 manager
                    with patch.object(mode.ec2_manager, 'max_retries', 3):
                        with patch.object(mode.ec2_manager, 'retry_base_delay', 0):
                            # Submit job
                            job_id = f"test-job-{uuid.uuid4().hex[:8]}"
                            resource_id = mode.submit_job(job_id, "echo 'Test job'", 1)
            
            # Verify resource was eventually created despite throttling
            assert resource_id in mode.resources
            assert mode.resources[resource_id]["job_id"] == job_id
            
            # Verify create_instance was called twice (once with throttling, once success)
            assert mock_create_instance.call_count == 2
            
            # Clean up
            with patch.object(mode, '_delete_ec2_instance'):
                with patch.object(mode, '_delete_security_group'):
                    with patch.object(mode, '_delete_subnet'):
                        with patch.object(mode, '_delete_vpc'):
                            mode.cleanup_infrastructure()
    
    @pytest.mark.localstack
    def test_partial_infrastructure_cleanup(self, localstack_session, file_state_store):
        """Test partial infrastructure cleanup after some resources fail to delete."""
        # Create a StandardMode instance
        provider_id = f"test-provider-{uuid.uuid4().hex[:8]}"
        
        # Set up mocks for AWS services using LocalStack
        with patch('boto3.Session', return_value=localstack_session):
            # Create a standard mode
            mode = StandardMode(
                provider_id=provider_id,
                session=localstack_session,
                state_store=file_state_store,
                region="us-east-1",
                instance_type="t2.micro",
                image_id="ami-12345678"  # Dummy AMI
            )
            
            # Initialize mode to create basic infrastructure
            with patch.object(mode, '_create_vpc', return_value="vpc-12345"):
                with patch.object(mode, '_create_subnet', return_value="subnet-12345"):
                    with patch.object(mode, '_create_security_group', return_value="sg-12345"):
                        mode.initialize()
            
            # Create an instance
            with patch.object(mode, '_create_ec2_instance') as mock_create_instance:
                mock_create_instance.return_value = {
                    'instance_id': "i-12345",
                    'private_ip': '10.0.0.1',
                    'public_ip': '54.123.456.789',
                    'dns_name': 'ec2-54-123-456-789.compute-1.amazonaws.com'
                }
                
                # Submit a job
                job_id = f"test-job-{uuid.uuid4().hex[:8]}"
                resource_id = mode.submit_job(job_id, "echo 'Test job'", 1)
            
            # Mock instance deletion to fail
            with patch.object(mode, '_delete_ec2_instance') as mock_delete_instance:
                mock_delete_instance.side_effect = ResourceDeletionError("Instance deletion failed")
                
                # Mock the other deletions to succeed
                with patch.object(mode, '_delete_security_group'):
                    with patch.object(mode, '_delete_subnet'):
                        with patch.object(mode, '_delete_vpc'):
                            # Cleanup should log the error but continue with other resources
                            with patch('logging.Logger.error') as mock_error:
                                mode.cleanup_infrastructure()
                                
                                # Verify error was logged
                                mock_error.assert_called()
            
            # Even though instance deletion failed, the mode should be marked as not initialized
            assert not mode.initialized
            assert mode.vpc_id is None
            assert mode.subnet_id is None
            assert mode.security_group_id is None
    
    @pytest.mark.localstack
    def test_spot_instance_interruption(self, localstack_session, file_state_store):
        """Test handling of spot instance interruption."""
        # Create a StandardMode instance with spot instances
        provider_id = f"test-provider-{uuid.uuid4().hex[:8]}"
        
        # Set up mocks for AWS services using LocalStack
        with patch('boto3.Session', return_value=localstack_session):
            # Create a standard mode with spot instances
            mode = StandardMode(
                provider_id=provider_id,
                session=localstack_session,
                state_store=file_state_store,
                region="us-east-1",
                instance_type="t2.micro",
                image_id="ami-12345678",  # Dummy AMI
                use_spot=True,
                spot_interruption_handling=True
            )
            
            # Initialize mode
            with patch.object(mode, '_create_vpc', return_value="vpc-12345"):
                with patch.object(mode, '_create_subnet', return_value="subnet-12345"):
                    with patch.object(mode, '_create_security_group', return_value="sg-12345"):
                        mode.initialize()
            
            # Verify spot interruption handler is set up
            assert mode.spot_interruption_monitor is not None
            assert mode.spot_interruption_handler is not None
            
            # Create a spot instance
            with patch.object(mode, '_create_ec2_instance') as mock_create_instance:
                # Return a spot instance
                instance_id = "i-spot-12345"
                mock_create_instance.return_value = {
                    'instance_id': instance_id,
                    'private_ip': '10.0.0.1',
                    'public_ip': '54.123.456.789',
                    'dns_name': 'ec2-54-123-456-789.compute-1.amazonaws.com',
                    'spot_instance': True
                }
                
                # Submit a job
                job_id = f"test-job-{uuid.uuid4().hex[:8]}"
                resource_id = mode.submit_job(job_id, "echo 'Test job'", 1)
            
            # Verify instance was registered with the spot interruption monitor
            assert instance_id in mode.spot_interruption_monitor.instance_handlers
            
            # Simulate spot interruption event
            with patch.object(mode, '_create_ec2_instance') as mock_create_instance:
                # Return a new spot instance (replacement)
                new_instance_id = "i-spot-replacement"
                mock_create_instance.return_value = {
                    'instance_id': new_instance_id,
                    'private_ip': '10.0.0.2',
                    'public_ip': '54.123.456.790',
                    'dns_name': 'ec2-54-123-456-790.compute-1.amazonaws.com',
                    'spot_instance': True
                }
                
                # Mock resource state to job mapping
                mode.resource_job_mapping = {resource_id: job_id}
                
                # Trigger the spot interruption handler
                handler = mode.spot_interruption_monitor.instance_handlers[instance_id]
                handler(instance_id, {
                    'InstanceAction': 'terminate',
                    'InstanceId': instance_id,
                    'Time': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                })
            
            # Clean up
            with patch.object(mode, '_delete_ec2_instance'):
                with patch.object(mode, '_delete_security_group'):
                    with patch.object(mode, '_delete_subnet'):
                        with patch.object(mode, '_delete_vpc'):
                            if mode.spot_interruption_monitor:
                                mode.spot_interruption_monitor.stop_monitoring()
                            mode.cleanup_infrastructure()
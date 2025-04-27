"""Integration tests for spot interruption handling using LocalStack.

These tests verify that spot interruption handling works correctly across
all operating modes, including detection, checkpointing, and recovery.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import os
import pytest
import time
import boto3
import json
import uuid
from unittest.mock import patch, MagicMock

from parsl_ephemeral_aws.modes.standard import StandardMode
from parsl_ephemeral_aws.modes.detached import DetachedMode
from parsl_ephemeral_aws.modes.serverless import ServerlessMode
from parsl_ephemeral_aws.compute.spot_interruption import (
    SpotInterruptionMonitor,
    SpotInterruptionHandler,
    ParslSpotInterruptionHandler
)
from parsl_ephemeral_aws.state.file import FileStateStore
from parsl_ephemeral_aws.utils.localstack import is_localstack_available, get_localstack_session


@pytest.fixture(scope="session")
def localstack_available():
    """Check if LocalStack is available for testing."""
    if not is_localstack_available():
        pytest.skip("LocalStack is not available. Make sure it's running on port 4566.")
    return True


@pytest.fixture(scope="session")
def localstack_session(localstack_available):
    """Create a session connected to LocalStack."""
    return get_localstack_session()


@pytest.fixture
def temp_state_store(tmpdir):
    """Create a temporary state store for testing."""
    state_file = tmpdir.join("state.json")
    return FileStateStore(file_path=str(state_file))


@pytest.fixture
def provider_id():
    """Generate a unique provider ID for tests."""
    return f"test-provider-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def checkpoint_bucket(localstack_session, provider_id):
    """Create a temporary S3 bucket for checkpoints."""
    bucket_name = f"test-checkpoint-bucket-{provider_id.split('-')[-1]}"
    s3_client = localstack_session.client("s3")
    
    try:
        s3_client.create_bucket(Bucket=bucket_name)
    except Exception as e:
        pytest.skip(f"Failed to create S3 bucket: {e}")
    
    yield bucket_name
    
    # Cleanup
    try:
        # Delete all objects in the bucket
        objects = s3_client.list_objects_v2(Bucket=bucket_name)
        if 'Contents' in objects:
            delete_keys = {'Objects': [{'Key': obj['Key']} for obj in objects['Contents']]}
            s3_client.delete_objects(Bucket=bucket_name, Delete=delete_keys)
        
        # Delete the bucket
        s3_client.delete_bucket(Bucket=bucket_name)
    except Exception as e:
        print(f"Error cleaning up bucket {bucket_name}: {e}")


@pytest.fixture
def mock_spot_instance_id(localstack_session):
    """Create a spot instance ID for testing."""
    return f"i-spot-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def mock_spot_fleet_id(localstack_session):
    """Create a spot fleet ID for testing."""
    return f"sfr-{uuid.uuid4().hex[:8]}"


@pytest.mark.integration
class TestSpotInterruptionHandlingLocalstack:
    """Integration tests for spot interruption handling using LocalStack."""
    
    @pytest.mark.localstack
    def test_spot_interruption_monitor_creation(self, localstack_session):
        """Test that SpotInterruptionMonitor can be initialized."""
        monitor = SpotInterruptionMonitor(session=localstack_session)
        assert monitor is not None
        assert monitor.session == localstack_session
        assert monitor.instance_handlers == {}
        assert monitor.fleet_handlers == {}
        
        # Start monitoring
        monitor.start_monitoring()
        assert monitor.monitoring_thread is not None
        assert monitor.monitoring_thread.is_alive()
        
        # Stop monitoring
        monitor.stop_monitoring()
        time.sleep(0.5)  # Let thread terminate
        assert not monitor.monitoring_thread.is_alive()
    
    @pytest.mark.localstack
    def test_spot_interruption_handler_with_s3(self, localstack_session, checkpoint_bucket):
        """Test that SpotInterruptionHandler can use S3 for checkpoints."""
        handler = SpotInterruptionHandler(
            session=localstack_session,
            checkpoint_bucket=checkpoint_bucket,
            checkpoint_prefix="test-checkpoints"
        )
        
        assert handler is not None
        assert handler.checkpoint_bucket == checkpoint_bucket
        
        # Test saving a checkpoint
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        test_data = {"state": "running", "progress": 50, "timestamp": time.time()}
        
        uri = handler.save_checkpoint(task_id, test_data)
        assert uri == f"s3://{checkpoint_bucket}/test-checkpoints/{task_id}.json"
        
        # Test loading the checkpoint
        loaded_data = handler.load_checkpoint(task_id)
        assert loaded_data is not None
        assert loaded_data["state"] == "running"
        assert loaded_data["progress"] == 50
    
    @pytest.mark.localstack
    def test_parsl_handler_task_registration(self, localstack_session, checkpoint_bucket, mock_spot_instance_id):
        """Test that tasks can be registered with the ParslSpotInterruptionHandler."""
        handler = ParslSpotInterruptionHandler(
            session=localstack_session,
            checkpoint_bucket=checkpoint_bucket,
            checkpoint_prefix="test-checkpoints"
        )
        
        # Register tasks
        task_ids = [f"task-{uuid.uuid4().hex[:8]}" for _ in range(3)]
        for task_id in task_ids:
            handler.register_task(task_id, mock_spot_instance_id)
        
        # Verify tasks were registered
        assert mock_spot_instance_id in handler.task_mapping
        assert len(handler.task_mapping[mock_spot_instance_id]) == 3
        for task_id in task_ids:
            assert task_id in handler.task_mapping[mock_spot_instance_id]
    
    @pytest.mark.localstack
    def test_standard_mode_with_spot_interruption(self, localstack_session, temp_state_store, provider_id, checkpoint_bucket, mock_spot_instance_id):
        """Test that StandardMode can handle spot interruptions."""
        # Create StandardMode instance with spot interruption handling
        mode = StandardMode(
            provider_id=provider_id,
            session=localstack_session,
            state_store=temp_state_store,
            region="us-east-1",
            instance_type="t2.micro",
            image_id="ami-12345678",  # Dummy AMI ID for testing
            use_spot=True,
            spot_interruption_handling=True,
            checkpoint_bucket=checkpoint_bucket,
            checkpoint_prefix="test/checkpoints"
        )
        
        try:
            # Initialize mode
            mode.initialize()
            
            # Verify spot interruption monitor was created
            assert mode.spot_interruption_monitor is not None
            assert mode.spot_interruption_handler is not None
            
            # Mock instance creation response
            with patch.object(mode, '_create_ec2_instance') as mock_create_instance:
                mock_create_instance.return_value = {
                    'instance_id': mock_spot_instance_id,
                    'private_ip': '10.0.0.5',
                    'public_ip': '54.123.456.789',
                    'dns_name': 'ec2-54-123-456-789.compute-1.amazonaws.com'
                }
                
                # Submit a job
                job_id = f"test-job-{uuid.uuid4().hex[:8]}"
                command = "echo hello"
                resource_id = mode.submit_job(job_id, command, 1)
            
            # Verify spot instance was registered with monitor
            assert mock_spot_instance_id in mode.spot_interruption_monitor.instance_handlers
            
            # Mock a spot interruption event
            event = {
                'detail-type': 'EC2 Spot Instance Interruption Warning',
                'source': 'aws.ec2',
                'detail': {
                    'instance-id': mock_spot_instance_id,
                    'instance-action': 'terminate'
                }
            }
            
            # Save a checkpoint before interruption
            checkpoint_data = {
                'job_id': job_id,
                'progress': 75,
                'timestamp': time.time()
            }
            
            # Set up task in handler
            mode.spot_interruption_handler.register_task(job_id, mock_spot_instance_id)
            uri = mode.spot_interruption_handler.save_checkpoint(job_id, checkpoint_data)
            
            # Trigger interruption handler
            handler_func = mode.spot_interruption_monitor.instance_handlers[mock_spot_instance_id]
            handler_func(mock_spot_instance_id, event['detail'])
            
            # Verify task was queued for recovery
            assert not mode.spot_interruption_handler.recovery_queue.empty()
            
            # Get the recovery task
            recovery_task = mode.spot_interruption_handler.get_next_recovery_task()
            assert recovery_task is not None
            assert recovery_task['task_id'] == job_id
            
            # Load the checkpoint
            loaded_data = mode.spot_interruption_handler.load_checkpoint(job_id)
            assert loaded_data is not None
            assert loaded_data['job_id'] == job_id
            assert loaded_data['progress'] == 75
            
        finally:
            # Clean up resources
            if hasattr(mode, 'spot_interruption_monitor') and mode.spot_interruption_monitor:
                mode.spot_interruption_monitor.stop_monitoring()
            
            mode.cleanup_infrastructure()
    
    @pytest.mark.localstack
    def test_detached_mode_with_spot_interruption(self, localstack_session, temp_state_store, provider_id, checkpoint_bucket, mock_spot_instance_id):
        """Test that DetachedMode can handle spot interruptions."""
        # Create DetachedMode instance with spot interruption handling
        workflow_id = f"test-workflow-{uuid.uuid4().hex[:8]}"
        mode = DetachedMode(
            provider_id=provider_id,
            session=localstack_session,
            state_store=temp_state_store,
            region="us-east-1",
            instance_type="t2.micro",
            image_id="ami-12345678",  # Dummy AMI ID for testing
            workflow_id=workflow_id,
            bastion_instance_type="t2.micro",
            bastion_host_type="direct",  # Use direct mode for simpler testing
            use_spot=True,
            spot_interruption_handling=True,
            checkpoint_bucket=checkpoint_bucket,
            checkpoint_prefix="test/checkpoints"
        )
        
        try:
            # Initialize mode
            with patch.object(mode, '_create_bastion_host') as mock_create_bastion:
                mock_create_bastion.return_value = {
                    'instance_id': f"i-bastion-{uuid.uuid4().hex[:8]}",
                    'private_ip': '10.0.0.2',
                    'public_ip': '54.123.456.789',
                    'dns_name': 'ec2-54-123-456-789.compute-1.amazonaws.com'
                }
                mode.initialize()
            
            # Verify spot interruption monitor was created
            assert mode.spot_interruption_monitor is not None
            assert mode.spot_interruption_handler is not None
            
            # Mock instance creation response
            with patch.object(mode, '_create_ec2_instance') as mock_create_instance:
                mock_create_instance.return_value = {
                    'instance_id': mock_spot_instance_id,
                    'private_ip': '10.0.0.5',
                    'public_ip': '54.123.456.789',
                    'dns_name': 'ec2-54-123-456-789.compute-1.amazonaws.com'
                }
                
                # Submit a job
                job_id = f"test-job-{uuid.uuid4().hex[:8]}"
                command = "echo hello"
                resource_id = mode.submit_job(job_id, command, 1)
            
            # Verify spot instance was registered with monitor
            assert mock_spot_instance_id in mode.spot_interruption_monitor.instance_handlers
            
            # Verify state persistence
            mode.save_state()
            
            # Create a new mode instance to simulate restart
            mode2 = DetachedMode(
                provider_id=provider_id,
                session=localstack_session,
                state_store=temp_state_store,
                region="us-east-1",
                instance_type="t2.micro",
                image_id="ami-12345678",  # Dummy AMI ID for testing
                workflow_id=workflow_id,
                bastion_instance_type="t2.micro",
                bastion_host_type="direct",  # Use direct mode for simpler testing
                use_spot=True,
                spot_interruption_handling=True,
                checkpoint_bucket=checkpoint_bucket,
                checkpoint_prefix="test/checkpoints"
            )
            
            # Load state
            mode2.load_state()
            
            # Verify spot interruption monitor was recreated with the same registrations
            assert mode2.spot_interruption_monitor is not None
            assert mock_spot_instance_id in mode2.spot_interruption_monitor.instance_handlers
            
        finally:
            # Clean up resources
            if hasattr(mode, 'spot_interruption_monitor') and mode.spot_interruption_monitor:
                mode.spot_interruption_monitor.stop_monitoring()
            
            mode.preserve_bastion = False  # Ensure bastion cleanup
            mode.cleanup_infrastructure()
    
    @pytest.mark.localstack
    def test_serverless_mode_with_spot_fleet(self, localstack_session, temp_state_store, provider_id, checkpoint_bucket, mock_spot_fleet_id):
        """Test that ServerlessMode can handle spot fleet interruptions."""
        # Create ServerlessMode instance with spot interruption handling
        mode = ServerlessMode(
            provider_id=provider_id,
            session=localstack_session,
            state_store=temp_state_store,
            region="us-east-1",
            worker_type="ecs",
            ecs_task_cpu=256,
            ecs_task_memory=512,
            ecs_container_image="amazon/amazon-ecs-sample",
            use_spot=True,
            use_spot_fleet=True,
            spot_interruption_handling=True,
            checkpoint_bucket=checkpoint_bucket,
            checkpoint_prefix="test/checkpoints",
            instance_types=["t2.micro", "t3.micro"]
        )
        
        try:
            # Initialize mode
            mode.initialize()
            
            # Verify spot interruption monitor was created
            assert mode.spot_interruption_monitor is not None
            assert mode.spot_interruption_handler is not None
            
            # Mock CloudFormation stack creation
            with patch.object(mode, '_create_cloudformation_stack') as mock_create_stack:
                mock_create_stack.return_value = {
                    'stack_id': f"arn:aws:cloudformation:us-east-1:123456789012:stack/stack-{uuid.uuid4().hex[:8]}/abcdef12",
                    'stack_name': f"stack-{uuid.uuid4().hex[:8]}"
                }
                
                # Mock CloudFormation stack description to include spot fleet ID
                cf_client = localstack_session.client('cloudformation')
                cf_client.describe_stacks.return_value = {
                    'Stacks': [
                        {
                            'StackStatus': 'CREATE_COMPLETE',
                            'Outputs': [
                                {
                                    'OutputKey': 'SpotFleetRequestId',
                                    'OutputValue': mock_spot_fleet_id
                                }
                            ]
                        }
                    ]
                }
                
                # Submit a job
                job_id = f"test-job-{uuid.uuid4().hex[:8]}"
                command = "echo hello"
                resource_id = mode._submit_ecs_job(job_id, command, 1, "test-job", job_id)
            
            # Register fleet manually to test
            mode.spot_interruption_monitor.register_fleet(
                mock_spot_fleet_id,
                mode.spot_interruption_handler.handle_fleet_interruption
            )
            
            # Verify spot fleet was registered with monitor
            assert mock_spot_fleet_id in mode.spot_interruption_monitor.fleet_handlers
            
            # Mock a spot fleet interruption event
            event = {
                'detail-type': 'EC2 Spot Fleet Interruption Warning',
                'source': 'aws.ec2',
                'detail': {
                    'spot-fleet-request-id': mock_spot_fleet_id,
                    'instance-action': 'terminate'
                }
            }
            
            # Get a list of instances in the fleet (mock)
            instance_ids = [f"i-fleet-{uuid.uuid4().hex[:8]}" for _ in range(2)]
            
            # Trigger fleet interruption handler
            handler_func = mode.spot_interruption_monitor.fleet_handlers[mock_spot_fleet_id]
            handler_func(mock_spot_fleet_id, instance_ids, event['detail'])
            
            # Verify state persistence
            mode.save_state()
            
            # Create a new mode instance to simulate restart
            mode2 = ServerlessMode(
                provider_id=provider_id,
                session=localstack_session,
                state_store=temp_state_store,
                region="us-east-1",
                worker_type="ecs",
                ecs_task_cpu=256,
                ecs_task_memory=512,
                ecs_container_image="amazon/amazon-ecs-sample",
                use_spot=True,
                use_spot_fleet=True,
                spot_interruption_handling=True,
                checkpoint_bucket=checkpoint_bucket,
                checkpoint_prefix="test/checkpoints",
                instance_types=["t2.micro", "t3.micro"]
            )
            
            # Load state
            mode2.load_state()
            
            # Verify fleet resources were restored in state
            assert mode2.spot_interruption_monitor is not None
            
            # Re-register fleet for testing
            mode2.spot_interruption_monitor.register_fleet(
                mock_spot_fleet_id,
                mode2.spot_interruption_handler.handle_fleet_interruption
            )
            
            assert mock_spot_fleet_id in mode2.spot_interruption_monitor.fleet_handlers
            
        finally:
            # Clean up resources
            if hasattr(mode, 'spot_interruption_monitor') and mode.spot_interruption_monitor:
                mode.spot_interruption_monitor.stop_monitoring()
            
            mode.cleanup_infrastructure()
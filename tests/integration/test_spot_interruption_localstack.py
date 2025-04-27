"""Integration tests for spot interruption handling with LocalStack.

This module focuses on testing spot interruption handling with LocalStack's 
mocked AWS services, simulating actual AWS API calls and responses.

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

from parsl_ephemeral_aws.compute.spot_interruption import (
    SpotInterruptionMonitor,
    SpotInterruptionHandler,
    ParslSpotInterruptionHandler,
    checkpointable
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
def checkpoint_bucket(localstack_session):
    """Create a temporary S3 bucket for checkpoints."""
    bucket_name = f"test-checkpoint-bucket-{uuid.uuid4().hex[:8]}"
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
def cloudwatch_event_client(localstack_session):
    """Create a CloudWatch Events client."""
    return localstack_session.client('events')


@pytest.fixture
def setup_cloudwatch_events(cloudwatch_event_client):
    """Set up CloudWatch Events for spot interruption testing."""
    # Create rule for spot instance interruption warnings
    rule_name = f"spot-interruption-rule-{uuid.uuid4().hex[:8]}"
    cloudwatch_event_client.put_rule(
        Name=rule_name,
        EventPattern=json.dumps({
            "source": ["aws.ec2"],
            "detail-type": ["EC2 Spot Instance Interruption Warning"]
        })
    )
    
    yield rule_name
    
    # Cleanup rule
    try:
        cloudwatch_event_client.delete_rule(Name=rule_name)
    except Exception as e:
        print(f"Error cleaning up CloudWatch Events rule {rule_name}: {e}")


@pytest.mark.integration
class TestSpotInterruptionLocalstack:
    """Integration tests for spot interruption using LocalStack."""
    
    @pytest.mark.localstack
    def test_s3_checkpoint_persistence(self, localstack_session, checkpoint_bucket):
        """Test that checkpoints can be saved and loaded from S3."""
        task_id = f"test-task-{uuid.uuid4().hex[:8]}"
        checkpoint_data = {
            "task_id": task_id,
            "state": "running",
            "iteration": 42,
            "results": [1, 2, 3, 4],
            "timestamp": time.time()
        }
        
        # Create handler
        handler = SpotInterruptionHandler(
            session=localstack_session,
            checkpoint_bucket=checkpoint_bucket,
            checkpoint_prefix="test/checkpoints"
        )
        
        # Save checkpoint
        uri = handler.save_checkpoint(task_id, checkpoint_data)
        assert uri == f"s3://{checkpoint_bucket}/test/checkpoints/{task_id}.json"
        
        # Verify object exists in S3
        s3_client = localstack_session.client('s3')
        response = s3_client.list_objects_v2(
            Bucket=checkpoint_bucket,
            Prefix=f"test/checkpoints/{task_id}"
        )
        
        assert 'Contents' in response
        assert len(response['Contents']) == 1
        assert response['Contents'][0]['Key'] == f"test/checkpoints/{task_id}.json"
        
        # Load checkpoint directly from S3
        obj = s3_client.get_object(
            Bucket=checkpoint_bucket,
            Key=f"test/checkpoints/{task_id}.json"
        )
        
        loaded_data = json.loads(obj['Body'].read().decode('utf-8'))
        assert loaded_data == checkpoint_data
        
        # Load checkpoint using handler
        handler_loaded_data = handler.load_checkpoint(task_id)
        assert handler_loaded_data == checkpoint_data
    
    @pytest.mark.localstack
    def test_checkpointable_decorator_with_s3(self, localstack_session, checkpoint_bucket):
        """Test that the checkpointable decorator works with real S3."""
        s3_client = localstack_session.client('s3')
        
        # Define a checkpointable function
        @checkpointable(checkpoint_bucket=checkpoint_bucket, checkpoint_prefix="test/checkpoints")
        def test_function(iterations=5, checkpoint_data=None):
            # Initialize state
            if checkpoint_data:
                state = checkpoint_data
                print(f"Resuming from iteration {state['iteration']}")
            else:
                state = {
                    'iteration': 0,
                    'result': 0
                }
                print("Starting new computation")
            
            # Process iterations with checkpointing
            for i in range(state['iteration'], iterations):
                state['result'] += i
                state['iteration'] = i + 1
                print(f"Iteration {i+1}/{iterations}, result: {state['result']}")
                
                # Yield checkpoint at each iteration
                yield state
            
            return state['result']
        
        # Run function to completion
        result = test_function(iterations=5)
        assert result == 10  # 0+1+2+3+4=10
        
        # Verify checkpoints were created in S3
        response = s3_client.list_objects_v2(
            Bucket=checkpoint_bucket,
            Prefix="test/checkpoints"
        )
        
        # Should have at least one checkpoint
        assert 'Contents' in response
        
        # Get the latest checkpoint
        checkpoints = sorted(response['Contents'], key=lambda x: x['LastModified'], reverse=True)
        latest_checkpoint_key = checkpoints[0]['Key']
        
        # Load the checkpoint
        obj = s3_client.get_object(
            Bucket=checkpoint_bucket,
            Key=latest_checkpoint_key
        )
        
        checkpoint_data = json.loads(obj['Body'].read().decode('utf-8'))
        assert checkpoint_data['iteration'] == 5
        assert checkpoint_data['result'] == 10
        
        # Now simulate interruption by running with the checkpoint
        # We'll start from the 3rd iteration (iteration=2)
        interrupted_checkpoint = {
            'iteration': 2,
            'result': 3  # 0+1+2=3
        }
        
        # Save this checkpoint manually
        interrupted_task_id = latest_checkpoint_key.split('/')[-1].split('.')[0]
        s3_client.put_object(
            Bucket=checkpoint_bucket,
            Key=f"test/checkpoints/{interrupted_task_id}.json",
            Body=json.dumps(interrupted_checkpoint)
        )
        
        # Run the function again - it should load the checkpoint
        with patch('parsl_ephemeral_aws.compute.spot_interruption.boto3.Session') as mock_session:
            # Return the real localstack session
            mock_session.return_value = localstack_session
            
            # Run with the latest task_id which has our interrupted checkpoint
            result = test_function(task_id=interrupted_task_id)
            
            # Should complete from iteration 2 onwards: 3+4=7, plus existing 3 = 10
            assert result == 10
    
    @pytest.mark.localstack
    def test_spot_interruption_event_detection(self, localstack_session, checkpoint_bucket, setup_cloudwatch_events):
        """Test that spot interruption events can be detected and processed."""
        # Create a spot interruption monitor
        monitor = SpotInterruptionMonitor(session=localstack_session)
        
        # Create a handler for tests
        handler = SpotInterruptionHandler(
            session=localstack_session,
            checkpoint_bucket=checkpoint_bucket,
            checkpoint_prefix="test/interruptions"
        )
        
        # Create a mock handler function to track interruption events
        interruption_events = []
        
        def mock_interruption_handler(instance_id, event):
            interruption_events.append({
                'instance_id': instance_id,
                'event': event
            })
        
        # Register an instance
        instance_id = f"i-spot-{uuid.uuid4().hex[:8]}"
        monitor.register_instance(instance_id, mock_interruption_handler)
        
        # Start monitoring
        monitor.start_monitoring()
        
        # Simulate an interruption notice event
        cloudwatch_event_client = localstack_session.client('events')
        
        event_detail = {
            'instance-id': instance_id,
            'instance-action': 'terminate',
            'time': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        }
        
        # Put the event - in LocalStack this should be detected by our monitor
        # Note: LocalStack may not fully simulate the event delivery to our monitor
        # This is a limitation of the testing environment
        try:
            cloudwatch_event_client.put_events(
                Entries=[
                    {
                        'Source': 'aws.ec2',
                        'DetailType': 'EC2 Spot Instance Interruption Warning',
                        'Detail': json.dumps(event_detail),
                        'Resources': [f"arn:aws:ec2:us-east-1:123456789012:instance/{instance_id}"]
                    }
                ]
            )
            
            # In a real environment, we would wait for the event to be processed
            # In LocalStack, we'll mock this by directly calling the handler
            monitor._handle_instance_interruption(instance_id, event_detail)
            
            # Verify the handler was called
            assert len(interruption_events) == 1
            assert interruption_events[0]['instance_id'] == instance_id
            assert interruption_events[0]['event'] == event_detail
            
        finally:
            # Stop monitoring
            monitor.stop_monitoring()
    
    @pytest.mark.localstack
    def test_spot_fleet_interruption(self, localstack_session, checkpoint_bucket):
        """Test handling of spot fleet interruptions."""
        # Create a spot interruption monitor
        monitor = SpotInterruptionMonitor(session=localstack_session)
        
        # Create a handler for tests
        handler = SpotInterruptionHandler(
            session=localstack_session,
            checkpoint_bucket=checkpoint_bucket,
            checkpoint_prefix="test/fleet-interruptions"
        )
        
        # Create a mock handler function to track fleet interruption events
        fleet_interruption_events = []
        
        def mock_fleet_handler(fleet_id, instance_ids, event):
            fleet_interruption_events.append({
                'fleet_id': fleet_id,
                'instance_ids': instance_ids,
                'event': event
            })
        
        # Register a fleet
        fleet_id = f"sfr-{uuid.uuid4().hex[:8]}"
        instance_ids = [f"i-fleet-{uuid.uuid4().hex[:8]}" for _ in range(3)]
        
        monitor.register_fleet(fleet_id, mock_fleet_handler)
        
        # Start monitoring
        monitor.start_monitoring()
        
        # Simulate a fleet interruption
        event_detail = {
            'spot-fleet-request-id': fleet_id,
            'instance-action': 'terminate',
            'time': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        }
        
        try:
            # In LocalStack, we'll mock this by directly calling the handler
            monitor._handle_fleet_interruption(fleet_id, instance_ids, event_detail)
            
            # Verify the handler was called
            assert len(fleet_interruption_events) == 1
            assert fleet_interruption_events[0]['fleet_id'] == fleet_id
            assert fleet_interruption_events[0]['instance_ids'] == instance_ids
            assert fleet_interruption_events[0]['event'] == event_detail
            
        finally:
            # Stop monitoring
            monitor.stop_monitoring()
    
    @pytest.mark.localstack
    def test_parsl_handler_recovery_queue(self, localstack_session, checkpoint_bucket):
        """Test the recovery queue in ParslSpotInterruptionHandler."""
        # Create handler
        handler = ParslSpotInterruptionHandler(
            session=localstack_session,
            checkpoint_bucket=checkpoint_bucket,
            checkpoint_prefix="test/recovery"
        )
        
        # Queue tasks for recovery with different priorities
        task_ids = [f"task-{uuid.uuid4().hex[:8]}" for _ in range(5)]
        priorities = [3, 1, 5, 2, 4]  # Mix of priorities, lower number = higher priority
        
        for i, task_id in enumerate(task_ids):
            # Create a checkpoint
            checkpoint_data = {
                'task_id': task_id,
                'priority': priorities[i],
                'state': 'interrupted',
                'progress': i * 20  # 0, 20, 40, 60, 80
            }
            
            # Save checkpoint
            uri = handler.save_checkpoint(task_id, checkpoint_data)
            
            # Queue for recovery
            handler.queue_task_for_recovery(task_id, uri, priorities[i])
        
        # Get tasks in priority order
        recovered_tasks = []
        while not handler.recovery_queue.empty():
            task = handler.get_next_recovery_task()
            recovered_tasks.append(task)
        
        # Verify tasks were recovered in priority order
        expected_order = sorted(range(5), key=lambda i: priorities[i])
        for i, task_idx in enumerate(expected_order):
            assert recovered_tasks[i]['task_id'] == task_ids[task_idx]
            assert recovered_tasks[i]['priority'] == priorities[task_idx]
    
    @pytest.mark.localstack
    def test_checkpoint_interval_timing(self, localstack_session, checkpoint_bucket):
        """Test that checkpoints are created at the specified interval."""
        # Set a short checkpoint interval for testing
        checkpoint_interval = 0.5  # seconds
        
        # Define a checkpointable function with forced timing
        @checkpointable(
            checkpoint_bucket=checkpoint_bucket, 
            checkpoint_prefix="test/intervals",
            checkpoint_interval=checkpoint_interval
        )
        def timed_function(iterations=10, checkpoint_data=None):
            # Initialize state
            if checkpoint_data:
                state = checkpoint_data
            else:
                state = {
                    'iteration': 0,
                    'result': 0,
                    'checkpoints': []
                }
            
            # Process iterations with checkpointing
            for i in range(state['iteration'], iterations):
                # Do some work
                state['result'] += i
                state['iteration'] = i + 1
                
                # Record checkpoint times
                if 'last_checkpoint_time' in state:
                    time_since_last = time.time() - state['last_checkpoint_time']
                    state['checkpoints'].append(time_since_last)
                
                state['last_checkpoint_time'] = time.time()
                
                # Yield state for checkpointing
                yield state
                
                # Sleep to control timing
                time.sleep(0.1)  # Short sleep between iterations
            
            return state
        
        # Run the function
        result = timed_function(iterations=8)
        
        # Verify checkpoints were created at the correct interval
        # The first checkpoint happens immediately, so we start checking from the second
        intervals = result['checkpoints'][1:]
        assert len(intervals) > 0
        
        # Allow some timing flexibility (between 0.4x and 2x the interval)
        # This is to account for test environment variability
        for interval in intervals:
            assert interval >= checkpoint_interval * 0.4, f"Interval {interval} is too short"
            # Upper bound is more flexible as scheduling can delay things
            assert interval <= checkpoint_interval * 2.0, f"Interval {interval} is too long"
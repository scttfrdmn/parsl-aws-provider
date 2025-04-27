"""Unit tests for the spot interruption handler.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import pytest
import boto3
import json
import time
import threading
import queue
from unittest.mock import MagicMock, patch

from parsl_ephemeral_aws.compute.spot_interruption import (
    SpotInterruptionMonitor,
    SpotInterruptionHandler,
    ParslSpotInterruptionHandler,
    checkpointable,
)
from parsl_ephemeral_aws.exceptions import (
    SpotInterruptionError,
    CheckpointError,
    CheckpointNotFoundError,
    TaskRecoveryError,
)


class TestSpotInterruptionMonitor:
    """Tests for the SpotInterruptionMonitor class."""
    
    @pytest.fixture
    def mock_session(self):
        """Create a mock boto3 session."""
        session = MagicMock(spec=boto3.Session)
        return session
    
    @pytest.fixture
    def mock_ec2_client(self):
        """Create a mock EC2 client."""
        client = MagicMock()
        
        # Mock describe_instances response
        client.describe_instances.return_value = {
            'Reservations': [
                {
                    'Instances': [
                        {
                            'InstanceId': 'i-test1',
                            'State': {'Name': 'running'}
                        },
                        {
                            'InstanceId': 'i-test2',
                            'State': {'Name': 'marked-for-termination'}
                        }
                    ]
                }
            ]
        }
        
        # Mock describe_spot_fleet_instances response
        client.describe_spot_fleet_instances.return_value = {
            'ActiveInstances': [
                {'InstanceId': 'i-test1', 'SpotInstanceRequestId': 'sir-test1'},
                {'InstanceId': 'i-test2', 'SpotInstanceRequestId': 'sir-test2'}
            ]
        }
        
        return client
    
    @pytest.fixture
    def mock_cloudwatch_client(self):
        """Create a mock CloudWatch client."""
        client = MagicMock()
        return client
    
    @pytest.fixture
    def monitor(self, mock_session):
        """Create a SpotInterruptionMonitor instance."""
        return SpotInterruptionMonitor(
            session=mock_session,
            check_interval=1,  # Short interval for testing
            lead_time=10
        )
    
    def test_init(self, monitor, mock_session):
        """Test initialization of SpotInterruptionMonitor."""
        assert monitor.session == mock_session
        assert monitor.check_interval == 1
        assert monitor.lead_time == 10
        assert monitor.instance_handlers == {}
        assert monitor.fleet_handlers == {}
        assert monitor.monitoring_thread is None
        assert isinstance(monitor.stop_event, threading.Event)
        assert isinstance(monitor.event_queue, queue.Queue)
    
    def test_register_instance(self, monitor):
        """Test registering a spot instance for monitoring."""
        handler = MagicMock()
        
        monitor.register_instance('i-test1', handler)
        
        assert 'i-test1' in monitor.instance_handlers
        assert monitor.instance_handlers['i-test1'] == handler
    
    def test_register_fleet(self, monitor):
        """Test registering a spot fleet for monitoring."""
        handler = MagicMock()
        
        monitor.register_fleet('sfr-test1', handler)
        
        assert 'sfr-test1' in monitor.fleet_handlers
        assert monitor.fleet_handlers['sfr-test1'] == handler
    
    def test_deregister_instance(self, monitor):
        """Test deregistering a spot instance."""
        handler = MagicMock()
        
        monitor.register_instance('i-test1', handler)
        assert 'i-test1' in monitor.instance_handlers
        
        monitor.deregister_instance('i-test1')
        assert 'i-test1' not in monitor.instance_handlers
    
    def test_deregister_fleet(self, monitor):
        """Test deregistering a spot fleet."""
        handler = MagicMock()
        
        monitor.register_fleet('sfr-test1', handler)
        assert 'sfr-test1' in monitor.fleet_handlers
        
        monitor.deregister_fleet('sfr-test1')
        assert 'sfr-test1' not in monitor.fleet_handlers
    
    @patch('threading.Thread')
    def test_start_monitoring(self, mock_thread, monitor):
        """Test starting the monitoring thread."""
        monitor.start_monitoring()
        
        mock_thread.assert_called_once()
        assert mock_thread.return_value.start.call_count == 1
        assert monitor.monitoring_thread is not None
    
    @patch('threading.Thread')
    def test_stop_monitoring(self, mock_thread, monitor):
        """Test stopping the monitoring thread."""
        # Setup a mock thread
        mock_thread_instance = MagicMock()
        mock_thread.return_value = mock_thread_instance
        
        # Start monitoring
        monitor.start_monitoring()
        
        # Stop monitoring
        monitor.stop_monitoring()
        
        assert monitor.stop_event.is_set()
        assert mock_thread_instance.join.call_count == 1
    
    @patch('threading.Thread')
    def test_check_instance_interruptions(self, mock_thread, monitor, mock_ec2_client, mock_cloudwatch_client):
        """Test checking for instance interruptions."""
        # Register an instance handler
        handler = MagicMock()
        monitor.register_instance('i-test2', handler)
        
        # Call the method directly
        monitor._check_instance_interruptions(mock_ec2_client, mock_cloudwatch_client)
        
        # Check if an event was queued for the interrupted instance
        assert not monitor.event_queue.empty()
        event_type, instance_id, event_details = monitor.event_queue.get()
        
        assert event_type == 'instance'
        assert instance_id == 'i-test2'
        assert event_details['InstanceId'] == 'i-test2'
        assert event_details['InstanceAction'] == 'terminate'
    
    @patch('threading.Thread')
    def test_check_fleet_interruptions(self, mock_thread, monitor, mock_ec2_client):
        """Test checking for fleet interruptions."""
        # Register a fleet handler
        handler = MagicMock()
        monitor.register_fleet('sfr-test1', handler)
        
        # Call the method directly
        monitor._check_fleet_interruptions(mock_ec2_client)
        
        # Verify that describe_spot_fleet_instances was called
        mock_ec2_client.describe_spot_fleet_instances.assert_called_with(
            SpotFleetRequestId='sfr-test1'
        )
        
        # Verify that describe_instances was called with the fleet instances
        mock_ec2_client.describe_instances.assert_called()
    
    @patch('threading.Thread')
    def test_process_interruption_events(self, mock_thread, monitor):
        """Test processing interruption events."""
        # Setup handlers
        instance_handler = MagicMock()
        fleet_handler = MagicMock()
        
        monitor.register_instance('i-test1', instance_handler)
        monitor.register_fleet('sfr-test1', fleet_handler)
        
        # Add events to the queue
        instance_event = ('instance', 'i-test1', {'InstanceId': 'i-test1'})
        fleet_event = ('fleet', 'sfr-test1', ['i-test1', 'i-test2'], {'FleetRequestId': 'sfr-test1'})
        
        monitor.event_queue.put(instance_event)
        monitor.event_queue.put(fleet_event)
        
        # Process events
        monitor._process_interruption_events()
        
        # Verify handlers were called
        instance_handler.assert_called_once_with('i-test1', {'InstanceId': 'i-test1'})
        fleet_handler.assert_called_once_with('sfr-test1', ['i-test1', 'i-test2'], {'FleetRequestId': 'sfr-test1'})


class TestSpotInterruptionHandler:
    """Tests for the SpotInterruptionHandler class."""
    
    @pytest.fixture
    def mock_session(self):
        """Create a mock boto3 session."""
        session = MagicMock(spec=boto3.Session)
        return session
    
    @pytest.fixture
    def mock_s3_client(self):
        """Create a mock S3 client."""
        client = MagicMock()
        
        # Mock head_bucket
        client.head_bucket.return_value = {}
        
        # Mock get_object
        client.get_object.return_value = {
            'Body': MagicMock(read=lambda: json.dumps({'test': 'data'}).encode())
        }
        
        return client
    
    @pytest.fixture
    def handler(self, mock_session, mock_s3_client):
        """Create a SpotInterruptionHandler instance."""
        mock_session.client.return_value = mock_s3_client
        
        return SpotInterruptionHandler(
            session=mock_session,
            checkpoint_bucket='test-bucket',
            checkpoint_prefix='test/prefix'
        )
    
    def test_init(self, handler, mock_session):
        """Test initialization of SpotInterruptionHandler."""
        assert handler.session == mock_session
        assert handler.checkpoint_bucket == 'test-bucket'
        assert handler.checkpoint_prefix == 'test/prefix'
        assert isinstance(handler.recovery_queue, queue.PriorityQueue)
        
        # Verify bucket existence was checked
        mock_session.client.return_value.head_bucket.assert_called_with(
            Bucket='test-bucket'
        )
    
    def test_init_without_bucket(self, mock_session):
        """Test initialization without a checkpoint bucket."""
        handler = SpotInterruptionHandler(
            session=mock_session,
            checkpoint_bucket=None
        )
        
        assert handler.checkpoint_bucket is None
        assert mock_session.client.return_value.head_bucket.call_count == 0
    
    def test_save_checkpoint(self, handler, mock_s3_client):
        """Test saving a checkpoint."""
        task_id = 'task-123'
        data = {'status': 'in_progress', 'iteration': 42}
        
        uri = handler.save_checkpoint(task_id, data)
        
        assert uri == f"s3://{handler.checkpoint_bucket}/{handler.checkpoint_prefix}/{task_id}.json"
        
        # Verify S3 put_object was called correctly
        mock_s3_client.put_object.assert_called_with(
            Bucket='test-bucket',
            Key=f"{handler.checkpoint_prefix}/{task_id}.json",
            Body=json.dumps(data),
            Metadata={
                'Priority': '1',
                'Timestamp': mock_s3_client.put_object.call_args[1]['Metadata']['Timestamp']
            }
        )
    
    def test_save_checkpoint_no_bucket(self, mock_session):
        """Test saving a checkpoint with no bucket configured."""
        handler = SpotInterruptionHandler(session=mock_session)
        
        with pytest.raises(SpotInterruptionError):
            handler.save_checkpoint('task-123', {})
    
    def test_load_checkpoint(self, handler, mock_s3_client):
        """Test loading a checkpoint."""
        task_id = 'task-123'
        
        data = handler.load_checkpoint(task_id)
        
        assert data == {'test': 'data'}
        
        # Verify S3 get_object was called correctly
        mock_s3_client.get_object.assert_called_with(
            Bucket='test-bucket',
            Key=f"{handler.checkpoint_prefix}/{task_id}.json"
        )
    
    def test_load_checkpoint_not_found(self, handler, mock_s3_client):
        """Test loading a checkpoint that doesn't exist."""
        from botocore.exceptions import ClientError
        
        # Mock get_object to raise NoSuchKey error
        mock_s3_client.get_object.side_effect = ClientError(
            {'Error': {'Code': 'NoSuchKey', 'Message': 'Not found'}},
            'GetObject'
        )
        
        data = handler.load_checkpoint('missing-task')
        
        assert data is None
    
    def test_queue_task_for_recovery(self, handler):
        """Test queuing a task for recovery."""
        task_id = 'task-123'
        checkpoint_uri = 's3://test-bucket/test/prefix/task-123.json'
        priority = 2
        
        handler.queue_task_for_recovery(task_id, checkpoint_uri, priority)
        
        # Verify the task was added to the queue
        queued_item = handler.recovery_queue.get()
        assert queued_item[0] == priority  # Priority
        assert queued_item[2] == task_id  # Task ID
        assert queued_item[3] == checkpoint_uri  # Checkpoint URI
    
    def test_get_next_recovery_task(self, handler):
        """Test getting the next recovery task."""
        # Add tasks to the queue
        handler.queue_task_for_recovery('task-1', 'uri-1', 2)
        handler.queue_task_for_recovery('task-2', 'uri-2', 1)  # Higher priority
        
        # Get the next task (should be task-2)
        task = handler.get_next_recovery_task()
        
        assert task['task_id'] == 'task-2'
        assert task['checkpoint_uri'] == 'uri-2'
        assert task['priority'] == 1
        
        # Get the next task (should be task-1)
        task = handler.get_next_recovery_task()
        
        assert task['task_id'] == 'task-1'
        assert task['checkpoint_uri'] == 'uri-1'
        assert task['priority'] == 2
        
        # No more tasks
        task = handler.get_next_recovery_task()
        assert task is None
    
    def test_handle_instance_interruption(self, handler):
        """Test the base implementation of handle_instance_interruption."""
        # This is just a placeholder in the base class, so no real logic to test
        instance_id = 'i-test1'
        event = {'InstanceId': 'i-test1', 'InstanceAction': 'terminate'}
        
        # Should not raise an exception
        handler.handle_instance_interruption(instance_id, event)
    
    def test_handle_fleet_interruption(self, handler):
        """Test the base implementation of handle_fleet_interruption."""
        # This is just a placeholder in the base class, so no real logic to test
        fleet_id = 'sfr-test1'
        instance_ids = ['i-test1', 'i-test2']
        event = {'FleetRequestId': fleet_id, 'InstanceAction': 'terminate'}
        
        # Should not raise an exception
        handler.handle_fleet_interruption(fleet_id, instance_ids, event)


class TestParslSpotInterruptionHandler:
    """Tests for the ParslSpotInterruptionHandler class."""
    
    @pytest.fixture
    def mock_session(self):
        """Create a mock boto3 session."""
        session = MagicMock(spec=boto3.Session)
        return session
    
    @pytest.fixture
    def mock_s3_client(self):
        """Create a mock S3 client."""
        client = MagicMock()
        
        # Mock head_bucket
        client.head_bucket.return_value = {}
        
        # Mock get_object
        client.get_object.return_value = {
            'Body': MagicMock(read=lambda: json.dumps({'test': 'data'}).encode())
        }
        
        return client
    
    @pytest.fixture
    def mock_executor(self):
        """Create a mock Parsl executor."""
        executor = MagicMock()
        return executor
    
    @pytest.fixture
    def handler(self, mock_session, mock_s3_client, mock_executor):
        """Create a ParslSpotInterruptionHandler instance."""
        mock_session.client.return_value = mock_s3_client
        
        return ParslSpotInterruptionHandler(
            session=mock_session,
            checkpoint_bucket='test-bucket',
            checkpoint_prefix='test/prefix',
            executor=mock_executor,
            executor_label='test_executor'
        )
    
    def test_init(self, handler, mock_session, mock_executor):
        """Test initialization of ParslSpotInterruptionHandler."""
        assert handler.session == mock_session
        assert handler.checkpoint_bucket == 'test-bucket'
        assert handler.checkpoint_prefix == 'test/prefix'
        assert handler.executor == mock_executor
        assert handler.executor_label == 'test_executor'
        assert handler.task_mapping == {}
    
    def test_register_task(self, handler):
        """Test registering a task."""
        handler.register_task('task-1', 'i-test1')
        handler.register_task('task-2', 'i-test1')
        handler.register_task('task-3', 'i-test2')
        
        assert 'i-test1' in handler.task_mapping
        assert 'i-test2' in handler.task_mapping
        assert handler.task_mapping['i-test1'] == ['task-1', 'task-2']
        assert handler.task_mapping['i-test2'] == ['task-3']
    
    def test_handle_instance_interruption(self, handler, mock_s3_client):
        """Test handling an instance interruption."""
        # Register tasks
        handler.register_task('task-1', 'i-test1')
        handler.register_task('task-2', 'i-test1')
        
        # Mock successful checkpoint loading
        mock_s3_client.get_object.return_value = {
            'Body': MagicMock(read=lambda: json.dumps({'checkpoint': 'data'}).encode())
        }
        
        # Handle interruption
        instance_id = 'i-test1'
        event = {'InstanceId': instance_id, 'InstanceAction': 'terminate'}
        
        handler.handle_instance_interruption(instance_id, event)
        
        # Verify checkpoints were loaded
        assert mock_s3_client.get_object.call_count == 2
        
        # Verify tasks were queued for recovery
        assert not handler.recovery_queue.empty()
        priority1, time1, task_id1, uri1 = handler.recovery_queue.get()
        priority2, time2, task_id2, uri2 = handler.recovery_queue.get()
        
        assert task_id1 in ['task-1', 'task-2']
        assert task_id2 in ['task-1', 'task-2']
        assert task_id1 != task_id2
        
        # Verify task mapping was cleaned up
        assert 'i-test1' not in handler.task_mapping
    
    def test_handle_fleet_interruption(self, handler):
        """Test handling a fleet interruption."""
        # Setup spy on handle_instance_interruption
        handler.handle_instance_interruption = MagicMock()
        
        # Handle fleet interruption
        fleet_id = 'sfr-test1'
        instance_ids = ['i-test1', 'i-test2']
        event = {'FleetRequestId': fleet_id, 'InstanceAction': 'terminate'}
        
        handler.handle_fleet_interruption(fleet_id, instance_ids, event)
        
        # Verify handle_instance_interruption was called for each instance
        assert handler.handle_instance_interruption.call_count == 2
        handler.handle_instance_interruption.assert_any_call('i-test1', event)
        handler.handle_instance_interruption.assert_any_call('i-test2', event)
    
    def test_recover_tasks(self, handler, mock_s3_client, mock_executor):
        """Test recovering tasks."""
        # Queue tasks for recovery
        handler.queue_task_for_recovery('task-1', 's3://test-bucket/test/prefix/task-1.json')
        handler.queue_task_for_recovery('task-2', 's3://test-bucket/test/prefix/task-2.json', priority=2)
        
        # Process recovery queue
        handler.recover_tasks()
        
        # Executor would be used in a real implementation, but we just verify
        # that the method doesn't fail for now. In a real implementation we
        # would check if new futures were created.
        assert handler.recovery_queue.empty()


class TestCheckpointableDecorator:
    """Tests for the checkpointable decorator."""
    
    def test_checkpointable_function(self):
        """Test that a function can be decorated with checkpointable."""
        @checkpointable(checkpoint_bucket='test-bucket')
        def test_func(x, y, checkpoint_data=None):
            if checkpoint_data:
                state = checkpoint_data
            else:
                state = {'iteration': 0, 'result': 0}
            
            for i in range(state['iteration'], 5):
                state['result'] += x * y
                state['iteration'] = i + 1
                yield state
            
            return state['result']
        
        # Function should still be callable
        result = test_func(2, 3)
        
        # Should have computed 2*3*5 = 30
        assert result == 30
    
    def test_checkpointable_with_initial_checkpoint(self):
        """Test resuming from a checkpoint."""
        @checkpointable()
        def test_func(checkpoint_data=None):
            if checkpoint_data:
                state = checkpoint_data
            else:
                state = {'iteration': 0, 'result': 0}
            
            for i in range(state['iteration'], 5):
                state['result'] += i
                state['iteration'] = i + 1
                yield state
            
            return state['result']
        
        # Start from a checkpoint at iteration 3
        checkpoint_data = {'iteration': 3, 'result': 3}  # 0+1+2=3
        result = test_func(checkpoint_data=checkpoint_data)
        
        # Should have computed 3+3+4 = 10
        assert result == 10
    
    def test_checkpointable_non_generator(self):
        """Test using checkpointable on a non-generator function."""
        @checkpointable()
        def test_func(x, checkpoint_data=None):
            if checkpoint_data:
                return checkpoint_data['result'] + x
            return x
        
        # Regular function call
        assert test_func(5) == 5
        
        # With checkpoint
        assert test_func(5, checkpoint_data={'result': 10}) == 15
    
    def test_checkpointable_exception_handling(self):
        """Test exception handling in checkpointable function."""
        @checkpointable()
        def test_func(checkpoint_data=None):
            if checkpoint_data:
                iteration = checkpoint_data['iteration']
            else:
                iteration = 0
            
            for i in range(iteration, 5):
                if i == 3:
                    raise ValueError("Test exception")
                yield {'iteration': i + 1}
            
            return "Done"
        
        # Function should raise the exception
        with pytest.raises(ValueError):
            test_func()
        
        # With checkpoint before the exception
        result = test_func(checkpoint_data={'iteration': 4})
        assert result == "Done"
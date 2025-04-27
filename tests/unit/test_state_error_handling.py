"""Unit tests for error handling in state persistence mechanisms.

These tests verify that state persistence classes properly handle error conditions
and edge cases, providing appropriate error messages and recovery mechanisms.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import os
import json
import tempfile
import pytest
from unittest.mock import MagicMock, patch, mock_open
import boto3
from botocore.exceptions import ClientError

from parsl_ephemeral_aws.state.file import FileStateStore
from parsl_ephemeral_aws.state.parameter_store import ParameterStoreState 
from parsl_ephemeral_aws.state.s3 import S3State
from parsl_ephemeral_aws.exceptions import StateError, StateSerializationError, StateDeserializationError


class TestFileStateStoreErrorHandling:
    """Tests for error handling in FileStateStore."""
    
    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for state files."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            yield tmp_dir
    
    @pytest.fixture
    def file_state_store(self, temp_dir):
        """Create a FileStateStore instance."""
        return FileStateStore(os.path.join(temp_dir, "state.json"), "test-provider")
    
    @pytest.fixture
    def test_state(self):
        """Create a test state dictionary."""
        return {
            "resources": {"res1": {"instance_id": "i-12345"}},
            "jobs": {"job1": {"status": "running"}}
        }
    
    def test_save_state_permission_error(self, file_state_store, test_state):
        """Test handling permission error when saving state."""
        with patch("builtins.open", side_effect=PermissionError("Permission denied")):
            with pytest.raises(StateError) as exc_info:
                file_state_store.save_state(test_state)
            
            assert "Permission denied" in str(exc_info.value)
    
    def test_save_state_directory_not_exists(self, temp_dir):
        """Test handling directory not exists error when saving state."""
        # Create a path with non-existent directories
        nested_path = os.path.join(temp_dir, "non_existent", "deeply", "nested", "state.json")
        file_store = FileStateStore(nested_path, "test-provider")
        
        # Should create directories and save successfully
        test_state = {"key": "value"}
        file_store.save_state(test_state)
        
        # Verify file was created
        assert os.path.exists(nested_path)
        
        # Verify content
        with open(nested_path, 'r') as f:
            saved_state = json.load(f)
            assert saved_state == test_state
    
    def test_load_state_file_not_found(self, file_state_store):
        """Test handling file not found when loading state."""
        # File doesn't exist yet
        state = file_state_store.load_state()
        
        # Should return None, not raise an exception
        assert state is None
    
    def test_load_state_invalid_json(self, file_state_store):
        """Test handling invalid JSON when loading state."""
        # Write invalid JSON to the file
        with open(file_state_store.file_path, 'w') as f:
            f.write("This is not valid JSON")
        
        # Should raise StateDeserializationError
        with pytest.raises(StateDeserializationError) as exc_info:
            file_state_store.load_state()
        
        assert "Failed to deserialize state" in str(exc_info.value)
    
    def test_delete_state_file_not_found(self, file_state_store):
        """Test handling file not found when deleting state."""
        # File doesn't exist yet
        file_state_store.delete_state()
        
        # Should not raise an exception
        assert not os.path.exists(file_state_store.file_path)
    
    def test_delete_state_permission_error(self, file_state_store, test_state):
        """Test handling permission error when deleting state."""
        # Save state first
        file_state_store.save_state(test_state)
        
        # Mock permission error on unlink
        with patch("os.remove", side_effect=PermissionError("Permission denied")):
            with pytest.raises(StateError) as exc_info:
                file_state_store.delete_state()
            
            assert "Permission denied" in str(exc_info.value)
    
    def test_concurrent_write_scenario(self, temp_dir):
        """Test a scenario mimicking concurrent writes."""
        file_path = os.path.join(temp_dir, "shared_state.json")
        
        # Create two separate state store instances for the same file
        store1 = FileStateStore(file_path, "provider1")
        store2 = FileStateStore(file_path, "provider2")
        
        # First store saves state
        state1 = {"owner": "provider1", "data": [1, 2, 3]}
        store1.save_state(state1)
        
        # Second store overwrites with its state
        state2 = {"owner": "provider2", "data": [4, 5, 6]}
        store2.save_state(state2)
        
        # Verify final state is from store2
        loaded_state = store1.load_state()
        assert loaded_state["owner"] == "provider2"
        assert loaded_state["data"] == [4, 5, 6]


@patch('boto3.Session')
class TestParameterStoreStateErrorHandling:
    """Tests for error handling in ParameterStoreState."""
    
    @pytest.fixture
    def provider_mock(self):
        """Create a mock provider."""
        provider = MagicMock()
        provider.workflow_id = "test-workflow"
        provider.region = "us-east-1"
        provider.aws_access_key_id = None
        provider.aws_secret_access_key = None
        provider.aws_session_token = None
        provider.aws_profile = None
        return provider
    
    @pytest.fixture
    def ssm_client_mock(self):
        """Create a mock SSM client."""
        client = MagicMock()
        return client
    
    @pytest.fixture
    def parameter_store_state(self, provider_mock, ssm_client_mock, boto3_session_mock):
        """Create a ParameterStoreState instance with mocked AWS clients."""
        session_instance = boto3_session_mock.return_value
        session_instance.client.return_value = ssm_client_mock
        
        return ParameterStoreState(provider_mock)
    
    def test_save_state_client_error(self, parameter_store_state, ssm_client_mock):
        """Test handling AWS client error when saving state."""
        # Setup mock to raise ClientError
        error_response = {"Error": {"Code": "InternalServerError", "Message": "Internal error"}}
        ssm_client_mock.get_parameter.side_effect = ClientError(error_response, "GetParameter")
        ssm_client_mock.put_parameter.side_effect = ClientError(error_response, "PutParameter")
        
        # Should raise StateError
        with pytest.raises(StateError) as exc_info:
            parameter_store_state.save_state("test_key", {"key": "value"})
        
        assert "Failed to save state" in str(exc_info.value)
    
    def test_save_state_json_error(self, parameter_store_state):
        """Test handling JSON serialization error when saving state."""
        # Create a state with an unencodable object
        test_state = {"key": MagicMock()}  # MagicMock can't be JSON serialized
        
        # Should raise StateError
        with pytest.raises(StateError) as exc_info:
            parameter_store_state.save_state("test_key", test_state)
        
        assert "Failed to save state" in str(exc_info.value)
    
    def test_load_state_parameter_not_found(self, parameter_store_state, ssm_client_mock):
        """Test handling parameter not found when loading state."""
        # Setup mock to raise ParameterNotFound error
        error_response = {"Error": {"Code": "ParameterNotFound", "Message": "Parameter not found"}}
        ssm_client_mock.get_parameter.side_effect = ClientError(error_response, "GetParameter")
        
        # Should return None, not raise exception
        state = parameter_store_state.load_state("test_key")
        assert state is None
    
    def test_load_state_client_error(self, parameter_store_state, ssm_client_mock):
        """Test handling other client errors when loading state."""
        # Setup mock to raise different ClientError
        error_response = {"Error": {"Code": "AccessDeniedException", "Message": "Access denied"}}
        ssm_client_mock.get_parameter.side_effect = ClientError(error_response, "GetParameter")
        
        # Should raise StateError
        with pytest.raises(StateError) as exc_info:
            parameter_store_state.load_state("test_key")
        
        assert "Failed to load state" in str(exc_info.value)
    
    def test_load_state_invalid_json(self, parameter_store_state, ssm_client_mock):
        """Test handling invalid JSON when loading state."""
        # Setup mock to return invalid JSON
        ssm_client_mock.get_parameter.return_value = {
            "Parameter": {"Value": "Not valid JSON"}
        }
        
        # Should raise StateError
        with pytest.raises(StateError) as exc_info:
            parameter_store_state.load_state("test_key")
        
        assert "Failed to load state" in str(exc_info.value)
    
    def test_delete_state_parameter_not_found(self, parameter_store_state, ssm_client_mock):
        """Test handling parameter not found when deleting state."""
        # Setup mock to raise ParameterNotFound error
        error_response = {"Error": {"Code": "ParameterNotFound", "Message": "Parameter not found"}}
        ssm_client_mock.delete_parameter.side_effect = ClientError(error_response, "DeleteParameter")
        
        # Should not raise exception
        parameter_store_state.delete_state("test_key")
        
        # Verify delete_parameter was called
        ssm_client_mock.delete_parameter.assert_called_once()
    
    def test_delete_state_client_error(self, parameter_store_state, ssm_client_mock):
        """Test handling other client errors when deleting state."""
        # Setup mock to raise different ClientError
        error_response = {"Error": {"Code": "AccessDeniedException", "Message": "Access denied"}}
        ssm_client_mock.delete_parameter.side_effect = ClientError(error_response, "DeleteParameter")
        
        # Should raise StateError
        with pytest.raises(StateError) as exc_info:
            parameter_store_state.delete_state("test_key")
        
        assert "Failed to delete state" in str(exc_info.value)


@patch('boto3.Session')
class TestS3StateErrorHandling:
    """Tests for error handling in S3State."""
    
    @pytest.fixture
    def provider_mock(self):
        """Create a mock provider."""
        provider = MagicMock()
        provider.workflow_id = "test-workflow"
        provider.region = "us-east-1"
        provider.aws_access_key_id = None
        provider.aws_secret_access_key = None
        provider.aws_session_token = None
        provider.aws_profile = None
        return provider
    
    @pytest.fixture
    def s3_client_mock(self):
        """Create a mock S3 client."""
        client = MagicMock()
        return client
    
    @pytest.fixture
    def s3_state(self, provider_mock, s3_client_mock, boto3_session_mock):
        """Create an S3State instance with mocked AWS clients."""
        session_instance = boto3_session_mock.return_value
        session_instance.client.return_value = s3_client_mock
        session_instance.resource.return_value = MagicMock()
        
        return S3State(provider_mock, "test-bucket")
    
    def test_save_state_client_error(self, s3_state, s3_client_mock):
        """Test handling AWS client error when saving state."""
        # Setup mock to raise ClientError
        error_response = {"Error": {"Code": "InternalError", "Message": "Internal error"}}
        s3_client_mock.put_object.side_effect = ClientError(error_response, "PutObject")
        
        # Should raise StateError
        with pytest.raises(StateError) as exc_info:
            s3_state.save_state("test_key", {"key": "value"})
        
        assert "Failed to save state" in str(exc_info.value)
    
    def test_save_state_json_error(self, s3_state):
        """Test handling JSON serialization error when saving state."""
        # Create a state with an unencodable object
        test_state = {"key": MagicMock()}  # MagicMock can't be JSON serialized
        
        # Should raise StateError
        with pytest.raises(StateError) as exc_info:
            s3_state.save_state("test_key", test_state)
        
        assert "Failed to save state" in str(exc_info.value)
    
    def test_save_state_bucket_not_found(self, s3_state, s3_client_mock):
        """Test handling bucket not found when saving state."""
        # Setup mock to raise NoSuchBucket error
        error_response = {"Error": {"Code": "NoSuchBucket", "Message": "The specified bucket does not exist"}}
        s3_client_mock.put_object.side_effect = ClientError(error_response, "PutObject")
        
        # Should raise StateError
        with pytest.raises(StateError) as exc_info:
            s3_state.save_state("test_key", {"key": "value"})
        
        assert "Failed to save state" in str(exc_info.value)
    
    def test_load_state_no_such_key(self, s3_state, s3_client_mock):
        """Test handling no such key when loading state."""
        # Setup mock to raise NoSuchKey error
        error_response = {"Error": {"Code": "NoSuchKey", "Message": "The specified key does not exist"}}
        s3_client_mock.get_object.side_effect = ClientError(error_response, "GetObject")
        
        # Should return None, not raise exception
        state = s3_state.load_state("test_key")
        assert state is None
    
    def test_load_state_client_error(self, s3_state, s3_client_mock):
        """Test handling other client errors when loading state."""
        # Setup mock to raise different ClientError
        error_response = {"Error": {"Code": "AccessDenied", "Message": "Access denied"}}
        s3_client_mock.get_object.side_effect = ClientError(error_response, "GetObject")
        
        # Should raise StateError
        with pytest.raises(StateError) as exc_info:
            s3_state.load_state("test_key")
        
        assert "Failed to load state" in str(exc_info.value)
    
    def test_load_state_invalid_json(self, s3_state, s3_client_mock):
        """Test handling invalid JSON when loading state."""
        # Setup mock to return invalid JSON
        mock_body = MagicMock()
        mock_body.read.return_value = b"Not valid JSON"
        s3_client_mock.get_object.return_value = {"Body": mock_body}
        
        # Should raise StateError
        with pytest.raises(StateError) as exc_info:
            s3_state.load_state("test_key")
        
        assert "Failed to load state" in str(exc_info.value)
    
    def test_delete_state_no_such_key(self, s3_state, s3_client_mock):
        """Test handling no such key when deleting state."""
        # Setup mock to raise NoSuchKey error
        error_response = {"Error": {"Code": "NoSuchKey", "Message": "The specified key does not exist"}}
        s3_client_mock.delete_object.side_effect = ClientError(error_response, "DeleteObject")
        
        # Should not raise exception
        s3_state.delete_state("test_key")
        
        # Verify delete_object was called
        s3_client_mock.delete_object.assert_called_once()
    
    def test_delete_state_client_error(self, s3_state, s3_client_mock):
        """Test handling other client errors when deleting state."""
        # Setup mock to raise different ClientError
        error_response = {"Error": {"Code": "AccessDenied", "Message": "Access denied"}}
        s3_client_mock.delete_object.side_effect = ClientError(error_response, "DeleteObject")
        
        # Should raise StateError
        with pytest.raises(StateError) as exc_info:
            s3_state.delete_state("test_key")
        
        assert "Failed to delete state" in str(exc_info.value)
    
    def test_bucket_creation_error(self, provider_mock, s3_client_mock, boto3_session_mock):
        """Test handling errors during bucket creation."""
        # Setup session mock
        session_instance = boto3_session_mock.return_value
        session_instance.client.return_value = s3_client_mock
        
        # Setup client mock to raise error on bucket check
        error_response = {"Error": {"Code": "404", "Message": "Not Found"}}
        s3_client_mock.head_bucket.side_effect = ClientError(error_response, "HeadBucket")
        
        # Setup error on bucket creation
        error_response_create = {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}}
        s3_client_mock.create_bucket.side_effect = ClientError(error_response_create, "CreateBucket")
        
        # Should raise StateError during initialization
        with pytest.raises(StateError) as exc_info:
            S3State(provider_mock, "test-bucket", create_bucket_if_not_exists=True)
        
        assert "Failed to create S3 bucket" in str(exc_info.value)
    
    def test_cleanup_workflow_states_error(self, s3_state, s3_client_mock):
        """Test handling errors during workflow state cleanup."""
        # Setup paginator mock
        paginator_mock = MagicMock()
        s3_client_mock.get_paginator.return_value = paginator_mock
        
        # Setup page iterator to return some objects
        page_iterator_mock = MagicMock()
        paginator_mock.paginate.return_value = page_iterator_mock
        page_iterator_mock.__iter__.return_value = [
            {"Contents": [{"Key": "obj1"}, {"Key": "obj2"}]}
        ]
        
        # Setup error on delete_objects
        error_response = {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}}
        s3_client_mock.delete_objects.side_effect = ClientError(error_response, "DeleteObjects")
        
        # Should raise StateError
        with pytest.raises(StateError) as exc_info:
            s3_state.cleanup_workflow_states()
        
        assert "Failed to clean up workflow states" in str(exc_info.value)
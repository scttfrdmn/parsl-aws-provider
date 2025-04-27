"""Integration tests for state persistence mechanisms.

These tests verify that each state persistence implementation works correctly
with real storage backend (file system, LocalStack for AWS services).

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import os
import json
import uuid
import pytest
import tempfile
import boto3
from typing import Dict, Any

from parsl_ephemeral_aws.state.file import FileStateStore
from parsl_ephemeral_aws.state.parameter_store import ParameterStoreState
from parsl_ephemeral_aws.state.s3 import S3State
from parsl_ephemeral_aws.exceptions import StateError
from parsl_ephemeral_aws.utils.localstack import is_localstack_available, get_localstack_session


# Skip all tests if LocalStack is not available
pytestmark = pytest.mark.skipif(
    not is_localstack_available(),
    reason="LocalStack is not available. Make sure it's running on port 4566."
)


class TestFileStateStoreIntegration:
    """Integration tests for FileStateStore."""
    
    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for state files."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            yield tmp_dir
    
    @pytest.fixture
    def file_state_store(self, temp_dir):
        """Create a FileStateStore instance with a real file."""
        provider_id = f"test-provider-{uuid.uuid4().hex[:8]}"
        file_path = os.path.join(temp_dir, f"{provider_id}-state.json")
        return FileStateStore(file_path=file_path, provider_id=provider_id)
    
    @pytest.fixture
    def complex_state(self):
        """Create a complex state dictionary with nested structures."""
        return {
            "provider_info": {
                "id": f"provider-{uuid.uuid4().hex[:8]}",
                "region": "us-east-1",
                "created_at": "2023-01-01T00:00:00Z"
            },
            "resources": {
                f"res-{uuid.uuid4().hex[:8]}": {
                    "instance_id": f"i-{uuid.uuid4().hex[:12]}",
                    "status": "running",
                    "ip_address": "10.0.0.1",
                    "tags": ["compute", "worker"]
                },
                f"res-{uuid.uuid4().hex[:8]}": {
                    "instance_id": f"i-{uuid.uuid4().hex[:12]}",
                    "status": "pending",
                    "ip_address": "10.0.0.2",
                    "tags": ["storage", "worker"]
                }
            },
            "jobs": {
                f"job-{uuid.uuid4().hex[:8]}": {
                    "status": "running",
                    "submitted_at": "2023-01-02T00:00:00Z",
                    "resource_id": "res-1",
                    "command": "echo 'Hello World'",
                    "environment": {
                        "PATH": "/usr/bin:/bin",
                        "HOME": "/home/user"
                    }
                }
            },
            "statistics": {
                "job_count": 10,
                "success_count": 8,
                "failure_count": 1,
                "pending_count": 1,
                "average_runtime": 42.5
            }
        }
    
    def test_full_lifecycle(self, file_state_store, complex_state):
        """Test the full lifecycle of state persistence."""
        # 1. Save state
        file_state_store.save_state(complex_state)
        
        # Verify file exists
        assert os.path.exists(file_state_store.file_path)
        
        # 2. Load state
        loaded_state = file_state_store.load_state()
        
        # Verify loaded state matches original
        assert loaded_state is not None
        assert loaded_state["provider_info"]["id"] == complex_state["provider_info"]["id"]
        assert len(loaded_state["resources"]) == len(complex_state["resources"])
        assert loaded_state["statistics"]["job_count"] == complex_state["statistics"]["job_count"]
        
        # 3. Update state
        loaded_state["statistics"]["job_count"] += 1
        loaded_state["statistics"]["success_count"] += 1
        file_state_store.save_state(loaded_state)
        
        # 4. Reload state and verify updates
        reloaded_state = file_state_store.load_state()
        assert reloaded_state["statistics"]["job_count"] == 11
        assert reloaded_state["statistics"]["success_count"] == 9
        
        # 5. Delete state
        file_state_store.delete_state()
        
        # Verify file no longer exists
        assert not os.path.exists(file_state_store.file_path)
        
        # 6. Load after delete should return None
        final_state = file_state_store.load_state()
        assert final_state is None
    
    def test_concurrent_access(self, temp_dir, complex_state):
        """Test concurrent access to the same state file."""
        provider_id = f"shared-provider-{uuid.uuid4().hex[:8]}"
        file_path = os.path.join(temp_dir, f"{provider_id}-state.json")
        
        # Create two separate state stores for the same file
        store1 = FileStateStore(file_path=file_path, provider_id=provider_id)
        store2 = FileStateStore(file_path=file_path, provider_id=provider_id)
        
        # Store 1 saves initial state
        store1.save_state(complex_state)
        
        # Store 2 loads state, modifies it, and saves back
        state2 = store2.load_state()
        state2["statistics"]["job_count"] = 20
        state2["provider_info"]["updated_by"] = "store2"
        store2.save_state(state2)
        
        # Store 1 reloads state - should see Store 2's changes
        updated_state = store1.load_state()
        assert updated_state["statistics"]["job_count"] == 20
        assert updated_state["provider_info"]["updated_by"] == "store2"


@pytest.mark.integration
class TestParameterStoreStateIntegration:
    """Integration tests for ParameterStoreState using LocalStack."""
    
    @pytest.fixture(scope="class")
    def localstack_session(self):
        """Create a session connected to LocalStack."""
        return get_localstack_session()
    
    @pytest.fixture
    def mock_provider(self):
        """Create a provider-like object."""
        class MockProvider:
            def __init__(self):
                self.workflow_id = f"test-workflow-{uuid.uuid4().hex[:8]}"
                self.region = "us-east-1"
                self.aws_access_key_id = "test"
                self.aws_secret_access_key = "test"
                self.aws_session_token = None
                self.aws_profile = None
        
        return MockProvider()
    
    @pytest.fixture
    def parameter_store_state(self, mock_provider, localstack_session):
        """Create a ParameterStoreState instance with LocalStack."""
        state_prefix = f"/parsl/test/{uuid.uuid4().hex[:8]}"
        
        # Override session creation to use our LocalStack session
        with patch.object(boto3, "Session", return_value=localstack_session):
            state_store = ParameterStoreState(
                provider=mock_provider,
                prefix=state_prefix
            )
            yield state_store
            
            # Cleanup
            try:
                # Find all parameters under our prefix
                paginator = localstack_session.client('ssm').get_paginator('get_parameters_by_path')
                page_iterator = paginator.paginate(
                    Path=state_prefix,
                    Recursive=True,
                    WithDecryption=True
                )
                
                parameters_to_delete = []
                for page in page_iterator:
                    for param in page.get('Parameters', []):
                        parameters_to_delete.append(param['Name'])
                
                # Delete in batches
                ssm_client = localstack_session.client('ssm')
                for i in range(0, len(parameters_to_delete), 10):
                    batch = parameters_to_delete[i:i+10]
                    if batch:
                        try:
                            ssm_client.delete_parameters(Names=batch)
                        except Exception as e:
                            print(f"Error cleaning up parameters: {e}")
            except Exception as e:
                print(f"Error during cleanup: {e}")
    
    @pytest.fixture
    def complex_state(self):
        """Create a complex state dictionary with nested structures."""
        return {
            "provider_info": {
                "id": f"provider-{uuid.uuid4().hex[:8]}",
                "region": "us-east-1",
                "created_at": "2023-01-01T00:00:00Z"
            },
            "resources": {
                f"res-{uuid.uuid4().hex[:8]}": {
                    "instance_id": f"i-{uuid.uuid4().hex[:12]}",
                    "status": "running",
                    "tags": ["compute", "worker"]
                }
            },
            "statistics": {
                "job_count": 5,
                "success_count": 3,
                "failure_count": 1,
                "pending_count": 1
            }
        }
    
    @pytest.mark.localstack
    def test_parameter_store_lifecycle(self, parameter_store_state, complex_state):
        """Test the full lifecycle of a Parameter Store state."""
        state_key = f"test-state-{uuid.uuid4().hex[:8]}"
        
        # 1. Save state
        parameter_store_state.save_state(state_key, complex_state)
        
        # 2. Load state
        loaded_state = parameter_store_state.load_state(state_key)
        
        # Verify loaded state matches original
        assert loaded_state is not None
        assert loaded_state["provider_info"]["id"] == complex_state["provider_info"]["id"]
        assert len(loaded_state["resources"]) == len(complex_state["resources"])
        assert loaded_state["statistics"]["job_count"] == complex_state["statistics"]["job_count"]
        
        # 3. Update state
        loaded_state["statistics"]["job_count"] += 1
        loaded_state["statistics"]["success_count"] += 1
        parameter_store_state.save_state(state_key, loaded_state)
        
        # 4. Reload state and verify updates
        reloaded_state = parameter_store_state.load_state(state_key)
        assert reloaded_state["statistics"]["job_count"] == 6
        assert reloaded_state["statistics"]["success_count"] == 4
        
        # 5. Delete state
        parameter_store_state.delete_state(state_key)
        
        # 6. Load after delete should return None
        final_state = parameter_store_state.load_state(state_key)
        assert final_state is None
    
    @pytest.mark.localstack
    def test_list_parameters(self, parameter_store_state):
        """Test listing parameters with a prefix."""
        # Create multiple parameters with a common prefix
        prefix = f"list-test-{uuid.uuid4().hex[:8]}"
        
        # Save multiple states with the same prefix
        parameter_store_state.save_state(f"{prefix}/state1", {"id": "state1", "value": 1})
        parameter_store_state.save_state(f"{prefix}/state2", {"id": "state2", "value": 2})
        parameter_store_state.save_state(f"{prefix}/state3", {"id": "state3", "value": 3})
        
        # Also save a state with a different prefix
        parameter_store_state.save_state(f"other-prefix-{uuid.uuid4().hex[:8]}", {"id": "other"})
        
        # List states with our prefix
        states = parameter_store_state.list_states(prefix)
        
        # Verify we got the right states
        assert len(states) == 3
        assert any(state["id"] == "state1" for state in states.values())
        assert any(state["id"] == "state2" for state in states.values())
        assert any(state["id"] == "state3" for state in states.values())
        
        # Cleanup created parameters
        for key in [f"{prefix}/state1", f"{prefix}/state2", f"{prefix}/state3"]:
            parameter_store_state.delete_state(key)


@pytest.mark.integration
class TestS3StateIntegration:
    """Integration tests for S3State using LocalStack."""
    
    @pytest.fixture(scope="class")
    def localstack_session(self):
        """Create a session connected to LocalStack."""
        return get_localstack_session()
    
    @pytest.fixture
    def s3_bucket_name(self, localstack_session):
        """Create a unique S3 bucket name and ensure it exists."""
        bucket_name = f"test-bucket-{uuid.uuid4().hex[:16]}"
        s3_client = localstack_session.client('s3')
        
        try:
            s3_client.create_bucket(Bucket=bucket_name)
        except Exception as e:
            pytest.skip(f"Failed to create test bucket: {e}")
        
        yield bucket_name
        
        # Cleanup
        try:
            # Delete all objects first
            objects = s3_client.list_objects_v2(Bucket=bucket_name)
            if 'Contents' in objects:
                delete_keys = {'Objects': [{'Key': obj['Key']} for obj in objects['Contents']]}
                s3_client.delete_objects(Bucket=bucket_name, Delete=delete_keys)
            
            # Then delete bucket
            s3_client.delete_bucket(Bucket=bucket_name)
        except Exception as e:
            print(f"Error cleaning up test bucket: {e}")
    
    @pytest.fixture
    def mock_provider(self):
        """Create a provider-like object."""
        class MockProvider:
            def __init__(self):
                self.workflow_id = f"test-workflow-{uuid.uuid4().hex[:8]}"
                self.region = "us-east-1"
                self.aws_access_key_id = "test"
                self.aws_secret_access_key = "test"
                self.aws_session_token = None
                self.aws_profile = None
        
        return MockProvider()
    
    @pytest.fixture
    def s3_state(self, mock_provider, s3_bucket_name, localstack_session):
        """Create an S3State instance with LocalStack."""
        key_prefix = f"parsl/test/{uuid.uuid4().hex[:8]}"
        
        # Override session creation to use our LocalStack session
        with patch.object(boto3, "Session", return_value=localstack_session):
            return S3State(
                provider=mock_provider,
                bucket_name=s3_bucket_name,
                key_prefix=key_prefix
            )
    
    @pytest.fixture
    def complex_state(self):
        """Create a complex state dictionary with nested structures."""
        return {
            "provider_info": {
                "id": f"provider-{uuid.uuid4().hex[:8]}",
                "region": "us-east-1",
                "created_at": "2023-01-01T00:00:00Z"
            },
            "resources": {
                f"res-{uuid.uuid4().hex[:8]}": {
                    "instance_id": f"i-{uuid.uuid4().hex[:12]}",
                    "status": "running",
                    "tags": ["compute", "worker"]
                }
            },
            "statistics": {
                "job_count": 5,
                "success_count": 3,
                "failure_count": 1,
                "pending_count": 1
            },
            "spot_fleets": {
                f"sfr-{uuid.uuid4().hex[:8]}": {
                    "instances": [f"i-{uuid.uuid4().hex[:12]}" for _ in range(3)],
                    "status": "active"
                }
            }
        }
    
    @pytest.mark.localstack
    def test_s3_state_lifecycle(self, s3_state, complex_state):
        """Test the full lifecycle of S3 state."""
        state_key = f"test-state-{uuid.uuid4().hex[:8]}"
        
        # 1. Save state
        s3_state.save_state(state_key, complex_state)
        
        # 2. Load state
        loaded_state = s3_state.load_state(state_key)
        
        # Verify loaded state matches original
        assert loaded_state is not None
        assert loaded_state["provider_info"]["id"] == complex_state["provider_info"]["id"]
        assert len(loaded_state["resources"]) == len(complex_state["resources"])
        assert loaded_state["statistics"]["job_count"] == complex_state["statistics"]["job_count"]
        assert len(loaded_state["spot_fleets"]) == len(complex_state["spot_fleets"])
        
        # 3. Update state
        loaded_state["statistics"]["job_count"] += 1
        loaded_state["statistics"]["success_count"] += 1
        s3_state.save_state(state_key, loaded_state)
        
        # 4. Reload state and verify updates
        reloaded_state = s3_state.load_state(state_key)
        assert reloaded_state["statistics"]["job_count"] == 6
        assert reloaded_state["statistics"]["success_count"] == 4
        
        # 5. Delete state
        s3_state.delete_state(state_key)
        
        # 6. Load after delete should return None
        final_state = s3_state.load_state(state_key)
        assert final_state is None
    
    @pytest.mark.localstack
    def test_list_s3_objects(self, s3_state):
        """Test listing S3 objects with a prefix."""
        # Create multiple states with a common prefix
        prefix = f"list-test-{uuid.uuid4().hex[:8]}"
        
        # Save multiple states with the same prefix
        s3_state.save_state(f"{prefix}/state1", {"id": "state1", "value": 1})
        s3_state.save_state(f"{prefix}/state2", {"id": "state2", "value": 2})
        s3_state.save_state(f"{prefix}/state3", {"id": "state3", "value": 3})
        
        # Also save a state with a different prefix
        s3_state.save_state(f"other-prefix-{uuid.uuid4().hex[:8]}", {"id": "other"})
        
        # List states with our prefix
        states = s3_state.list_states(prefix)
        
        # Verify we got the right states
        assert len(states) == 3
        assert any(state["id"] == "state1" for state in states.values())
        assert any(state["id"] == "state2" for state in states.values())
        assert any(state["id"] == "state3" for state in states.values())
        
        # Cleanup created objects
        for key in [f"{prefix}/state1", f"{prefix}/state2", f"{prefix}/state3"]:
            s3_state.delete_state(key)
    
    @pytest.mark.localstack
    def test_cleanup_workflow_states(self, s3_state, mock_provider, localstack_session):
        """Test cleaning up all workflow states."""
        # Create several objects for this workflow
        workflow_prefix = mock_provider.workflow_id
        
        # Save multiple states for this workflow
        for i in range(5):
            s3_state.save_state(f"{workflow_prefix}/state{i}", {"workflow": workflow_prefix, "id": f"state{i}"})
        
        # Save a state for a different workflow
        other_key = f"other-workflow-{uuid.uuid4().hex[:8]}/state"
        s3_state.save_state(other_key, {"workflow": "other", "id": "other"})
        
        # Cleanup workflow states
        s3_state.cleanup_workflow_states()
        
        # Verify our workflow states are gone
        for i in range(5):
            assert s3_state.load_state(f"{workflow_prefix}/state{i}") is None
        
        # Verify other workflow state still exists
        assert s3_state.load_state(other_key) is not None
        
        # Cleanup the other state
        s3_state.delete_state(other_key)
    
    @pytest.mark.localstack
    def test_create_bucket_if_not_exists(self, mock_provider, localstack_session):
        """Test creating a bucket if it doesn't exist."""
        bucket_name = f"auto-create-bucket-{uuid.uuid4().hex[:16]}"
        s3_client = localstack_session.client('s3')
        
        # Verify bucket doesn't exist
        try:
            s3_client.head_bucket(Bucket=bucket_name)
            bucket_exists = True
        except:
            bucket_exists = False
        
        assert not bucket_exists
        
        # Create S3State with create_bucket_if_not_exists=True
        with patch.object(boto3, "Session", return_value=localstack_session):
            s3_state = S3State(
                provider=mock_provider,
                bucket_name=bucket_name,
                create_bucket_if_not_exists=True
            )
        
        # Verify bucket was created
        try:
            s3_client.head_bucket(Bucket=bucket_name)
            bucket_exists = True
        except:
            bucket_exists = False
        
        assert bucket_exists
        
        # Test saving and loading state in the new bucket
        test_state = {"created": "auto", "test": True}
        s3_state.save_state("test_key", test_state)
        
        loaded_state = s3_state.load_state("test_key")
        assert loaded_state["created"] == "auto"
        
        # Cleanup
        s3_state.delete_state("test_key")
        try:
            s3_client.delete_bucket(Bucket=bucket_name)
        except Exception as e:
            print(f"Error cleaning up bucket: {e}")
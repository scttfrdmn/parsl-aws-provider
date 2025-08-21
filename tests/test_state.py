"""Tests for the state module.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import json
import tempfile
import pytest
from unittest.mock import MagicMock, patch

from parsl_ephemeral_aws.state.base import StateStore
from parsl_ephemeral_aws.state.file import FileState
from parsl_ephemeral_aws.state.parameter_store import ParameterStoreState
from parsl_ephemeral_aws.state.s3 import S3State
from parsl_ephemeral_aws.utils.serialization import serialize_state, deserialize_state


class TestStateBase:
    """Tests for the base StateStore class."""

    def test_state_store_interface(self):
        """Test that the StateStore interface has the required methods."""
        assert hasattr(StateStore, "save_state")
        assert hasattr(StateStore, "load_state")
        assert hasattr(StateStore, "delete_state")
        assert hasattr(StateStore, "list_states")


class TestFileState:
    """Tests for the FileState class."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for state files."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            yield tmp_dir

    @pytest.fixture
    def provider_mock(self):
        """Create a mock provider."""
        provider = MagicMock()
        provider.workflow_id = "test-workflow"
        return provider

    @pytest.fixture
    def file_state(self, provider_mock, temp_dir):
        """Create a FileState instance."""
        return FileState(provider_mock, temp_dir)

    def test_save_and_load_state(self, file_state):
        """Test saving and loading state."""
        test_state = {"key": "value", "nested": {"data": 42}}

        # Save state
        file_state.save_state("test_key", test_state)

        # Load state
        loaded_state = file_state.load_state("test_key")

        assert loaded_state == test_state

    def test_delete_state(self, file_state):
        """Test deleting state."""
        test_state = {"key": "value"}

        # Save state
        file_state.save_state("test_key", test_state)

        # Verify it exists
        assert file_state.load_state("test_key") is not None

        # Delete state
        file_state.delete_state("test_key")

        # Verify it's gone
        assert file_state.load_state("test_key") is None

    def test_list_states(self, file_state):
        """Test listing states with a prefix."""
        # Save multiple states
        file_state.save_state("prefix1_key1", {"data": 1})
        file_state.save_state("prefix1_key2", {"data": 2})
        file_state.save_state("prefix2_key1", {"data": 3})

        # List states with prefix1
        states = file_state.list_states("prefix1")

        assert len(states) == 2
        assert "prefix1_key1" in states or "key1" in states  # exact key name might vary
        assert "prefix1_key2" in states or "key2" in states

        # Verify state data
        for state in states.values():
            assert "data" in state
            assert state["data"] in [1, 2]


@patch("boto3.Session")
class TestParameterStoreState:
    """Tests for the ParameterStoreState class."""

    @pytest.fixture
    def provider_mock(self):
        """Create a mock provider."""
        provider = MagicMock()
        provider.workflow_id = "test-workflow"
        provider.region = "us-east-1"
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

    def test_save_state_new_parameter(self, parameter_store_state, ssm_client_mock):
        """Test saving state as a new parameter."""
        # Setup mock to simulate parameter not found
        ssm_client_mock.get_parameter.side_effect = MagicMock(
            side_effect=Exception("ParameterNotFound")
        )

        # Save state
        test_state = {"key": "value"}
        parameter_store_state.save_state("test_key", test_state)

        # Verify put_parameter was called with the right args
        ssm_client_mock.put_parameter.assert_called_once()
        args, kwargs = ssm_client_mock.put_parameter.call_args
        assert kwargs["Name"] == "/parsl/workflows/test_key"
        assert json.loads(kwargs["Value"]) == test_state

    def test_load_state(self, parameter_store_state, ssm_client_mock):
        """Test loading state."""
        # Setup mock to return parameter
        test_state = {"key": "value"}
        ssm_client_mock.get_parameter.return_value = {
            "Parameter": {"Value": json.dumps(test_state)}
        }

        # Load state
        loaded_state = parameter_store_state.load_state("test_key")

        # Verify state and parameters
        assert loaded_state == test_state
        ssm_client_mock.get_parameter.assert_called_with(
            Name="/parsl/workflows/test_key", WithDecryption=True
        )

    def test_delete_state(self, parameter_store_state, ssm_client_mock):
        """Test deleting state."""
        # Delete state
        parameter_store_state.delete_state("test_key")

        # Verify delete_parameter was called
        ssm_client_mock.delete_parameter.assert_called_with(
            Name="/parsl/workflows/test_key"
        )


@patch("boto3.Session")
class TestS3State:
    """Tests for the S3State class."""

    @pytest.fixture
    def provider_mock(self):
        """Create a mock provider."""
        provider = MagicMock()
        provider.workflow_id = "test-workflow"
        provider.region = "us-east-1"
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

    def test_save_state(self, s3_state, s3_client_mock):
        """Test saving state to S3."""
        # Save state
        test_state = {"key": "value"}
        s3_state.save_state("test_key", test_state)

        # Verify put_object was called with the right args
        s3_client_mock.put_object.assert_called_once()
        args, kwargs = s3_client_mock.put_object.call_args
        assert kwargs["Bucket"] == "test-bucket"
        assert kwargs["Key"] == "parsl/workflows/test_key"
        assert json.loads(kwargs["Body"]) == test_state

    def test_load_state(self, s3_state, s3_client_mock):
        """Test loading state from S3."""
        # Setup mock to return object
        test_state = {"key": "value"}
        s3_client_mock.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(test_state).encode())
        }

        # Load state
        loaded_state = s3_state.load_state("test_key")

        # Verify state and parameters
        assert loaded_state == test_state
        s3_client_mock.get_object.assert_called_with(
            Bucket="test-bucket", Key="parsl/workflows/test_key"
        )

    def test_delete_state(self, s3_state, s3_client_mock):
        """Test deleting state from S3."""
        # Delete state
        s3_state.delete_state("test_key")

        # Verify delete_object was called
        s3_client_mock.delete_object.assert_called_with(
            Bucket="test-bucket", Key="parsl/workflows/test_key"
        )


class TestSerialization:
    """Tests for the serialization utilities."""

    def test_serialize_deserialize(self):
        """Test serializing and deserializing state."""
        import uuid
        import datetime

        # Create a test state with various types
        test_state = {
            "string": "value",
            "number": 42,
            "nested": {"data": True},
            "uuid": uuid.uuid4(),
            "datetime": datetime.datetime.now(),
            "set": {"a", "b", "c"},
        }

        # Serialize
        serialized = serialize_state(test_state)

        # Deserialize
        deserialized = deserialize_state(serialized)

        # Check types and values
        assert deserialized["string"] == test_state["string"]
        assert deserialized["number"] == test_state["number"]
        assert deserialized["nested"] == test_state["nested"]
        assert deserialized["uuid"] == test_state["uuid"]
        assert isinstance(deserialized["datetime"], datetime.datetime)
        assert isinstance(deserialized["set"], set)
        assert deserialized["set"] == test_state["set"]

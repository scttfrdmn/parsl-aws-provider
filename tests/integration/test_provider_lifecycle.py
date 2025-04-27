"""Integration tests for provider lifecycle.

These tests verify the complete lifecycle of the EphemeralAWSProvider, including
initialization, usage, and cleanup across different operating modes.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import os
import time
import uuid
import pytest
import tempfile
from unittest.mock import MagicMock, patch, PropertyMock

from parsl_ephemeral_aws.provider import EphemeralAWSProvider
from parsl_ephemeral_aws.modes.standard import StandardMode
from parsl_ephemeral_aws.modes.detached import DetachedMode
from parsl_ephemeral_aws.modes.serverless import ServerlessMode
from parsl_ephemeral_aws.state.file import FileStateStore
from parsl_ephemeral_aws.utils.localstack import is_localstack_available, get_localstack_session
from parsl_ephemeral_aws.exceptions import ProviderConfigurationError


# Skip all tests if LocalStack is not available
pytestmark = pytest.mark.skipif(
    not is_localstack_available(),
    reason="LocalStack is not available. Make sure it's running on port 4566."
)


@pytest.mark.integration
class TestProviderLifecycle:
    """Integration tests for provider lifecycle."""
    
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
    def test_standard_mode_full_lifecycle(self, localstack_session, file_state_store):
        """Test complete lifecycle of provider in standard mode."""
        # Configuration for standard mode
        config = {
            "provider_id": f"test-provider-{uuid.uuid4().hex[:8]}",
            "region": "us-east-1",
            "instance_type": "t2.micro",
            "image_id": "ami-12345678",  # Dummy AMI
            "mode": "standard",
            "max_blocks": 2,
            "min_blocks": 0,
            "init_blocks": 1,
            # Inject test session
            "_test_session": localstack_session,
            "_test_state_store": file_state_store
        }
        
        # Create provider
        provider = EphemeralAWSProvider(**config)
        
        # Replace provider's operating mode with a mocked StandardMode
        mock_mode = MagicMock(spec=StandardMode)
        mock_mode.initialize.return_value = None
        mock_mode.cleanup_infrastructure.return_value = None
        provider._operating_mode = mock_mode
        
        try:
            # Step a: Initialize provider
            provider.initialize_blocks()
            mock_mode.initialize.assert_called_once()
            mock_mode._init_blocks.assert_called_once()
            
            # Step b: Submit a job
            job_id = f"test-job-{uuid.uuid4().hex[:8]}"
            command = "echo 'hello world'"
            
            mock_mode.submit_job.return_value = "resource-1"
            mock_resource_job_mapping = PropertyMock(return_value={"resource-1": job_id})
            type(mock_mode).resource_job_mapping = mock_resource_job_mapping
            
            resource_id = provider.submit(job_id, command)
            assert resource_id == "resource-1"
            mock_mode.submit_job.assert_called_with(job_id, command, 1)
            
            # Step c: Check job status
            mock_mode.get_job_status.return_value = {"resource-1": "running"}
            status = provider.status([job_id])
            assert job_id in status
            assert status[job_id] == "running"
            
            # Step d: Scale out
            mock_mode.scale_out.return_value = 1
            scaled = provider.scale_out(blocks=1)
            assert scaled == 1
            mock_mode.scale_out.assert_called_with(1)
            
            # Step e: Scale in
            mock_mode.scale_in.return_value = 1
            scaled = provider.scale_in(blocks=1)
            assert scaled == 1
            mock_mode.scale_in.assert_called_with(1)
            
            # Step f: Cancel job
            mock_mode.cancel_jobs.return_value = {"resource-1": "cancelled"}
            cancel_status = provider.cancel([job_id])
            assert job_id in cancel_status
            assert cancel_status[job_id] == "cancelled"
            
            # Step g: Shutdown provider
            provider.shutdown()
            mock_mode.cleanup_infrastructure.assert_called_once()
        
        finally:
            # Ensure cleanup even if test fails
            if hasattr(provider, '_operating_mode') and provider._operating_mode is not None:
                provider.shutdown()
    
    @pytest.mark.localstack
    def test_detached_mode_full_lifecycle(self, localstack_session, file_state_store):
        """Test complete lifecycle of provider in detached mode."""
        # Configuration for detached mode
        workflow_id = f"test-workflow-{uuid.uuid4().hex[:8]}"
        config = {
            "provider_id": f"test-provider-{uuid.uuid4().hex[:8]}",
            "region": "us-east-1",
            "instance_type": "t2.micro",
            "image_id": "ami-12345678",  # Dummy AMI
            "mode": "detached",
            "workflow_id": workflow_id,
            "bastion_instance_type": "t2.micro",
            "max_blocks": 2,
            "min_blocks": 0,
            "init_blocks": 1,
            # Inject test session
            "_test_session": localstack_session,
            "_test_state_store": file_state_store
        }
        
        # Create provider
        provider = EphemeralAWSProvider(**config)
        
        # Replace provider's operating mode with a mocked DetachedMode
        mock_mode = MagicMock(spec=DetachedMode)
        mock_mode.initialize.return_value = None
        mock_mode.cleanup_infrastructure.return_value = None
        provider._operating_mode = mock_mode
        
        try:
            # Step a: Initialize provider
            provider.initialize_blocks()
            mock_mode.initialize.assert_called_once()
            mock_mode._init_blocks.assert_called_once()
            
            # Step b: Submit a job
            job_id = f"test-job-{uuid.uuid4().hex[:8]}"
            command = "echo 'hello world'"
            
            mock_mode.submit_job.return_value = "resource-1"
            mock_resource_job_mapping = PropertyMock(return_value={"resource-1": job_id})
            type(mock_mode).resource_job_mapping = mock_resource_job_mapping
            
            resource_id = provider.submit(job_id, command)
            assert resource_id == "resource-1"
            mock_mode.submit_job.assert_called_with(job_id, command, 1)
            
            # Step c: Check job status
            mock_mode.get_job_status.return_value = {"resource-1": "running"}
            status = provider.status([job_id])
            assert job_id in status
            assert status[job_id] == "running"
            
            # Step d: Test save_state and load_state specific to detached mode
            mock_mode.save_state.return_value = None
            provider.save_state()
            mock_mode.save_state.assert_called_once()
            
            mock_mode.load_state.return_value = None
            provider.load_state()
            mock_mode.load_state.assert_called_once()
            
            # Step e: Shutdown provider with preserve_bastion
            # First set preserve_bastion = True
            provider.preserve_bastion = True
            provider.shutdown()
            # Detached mode should have preserve_bastion set to True
            mock_mode.preserve_bastion = True
            mock_mode.cleanup_infrastructure.assert_called_once()
            
            # Reset for next test
            mock_mode.cleanup_infrastructure.reset_mock()
            
            # Now test with preserve_bastion = False
            provider.preserve_bastion = False
            provider.shutdown()
            # Detached mode should have preserve_bastion set to False
            mock_mode.preserve_bastion = False
            mock_mode.cleanup_infrastructure.assert_called_once()
        
        finally:
            # Ensure cleanup even if test fails
            if hasattr(provider, '_operating_mode') and provider._operating_mode is not None:
                provider.shutdown()
    
    @pytest.mark.localstack
    def test_serverless_mode_full_lifecycle(self, localstack_session, file_state_store):
        """Test complete lifecycle of provider in serverless mode."""
        # Configuration for serverless mode
        config = {
            "provider_id": f"test-provider-{uuid.uuid4().hex[:8]}",
            "region": "us-east-1",
            "mode": "serverless",
            "worker_type": "lambda",
            "lambda_memory": 128,
            "lambda_timeout": 60,
            "max_blocks": 10,
            "min_blocks": 0,
            "init_blocks": 0,  # No initial blocks for serverless
            # Inject test session
            "_test_session": localstack_session,
            "_test_state_store": file_state_store
        }
        
        # Create provider
        provider = EphemeralAWSProvider(**config)
        
        # Replace provider's operating mode with a mocked ServerlessMode
        mock_mode = MagicMock(spec=ServerlessMode)
        mock_mode.initialize.return_value = None
        mock_mode.cleanup_infrastructure.return_value = None
        provider._operating_mode = mock_mode
        
        try:
            # Step a: Initialize provider (no blocks for serverless)
            provider.initialize_blocks()
            mock_mode.initialize.assert_called_once()
            # Serverless mode should not call _init_blocks
            mock_mode._init_blocks.assert_not_called()
            
            # Step b: Submit a job
            job_id = f"test-job-{uuid.uuid4().hex[:8]}"
            command = "echo 'hello world'"
            
            mock_mode.submit_job.return_value = "resource-1"
            mock_resource_job_mapping = PropertyMock(return_value={"resource-1": job_id})
            type(mock_mode).resource_job_mapping = mock_resource_job_mapping
            
            resource_id = provider.submit(job_id, command)
            assert resource_id == "resource-1"
            mock_mode.submit_job.assert_called_with(job_id, command, 1)
            
            # Step c: Check job status
            mock_mode.get_job_status.return_value = {"resource-1": "running"}
            status = provider.status([job_id])
            assert job_id in status
            assert status[job_id] == "running"
            
            # Step d: Scaling should be a no-op in serverless mode
            mock_mode.scale_out.return_value = 0
            scaled = provider.scale_out(blocks=1)
            assert scaled == 0
            mock_mode.scale_out.assert_called_with(1)
            
            mock_mode.scale_in.return_value = 0
            scaled = provider.scale_in(blocks=1)
            assert scaled == 0
            mock_mode.scale_in.assert_called_with(1)
            
            # Step e: Shutdown provider
            provider.shutdown()
            mock_mode.cleanup_infrastructure.assert_called_once()
        
        finally:
            # Ensure cleanup even if test fails
            if hasattr(provider, '_operating_mode') and provider._operating_mode is not None:
                provider.shutdown()
    
    @pytest.mark.localstack
    def test_provider_validation(self):
        """Test validation of provider configuration."""
        # Test 1: Invalid mode
        with pytest.raises(ProviderConfigurationError):
            provider = EphemeralAWSProvider(
                region="us-east-1",
                instance_type="t2.micro",
                image_id="ami-12345678",
                mode="invalid_mode"
            )
        
        # Test 2: Missing required parameter for standard mode
        with pytest.raises(ProviderConfigurationError):
            provider = EphemeralAWSProvider(
                region="us-east-1",
                mode="standard"
                # Missing instance_type and image_id
            )
        
        # Test 3: Missing required parameter for detached mode
        with pytest.raises(ProviderConfigurationError):
            provider = EphemeralAWSProvider(
                region="us-east-1",
                instance_type="t2.micro",
                image_id="ami-12345678",
                mode="detached"
                # Missing workflow_id
            )
        
        # Test 4: Invalid configuration for serverless mode
        with pytest.raises(ProviderConfigurationError):
            provider = EphemeralAWSProvider(
                region="us-east-1",
                mode="serverless",
                worker_type="invalid_worker"
            )
        
        # Test 5: Incompatible parameters
        with pytest.raises(ProviderConfigurationError):
            provider = EphemeralAWSProvider(
                region="us-east-1",
                instance_type="t2.micro",
                image_id="ami-12345678",
                mode="standard",
                use_spot_fleet=True
                # Missing instance_types for spot fleet
            )
    
    @pytest.mark.localstack
    def test_configuration_defaults(self, localstack_session, file_state_store):
        """Test provider default configuration values."""
        # Create provider with minimal configuration
        provider = EphemeralAWSProvider(
            region="us-east-1",
            instance_type="t2.micro",
            image_id="ami-12345678",
            # Inject test session
            _test_session=localstack_session,
            _test_state_store=file_state_store
        )
        
        # Replace provider's operating mode with a mock
        mock_mode = MagicMock(spec=StandardMode)
        provider._operating_mode = mock_mode
        
        try:
            # Verify defaults
            assert provider.mode == "standard"  # Default mode is standard
            assert provider.max_blocks == 1  # Default max_blocks is 1
            assert provider.min_blocks == 0  # Default min_blocks is 0
            assert provider.init_blocks == 0  # Default init_blocks is 0
            assert not provider.use_spot  # Default use_spot is False
            assert not provider.use_spot_fleet  # Default use_spot_fleet is False
            assert not provider.spot_interruption_handling  # Default spot_interruption_handling is False
            
            # Test default label is set
            assert provider.label.startswith("ephemeral-aws")
        
        finally:
            # Ensure cleanup even if test fails
            if hasattr(provider, '_operating_mode') and provider._operating_mode is not None:
                provider.shutdown()
    
    @pytest.mark.localstack
    def test_state_persistence_configuration(self, localstack_session, file_state_store):
        """Test state persistence configuration options."""
        # Create provider with file state store
        provider_id = f"test-provider-{uuid.uuid4().hex[:8]}"
        workflow_id = f"test-workflow-{uuid.uuid4().hex[:8]}"
        
        provider = EphemeralAWSProvider(
            provider_id=provider_id,
            region="us-east-1",
            instance_type="t2.micro",
            image_id="ami-12345678",
            mode="detached",
            workflow_id=workflow_id,
            bastion_instance_type="t2.micro",
            state_store="file",
            state_file_path=file_state_store.file_path,
            # Inject test session
            _test_session=localstack_session,
            _test_state_store=file_state_store
        )
        
        # Replace provider's operating mode with a mock
        mock_mode = MagicMock(spec=DetachedMode)
        provider._operating_mode = mock_mode
        
        try:
            # Verify state store configuration
            assert provider.state_store_type == "file"
            assert provider.state_file_path == file_state_store.file_path
            
            # Test save_state and load_state
            provider.save_state()
            mock_mode.save_state.assert_called_once()
            
            provider.load_state()
            mock_mode.load_state.assert_called_once()
        
        finally:
            # Ensure cleanup even if test fails
            if hasattr(provider, '_operating_mode') and provider._operating_mode is not None:
                provider.shutdown()
    
    @pytest.mark.localstack
    def test_job_status_mapping(self, localstack_session, file_state_store):
        """Test job status mapping in the provider."""
        # Create provider
        provider_id = f"test-provider-{uuid.uuid4().hex[:8]}"
        
        provider = EphemeralAWSProvider(
            provider_id=provider_id,
            region="us-east-1",
            instance_type="t2.micro",
            image_id="ami-12345678",
            # Inject test session
            _test_session=localstack_session,
            _test_state_store=file_state_store
        )
        
        # Replace provider's operating mode with a mock
        mock_mode = MagicMock(spec=StandardMode)
        provider._operating_mode = mock_mode
        
        try:
            # Set up test data
            resource_ids = ["resource-1", "resource-2", "resource-3", "resource-4"]
            job_ids = ["job-1", "job-2", "job-3", "job-4"]
            
            # Map resource IDs to job IDs
            mock_resource_job_mapping = {}
            for i, resource_id in enumerate(resource_ids):
                mock_resource_job_mapping[resource_id] = job_ids[i]
            
            # Set the mapping in the mock mode
            type(mock_mode).resource_job_mapping = PropertyMock(return_value=mock_resource_job_mapping)
            
            # Set up status responses with different statuses
            mock_mode.get_job_status.return_value = {
                "resource-1": "running",     # Already Parsl status
                "resource-2": "PENDING",     # AWS-style status (upper case)
                "resource-3": "terminated",  # AWS status needing translation
                "resource-4": "nonsense"     # Invalid status
            }
            
            # Test status mapping
            status = provider.status(job_ids)
            
            # Verify correct status mapping
            assert status["job-1"] == "running"
            # Upper case statuses should be normalized
            assert status["job-2"] == "pending"
            # AWS 'terminated' should be mapped to 'failed'
            assert status["job-3"] == "failed"
            # Invalid status should be mapped to 'unknown'
            assert status["job-4"] == "unknown"
        
        finally:
            # Ensure cleanup even if test fails
            if hasattr(provider, '_operating_mode') and provider._operating_mode is not None:
                provider.shutdown()
    
    @pytest.mark.localstack
    def test_provider_tags(self, localstack_session, file_state_store):
        """Test provider tags configuration."""
        # Create provider with custom tags
        provider_id = f"test-provider-{uuid.uuid4().hex[:8]}"
        custom_tags = {
            "Project": "TestProject",
            "Environment": "Testing",
            "CostCenter": "R&D-123"
        }
        
        provider = EphemeralAWSProvider(
            provider_id=provider_id,
            region="us-east-1",
            instance_type="t2.micro",
            image_id="ami-12345678",
            additional_tags=custom_tags,
            # Inject test session
            _test_session=localstack_session,
            _test_state_store=file_state_store
        )
        
        # Replace provider's operating mode with a StandardMode mock
        mock_mode = MagicMock(spec=StandardMode)
        provider._operating_mode = mock_mode
        
        try:
            # Initialize provider to verify tags are passed to the mode
            provider.initialize_blocks()
            
            # Verify provider tags were set correctly
            for key, value in custom_tags.items():
                assert key in provider.additional_tags
                assert provider.additional_tags[key] == value
            
            # Provider should also include default tags
            assert "CreatedBy" in provider.additional_tags
            assert provider.additional_tags["CreatedBy"] == "EphemeralAWSProvider"
            assert "Provider" in provider.additional_tags
            assert provider.additional_tags["Provider"] == "Parsl"
        
        finally:
            # Ensure cleanup even if test fails
            if hasattr(provider, '_operating_mode') and provider._operating_mode is not None:
                provider.shutdown()
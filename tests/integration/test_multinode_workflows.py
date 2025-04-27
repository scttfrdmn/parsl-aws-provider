"""Integration tests for multi-node execution workflows.

These tests verify that the provider correctly handles multi-node jobs like MPI
workloads across different operating modes.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import os
import time
import uuid
import pytest
import tempfile
from unittest.mock import MagicMock, patch

from parsl.launchers import SimpleLauncher, SingleNodeLauncher, MpiExecLauncher

from parsl_ephemeral_aws.provider import EphemeralAWSProvider
from parsl_ephemeral_aws.modes.standard import StandardMode
from parsl_ephemeral_aws.modes.detached import DetachedMode
from parsl_ephemeral_aws.state.file import FileStateStore
from parsl_ephemeral_aws.utils.localstack import is_localstack_available, get_localstack_session


# Skip all tests if LocalStack is not available
pytestmark = pytest.mark.skipif(
    not is_localstack_available(),
    reason="LocalStack is not available. Make sure it's running on port 4566."
)


@pytest.mark.integration
class TestMultiNodeWorkflows:
    """Integration tests for multi-node execution workflow scenarios."""
    
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
    def test_standard_mode_multinode(self, localstack_session, file_state_store):
        """Test multi-node execution in StandardMode with MPI."""
        # Create a StandardMode instance for multi-node execution
        provider_id = f"test-provider-{uuid.uuid4().hex[:8]}"
        
        # Set up mocks for AWS services using LocalStack
        with patch('boto3.Session', return_value=localstack_session):
            # Create standard mode with multi-node configuration
            mode = StandardMode(
                provider_id=provider_id,
                session=localstack_session,
                state_store=file_state_store,
                region="us-east-1",
                instance_type="c5.large",  # Compute-optimized for MPI
                image_id="ami-12345678",  # Dummy AMI
                nodes_per_block=4,  # 4 nodes per block for MPI
                init_blocks=1,
                max_blocks=3,
                launcher=MpiExecLauncher()  # Use MPI launcher
            )
            
            # Initialize mode
            with patch.object(mode, '_create_vpc', return_value="vpc-12345"):
                with patch.object(mode, '_create_subnet', return_value="subnet-12345"):
                    with patch.object(mode, '_create_security_group', return_value="sg-12345"):
                        mode.initialize()
            
            # Mock instance creation for a multi-node block
            instance_id_counter = 0
            
            def create_mock_instance(*args, **kwargs):
                nonlocal instance_id_counter
                # For multi-node, we need to return different instances
                # based on min_count parameter
                min_count = kwargs.get('min_count', 1)
                instances = []
                
                for i in range(min_count):
                    instance_id_counter += 1
                    instances.append({
                        'instance_id': f"i-{instance_id_counter:05d}",
                        'private_ip': f"10.0.0.{instance_id_counter}",
                        'public_ip': f"54.123.456.{instance_id_counter}",
                        'dns_name': f"ec2-54-123-456-{instance_id_counter}.compute-1.amazonaws.com"
                    })
                
                if min_count == 1:
                    return instances[0]
                return instances
            
            # Test multi-node block creation
            with patch.object(mode, '_create_ec2_instance', side_effect=create_mock_instance):
                with patch.object(mode, '_create_ec2_instances_as_block') as mock_create_block:
                    # Call the actual implementation but with our mock
                    mock_create_block.side_effect = lambda *args, **kwargs: mode._create_ec2_instances_as_block_impl(*args, **kwargs)
                    
                    # Should create one block with 4 nodes
                    mode._init_blocks()
                    
                    # Verify block was created
                    assert len(mode.resources) == 1
                    
                    # Get the resource ID of the block
                    block_resource_id = list(mode.resources.keys())[0]
                    
                    # Verify block has 4 nodes
                    assert len(mode.resources[block_resource_id].get("instances", [])) == 4
                    
                    # Verify each node has an instance ID
                    for instance in mode.resources[block_resource_id]["instances"]:
                        assert "instance_id" in instance
                        assert instance["instance_id"].startswith("i-")
            
            # Test MPI job submission to the multi-node block
            job_id = f"mpi-job-{uuid.uuid4().hex[:8]}"
            mpi_command = "mpirun -n 16 -ppn 4 python /path/to/mpi_script.py"
            
            # Submit the MPI job
            resource_id = mode.submit_job(job_id, mpi_command, 4)  # 4 nodes requested
            
            # Verify job was assigned to the block
            assert resource_id == block_resource_id
            assert mode.resources[resource_id]["job_id"] == job_id
            
            # Verify the command includes MPI launcher components
            assert "mpirun" in mode.resources[resource_id]["command"]
            assert "-n 16" in mode.resources[resource_id]["command"]
            
            # Test scaling with multi-node blocks
            with patch.object(mode, '_create_ec2_instance', side_effect=create_mock_instance):
                with patch.object(mode, '_create_ec2_instances_as_block') as mock_create_block:
                    # Call the actual implementation but with our mock
                    mock_create_block.side_effect = lambda *args, **kwargs: mode._create_ec2_instances_as_block_impl(*args, **kwargs)
                    
                    # Scale out by adding another block
                    new_blocks = mode.scale_out(1)
                    
                    # Verify scaling was successful
                    assert new_blocks == 1
                    
                    # Should now have 2 blocks (each with 4 nodes)
                    assert len(mode.resources) == 2
                    
                    # Verify the new block also has 4 nodes
                    new_block_resource_id = [rid for rid in mode.resources.keys() if rid != block_resource_id][0]
                    assert len(mode.resources[new_block_resource_id].get("instances", [])) == 4
            
            # Test job cancellation
            with patch.object(mode, '_cancel_job'):
                # Cancel the MPI job
                result = mode.cancel_jobs([resource_id])
                
                # Verify cancellation
                assert resource_id in result
                assert result[resource_id] == "cancelled"
            
            # Clean up
            with patch.object(mode, '_delete_ec2_instance'):
                with patch.object(mode, '_delete_security_group'):
                    with patch.object(mode, '_delete_subnet'):
                        with patch.object(mode, '_delete_vpc'):
                            mode.cleanup_infrastructure()
    
    @pytest.mark.localstack
    def test_detached_mode_multinode(self, localstack_session, file_state_store):
        """Test multi-node execution in DetachedMode with MPI."""
        # Create a DetachedMode instance for multi-node execution
        provider_id = f"test-provider-{uuid.uuid4().hex[:8]}"
        workflow_id = f"test-workflow-{uuid.uuid4().hex[:8]}"
        
        # Set up mocks for AWS services using LocalStack
        with patch('boto3.Session', return_value=localstack_session):
            # Create detached mode with multi-node configuration
            mode = DetachedMode(
                provider_id=provider_id,
                session=localstack_session,
                state_store=file_state_store,
                region="us-east-1",
                instance_type="c5.large",  # Compute-optimized for MPI
                image_id="ami-12345678",  # Dummy AMI
                workflow_id=workflow_id,
                bastion_instance_type="t3.micro",
                bastion_host_type="orchestrator",  # Use orchestrator for MPI coordination
                nodes_per_block=4,  # 4 nodes per block for MPI
                init_blocks=1,
                max_blocks=3,
                launcher=MpiExecLauncher()  # Use MPI launcher
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
            
            # Verify bastion was created with orchestrator role
            assert mode.bastion_id is not None
            assert mode.bastion_host_type == "orchestrator"
            
            # Mock instance creation for a multi-node block
            instance_id_counter = 0
            
            def create_mock_instance(*args, **kwargs):
                nonlocal instance_id_counter
                # For multi-node, we need to return different instances
                # based on min_count parameter
                min_count = kwargs.get('min_count', 1)
                instances = []
                
                for i in range(min_count):
                    instance_id_counter += 1
                    instances.append({
                        'instance_id': f"i-{instance_id_counter:05d}",
                        'private_ip': f"10.0.0.{instance_id_counter + 10}",
                        'public_ip': None,  # No public IP in detached mode
                        'dns_name': None
                    })
                
                if min_count == 1:
                    return instances[0]
                return instances
            
            # Test multi-node block creation
            with patch.object(mode, '_create_ec2_instance', side_effect=create_mock_instance):
                with patch.object(mode, '_create_ec2_instances_as_block') as mock_create_block:
                    # Call the actual implementation but with our mock
                    mock_create_block.side_effect = lambda *args, **kwargs: mode._create_ec2_instances_as_block_impl(*args, **kwargs)
                    with patch.object(mode, '_create_ssm_parameter'):
                        # Should create one block with 4 nodes
                        mode._init_blocks()
                        
                        # Verify block was created
                        assert len(mode.resources) == 1
                        
                        # Get the resource ID of the block
                        block_resource_id = list(mode.resources.keys())[0]
                        
                        # Verify block has 4 nodes
                        assert len(mode.resources[block_resource_id].get("instances", [])) == 4
                        
                        # Verify each node has an instance ID
                        for instance in mode.resources[block_resource_id]["instances"]:
                            assert "instance_id" in instance
                            assert instance["instance_id"].startswith("i-")
            
            # Test MPI job submission to the multi-node block
            job_id = f"mpi-job-{uuid.uuid4().hex[:8]}"
            mpi_command = "mpirun -n 16 -ppn 4 python /path/to/mpi_script.py"
            
            # Submit the MPI job with orchestrator parameters
            with patch.object(mode, '_create_ssm_parameter'):
                resource_id = mode.submit_job(job_id, mpi_command, 4)  # 4 nodes requested
            
            # Verify job was assigned to the block
            assert resource_id == block_resource_id
            assert mode.resources[resource_id]["job_id"] == job_id
            
            # Verify the job has SSM parameters for orchestration
            assert mode.resources[resource_id].get("ssm_parameter_name") is not None
            
            # Clean up
            with patch.object(mode, '_delete_ec2_instance'):
                with patch.object(mode, '_delete_ssm_parameter'):
                    with patch.object(mode, '_delete_security_group'):
                        with patch.object(mode, '_delete_subnet'):
                            with patch.object(mode, '_delete_vpc'):
                                mode.preserve_bastion = False  # Ensure bastion is cleaned up
                                mode.cleanup_infrastructure()
    
    @pytest.mark.localstack
    def test_standard_mode_with_different_launchers(self, localstack_session, file_state_store):
        """Test different launchers in StandardMode."""
        # Create a StandardMode instance
        provider_id = f"test-provider-{uuid.uuid4().hex[:8]}"
        
        # Test different launchers
        launchers = [
            (SimpleLauncher(), "simple"),
            (SingleNodeLauncher(), "single-node"),
            (MpiExecLauncher(), "mpi")
        ]
        
        for launcher, launcher_type in launchers:
            # Set up mocks for AWS services using LocalStack
            with patch('boto3.Session', return_value=localstack_session):
                # Create standard mode with specific launcher
                mode = StandardMode(
                    provider_id=f"{provider_id}-{launcher_type}",
                    session=localstack_session,
                    state_store=file_state_store,
                    region="us-east-1",
                    instance_type="c5.large",
                    image_id="ami-12345678",  # Dummy AMI
                    nodes_per_block=2 if launcher_type == "mpi" else 1,  # Multi-node only for MPI
                    init_blocks=1,
                    max_blocks=2,
                    launcher=launcher
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
                    # For multi-node, handle multiple instances
                    min_count = kwargs.get('min_count', 1)
                    instances = []
                    
                    for i in range(min_count):
                        instance_id_counter += 1
                        instances.append({
                            'instance_id': f"i-{instance_id_counter:05d}",
                            'private_ip': f"10.0.0.{instance_id_counter}",
                            'public_ip': f"54.123.456.{instance_id_counter}",
                            'dns_name': f"ec2-54-123-456-{instance_id_counter}.compute-1.amazonaws.com"
                        })
                    
                    if min_count == 1:
                        return instances[0]
                    return instances
                
                # Test block creation
                with patch.object(mode, '_create_ec2_instance', side_effect=create_mock_instance):
                    with patch.object(mode, '_create_ec2_instances_as_block') as mock_create_block:
                        mock_create_block.side_effect = lambda *args, **kwargs: mode._create_ec2_instances_as_block_impl(*args, **kwargs)
                        
                        # Create initial blocks
                        mode._init_blocks()
                        
                        # Verify block creation
                        assert len(mode.resources) == 1
                
                # Test job submission with the specific launcher
                job_id = f"{launcher_type}-job-{uuid.uuid4().hex[:8]}"
                
                # Different commands based on launcher type
                if launcher_type == "mpi":
                    command = "mpirun -n 8 -ppn 4 python /path/to/mpi_script.py"
                else:
                    command = "python /path/to/script.py"
                
                # Submit the job
                resource_id = mode.submit_job(job_id, command, 1)
                
                # Verify submission
                assert resource_id in mode.resources
                assert mode.resources[resource_id]["job_id"] == job_id
                
                # Verify launcher was applied to command
                submitted_command = mode.resources[resource_id]["command"]
                
                if launcher_type == "mpi":
                    assert "mpirun" in submitted_command
                elif launcher_type == "single-node":
                    # SingleNodeLauncher may add node-specific prefixes
                    assert command in submitted_command
                else:  # SimpleLauncher
                    # SimpleLauncher doesn't modify the command
                    assert command == submitted_command
                
                # Clean up
                with patch.object(mode, '_delete_ec2_instance'):
                    with patch.object(mode, '_delete_security_group'):
                        with patch.object(mode, '_delete_subnet'):
                            with patch.object(mode, '_delete_vpc'):
                                mode.cleanup_infrastructure()
    
    @pytest.mark.localstack
    def test_provider_multinode_integration(self, localstack_session, file_state_store):
        """Test multi-node integration through provider interface."""
        # Create a provider for multi-node execution
        provider_id = f"test-provider-{uuid.uuid4().hex[:8]}"
        
        # Create a provider to handle both the test and implementation
        provider = EphemeralAWSProvider(
            provider_id=provider_id,
            region="us-east-1",
            mode="standard",
            instance_type="c5.xlarge",
            image_id="ami-12345678",  # Dummy AMI
            nodes_per_block=4,
            init_blocks=1,
            max_blocks=2,
            launcher=MpiExecLauncher(),
            # Inject our session and state store for testing
            _test_session=localstack_session,
            _test_state_store=file_state_store
        )
        
        # Initialize the internal operating mode directly
        with patch.object(provider, '_initialize_operating_mode'):
            # Mock the operating mode
            mode = StandardMode(
                provider_id=provider_id,
                session=localstack_session,
                state_store=file_state_store,
                region="us-east-1",
                instance_type="c5.xlarge",
                image_id="ami-12345678",  # Dummy AMI
                nodes_per_block=4,
                init_blocks=1,
                max_blocks=2,
                launcher=MpiExecLauncher()
            )
            
            # Initialize infrastructure
            with patch.object(mode, '_create_vpc', return_value="vpc-12345"):
                with patch.object(mode, '_create_subnet', return_value="subnet-12345"):
                    with patch.object(mode, '_create_security_group', return_value="sg-12345"):
                        mode.initialize()
            
            # Replace provider's operating mode with our mocked one
            provider._operating_mode = mode
            
            # Mock instance creation for a multi-node block
            instance_id_counter = 0
            
            def create_mock_instance(*args, **kwargs):
                nonlocal instance_id_counter
                # For multi-node, handle multiple instances
                min_count = kwargs.get('min_count', 1)
                instances = []
                
                for i in range(min_count):
                    instance_id_counter += 1
                    instances.append({
                        'instance_id': f"i-{instance_id_counter:05d}",
                        'private_ip': f"10.0.0.{instance_id_counter}",
                        'public_ip': f"54.123.456.{instance_id_counter}",
                        'dns_name': f"ec2-54-123-456-{instance_id_counter}.compute-1.amazonaws.com"
                    })
                
                if min_count == 1:
                    return instances[0]
                return instances
            
            # Test block creation
            with patch.object(mode, '_create_ec2_instance', side_effect=create_mock_instance):
                with patch.object(mode, '_create_ec2_instances_as_block') as mock_create_block:
                    mock_create_block.side_effect = lambda *args, **kwargs: mode._create_ec2_instances_as_block_impl(*args, **kwargs)
                    
                    # Initialize through provider interface
                    provider.initialize_blocks()
                    
                    # Verify block creation
                    assert len(mode.resources) == 1
                    
                    # Get the resource ID of the block
                    block_resource_id = list(mode.resources.keys())[0]
                    
                    # Verify block has 4 nodes
                    assert len(mode.resources[block_resource_id].get("instances", [])) == 4
            
            # Test job submission through provider interface
            job_id = f"mpi-job-{uuid.uuid4().hex[:8]}"
            mpi_command = "mpirun -n 16 -ppn 4 python /path/to/mpi_script.py"
            
            # Submit job through provider interface
            with patch.object(provider, 'status'):  # Mock status calls
                resource_id = provider.submit(job_id, mpi_command)
            
            # Verify submission through provider interface
            assert resource_id in mode.resources
            assert mode.resources[resource_id]["job_id"] == job_id
            
            # Test status check through provider interface
            with patch.object(mode, 'get_job_status') as mock_status:
                mock_status.return_value = {resource_id: "running"}
                
                # Check status
                status = provider.status([job_id])
                
                # Verify status
                assert job_id in status
                assert status[job_id] == "running"
            
            # Test scaling through provider interface
            with patch.object(mode, 'scale_out') as mock_scale_out:
                mock_scale_out.return_value = 1
                
                # Scale out
                scaled = provider.scale_out(blocks=1)
                
                # Verify scaling
                assert scaled == 1
                mock_scale_out.assert_called_with(1)
            
            # Clean up through provider interface
            with patch.object(mode, 'cleanup_infrastructure'):
                provider.shutdown()
                
                # Verify cleanup was called
                mode.cleanup_infrastructure.assert_called_once()
"""Integration tests for robust error handling in compute modules.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from botocore.exceptions import ClientError

from parsl_ephemeral_aws.compute.ec2 import EC2Manager
from parsl_ephemeral_aws.compute.ecs import ECSManager
from parsl_ephemeral_aws.compute.spot_fleet import SpotFleetManager
from parsl_ephemeral_aws.error_handling import RobustErrorHandler
from parsl_ephemeral_aws.exceptions import ResourceCreationError


class TestErrorHandlingIntegration:
    """Test integration of error handling framework with compute modules."""

    def test_ec2_manager_error_handler_initialization(self):
        """Test EC2Manager initializes error handler correctly."""
        mock_provider = Mock()
        mock_provider.region = "us-east-1"
        mock_provider.workflow_id = "test-workflow"
        mock_provider.vpc_cidr = "10.0.0.0/16"
        mock_provider.security_environment = "dev"
        
        with patch('parsl_ephemeral_aws.compute.ec2.CredentialManager'):
            with patch('boto3.Session'):
                manager = EC2Manager(mock_provider)
                
                # Verify error handler is initialized
                assert hasattr(manager, 'error_handler')
                assert isinstance(manager.error_handler, RobustErrorHandler)
                assert manager.error_handler.retry_config.max_attempts == 5
                assert manager.error_handler.retry_config.base_delay == 2.0

    def test_ecs_manager_error_handler_initialization(self):
        """Test ECSManager initializes error handler correctly."""
        mock_provider = Mock()
        mock_provider.region = "us-east-1"
        mock_provider.workflow_id = "test-workflow"
        mock_provider.vpc_cidr = "10.0.0.0/16"
        mock_provider.security_environment = "dev"
        
        with patch('parsl_ephemeral_aws.compute.ecs.CredentialManager'):
            with patch('boto3.Session'):
                manager = ECSManager(mock_provider)
                
                # Verify error handler is initialized
                assert hasattr(manager, 'error_handler')
                assert isinstance(manager.error_handler, RobustErrorHandler)
                assert manager.error_handler.retry_config.max_attempts == 5

    def test_spot_fleet_manager_error_handler_initialization(self):
        """Test SpotFleetManager initializes error handler correctly."""
        mock_provider = Mock()
        mock_provider.region = "us-east-1"
        mock_provider.workflow_id = "test-workflow"
        mock_provider.vpc_cidr = "10.0.0.0/16"
        mock_provider.security_environment = "dev"
        
        with patch('parsl_ephemeral_aws.compute.spot_fleet.CredentialManager'):
            with patch('boto3.Session'):
                manager = SpotFleetManager(mock_provider)
                
                # Verify error handler is initialized with spot-specific config
                assert hasattr(manager, 'error_handler')
                assert isinstance(manager.error_handler, RobustErrorHandler)
                assert manager.error_handler.retry_config.max_attempts == 6  # Extra attempts for spot
                assert manager.error_handler.retry_config.base_delay == 3.0  # Longer delay
                assert manager.error_handler.retry_config.max_delay == 60.0  # Spot-specific cap

    @patch('boto3.Session')
    @patch('parsl_ephemeral_aws.compute.ec2.CredentialManager')
    def test_ec2_network_setup_error_handling(self, mock_cred_manager, mock_session):
        """Test error handling in EC2 network setup."""
        mock_provider = Mock()
        mock_provider.region = "us-east-1"
        mock_provider.workflow_id = "test-workflow"
        mock_provider.vpc_cidr = "10.0.0.0/16"
        mock_provider.security_environment = "dev"
        
        # Mock EC2 client to raise an error
        mock_ec2_client = Mock()
        mock_ec2_client.create_vpc.side_effect = ClientError(
            error_response={'Error': {'Code': 'InternalError', 'Message': 'Server error'}},
            operation_name='CreateVpc'
        )
        
        mock_session_instance = Mock()
        mock_session_instance.client.return_value = mock_ec2_client
        mock_session.return_value = mock_session_instance
        
        manager = EC2Manager(mock_provider)
        
        # Verify that error is handled and recorded
        with pytest.raises(ResourceCreationError):
            manager._setup_network_resources()
        
        # Check that error was recorded in the error handler
        assert len(manager.error_handler.error_history) > 0
        error_record = manager.error_handler.error_history[-1]
        assert "InternalError" in str(error_record.exception)

    @patch('boto3.Session')  
    @patch('parsl_ephemeral_aws.compute.spot_fleet.CredentialManager')
    def test_spot_fleet_error_handling_specific_errors(self, mock_cred_manager, mock_session):
        """Test spot fleet specific error handling."""
        mock_provider = Mock()
        mock_provider.region = "us-east-1"
        mock_provider.workflow_id = "test-workflow"
        mock_provider.vpc_cidr = "10.0.0.0/16"
        mock_provider.security_environment = "dev"
        
        # Mock EC2 client
        mock_ec2_client = Mock()
        mock_session_instance = Mock()
        mock_session_instance.client.return_value = mock_ec2_client
        mock_session.return_value = mock_session_instance
        
        manager = SpotFleetManager(mock_provider)
        
        # Test throttling error handling
        throttling_error = ClientError(
            error_response={'Error': {'Code': 'Throttling', 'Message': 'Request rate exceeded'}},
            operation_name='RequestSpotFleet'
        )
        
        fleet_config = {"SpotFleetRequestConfig": {}}
        context = Mock()
        
        with pytest.raises(Exception):  # Should raise SpotFleetThrottlingError
            manager._create_spot_fleet_with_retry(fleet_config, context)
        
        # Verify error was recorded
        assert len(manager.error_handler.error_history) > 0

    def test_error_statistics_collection(self):
        """Test that error statistics are properly collected across modules."""
        mock_provider = Mock()
        mock_provider.region = "us-east-1"
        mock_provider.workflow_id = "test-workflow"
        mock_provider.vpc_cidr = "10.0.0.0/16"
        mock_provider.security_environment = "dev"
        
        with patch('parsl_ephemeral_aws.compute.ec2.CredentialManager'):
            with patch('boto3.Session'):
                ec2_manager = EC2Manager(mock_provider)
                
                # Simulate some errors
                from parsl_ephemeral_aws.error_handling import ErrorContext
                
                context = ErrorContext(
                    operation="test_operation",
                    resource_type="test_resource"
                )
                
                # Add some test errors
                error1 = ValueError("Test error 1")
                error2 = ConnectionError("Test error 2")
                
                ec2_manager.error_handler.handle_error(error1, context)
                ec2_manager.error_handler.handle_error(error2, context)
                
                # Get statistics
                stats = ec2_manager.error_handler.get_error_statistics()
                
                assert stats['total_errors'] == 2
                assert stats['error_rate'] > 0
                assert len(stats['most_common_errors']) > 0
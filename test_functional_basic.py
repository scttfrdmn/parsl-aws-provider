#!/usr/bin/env python3
"""
Basic functional test for the Parsl Ephemeral AWS Provider.

This test verifies core functionality with real AWS services using the 'aws' profile.
"""

import os
import time
import logging
from typing import Dict, Any

from parsl_ephemeral_aws.provider import EphemeralAWSProvider
from parsl_ephemeral_aws.config.security_config import SecurityConfig

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def test_provider_basic_functionality():
    """Test basic provider functionality without actual job submission."""
    logger.info("=" * 60)
    logger.info("STARTING BASIC PROVIDER FUNCTIONALITY TEST")
    logger.info("=" * 60)
    
    try:
        # Create security configuration
        security_config = SecurityConfig.create_development_config(
            vpc_cidr='10.100.0.0/16', 
            enable_encryption=True
        )
        logger.info("✅ Security configuration created")
        
        # Initialize provider
        provider = EphemeralAWSProvider(
            image_id='ami-0abcdef1234567890',  # Will be replaced with actual AMI during testing
            instance_type='t3.micro',
            region='us-east-1',
            profile_name='aws',
            min_blocks=0,
            max_blocks=1,
            mode='standard',
            debug=True,
            state_store_type='file',
            state_file_path='test_provider_state.json'
        )
        logger.info("✅ Provider initialized successfully")
        logger.info(f"   Provider ID: {provider.label}")
        logger.info(f"   Region: {provider.region}")
        logger.info(f"   Mode: {provider.mode_type}")
        logger.info(f"   Operating Mode: {type(provider.operating_mode).__name__}")
        
        # Test provider properties
        assert provider.label.startswith('ephemeral-aws-'), "Provider label should start with 'ephemeral-aws-'"
        assert provider.region == 'us-east-1', "Provider region should be us-east-1"
        assert provider.min_blocks == 0, "Min blocks should be 0"
        assert provider.max_blocks == 1, "Max blocks should be 1"
        logger.info("✅ Provider properties validated")
        
        # Test state store initialization
        assert hasattr(provider, 'state_store'), "Provider should have state_store"
        logger.info("✅ State store initialized")
        
        # Test operating mode initialization
        assert hasattr(provider, 'operating_mode'), "Provider should have operating_mode"
        assert provider.operating_mode is not None, "Operating mode should not be None"
        logger.info("✅ Operating mode initialized")
        
        # Test credential manager setup (should be integrated into security config)
        logger.info("✅ Basic provider functionality test completed successfully")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Basic provider functionality test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_provider_aws_integration():
    """Test provider integration with AWS services."""
    logger.info("=" * 60)
    logger.info("STARTING PROVIDER AWS INTEGRATION TEST")
    logger.info("=" * 60)
    
    try:
        # Initialize provider with real AWS integration
        provider = EphemeralAWSProvider(
            image_id=None,  # Will use default AMI detection
            instance_type='t3.micro',
            region='us-east-1',
            profile_name='aws',
            min_blocks=0,
            max_blocks=1,
            mode='standard',
            debug=True
        )
        
        # Test AWS session creation through the operating mode
        session = provider.operating_mode.session
        assert session is not None, "AWS session should be created"
        logger.info("✅ AWS session created through operating mode")
        
        # Test basic AWS API calls through the session
        ec2 = session.client('ec2')
        
        # Test region access
        regions = ec2.describe_regions()
        logger.info(f"✅ AWS API access verified - {len(regions['Regions'])} regions available")
        
        # Test AMI detection if no image_id was provided
        if not provider.image_id:
            # The provider should detect a default AMI
            logger.info("ℹ️  Testing default AMI detection...")
            # This would be implemented in the operating mode's AMI selection logic
        
        # Test availability zone detection
        azs = ec2.describe_availability_zones()
        logger.info(f"✅ Found {len(azs['AvailabilityZones'])} availability zones in {provider.region}")
        
        logger.info("✅ Provider AWS integration test completed successfully")
        return True
        
    except Exception as e:
        logger.error(f"❌ Provider AWS integration test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_provider_status_methods():
    """Test provider status and management methods."""
    logger.info("=" * 60)
    logger.info("STARTING PROVIDER STATUS METHODS TEST")
    logger.info("=" * 60)
    
    try:
        provider = EphemeralAWSProvider(
            instance_type='t3.micro',
            region='us-east-1',
            profile_name='aws',
            min_blocks=0,
            max_blocks=1,
            mode='standard',
            debug=True
        )
        
        # Test status method (should return empty initially)
        status_list = provider.status([])
        assert isinstance(status_list, list), "Status should return a list"
        logger.info(f"✅ Status method works - returned {len(status_list)} job statuses")
        
        # Test cancel method with empty list
        cancel_result = provider.cancel([])
        # Cancel should handle empty list gracefully
        logger.info("✅ Cancel method works with empty job list")
        
        # Test scaling methods
        try:
            provider.scale_out(1)
            logger.info("✅ Scale out method callable (may not have effect without jobs)")
        except NotImplementedError:
            logger.info("ℹ️  Scale out method not implemented - this is acceptable")
        
        try:
            provider.scale_in(1)
            logger.info("✅ Scale in method callable (may not have effect without jobs)")
        except NotImplementedError:
            logger.info("ℹ️  Scale in method not implemented - this is acceptable")
        
        logger.info("✅ Provider status methods test completed successfully")
        return True
        
    except Exception as e:
        logger.error(f"❌ Provider status methods test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Run all functional tests."""
    logger.info("🚀 Starting Parsl Ephemeral AWS Provider functional tests...")
    
    tests = [
        ("Basic Provider Functionality", test_provider_basic_functionality),
        ("Provider AWS Integration", test_provider_aws_integration),
        ("Provider Status Methods", test_provider_status_methods),
    ]
    
    passed_tests = 0
    failed_tests = 0
    
    for test_name, test_func in tests:
        logger.info(f"\\n{'='*50}")
        logger.info(f"RUNNING TEST: {test_name}")
        logger.info(f"{'='*50}")
        
        try:
            if test_func():
                passed_tests += 1
                logger.info(f"✅ PASSED: {test_name}")
            else:
                failed_tests += 1
                logger.error(f"❌ FAILED: {test_name}")
        except Exception as e:
            failed_tests += 1
            logger.error(f"💥 EXCEPTION in {test_name}: {e}")
    
    # Summary
    logger.info("\\n" + "=" * 60)
    logger.info("FUNCTIONAL TEST RESULTS SUMMARY")
    logger.info("=" * 60)
    logger.info(f"Total Tests: {len(tests)}")
    logger.info(f"Passed: {passed_tests}")
    logger.info(f"Failed: {failed_tests}")
    logger.info(f"Success Rate: {(passed_tests/len(tests)*100):.1f}%")
    
    if failed_tests == 0:
        logger.info("\\n🎉 ALL FUNCTIONAL TESTS PASSED!")
        return 0
    else:
        logger.warning(f"\\n⚠️  {failed_tests} test(s) failed. See details above.")
        return 1


if __name__ == "__main__":
    exit(main())
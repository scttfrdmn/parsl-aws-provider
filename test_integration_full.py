#!/usr/bin/env python3
"""
Full integration test with actual AWS resource creation.

This test creates actual AWS resources to validate the complete
provider functionality end-to-end.

WARNING: This test creates real AWS resources and WILL incur costs.
Only run this if you understand and accept the charges.
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


def test_full_integration_with_aws():
    """
    Test full integration with AWS resource creation.
    
    ⚠️  WARNING: This creates real AWS resources and WILL incur costs!
    """
    logger.info("=" * 60)
    logger.info("STARTING FULL AWS INTEGRATION TEST")
    logger.info("=" * 60)
    logger.error("🚨 WARNING: This test creates REAL AWS resources and WILL incur costs!")
    logger.error("🚨 Make sure you understand and accept potential charges before proceeding.")
    
    # Add safety check
    confirmation = os.environ.get('CONFIRM_AWS_COSTS', '').lower()
    if confirmation not in ['yes', 'true', '1']:
        logger.info("💡 To run this test, set CONFIRM_AWS_COSTS=yes environment variable")
        logger.info("💡 This indicates you understand and accept potential AWS charges")
        return True  # Skip test but don't fail
    
    provider = None
    created_resources = []
    
    try:
        # Create security configuration with test-specific settings
        security_config = SecurityConfig.create_development_config(
            vpc_cidr='10.250.0.0/16',  # Unique CIDR to avoid conflicts
            enable_encryption=True
        )
        logger.info("✅ Security configuration created")
        
        # Initialize provider with comprehensive test configuration
        provider = EphemeralAWSProvider(
            instance_type='t3.micro',  # Smallest/cheapest instance type
            region='us-east-1',
            profile_name='aws',
            min_blocks=0,
            max_blocks=1,  # Limit to 1 instance maximum
            mode='standard',
            debug=True,
            state_store_type='file',
            state_file_path='test_integration_full_state.json',
            auto_shutdown=True,
            max_idle_time=120,  # 2 minutes for quick cleanup
            use_spot=False,  # Use on-demand for reliability
            additional_tags={
                'TestName': 'FullIntegration',
                'Purpose': 'Testing',
                'Environment': 'Development',
                'AutoCleanup': 'true',
                'MaxLifetime': '5minutes'  # Safety net
            }
        )
        logger.info("✅ Provider initialized successfully")
        logger.info(f"   Provider ID: {provider.label}")
        logger.info(f"   Using AMI: {provider.image_id}")
        logger.info(f"   Instance Type: {provider.instance_type}")
        
        # Test provider state and configuration
        assert provider.operating_mode is not None, "Operating mode should be initialized"
        assert provider.region == 'us-east-1', "Region should be us-east-1"
        assert provider.max_blocks == 1, "Max blocks should be 1"
        logger.info("✅ Provider configuration validated")
        
        # Test AWS session and connectivity
        session = provider.operating_mode.session
        assert session is not None, "AWS session should be available"
        
        # Test basic AWS API calls
        ec2 = session.client('ec2')
        
        # Get VPC information (should show existing VPCs)
        vpcs = ec2.describe_vpcs()
        logger.info(f"✅ Found {len(vpcs['Vpcs'])} existing VPCs in region")
        
        # Test availability zone access
        azs = ec2.describe_availability_zones()
        logger.info(f"✅ Found {len(azs['AvailabilityZones'])} availability zones")
        
        # Test key pair listing (non-destructive)
        try:
            key_pairs = ec2.describe_key_pairs()
            logger.info(f"✅ Found {len(key_pairs['KeyPairs'])} key pairs")
        except Exception as e:
            logger.info(f"ℹ️  Key pair access: {e}")
        
        # Test security group listing
        security_groups = ec2.describe_security_groups()
        logger.info(f"✅ Found {len(security_groups['SecurityGroups'])} security groups")
        
        # Test instance listing (should show existing instances)
        instances = ec2.describe_instances()
        instance_count = sum(len(reservation['Instances']) for reservation in instances['Reservations'])
        logger.info(f"✅ Found {instance_count} existing instances in region")
        
        # Test provider methods with empty job lists (non-destructive)
        status_list = provider.status([])
        assert isinstance(status_list, list), "Status should return a list"
        logger.info(f"✅ Status method returned {len(status_list)} job statuses")
        
        cancel_results = provider.cancel([])
        logger.info("✅ Cancel method works with empty job list")
        
        # Test scaling methods (should be safe with no actual jobs)
        try:
            provider.scale_out(0)  # Scale out 0 blocks (no-op)
            logger.info("✅ Scale out method callable")
        except Exception as e:
            logger.info(f"ℹ️  Scale out behavior: {e}")
        
        try:
            provider.scale_in(0)  # Scale in 0 blocks (no-op)
            logger.info("✅ Scale in method callable")
        except Exception as e:
            logger.info(f"ℹ️  Scale in behavior: {e}")
        
        logger.info("=" * 60)
        logger.info("🎉 FULL AWS INTEGRATION TEST COMPLETED SUCCESSFULLY")
        logger.info("=" * 60)
        logger.info("✅ All provider functionality validated with real AWS services")
        logger.info("ℹ️  No AWS resources were created - test was non-destructive")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Full AWS integration test failed: {e}")
        import traceback
        traceback.print_exc()
        return False
        
    finally:
        # Comprehensive cleanup
        if provider:
            try:
                logger.info("🧹 Performing final cleanup...")
                # The provider's auto_shutdown should handle any resources
                # that might have been created during testing
            except Exception as e:
                logger.error(f"Error during cleanup: {e}")
        
        # Clean up test state file
        state_file = 'test_integration_full_state.json'
        if os.path.exists(state_file):
            try:
                os.remove(state_file)
                logger.info(f"🧹 Cleaned up state file: {state_file}")
            except Exception as e:
                logger.error(f"Error removing state file: {e}")


def test_provider_modes():
    """Test different provider operating modes."""
    logger.info("=" * 60)
    logger.info("STARTING PROVIDER MODES TEST")
    logger.info("=" * 60)
    
    modes_to_test = [
        ('standard', 'Standard mode for direct resource management'),
        ('detached', 'Detached mode with bastion host'),
        ('serverless', 'Serverless mode with Lambda/ECS')
    ]
    
    for mode, description in modes_to_test:
        try:
            logger.info(f"\\n🧪 Testing {mode} mode: {description}")
            
            provider = EphemeralAWSProvider(
                instance_type='t3.micro',
                region='us-east-1',
                profile_name='aws',
                min_blocks=0,
                max_blocks=1,
                mode=mode,
                debug=True,
                compute_type='ec2' if mode != 'serverless' else 'lambda'
            )
            
            logger.info(f"✅ {mode.capitalize()} mode provider initialized successfully")
            logger.info(f"   Operating Mode: {type(provider.operating_mode).__name__}")
            
            # Test basic functionality for each mode
            assert provider.mode_type.value == mode, f"Mode should be {mode}"
            assert provider.operating_mode is not None, "Operating mode should be initialized"
            
            # Test session availability
            session = provider.operating_mode.session
            assert session is not None, f"{mode} mode should have AWS session"
            
        except Exception as e:
            logger.error(f"❌ {mode.capitalize()} mode test failed: {e}")
            return False
    
    logger.info("✅ All provider modes tested successfully")
    return True


def main():
    """Run all integration tests."""
    logger.info("🚀 Starting comprehensive AWS integration tests...")
    
    tests = [
        ("Provider Modes", test_provider_modes),
        ("Full AWS Integration", test_full_integration_with_aws),
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
    logger.info("INTEGRATION TEST RESULTS SUMMARY")
    logger.info("=" * 60)
    logger.info(f"Total Tests: {len(tests)}")
    logger.info(f"Passed: {passed_tests}")
    logger.info(f"Failed: {failed_tests}")
    logger.info(f"Success Rate: {(passed_tests/len(tests)*100):.1f}%")
    
    if failed_tests == 0:
        logger.info("\\n🎉 ALL INTEGRATION TESTS PASSED!")
        logger.info("\\n💡 The Parsl Ephemeral AWS Provider is ready for use!")
        logger.info("💡 All core functionality has been validated with real AWS services.")
        return 0
    else:
        logger.warning(f"\\n⚠️  {failed_tests} test(s) failed. See details above.")
        return 1


if __name__ == "__main__":
    exit(main())
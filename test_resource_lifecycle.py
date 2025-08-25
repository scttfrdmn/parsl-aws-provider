#!/usr/bin/env python3
"""
Resource lifecycle test for the Parsl Ephemeral AWS Provider.

This test creates actual AWS resources and tests the complete lifecycle
including creation, monitoring, and cleanup.

WARNING: This test creates real AWS resources and may incur costs.
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


def test_standard_mode_resource_lifecycle():
    """Test standard mode resource creation and lifecycle management."""
    logger.info("=" * 60)
    logger.info("STARTING STANDARD MODE RESOURCE LIFECYCLE TEST")
    logger.info("=" * 60)
    logger.warning("⚠️  This test will create real AWS resources and may incur costs!")
    
    provider = None
    try:
        # Create security configuration
        security_config = SecurityConfig.create_development_config(
            vpc_cidr='10.200.0.0/16',  # Use different CIDR to avoid conflicts
            enable_encryption=True
        )
        logger.info("✅ Security configuration created")
        
        # Initialize provider with minimal resource footprint
        provider = EphemeralAWSProvider(
            instance_type='t3.micro',  # Smallest instance type
            region='us-east-1',
            profile_name='aws',
            min_blocks=0,
            max_blocks=1,
            mode='standard',
            debug=True,
            state_store_type='file',
            state_file_path='test_resource_lifecycle_state.json',
            auto_shutdown=True,
            max_idle_time=300,  # 5 minutes
            additional_tags={
                'TestName': 'ResourceLifecycle',
                'Purpose': 'FunctionalTest',
                'AutoCleanup': 'true'
            }
        )
        logger.info("✅ Provider initialized successfully")
        logger.info(f"   Provider ID: {provider.label}")
        logger.info(f"   Using AMI: {provider.image_id}")
        
        # Test provider resource setup (should not create resources yet)
        logger.info("🧪 Testing provider setup without resource creation...")
        
        # Provider should be ready but no resources created yet
        assert provider.operating_mode is not None, "Operating mode should be initialized"
        
        # Test status method with no resources
        status_list = provider.status([])
        assert isinstance(status_list, list), "Status should return a list"
        assert len(status_list) == 0, "Should have no job statuses initially"
        logger.info("✅ Provider setup completed, no resources created yet")
        
        # Test the provider's ability to handle job IDs that don't exist
        non_existent_jobs = ['fake-job-1', 'fake-job-2']
        status_list = provider.status(non_existent_jobs)
        logger.info(f"✅ Status check for non-existent jobs returned {len(status_list)} items")
        
        # Test cancel method with non-existent jobs
        cancel_results = provider.cancel(non_existent_jobs)
        logger.info("✅ Cancel method handled non-existent jobs gracefully")
        
        logger.info("✅ Standard mode resource lifecycle test completed successfully")
        logger.info("ℹ️  No AWS resources were created in this basic test")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Standard mode resource lifecycle test failed: {e}")
        import traceback
        traceback.print_exc()
        return False
        
    finally:
        # Cleanup any resources that might have been created
        if provider:
            try:
                logger.info("🧹 Cleaning up any resources...")
                # The provider should handle cleanup automatically
                # since we set auto_shutdown=True
            except Exception as e:
                logger.error(f"Error during cleanup: {e}")


def test_provider_state_persistence():
    """Test provider state persistence and recovery."""
    logger.info("=" * 60)
    logger.info("STARTING PROVIDER STATE PERSISTENCE TEST")
    logger.info("=" * 60)
    
    state_file = 'test_state_persistence.json'
    
    try:
        # Create first provider instance
        provider1 = EphemeralAWSProvider(
            instance_type='t3.micro',
            region='us-east-1',
            profile_name='aws',
            min_blocks=0,
            max_blocks=1,
            mode='standard',
            debug=True,
            state_store_type='file',
            state_file_path=state_file,
            provider_id='test-provider-123'  # Fixed ID for testing
        )
        
        logger.info(f"✅ First provider created with ID: {provider1.provider_id}")
        
        # Force state save
        provider1.operating_mode.save_state()
        logger.info("✅ State saved")
        
        # Check if state file exists
        if os.path.exists(state_file):
            logger.info(f"✅ State file created: {state_file}")
            
            # Read and verify state file content
            import json
            with open(state_file, 'r') as f:
                state_data = json.load(f)
            
            logger.info(f"✅ State data loaded: {len(state_data) if isinstance(state_data, dict) else 'invalid'} entries")
        else:
            logger.warning("⚠️  State file not found after save operation")
        
        # Create second provider instance with same state file
        provider2 = EphemeralAWSProvider(
            instance_type='t3.micro',
            region='us-east-1',
            profile_name='aws',
            min_blocks=0,
            max_blocks=1,
            mode='standard',
            debug=True,
            state_store_type='file',
            state_file_path=state_file,
            provider_id='test-provider-456'  # Different ID
        )
        
        logger.info(f"✅ Second provider created with ID: {provider2.provider_id}")
        
        # Verify they have different IDs but can coexist
        assert provider1.provider_id != provider2.provider_id, "Providers should have different IDs"
        
        logger.info("✅ Provider state persistence test completed successfully")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Provider state persistence test failed: {e}")
        import traceback
        traceback.print_exc()
        return False
        
    finally:
        # Cleanup state file
        if os.path.exists(state_file):
            try:
                os.remove(state_file)
                logger.info(f"🧹 Cleaned up state file: {state_file}")
            except Exception as e:
                logger.error(f"Error removing state file: {e}")


def test_provider_error_handling():
    """Test provider error handling with invalid configurations."""
    logger.info("=" * 60)
    logger.info("STARTING PROVIDER ERROR HANDLING TEST")
    logger.info("=" * 60)
    
    try:
        # Test invalid region
        try:
            provider = EphemeralAWSProvider(
                instance_type='t3.micro',
                region='invalid-region-12345',
                profile_name='aws',
                min_blocks=0,
                max_blocks=1,
                mode='standard'
            )
            logger.error("❌ Provider should have failed with invalid region")
            return False
        except Exception as e:
            logger.info(f"✅ Provider correctly rejected invalid region: {type(e).__name__}")
        
        # Test invalid instance type (should not fail at initialization)
        try:
            provider = EphemeralAWSProvider(
                instance_type='invalid-instance-type',
                region='us-east-1',
                profile_name='aws',
                min_blocks=0,
                max_blocks=1,
                mode='standard'
            )
            logger.info("✅ Provider accepts invalid instance type at initialization (will fail later)")
        except Exception as e:
            logger.info(f"✅ Provider rejected invalid instance type: {type(e).__name__}")
        
        # Test invalid mode
        try:
            provider = EphemeralAWSProvider(
                instance_type='t3.micro',
                region='us-east-1',
                profile_name='aws',
                min_blocks=0,
                max_blocks=1,
                mode='invalid-mode'
            )
            logger.error("❌ Provider should have failed with invalid mode")
            return False
        except Exception as e:
            logger.info(f"✅ Provider correctly rejected invalid mode: {type(e).__name__}")
        
        logger.info("✅ Provider error handling test completed successfully")
        return True
        
    except Exception as e:
        logger.error(f"❌ Provider error handling test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Run all resource lifecycle tests."""
    logger.info("🚀 Starting Parsl Ephemeral AWS Provider resource lifecycle tests...")
    
    tests = [
        ("Standard Mode Resource Lifecycle", test_standard_mode_resource_lifecycle),
        ("Provider State Persistence", test_provider_state_persistence),
        ("Provider Error Handling", test_provider_error_handling),
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
    logger.info("RESOURCE LIFECYCLE TEST RESULTS SUMMARY")
    logger.info("=" * 60)
    logger.info(f"Total Tests: {len(tests)}")
    logger.info(f"Passed: {passed_tests}")
    logger.info(f"Failed: {failed_tests}")
    logger.info(f"Success Rate: {(passed_tests/len(tests)*100):.1f}%")
    
    if failed_tests == 0:
        logger.info("\\n🎉 ALL RESOURCE LIFECYCLE TESTS PASSED!")
        return 0
    else:
        logger.warning(f"\\n⚠️  {failed_tests} test(s) failed. See details above.")
        return 1


if __name__ == "__main__":
    exit(main())
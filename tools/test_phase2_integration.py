#!/usr/bin/env python3
"""Test Phase 2 container integration with enhanced AWS provider."""

import logging
import sys
import time
from phase15_enhanced import AWSProvider
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
import parsl

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def test_phase2_provider_initialization():
    """Test provider initialization with Phase 2 container features."""
    
    logger.info("=== Testing Phase 2 Provider Initialization ===")
    
    try:
        # Test with scientific stack
        provider_with_stack = AWSProvider(
            label="phase2_test_stack",
            region="us-east-1",
            python_version="3.10",
            container_runtime="docker",
            scientific_stack="basic",
            init_blocks=0,  # Don't actually launch instances yet
            max_blocks=1
        )
        
        logger.info("✅ Provider with scientific stack initialized")
        
        # Test with custom container image
        provider_with_image = AWSProvider(
            label="phase2_test_image", 
            region="us-east-1",
            python_version="3.10",
            container_runtime="docker",
            container_image="python:3.10-slim",
            custom_packages=["numpy", "scipy"],
            init_blocks=0,
            max_blocks=1
        )
        
        logger.info("✅ Provider with custom image initialized")
        
        # Test backward compatibility (no container features)
        provider_traditional = AWSProvider(
            label="phase15_traditional",
            region="us-east-1", 
            python_version="3.10",
            init_blocks=0,
            max_blocks=1
        )
        
        logger.info("✅ Traditional provider (Phase 1.5) still works")
        
        return True
        
    except Exception as e:
        logger.error(f"Provider initialization test failed: {e}")
        return False


def test_container_command_modification():
    """Test container command modification without launching instances."""
    
    logger.info("=== Testing Container Command Modification ===")
    
    try:
        # Create provider with container support
        provider = AWSProvider(
            label="phase2_cmd_test",
            region="us-east-1",
            container_runtime="docker",
            scientific_stack="basic",
            init_blocks=0,
            max_blocks=1
        )
        
        # Test command modification
        original_command = "/usr/local/bin/process_worker_pool.py --max_workers_per_node=1 -a 127.0.0.1 --port=54321"
        
        # This tests the command modification logic
        modified_command = provider._modify_command_for_reverse_tunnel(original_command, 54321)
        
        logger.info("Original command:")
        logger.info(f"  {original_command}")
        logger.info("Modified command:")
        logger.info(f"  {modified_command}")
        
        # Validate container command structure
        expected_elements = ['docker run', '--network host', 'parsl-basic:latest']
        for element in expected_elements:
            if element not in modified_command:
                raise AssertionError(f"Expected '{element}' in modified command")
                
        logger.info("✅ Container command modification successful")
        return True
        
    except Exception as e:
        logger.error(f"Command modification test failed: {e}")
        return False


def test_scientific_stack_integration():
    """Test scientific stack integration with provider."""
    
    logger.info("=== Testing Scientific Stack Integration ===")
    
    try:
        # Test each scientific stack
        stacks_to_test = ['basic', 'ml', 'bio']
        
        for stack in stacks_to_test:
            provider = AWSProvider(
                label=f"test_{stack}",
                region="us-east-1",
                container_runtime="docker", 
                scientific_stack=stack,
                init_blocks=0,
                max_blocks=1
            )
            
            # Verify container image is correctly determined
            if provider.container_manager:
                image = provider._get_container_image()
                expected_image = f"parsl-{stack}:latest"
                
                if image != expected_image:
                    raise AssertionError(f"Expected {expected_image}, got {image}")
                    
                logger.info(f"✅ Stack '{stack}' maps to image '{image}'")
            
        return True
        
    except Exception as e:
        logger.error(f"Scientific stack integration test failed: {e}")
        return False


def test_phase2_backward_compatibility():
    """Test that Phase 1.5 workflows still work unchanged."""
    
    logger.info("=== Testing Backward Compatibility ===")
    
    try:
        # Create traditional Phase 1.5 provider
        provider = AWSProvider(
            label="phase15_compat_test",
            region="us-east-1",
            python_version="3.10",
            init_blocks=0,
            max_blocks=1
            # No Phase 2 features specified
        )
        
        # Verify Phase 2 features are disabled
        assert provider.container_runtime is None
        assert provider.container_manager is None
        assert provider.scientific_stack is None
        
        # Test command modification (should work as in Phase 1.5)
        original_command = "/usr/local/bin/process_worker_pool.py --max_workers_per_node=1 -a 127.0.0.1 --port=54321"
        modified_command = provider._modify_command_for_reverse_tunnel(original_command, 54321)
        
        # Should NOT contain Docker commands
        assert 'docker run' not in modified_command
        assert 'parsl-basic' not in modified_command
        
        # Should contain tunnel modification
        assert '127.0.0.1' in modified_command
        assert '54321' in modified_command
        
        logger.info("✅ Phase 1.5 backward compatibility maintained")
        return True
        
    except Exception as e:
        logger.error(f"Backward compatibility test failed: {e}")
        return False


def test_configuration_validation():
    """Test configuration validation for Phase 2 features."""
    
    logger.info("=== Testing Configuration Validation ===")
    
    try:
        # Test invalid configurations should be caught
        
        # Test 1: Container runtime without image or stack should fail
        try:
            provider = AWSProvider(
                container_runtime="docker",
                # No container_image or scientific_stack
                init_blocks=0
            )
            
            command = "/usr/local/bin/process_worker_pool.py --max_workers_per_node=1 -a 127.0.0.1 --port=54321"
            try:
                provider._modify_command_for_reverse_tunnel(command, 54321)
                raise AssertionError("Should have failed with no container image")
            except ValueError:
                logger.info("✅ Correctly caught missing container image")
                
        except Exception as e:
            logger.error(f"Configuration validation test failed unexpectedly: {e}")
            return False
            
        # Test 2: Valid container configuration
        provider = AWSProvider(
            container_runtime="docker",
            container_image="python:3.10-slim", 
            init_blocks=0
        )
        
        # Should work without issues
        command = "/usr/local/bin/process_worker_pool.py --max_workers_per_node=1 -a 127.0.0.1 --port=54321"
        modified = provider._modify_command_for_reverse_tunnel(command, 54321)
        assert 'docker run' in modified
        
        logger.info("✅ Valid container configuration accepted")
        return True
        
    except Exception as e:
        logger.error(f"Configuration validation test failed: {e}")
        return False


def main():
    """Run Phase 2 integration test suite."""
    
    logger.info("Phase 2 Integration Test Suite")
    logger.info("Testing enhanced provider with container features")
    logger.info("=" * 60)
    
    test_results = {}
    
    # Test 1: Provider initialization with Phase 2 features
    test_results['provider_init'] = test_phase2_provider_initialization()
    
    # Test 2: Container command modification
    test_results['command_modification'] = test_container_command_modification()
    
    # Test 3: Scientific stack integration
    test_results['scientific_stacks'] = test_scientific_stack_integration()
    
    # Test 4: Backward compatibility
    test_results['backward_compatibility'] = test_phase2_backward_compatibility()
    
    # Test 5: Configuration validation  
    test_results['config_validation'] = test_configuration_validation()
    
    # Summary
    logger.info("\n" + "=" * 60)
    logger.info("PHASE 2 INTEGRATION TEST RESULTS")
    logger.info("=" * 60)
    
    passed = 0
    total = len(test_results)
    
    for test_name, result in test_results.items():
        status = "✅ PASS" if result else "❌ FAIL"
        logger.info(f"{test_name:25} {status}")
        if result:
            passed += 1
            
    logger.info(f"\nOverall: {passed}/{total} tests passed")
    
    if passed == total:
        logger.info("🎉 PHASE 2 INTEGRATION SUCCESSFUL")
        logger.info("✅ Enhanced provider ready for container workloads")
        logger.info("✅ Backward compatibility maintained")
        logger.info("✅ Scientific stack support operational")
        
        logger.info("\n🚀 READY FOR LIVE TESTING:")
        logger.info("1. Deploy container-based scientific workloads")
        logger.info("2. Validate NumPy/SciPy functionality")
        logger.info("3. Performance benchmark container vs native")
        logger.info("4. Test Globus Compute integration")
        
        return 0
    else:
        logger.error("❌ Phase 2 integration has issues - review before proceeding")
        return 1


if __name__ == "__main__":
    sys.exit(main())
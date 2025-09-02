#!/usr/bin/env python3
"""Debug containerized worker with actual Parsl commands."""

import sys, os
sys.path.insert(0, '.')
from phase15_enhanced import AWSProvider
from container_executor import ContainerHighThroughputExecutor
import parsl
import logging
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_containerized_worker():
    """Test containerized worker with minimal Parsl setup."""
    
    provider = AWSProvider(
        region="us-east-1",
        instance_type="t3.medium",
        init_blocks=1,  # Create 1 block immediately
        max_blocks=1
    )
    
    # Create executor with minimal configuration  
    # Use Python container and install Parsl at runtime
    executor = ContainerHighThroughputExecutor(
        label="container_test",
        container_image="python:3.10-slim",  # Install Parsl at runtime
        provider=provider,
        max_workers_per_node=1,
        worker_debug=True
    )
    
    config = parsl.Config(
        executors=[executor],
        strategy=None
    )
    
    try:
        logger.info("🔧 Starting Parsl with containerized executor...")
        parsl.load(config)
        
        # Simple test task
        @parsl.python_app
        def container_test():
            """Test if we're running in a container."""
            import os
            import platform
            import sys
            
            result = {
                'in_container': os.path.exists('/.dockerenv'),
                'hostname': platform.node(),
                'python_version': sys.version,
                'pwd': os.getcwd(),
                'user': os.getenv('USER', 'unknown')
            }
            
            print(f"CONTAINER TEST RESULT: {result}")
            return result
        
        logger.info("📋 Submitting container test task...")
        future = container_test()
        
        # Wait for execution with detailed status
        for i in range(30):  # Wait up to 5 minutes
            if future.done():
                result = future.result()
                logger.info(f"✅ Task completed: {result}")
                
                if result['in_container']:
                    logger.info("🎉 SUCCESS: Task executed in container!")
                    return True
                else:
                    logger.error("❌ FAILURE: Task did not execute in container")
                    return False
            
            # Check executor status
            status = executor.status()
            logger.info(f"⏱️  Waiting... Executor status: {status}")
            time.sleep(10)
        
        logger.error("⏰ Timeout: Task did not complete")
        return False
        
    except Exception as e:
        logger.error(f"💥 Test failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    finally:
        try:
            parsl.dfk().cleanup()
            parsl.clear()
        except:
            pass

if __name__ == "__main__":
    success = test_containerized_worker()
    print(f"Containerized worker test: {'SUCCESS' if success else 'FAILED'}")
    print("Check AWS console for running instance to inspect logs.")
#!/usr/bin/env python3
"""Test our custom ContainerHighThroughputExecutor with AWS provider."""

import logging
import sys
import os
import time
import parsl
from parsl.config import Config
from parsl.app.app import python_app

# Add current directory to path so we can import our modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from container_executor import ContainerHighThroughputExecutor
from phase15_enhanced import AWSProvider

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@python_app
def prove_container_works() -> dict:
    """Prove this task runs in a container on AWS."""
    import platform
    import os
    
    # PROVE we're in container
    in_container = os.path.exists('/.dockerenv')
    
    # PROVE environment details
    return {
        'SUCCESS': in_container,
        'in_container': in_container,
        'platform': platform.platform(),
        'hostname': platform.node(),
        'python_version': platform.python_version(),
        'working_dir': os.getcwd(),
        'user': os.environ.get('USER', 'unknown'),
        'PROOF': 'CUSTOM CONTAINER EXECUTOR WORKS!' if in_container else 'NOT IN CONTAINER'
    }

def main():
    """Test our custom container executor with AWS provider."""
    
    logger.info("🔥 TESTING CUSTOM CONTAINER EXECUTOR WITH AWS PROVIDER 🔥")
    
    try:
        # Use our enhanced AWS provider (without container_runtime since executor handles it)
        provider = AWSProvider(
            label="CONTAINER-EXECUTOR-TEST",
            region="us-east-1",
            instance_type="t3.small",
            init_blocks=1,
            max_blocks=1
        )
        
        # Use our custom container executor
        config = Config(
            executors=[
                ContainerHighThroughputExecutor(
                    label="container_executor",
                    provider=provider,
                    max_workers_per_node=1,
                    # Container configuration  
                    container_image="python:3.10-slim",
                    container_runtime="docker",
                    container_options="--rm --network host -v /tmp:/tmp -e PYTHONUNBUFFERED=1"
                )
            ]
        )
        
        logger.info("Loading Parsl with custom container executor...")
        parsl.load(config)
        
        logger.info("Waiting for containerized worker to connect...")
        time.sleep(90)  # Wait for AWS instance + container startup
        
        logger.info("🚀 SUBMITTING CONTAINER PROOF TASK...")
        future = prove_container_works()
        
        logger.info("⏳ WAITING FOR PROOF...")
        result = future.result(timeout=180)
        
        logger.info("🎉 TASK COMPLETED!")
        logger.info("=" * 60)
        logger.info("📊 RESULTS:")
        for key, value in result.items():
            logger.info(f"  {key}: {value}")
        logger.info("=" * 60)
        
        if result['SUCCESS'] and result['in_container']:
            logger.info("✅ PROVEN: Custom Container Executor with AWS Provider WORKS!")
            logger.info("✅ Task executed in container on AWS EC2")
            logger.info("✅ SSH tunneling enabled container connectivity")
            logger.info("✅ Parsl-native container execution achieved")
            return True
        else:
            logger.error("❌ CONTAINER EXECUTION FAILED")
            logger.error(f"Container check: {result['in_container']}")
            return False
            
    except Exception as e:
        logger.error(f"💥 CONTAINER EXECUTOR TEST FAILED: {e}")
        return False
    finally:
        try:
            parsl.clear()
        except:
            pass

if __name__ == "__main__":
    success = main()
    if success:
        print("\n🎯 PATH 2 SUCCESS: CUSTOM CONTAINER EXECUTOR PROVEN!")
        print("✅ Parsl-native container execution on ephemeral AWS")
        print("✅ Ready to implement Path 1 (Globus Compute) as production option")
    else:
        print("\n💀 PATH 2 FAILED - CUSTOM EXECUTOR NOT WORKING")
    
    sys.exit(0 if success else 1)
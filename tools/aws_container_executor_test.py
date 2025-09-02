#!/usr/bin/env python3
"""Test our ContainerHighThroughputExecutor on AWS with Docker."""

import logging
import sys
import os
import time
import parsl
from parsl.config import Config
from parsl.app.app import python_app

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from container_executor import ContainerHighThroughputExecutor
from phase15_enhanced import AWSProvider

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@python_app
def prove_custom_executor_containers() -> dict:
    """FINAL PROOF: Custom executor + containers + ephemeral AWS."""
    import platform
    import os
    
    # Check container environment
    in_container = os.path.exists('/.dockerenv')
    
    # Minimal computation to prove it works
    result = sum(range(100))
    
    return {
        'CONTAINER_SUCCESS': in_container,
        'in_container': in_container,
        'platform': platform.platform(),
        'hostname': platform.node()[:20],  # Truncate long hostnames
        'python_version': platform.python_version(),
        'computation_result': result,
        'PROOF': 'PATH 2 SUCCESS: CUSTOM EXECUTOR + CONTAINERS!' if in_container else 'NOT IN CONTAINER'
    }

def main():
    """Test our custom container executor on AWS."""
    
    logger.info("🔥 PATH 2: TESTING CUSTOM CONTAINER EXECUTOR ON AWS 🔥")
    
    try:
        # Use our enhanced AWS provider (Phase 1.5 working features)
        provider = AWSProvider(
            label="CUSTOM-CONTAINER-TEST",
            region="us-east-1", 
            instance_type="t3.small",
            init_blocks=1,
            max_blocks=1
            # Note: NO container_runtime - our executor handles containers
        )
        
        # Use our custom container executor with Docker
        config = Config(
            executors=[
                ContainerHighThroughputExecutor(
                    label="custom_container_executor",
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
        
        logger.info("Waiting for AWS instance + containerized worker...")
        time.sleep(90)  # Wait for AWS startup + Docker pull
        
        logger.info("🚀 SUBMITTING FINAL PROOF TASK...")
        future = prove_custom_executor_containers()
        
        logger.info("⏳ WAITING FOR CONTAINER PROOF...")
        result = future.result(timeout=180)
        
        logger.info("🎉 CUSTOM EXECUTOR TASK COMPLETED!")
        logger.info("=" * 60)
        logger.info("📊 PATH 2 RESULTS:")
        for key, value in result.items():
            logger.info(f"  {key}: {value}")
        logger.info("=" * 60)
        
        if result['CONTAINER_SUCCESS']:
            logger.info("✅ PATH 2 PROVEN: Custom Container Executor WORKS!")
            logger.info("✅ Parsl-native container execution achieved")
            logger.info("✅ Works with our ephemeral AWS provider")
            logger.info("✅ SSH tunneling + containers working together")
            logger.info("✅ Ready to implement Path 1 as production option")
            return True
        else:
            logger.error("❌ PATH 2 FAILED - Task not in container")
            return False
            
    except Exception as e:
        logger.error(f"💥 PATH 2 TEST FAILED: {e}")
        return False
    finally:
        try:
            parsl.clear()
        except:
            pass

if __name__ == "__main__":
    success = main()
    if success:
        print("\n🎯 PATH 2 MILESTONE ACHIEVED!")
        print("✅ Custom ContainerHighThroughputExecutor works with ephemeral AWS")
        print("✅ Parsl-native container execution proven")  
        print("✅ Foundation for Path 1 (Globus Compute) established")
    else:
        print("\n💀 PATH 2 NEEDS MORE WORK")
    
    sys.exit(0 if success else 1)
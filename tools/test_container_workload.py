#!/usr/bin/env python3
"""Simple end-to-end container workload test."""

import logging
import sys
import time
import parsl
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
from parsl.app.app import python_app
from phase15_enhanced import AWSProvider

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@python_app
def simple_container_task() -> dict:
    """Simple computational task to verify container execution."""
    import platform
    import os
    import numpy as np
    
    # Verify we're in container
    in_container = os.path.exists('/.dockerenv')
    
    # Simple numpy computation
    data = np.random.random(1000)
    result = np.mean(data)
    
    return {
        'in_container': in_container,
        'platform': platform.platform(),
        'hostname': platform.node(), 
        'python_version': platform.python_version(),
        'numpy_result': float(result),
        'cwd': os.getcwd(),
        'user': os.environ.get('USER', 'unknown')
    }

def main():
    """Run simple container workload test."""
    
    logger.info("=== Simple Container Workload Test ===")
    
    try:
        # Create Phase 2 provider with basic container stack
        provider = AWSProvider(
            label="container-test",
            region="us-east-1",
            container_runtime="docker",
            scientific_stack="basic",
            init_blocks=1,
            max_blocks=1,
            min_blocks=0,
            instance_type="t3.medium"
        )
        
        # Simple config with one executor
        config = Config(
            executors=[
                HighThroughputExecutor(
                    label="container_executor",
                    provider=provider,
                    max_workers_per_node=1
                )
            ]
        )
        
        logger.info("Loading Parsl with container provider...")
        parsl.load(config)
        
        logger.info("Waiting for container worker...")
        time.sleep(60)  # Give container worker time to start
        
        logger.info("Submitting container task...")
        future = simple_container_task()
        
        logger.info("Waiting for task completion (max 2 minutes)...")
        result = future.result(timeout=120)
        
        logger.info("✅ Container Task Results:")
        logger.info(f"  In container: {result['in_container']}")
        logger.info(f"  Platform: {result['platform']}")
        logger.info(f"  Hostname: {result['hostname']}")
        logger.info(f"  Python: {result['python_version']}")
        logger.info(f"  NumPy result: {result['numpy_result']:.6f}")
        logger.info(f"  Working dir: {result['cwd']}")
        logger.info(f"  User: {result['user']}")
        
        parsl.clear()
        
        if result['in_container']:
            logger.info("🎉 SUCCESS: Container workload executed successfully!")
            logger.info("✅ Phase 2 container execution PROVEN functional")
            return True
        else:
            logger.error("❌ FAILURE: Task did not execute in container")
            return False
            
    except Exception as e:
        logger.error(f"Container workload test failed: {e}")
        try:
            parsl.clear()
        except:
            pass
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
#!/usr/bin/env python3
"""Test our container executor with LocalProvider first."""

import logging
import sys
import os
import time
import parsl
from parsl.config import Config
from parsl.providers import LocalProvider
from parsl.app.app import python_app

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from container_executor import ContainerHighThroughputExecutor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@python_app
def test_container() -> dict:
    """Test if we're running in container."""
    import os
    import platform
    
    return {
        'in_container': os.path.exists('/.dockerenv'),
        'platform': platform.platform(),
        'hostname': platform.node(),
        'python_version': platform.python_version()
    }

def main():
    """Test container executor locally first."""
    
    logger.info("Testing custom container executor locally...")
    
    # Test with LocalProvider first to isolate container executor
    config = Config(
        executors=[
            ContainerHighThroughputExecutor(
                label="local_container_test",
                provider=LocalProvider(init_blocks=1, max_blocks=1),
                max_workers_per_node=1,
                container_image="python:3.10-slim",
                container_runtime="docker"
            )
        ]
    )
    
    try:
        logger.info("Loading Parsl with local container executor...")
        parsl.load(config)
        
        logger.info("Waiting for local containerized worker...")
        time.sleep(10)
        
        logger.info("Submitting container test...")
        future = test_container()
        result = future.result(timeout=30)
        
        logger.info(f"Results: {result}")
        
        if result['in_container']:
            logger.info("✅ LOCAL CONTAINER EXECUTOR WORKS!")
            return True
        else:
            logger.error("❌ Task not running in container")
            return False
            
    except Exception as e:
        logger.error(f"Test failed: {e}")
        return False
    finally:
        try:
            parsl.clear()
        except:
            pass

if __name__ == "__main__":
    success = main()
    print(f"LOCAL TEST: {'SUCCESS' if success else 'FAILED'}")
#!/usr/bin/env python3
"""Simplest possible container test."""

import logging
import time
import parsl
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
from parsl.app.app import bash_app
from phase15_enhanced import AWSProvider

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@bash_app
def check_container_env():
    """Use bash_app to check if we're in container."""
    return "ls -la /.dockerenv 2>/dev/null && echo 'IN CONTAINER' || echo 'NOT IN CONTAINER'"

def main():
    """Test with bash_app instead of python_app."""
    
    provider = AWSProvider(
        label="SIMPLE-TEST",
        region="us-east-1",
        container_runtime="docker",
        container_image="parsl-base:latest",
        init_blocks=1,
        max_blocks=1,
        instance_type="t3.small"
    )
    
    config = Config(
        executors=[
            HighThroughputExecutor(
                label="simple_executor",
                provider=provider,
                max_workers_per_node=1
            )
        ]
    )
    
    try:
        logger.info("Loading Parsl...")
        parsl.load(config)
        
        logger.info("Waiting for worker...")
        time.sleep(60)
        
        logger.info("Submitting container check...")
        future = check_container_env()
        result = future.result(timeout=60)
        
        logger.info(f"Container check result: {result}")
        
        if "IN CONTAINER" in result:
            logger.info("✅ BASH execution is in container!")
            return True
        else:
            logger.error("❌ BASH execution not in container")
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
    print(f"Result: {'SUCCESS' if success else 'FAILED'}")
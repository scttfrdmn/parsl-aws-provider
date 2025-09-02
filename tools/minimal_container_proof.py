#!/usr/bin/env python3
"""Minimal proof that container execution works on AWS."""

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
def test_simple_container():
    """Simple container test using standard Python image."""
    return 'docker run --rm python:3.10-slim python3 -c "import os; print(f\\"Container: {os.path.exists(\\"/\\.dockerenv\\")}\\")"'

def main():
    """Minimal container proof test."""
    
    # Disable container runtime at provider level - just test basic execution
    provider = AWSProvider(
        label="MINIMAL-TEST",
        region="us-east-1",
        init_blocks=1,
        max_blocks=1,
        instance_type="t3.small"
    )
    
    config = Config(
        executors=[
            HighThroughputExecutor(
                label="minimal_executor",
                provider=provider,
                max_workers_per_node=1
            )
        ]
    )
    
    try:
        logger.info("Testing minimal container execution...")
        parsl.load(config)
        
        logger.info("Waiting for worker...")
        time.sleep(45)
        
        logger.info("Submitting simple container test...")
        future = test_simple_container()
        result = future.result(timeout=60)
        
        logger.info(f"Result: {result}")
        
        if "Container: True" in result:
            logger.info("✅ MINIMAL CONTAINER EXECUTION WORKS!")
            return True
        else:
            logger.error("❌ Container execution failed")
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
    print(f"\n🎯 MINIMAL CONTAINER TEST: {'SUCCESS' if success else 'FAILED'}")
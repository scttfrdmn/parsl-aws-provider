#!/usr/bin/env python3
"""Test task-level container execution."""

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
def container_task():
    """Execute task inside container."""
    # This bash_app will run a Docker command that executes Python inside container
    return '''
# Run Python task inside parsl-base container
docker run --rm parsl-base:latest python3 -c "
import os
import platform
import numpy as np

print('=== CONTAINER TASK EXECUTION ===')
print(f'In container: {os.path.exists(\"/\.dockerenv\")}')
print(f'Platform: {platform.platform()}')
print(f'Python version: {platform.python_version()}')

# Test computation
data = np.random.random(100)
result = np.mean(data * data)
print(f'NumPy computation: {result}')
print('=== TASK COMPLETE ===')
"
'''

def main():
    """Test task-level container execution."""
    
    provider = AWSProvider(
        label="TASK-CONTAINER-TEST",
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
                label="task_executor",
                provider=provider,
                max_workers_per_node=1
            )
        ]
    )
    
    try:
        logger.info("Loading Parsl (worker on host, tasks in containers)...")
        parsl.load(config)
        
        logger.info("Waiting for worker connection...")
        time.sleep(60)
        
        logger.info("Submitting containerized task...")
        future = container_task()
        result = future.result(timeout=120)
        
        logger.info(f"Task result:\n{result}")
        
        if "In container: True" in result:
            logger.info("✅ TASK-LEVEL CONTAINER EXECUTION WORKS!")
            return True
        else:
            logger.error("❌ Task-level container execution failed")
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
    print(f"\n🎯 TASK-LEVEL CONTAINER TEST: {'SUCCESS' if success else 'FAILED'}")
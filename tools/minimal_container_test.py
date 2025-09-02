#!/usr/bin/env python3
"""
Minimal container test to isolate the Docker execution issue.
"""

import logging
import parsl
from parsl.config import Config

from phase15_enhanced import AWSProvider
from container_executor import ContainerHighThroughputExecutor

logging.basicConfig(level=logging.INFO)

def main():
    print("🔍 Minimal Container Test")
    
    # Test with minimal container executor - no complex options
    container_executor = ContainerHighThroughputExecutor(
        label="minimal_test",
        provider=AWSProvider(
            enable_ssm_tunneling=True,
            init_blocks=1,
            max_blocks=1,
            min_blocks=1,
            ami_id="ami-0cab818949226441f"
        ),
        container_image="python:3.10-slim",  # Smaller image
        container_runtime="docker",
        container_options="",  # No options at all
        max_workers_per_node=1  # Force single worker to eliminate confusion
    )
    
    config = Config(executors=[container_executor])
    
    @parsl.python_app
    def minimal_test():
        # Just check if we're in a container - simplest possible test
        import os
        in_container = os.path.exists("/.dockerenv")
        print(f"In container: {in_container}")
        return in_container
    
    try:
        parsl.load(config)
        future = minimal_test()
        result = future.result(timeout=120)
        
        print(f"✅ Container result: {result}")
        if result:
            print("🎉 SUCCESS: Task executed in container!")
        else:
            print("❌ FAILED: Task executed on host, not container")
        return result
        
    finally:
        parsl.clear()

if __name__ == "__main__":
    main()
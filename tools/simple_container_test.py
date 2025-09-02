#!/usr/bin/env python3
"""
Simple test using Globus Compute's exact approach.
"""

import logging
import parsl
from parsl.config import Config

from phase15_enhanced import AWSProvider
from container_executor import ContainerHighThroughputExecutor

logging.basicConfig(level=logging.INFO)

def main():
    print("🔍 Simple Container Test - Globus Approach")
    
    # Globus Compute's exact approach - simple configuration
    container_executor = ContainerHighThroughputExecutor(
        label="simple_test",
        provider=AWSProvider(
            enable_ssm_tunneling=True,
            init_blocks=1,
            max_blocks=1,
            min_blocks=1,
            ami_id="ami-0cab818949226441f"
        ),
        container_image="python:3.10",  # Full python image with pip
        container_runtime="docker",
        container_options="--rm"  # Minimal options like Globus
    )
    
    config = Config(executors=[container_executor])
    
    @parsl.python_app
    def container_test():
        import os
        return os.path.exists("/.dockerenv")
    
    try:
        parsl.load(config)
        future = container_test()
        result = future.result(timeout=180)
        
        print(f"✅ Container execution: {result}")
        return result
        
    finally:
        parsl.clear()

if __name__ == "__main__":
    main()
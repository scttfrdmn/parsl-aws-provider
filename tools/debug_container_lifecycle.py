#!/usr/bin/env python3
"""Debug container lifecycle to understand why containers exit."""

import sys, os
sys.path.insert(0, '.')
from phase15_enhanced import AWSProvider  
from container_executor import ContainerHighThroughputExecutor
import parsl
import logging
import time

logging.basicConfig(level=logging.DEBUG)

def debug_container_lifecycle():
    """Test container lifecycle with detailed debugging."""
    
    provider = AWSProvider(
        region="us-east-1",
        instance_type="t3.medium", 
        init_blocks=1,
        max_blocks=1
    )
    
    executor = ContainerHighThroughputExecutor(
        label="debug_lifecycle",
        container_image="python:3.10-slim",
        provider=provider,
        max_workers_per_node=1,
        worker_debug=True
    )
    
    config = parsl.Config(executors=[executor], strategy=None)
    
    try:
        parsl.load(config)
        
        print("📊 Waiting for workers to register...")
        time.sleep(120)  # Wait longer for debugging
        
        @parsl.python_app
        def check_environment():
            import os
            import platform
            import time
            
            # Check if we're in container and collect extensive info
            return {
                'in_container': os.path.exists('/.dockerenv'),
                'platform': platform.platform(),
                'hostname': platform.node(),
                'working_dir': os.getcwd(),
                'env_vars': dict(os.environ),
                'timestamp': time.time()
            }
        
        print("🚀 Submitting containerized task...")
        future = check_environment()
        result = future.result(timeout=300)
        
        print(f"✅ RESULT: {result}")
        print(f"🐳 In container: {result.get('in_container', 'UNKNOWN')}")
        print(f"🏷️  Hostname: {result.get('hostname', 'UNKNOWN')}")
        
        return result.get('in_container', False)
        
    finally:
        try:
            parsl.dfk().cleanup()
            parsl.clear()
        except:
            pass

if __name__ == "__main__":
    success = debug_container_lifecycle()
    print(f"\n🎯 Container execution: {'SUCCESS' if success else 'FAILED'}")
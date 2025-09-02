#!/usr/bin/env python3
"""Test container execution with working Amazon Linux 2 AMI."""

import sys, os
sys.path.insert(0, '.')
from phase15_enhanced import AWSProvider  
from container_executor import ContainerHighThroughputExecutor
import parsl
import logging

logging.basicConfig(level=logging.INFO)

def test_working_ami_container():
    """Test container execution with known-working AMI."""
    
    provider = AWSProvider(
        region="us-east-1",
        instance_type="t3.medium", 
        ami_id="ami-065778886ef8ec7c8",  # Working Ubuntu 22.04 AMI with SSM
        init_blocks=1,
        max_blocks=1
    )
    
    executor = ContainerHighThroughputExecutor(
        label="working_ami_test",
        container_image="python:3.10-slim",
        provider=provider,
        max_workers_per_node=1,
        worker_debug=True
    )
    
    config = parsl.Config(executors=[executor], strategy=None)
    
    try:
        parsl.load(config)
        
        @parsl.python_app
        def test_container_execution():
            import os
            import platform
            
            return {
                'in_container': os.path.exists('/.dockerenv'),
                'hostname': platform.node(),
                'platform': platform.platform(),
                'working_dir': os.getcwd(),
                'python_version': platform.python_version()
            }
        
        print("🚀 Submitting container test with working AMI...")
        future = test_container_execution()
        result = future.result(timeout=300)
        
        print(f"\n✅ RESULT: {result}")
        is_container = result.get('in_container', False)
        print(f"🐳 Container execution: {'SUCCESS' if is_container else 'FAILED'}")
        
        return is_container
        
    finally:
        try:
            parsl.dfk().cleanup()
            parsl.clear()
        except:
            pass

if __name__ == "__main__":
    success = test_working_ami_container()
    print(f"\n🎯 Final result: {'CONTAINER WORKING' if success else 'STILL DEBUGGING'}")
#!/usr/bin/env python3
"""Final container execution proof with working infrastructure."""

import sys, os
sys.path.insert(0, '.')
from phase15_enhanced import AWSProvider  
from container_executor import ContainerHighThroughputExecutor
import parsl
import logging

logging.basicConfig(level=logging.INFO)

def test_final_container_proof():
    """Test container execution with confirmed working infrastructure."""
    
    # Use existing instance that has Docker working
    provider = AWSProvider(
        region="us-east-1",
        instance_type="t3.medium", 
        ami_id=None,  # Use default AMI selection  
        init_blocks=1,
        max_blocks=1
    )
    
    # Force use the working instance by manually setting it up
    provider.instances = {"manual-test": "i-0e56686130d1a6725"}
    provider.job_map = {"manual-test": {"instance_id": "i-0e56686130d1a6725"}}
    
    executor = ContainerHighThroughputExecutor(
        label="final_proof",
        container_image="python:3.10-slim",
        provider=provider,
        max_workers_per_node=1,
        worker_debug=True
    )
    
    # Override the launch command directly to test  
    original_get_launch = executor._get_launch_command
    
    def debug_get_launch_command(block_id):
        print(f"🔍 _get_launch_command called with block_id: {block_id}")
        result = original_get_launch(block_id)
        print(f"🐳 Generated command: {result}")
        return result
    
    executor._get_launch_command = debug_get_launch_command
    
    config = parsl.Config(executors=[executor], strategy=None)
    
    try:
        parsl.load(config)
        
        @parsl.python_app
        def final_container_test():
            import os
            import platform
            
            return {
                'in_container': os.path.exists('/.dockerenv'),
                'hostname': platform.node(),
                'platform': platform.platform()
            }
        
        print("🚀 Testing final container execution...")
        future = final_container_test()
        result = future.result(timeout=300)
        
        print(f"✅ RESULT: {result}")
        return result.get('in_container', False)
        
    finally:
        try:
            parsl.dfk().cleanup() 
            parsl.clear()
        except:
            pass

if __name__ == "__main__":
    success = test_final_container_proof()
    print(f"\n🎯 CONTAINER EXECUTION: {'✅ SUCCESS' if success else '❌ FAILED'}")
#!/usr/bin/env python3
"""Clean container execution test with fixed command generation."""

import sys, os
sys.path.insert(0, '.')
from phase15_enhanced import AWSProvider  
from container_executor import ContainerHighThroughputExecutor
import parsl
import logging

logging.basicConfig(level=logging.INFO)

def test_clean_container_execution():
    """Test container execution with clean infrastructure."""
    
    # Create fresh provider without forcing specific instances
    provider = AWSProvider(
        region="us-east-1",
        instance_type="t3.medium", 
        ami_id=None,  # Use default AMI selection  
        init_blocks=1,
        max_blocks=1
    )
    
    executor = ContainerHighThroughputExecutor(
        label="clean_container_test",
        container_image="python:3.10-slim",
        provider=provider,
        max_workers_per_node=1,
        worker_debug=True
    )
    
    config = parsl.Config(executors=[executor], strategy=None)
    
    try:
        parsl.load(config)
        
        @parsl.python_app
        def container_detection_test():
            import os
            import platform
            
            return {
                'in_container': os.path.exists('/.dockerenv'),
                'hostname': platform.node(),
                'platform': platform.platform(),
                'python_version': platform.python_version()
            }
        
        print("🚀 Testing clean container execution...")
        future = container_detection_test()
        result = future.result(timeout=180)  # 3 minutes timeout
        
        print(f"✅ RESULT: {result}")
        return result.get('in_container', False)
        
    finally:
        try:
            parsl.dfk().cleanup() 
            parsl.clear()
        except:
            pass

if __name__ == "__main__":
    success = test_clean_container_execution()
    print(f"\n🎯 CONTAINER EXECUTION: {'✅ SUCCESS' if success else '❌ FAILED'}")
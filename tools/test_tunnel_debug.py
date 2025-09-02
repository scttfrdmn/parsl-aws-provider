#!/usr/bin/env python3
"""Quick test to verify tunnel connectivity debugging."""

import sys, os
sys.path.insert(0, '.')
from phase15_enhanced import AWSProvider  
from container_executor import ContainerHighThroughputExecutor
import parsl
import logging

logging.basicConfig(level=logging.INFO)

def test_tunnel_connectivity():
    """Test tunnel connectivity with debugging."""
    
    # Use existing working instance
    provider = AWSProvider(
        region="us-east-1",
        instance_type="t3.medium", 
        ami_id=None,
        init_blocks=1,
        max_blocks=1
    )
    
    # Force use existing instance 
    provider.instances = {"manual-test": "i-0e56686130d1a6725"}
    provider.job_map = {"manual-test": {"instance_id": "i-0e56686130d1a6725"}}
    
    executor = ContainerHighThroughputExecutor(
        label="tunnel_debug",
        container_image="python:3.10-slim",
        provider=provider,
        max_workers_per_node=1,
    )
    
    config = parsl.Config(executors=[executor], strategy=None)
    
    try:
        parsl.load(config)
        
        @parsl.python_app
        def simple_test():
            return "container connected"
        
        print("🚀 Testing with tunnel debugging...")
        future = simple_test()
        result = future.result(timeout=120)
        
        print(f"✅ RESULT: {result}")
        return True
        
    except Exception as e:
        print(f"❌ Test failed: {e}")
        return False
        
    finally:
        try:
            parsl.dfk().cleanup() 
            parsl.clear()
        except:
            pass

if __name__ == "__main__":
    success = test_tunnel_connectivity()
    print(f"\n🎯 TUNNEL TEST: {'✅ SUCCESS' if success else '❌ FAILED'}")
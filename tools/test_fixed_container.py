#!/usr/bin/env python3
"""Test the fixed container execution with module approach."""

import sys, os
sys.path.insert(0, '.')
from phase15_enhanced import AWSProvider  
from container_executor import ContainerHighThroughputExecutor
import parsl
import logging
import time

logging.basicConfig(level=logging.INFO)

def test_fixed_container():
    """Test container execution with fixed module approach."""
    
    provider = AWSProvider(
        region="us-east-1",
        instance_type="t3.medium", 
        init_blocks=1,
        max_blocks=1
    )
    
    executor = ContainerHighThroughputExecutor(
        label="fixed_container",
        container_image="python:3.10-slim",
        provider=provider,
        max_workers_per_node=1,
        worker_debug=True
    )
    
    config = parsl.Config(executors=[executor], strategy=None)
    
    try:
        parsl.load(config)
        
        # Show what command is being generated
        print("=" * 60)
        print("CONTAINER COMMAND ANALYSIS")
        print("=" * 60)
        
        @parsl.python_app
        def verify_container():
            import os
            import platform
            
            # Comprehensive container verification
            container_info = {
                'in_container': os.path.exists('/.dockerenv'),
                'hostname': platform.node(),
                'platform': platform.platform(),
                'python_path': os.__file__,
                'working_directory': os.getcwd(),
                'uid': os.getuid() if hasattr(os, 'getuid') else 'unknown'
            }
            
            # Extra check for container indicators
            try:
                with open('/proc/1/cgroup', 'r') as f:
                    cgroup_info = f.read()
                    container_info['has_docker_cgroup'] = 'docker' in cgroup_info.lower()
            except:
                container_info['has_docker_cgroup'] = False
            
            return container_info
        
        print("🚀 Submitting container verification task...")
        future = verify_container()
        result = future.result(timeout=300)
        
        print("\n" + "=" * 60)
        print("CONTAINER VERIFICATION RESULTS")
        print("=" * 60)
        for key, value in result.items():
            print(f"{key}: {value}")
        
        is_containerized = result.get('in_container', False)
        print(f"\n🎯 CONTAINERIZED EXECUTION: {'✅ SUCCESS' if is_containerized else '❌ FAILED'}")
        
        return is_containerized
        
    except Exception as e:
        print(f"❌ ERROR: {e}")
        return False
        
    finally:
        try:
            parsl.dfk().cleanup()
            parsl.clear()
        except:
            pass

if __name__ == "__main__":
    success = test_fixed_container()
    print(f"\n🏁 FINAL RESULT: {'CONTAINER EXECUTION WORKING' if success else 'STILL DEBUGGING NEEDED'}")
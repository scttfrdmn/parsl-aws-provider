#!/usr/bin/env python3
"""Test container worker while interchange is actively running."""

import sys, os
sys.path.insert(0, '.')
from phase15_enhanced import AWSProvider  
from container_executor import ContainerHighThroughputExecutor
import parsl
import logging
import time
import threading

logging.basicConfig(level=logging.INFO)

def test_live_container_worker():
    """Test container worker connection while interchange is running."""
    
    provider = AWSProvider(
        region="us-east-1",
        instance_type="t3.medium", 
        init_blocks=1,
        max_blocks=1
    )
    
    executor = ContainerHighThroughputExecutor(
        label="live_test",
        container_image="python:3.10-slim",
        provider=provider,
        max_workers_per_node=1,
        worker_debug=True
    )
    
    config = parsl.Config(executors=[executor], strategy=None)
    
    def submit_test_task():
        """Submit a test task after waiting for setup."""
        time.sleep(120)  # Wait for infrastructure setup
        
        @parsl.python_app
        def container_verification():
            import os
            import platform
            import time
            
            return {
                'in_container': os.path.exists('/.dockerenv'),
                'hostname': platform.node(),
                'timestamp': time.time(),
                'working_dir': os.getcwd(),
                'python_version': platform.python_version()
            }
        
        print("🚀 Submitting task to test container execution...")
        future = container_verification()
        result = future.result(timeout=180)
        
        print(f"✅ TASK RESULT: {result}")
        return result.get('in_container', False)
    
    try:
        parsl.load(config)
        
        print("📊 Executor started, waiting for workers...")
        print("📊 This test will:")
        print("   1. Start the interchange") 
        print("   2. Launch AWS instance with container worker")
        print("   3. Submit a task to verify container execution")
        print("   4. Keep everything running for manual inspection")
        
        # Submit task in background thread to not block the main process
        task_thread = threading.Thread(target=submit_test_task)
        task_thread.start()
        
        # Monitor for a while
        for i in range(12):  # 12 * 30 = 6 minutes
            time.sleep(30)
            print(f"📊 Status check {i+1}/12...")
            
            # Check if we have any managers connected
            try:
                managers = executor.connected_managers()
                print(f"   Connected managers: {len(managers) if managers else 0}")
                for mgr_id, mgr_info in (managers or {}).items():
                    worker_count = mgr_info.get('worker_count', 0)
                    hostname = mgr_info.get('hostname', 'unknown')
                    print(f"     Manager {mgr_id}: {worker_count} workers on {hostname}")
            except:
                print("   Manager info not available")
        
        # Wait for task thread to complete
        task_thread.join(timeout=60)
        
        print("\n🎯 Test completed. Check the instance manually:")
        
        # Show current instance info
        for job_id, job_info in provider.job_map.items():
            instance_id = job_info.get('instance_id')
            if instance_id:
                print(f"   Instance: {instance_id}")
                print(f"   Connect: aws ssm start-session --target {instance_id}")
        
        return True
        
    except Exception as e:
        print(f"❌ ERROR: {e}")
        return False
        
    finally:
        print("\n🛑 NOT calling parsl.clear() to keep infrastructure running")
        print("   You can manually clean up later")

if __name__ == "__main__":
    test_live_container_worker()
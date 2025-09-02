#!/usr/bin/env python3
"""
Quick Phase 2 test with longer timeout and simpler task.
"""

import logging
import parsl
from parsl.config import Config

from phase15_enhanced import AWSProvider
from container_executor import ContainerHighThroughputExecutor

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def main():
    print("🧪 Quick Phase 2 Container Test")
    print("=" * 40)
    
    # Configure container executor with Phase 2 AMI
    container_executor = ContainerHighThroughputExecutor(
        label="quick_phase2",
        provider=AWSProvider(
            enable_ssm_tunneling=True,
            init_blocks=1,
            max_blocks=1,
            min_blocks=1,
            ami_id="ami-0cab818949226441f"  # Phase 2 AMI with container runtimes
        ),
        container_image="python:3.10-slim",
        container_runtime="docker",
        container_options="--rm --network host"
    )
    
    config = Config(executors=[container_executor])
    
    @parsl.python_app
    def simple_container_check():
        """Simple container detection."""
        import os
        return {
            "in_container": os.path.exists("/.dockerenv"),
            "message": "Container check complete"
        }
    
    try:
        print("📦 Loading Parsl...")
        parsl.load(config)
        
        print("🚀 Submitting simple task...")
        future = simple_container_check()
        
        print("⏳ Waiting up to 300 seconds...")
        result = future.result(timeout=300)
        
        print(f"\n✅ Result: {result}")
        
        if result['in_container']:
            print("🎉 SUCCESS: Task executed in container!")
        else:
            print("❌ Task executed on host")
        
        return result['in_container']
        
    except Exception as e:
        print(f"❌ Test failed: {e}")
        return False
        
    finally:
        print("🧹 Cleaning up...")
        parsl.clear()

if __name__ == "__main__":
    main()
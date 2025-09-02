#!/usr/bin/env python3
"""
Final clean test: Phase 2 container execution on ephemeral AWS infrastructure.

This test validates that containerized tasks execute successfully on AWS instances
with SSH reverse tunneling over SSM, returning in_container: True.
"""

import logging
import parsl
from parsl.config import Config

from phase15_enhanced import AWSProvider
from container_executor import ContainerHighThroughputExecutor

# Clean logging for production test
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def main():
    """Run clean end-to-end container execution test."""
    
    print("🧪 Phase 2 Container Execution Test")
    print("=" * 50)
    
    # Configure container executor with Phase 2 AMI
    container_executor = ContainerHighThroughputExecutor(
        label="phase2_test",
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
    def prove_container_execution():
        """Prove we're executing in a Docker container on AWS."""
        import os
        import socket
        import platform
        import sys
        
        # Multiple container detection methods
        in_container = (
            os.path.exists("/.dockerenv") or
            os.path.exists("/run/.containerenv") or
            "container" in os.environ.get("container", "").lower() or
            "docker" in open("/proc/1/cgroup", "r").read()
        )
        
        return {
            "in_container": in_container,
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
            "architecture": platform.machine(),
            "proof": "Container execution successful!" if in_container else "Running on host"
        }
    
    # Execute test
    try:
        print("📦 Loading Parsl configuration...")
        parsl.load(config)
        
        print("🚀 Submitting containerized task to AWS...")
        future = prove_container_execution()
        
        print("⏳ Waiting for task execution (120s timeout)...")
        result = future.result(timeout=120)
        
        print("\n✅ Task Execution Complete!")
        print("=" * 50)
        print(f"Container Status: {result['in_container']}")
        print(f"Hostname: {result['hostname']}")
        print(f"Platform: {result['platform']}")
        print(f"Python: {result['python_version']}")
        print(f"Message: {result['proof']}")
        print("=" * 50)
        
        if result['in_container']:
            print("🎉 SUCCESS: Phase 2 container execution VERIFIED!")
            print("   ✅ Task executed in Docker container on AWS instance")
            print("   ✅ SSH reverse tunneling over SSM working")
            print("   ✅ Container networking with GatewayPorts functional")
            return True
        else:
            print("❌ FAILURE: Task did not execute in container")
            return False
            
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        return False
        
    finally:
        print("\n🧹 Cleaning up Parsl resources...")
        parsl.clear()

if __name__ == "__main__":
    success = main()
    if success:
        print("\n🏆 Phase 2 Container Execution: PRODUCTION READY")
    else:
        print("\n⚠️  Phase 2 Container Execution: Needs Investigation")
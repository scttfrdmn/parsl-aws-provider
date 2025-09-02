#!/usr/bin/env python3
"""
Diagnostic test to understand container execution environment.
"""

import logging
import parsl
from parsl.config import Config

from phase15_enhanced import AWSProvider
from container_executor import ContainerHighThroughputExecutor

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def main():
    print("🔍 Diagnostic Container Environment Test")
    print("=" * 50)
    
    # Test with Phase 2 AMI and container
    container_executor = ContainerHighThroughputExecutor(
        label="diagnostic",
        provider=AWSProvider(
            enable_ssm_tunneling=True,
            init_blocks=1,
            max_blocks=1,
            min_blocks=1,
            ami_id="ami-0cab818949226441f"  # Phase 2 AMI
        ),
        container_image="python:3.10-slim",
        container_runtime="docker",
        container_options="--rm --network host"
    )
    
    config = Config(executors=[container_executor])
    
    @parsl.python_app
    def diagnostic_check():
        """Comprehensive diagnostic of execution environment."""
        import os
        import subprocess
        import sys
        
        results = {}
        
        # Container detection methods
        results["dockerenv_exists"] = os.path.exists("/.dockerenv")
        results["containerenv_exists"] = os.path.exists("/run/.containerenv")
        results["container_env_var"] = os.environ.get("container", "not_set")
        
        # Process information
        results["pid_1_command"] = "unknown"
        try:
            with open("/proc/1/cmdline", "r") as f:
                results["pid_1_command"] = f.read().replace('\x00', ' ').strip()
        except:
            pass
            
        # Cgroup analysis
        results["cgroup_info"] = "unknown"
        try:
            with open("/proc/1/cgroup", "r") as f:
                cgroup_content = f.read()
                results["cgroup_info"] = cgroup_content
                results["docker_in_cgroup"] = "docker" in cgroup_content.lower()
        except:
            pass
            
        # Working directory and filesystem
        results["pwd"] = os.getcwd()
        results["python_executable"] = sys.executable
        
        # Try to run Docker command if available
        try:
            result = subprocess.run(["docker", "--version"], capture_output=True, text=True, timeout=5)
            results["docker_available"] = result.returncode == 0
            results["docker_version"] = result.stdout.strip() if result.returncode == 0 else "failed"
        except:
            results["docker_available"] = False
            results["docker_version"] = "not_available"
            
        # Summary
        in_container = any([
            results["dockerenv_exists"],
            results["containerenv_exists"],
            results.get("docker_in_cgroup", False)
        ])
        
        results["FINAL_VERDICT"] = "IN_CONTAINER" if in_container else "ON_HOST"
        
        return results
    
    try:
        print("📦 Loading Parsl with diagnostic config...")
        parsl.load(config)
        
        print("🚀 Submitting diagnostic task...")
        future = diagnostic_check()
        
        print("⏳ Waiting for diagnostic results (300s timeout)...")
        result = future.result(timeout=300)
        
        print("\n📊 DIAGNOSTIC RESULTS")
        print("=" * 50)
        for key, value in result.items():
            print(f"{key}: {value}")
        print("=" * 50)
        
        if result["FINAL_VERDICT"] == "IN_CONTAINER":
            print("🎉 SUCCESS: Task executed in container!")
            return True
        else:
            print("❌ FAILURE: Task executed on host")
            print("\n🔍 Debugging info:")
            print(f"   - /.dockerenv exists: {result['dockerenv_exists']}")
            print(f"   - Docker in cgroup: {result.get('docker_in_cgroup', False)}")
            print(f"   - PID 1 command: {result['pid_1_command']}")
            return False
        
    except Exception as e:
        print(f"❌ Diagnostic failed: {e}")
        return False
        
    finally:
        print("\n🧹 Cleaning up...")
        parsl.clear()

if __name__ == "__main__":
    main()
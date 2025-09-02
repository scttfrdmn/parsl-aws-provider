#!/usr/bin/env python3
"""Test container command generation without full execution."""

import sys, os
sys.path.insert(0, '.')
from container_executor import ContainerHighThroughputExecutor
from phase15_enhanced import AWSProvider

def test_container_command():
    """Test that container commands are generated correctly."""
    
    print("🔍 Testing container command generation...")
    
    # Create executor with container
    executor = ContainerHighThroughputExecutor(
        label="test_container_cmd",
        container_image="python:3.10-slim",
        max_workers_per_node=1,
    )
    
    # Set up a mock launch command (what Parsl would generate)
    executor.launch_cmd = "process_worker_pool.py --debug --max_workers_per_node=1 -a 127.0.0.1 -p 0 -c 1.0 -m None --poll 10 --port=54022 --cert_dir None --logdir=/test/logs --block_id={block_id} --hb_period=30 --hb_threshold=120"
    
    print(f"📋 Original command: {executor.launch_cmd}")
    
    # Test containerized command generation
    containerized = executor.containerized_launch_cmd()
    print(f"🐳 Containerized command: {containerized}")
    
    # Simulate the start() method behavior
    print("\n🚀 Simulating start() method...")
    original_cmd = executor.launch_cmd
    executor.launch_cmd = executor.containerized_launch_cmd()
    print(f"📋 Original: {original_cmd}")
    print(f"🐳 After start(): {executor.launch_cmd}")
    
    # Test _get_launch_command for a specific block
    block_command = executor._get_launch_command("0")
    print(f"🎯 Block command: {block_command}")
    
    # Check for nested docker commands
    if block_command.count("docker run") > 1:
        print("❌ NESTED DOCKER COMMANDS DETECTED!")
        return False
    elif "docker run" in block_command:
        print("✅ Single docker command generated")
        return True
    else:
        print("❌ No docker command found")
        return False

if __name__ == "__main__":
    success = test_container_command()
    print(f"\n🎯 COMMAND GENERATION: {'✅ SUCCESS' if success else '❌ FAILED'}")
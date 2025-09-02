#!/usr/bin/env python3
"""Test command generation for container executor."""

import sys, os
sys.path.insert(0, '.')
from container_executor import ContainerHighThroughputExecutor
from parsl.providers import LocalProvider

def test_command_generation():
    """Test what commands the container executor generates."""
    
    # Use LocalProvider to test command generation without AWS
    provider = LocalProvider()
    
    executor = ContainerHighThroughputExecutor(
        label="command_test",
        container_image="python:3.10-slim",
        provider=provider,
        max_workers_per_node=1,
        worker_debug=True
    )
    
    # Simulate launch_cmd from HighThroughputExecutor
    executor.launch_cmd = "process_worker_pool.py --max_workers_per_node=1 -a 127.0.0.1 -p 0 --port=54000 --logdir=/tmp/test"
    executor.run_dir = "/tmp/test"
    
    print("Original launch_cmd:", executor.launch_cmd)
    
    container_cmd = executor.containerized_launch_cmd()
    print("Containerized command:", container_cmd)
    
    return container_cmd

if __name__ == "__main__":
    cmd = test_command_generation()
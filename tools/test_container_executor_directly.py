#!/usr/bin/env python3
"""Direct test of container executor to verify containerization is working."""

import logging
from container_executor import ContainerHighThroughputExecutor
from phase15_enhanced import AWSProvider

logging.basicConfig(level=logging.DEBUG)

# Create container executor
container_executor = ContainerHighThroughputExecutor(
    label="direct_test",
    provider=AWSProvider(enable_ssm_tunneling=True),
    container_image="python:3.10-slim",
    container_runtime="docker",
    container_options="--rm --network host"
)

print("=== Testing Container Executor ===")
print(f"Container image: {container_executor.container_image}")
print(f"Container runtime: {container_executor.container_runtime}")

# Test containerized_launch_cmd method directly
test_command = "process_worker_pool.py --debug -a 127.0.0.1 --port=54489"
container_executor.launch_cmd = test_command

try:
    containerized_cmd = container_executor.containerized_launch_cmd()
    print(f"\nOriginal command: {test_command}")
    print(f"Containerized command: {containerized_cmd}")
    
    if "docker run" in containerized_cmd:
        print("✅ Containerization working!")
    else:
        print("❌ Containerization not applied")
        
except Exception as e:
    print(f"❌ Error testing containerization: {e}")

# Test the start() method
print(f"\n=== Testing start() method ===")
try:
    container_executor.start()
    print(f"Launch command after start(): {container_executor.launch_cmd}")
    
    if "docker run" in container_executor.launch_cmd:
        print("✅ start() method applying containerization!")
    else:
        print("❌ start() method not applying containerization")
        
except Exception as e:
    print(f"❌ Error in start() method: {e}")
finally:
    container_executor.shutdown()
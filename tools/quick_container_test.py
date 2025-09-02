#!/usr/bin/env python3
"""Quick test to verify container execution returns in_container: True"""

import logging
import parsl
from parsl.config import Config

# Import our enhanced provider and container executor
from phase15_enhanced import AWSProvider
from container_executor import ContainerHighThroughputExecutor

logging.basicConfig(level=logging.INFO)

# Configure container executor with AWS provider
container_executor = ContainerHighThroughputExecutor(
    label="quick_test",
    provider=AWSProvider(
        enable_ssm_tunneling=True,
        max_blocks=1,
        init_blocks=1,  # Force resource creation
        min_blocks=1,
    ),
    container_image="python:3.10-slim",
    container_runtime="docker",
    container_options="--rm --network host",
)

# Parsl configuration
config = Config(executors=[container_executor])


# Test function to detect container environment
@parsl.python_app
def check_container_environment():
    """Simple function to check if we're in a container."""
    import os
    import platform

    # Check common container indicators
    in_container = (
        os.path.exists("/.dockerenv")
        or os.path.exists("/run/.containerenv")
        or "container" in os.environ.get("container", "").lower()
    )

    return {
        "in_container": in_container,
        "hostname": platform.node(),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
    }


if __name__ == "__main__":
    print("🧪 Quick container execution test")

    # Initialize Parsl
    parsl.load(config)

    try:
        # Execute test function
        print("📦 Executing container task...")
        future = check_container_environment()

        # Wait for result with 90 second timeout
        result = future.result(timeout=90)

        print("✅ Task completed!")
        print(f"Result: {result}")

        if result.get("in_container"):
            print("🎉 SUCCESS: Container execution confirmed!")
        else:
            print("❌ FAILURE: Not executing in container")

    except Exception as e:
        print(f"❌ Test failed: {e}")
    finally:
        parsl.clear()

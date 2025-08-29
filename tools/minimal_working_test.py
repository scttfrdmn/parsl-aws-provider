#!/usr/bin/env python3
"""
Minimal working test based on our successful previous validation.
"""

import parsl
from parsl import python_app
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
import sys
from phase15_enhanced import AWSProvider


@python_app
def simple_task():
    """Very simple computation to test connectivity."""
    import platform
    import time

    result = {
        "hostname": platform.node(),
        "timestamp": time.time(),
        "message": "Task executed successfully on remote AWS instance!",
    }
    return result


def main():
    """Run minimal working test."""
    print("🧪 MINIMAL WORKING TEST")
    print("=" * 40)
    print("Testing basic Parsl + AWS + SSM functionality")
    print()

    # Simple provider configuration
    provider = AWSProvider(
        label="minimal_test", init_blocks=1, max_blocks=1, min_blocks=0
    )

    config = Config(
        executors=[
            HighThroughputExecutor(
                label="test_executor",
                provider=provider,
                max_workers_per_node=1,
                cores_per_worker=1,
            )
        ]
    )

    print("✅ Configuration ready")
    print("⚡ Loading Parsl...")

    parsl.load(config)
    print("✅ Parsl loaded")

    print("📤 Submitting simple task...")
    future = simple_task()

    print("⏳ Waiting for result (5 minute timeout)...")
    try:
        result = future.result(timeout=300)  # 5 minute timeout
        print("🎉 SUCCESS!")
        print(f"   Remote hostname: {result['hostname']}")
        print(f"   Message: {result['message']}")
        success = True
    except Exception as e:
        print(f"❌ FAILED: {e}")
        success = False

    print("\n🧹 Cleaning up...")
    parsl.clear()
    print("✅ Done")

    return success


if __name__ == "__main__":
    success = main()
    if success:
        print("\n🏆 MINIMAL TEST PASSED")
        print("✅ Phase 1.5 Enhanced AWS Provider is working!")
    else:
        print("\n❌ MINIMAL TEST FAILED")

    sys.exit(0 if success else 1)

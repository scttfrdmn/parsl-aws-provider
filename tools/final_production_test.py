#!/usr/bin/env python3
"""
Final production test - from scratch validation.
"""

import parsl
from parsl import python_app
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
import sys
from phase15_enhanced import AWSProvider


@python_app
def test_task():
    """Simple test task."""
    import platform

    return f"SUCCESS: Task ran on {platform.node()}"


def main():
    print("🚀 FINAL PRODUCTION TEST")
    print("=" * 40)

    # Create provider
    provider = AWSProvider(label="production_test", init_blocks=1, max_blocks=1)

    config = Config(
        executors=[
            HighThroughputExecutor(
                label="prod_executor", provider=provider, max_workers_per_node=1
            )
        ]
    )

    # Load and run
    parsl.load(config)
    future = test_task()

    try:
        result = future.result(timeout=300)
        print(f"✅ {result}")
        success = True
    except Exception as e:
        print(f"❌ {e}")
        success = False

    parsl.clear()
    return success


if __name__ == "__main__":
    success = main()
    if success:
        print("\n🎉 PRODUCTION READY")
    sys.exit(0 if success else 1)

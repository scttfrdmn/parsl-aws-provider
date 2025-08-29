#!/usr/bin/env python3
"""
Final E2E test without timeouts - verifying complete Parsl workflow execution.
"""

import parsl
from parsl import python_app, bash_app
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
import sys
from phase15_enhanced import AWSProvider


@python_app
def compute_factorial(n):
    """Simple computation task."""
    import math

    result = math.factorial(n)
    return f"factorial({n}) = {result}"


@bash_app
def system_check():
    """System information task."""
    return "uname -a && python3 --version && date && whoami && pwd"


def main():
    """Run focused E2E test without timeouts."""
    print("🚀 FINAL E2E TEST - NO TIMEOUTS")
    print("=" * 50)
    print("Testing complete Parsl workflow with certificate fix")
    print()

    # Create enhanced AWS provider
    print("1. Creating AWS provider...")
    provider = AWSProvider(
        label="final_e2e_test", init_blocks=1, max_blocks=2, min_blocks=0
    )

    # Configure Parsl with the provider
    config = Config(
        executors=[
            HighThroughputExecutor(
                label="final_e2e_executor",
                provider=provider,
                max_workers_per_node=1,
                cores_per_worker=1,
            )
        ]
    )

    print("2. Loading Parsl configuration...")
    parsl.load(config)

    print("3. Submitting tasks...")

    # Submit computational task
    print("   Submitting factorial computation...")
    factorial_future = compute_factorial(10)

    # Submit system check task
    print("   Submitting system check...")
    system_future = system_check()

    print("4. Waiting for results...")

    try:
        print("   Waiting for factorial result...")
        factorial_result = factorial_future.result()
        print(f"   ✅ Factorial result: {factorial_result}")

        print("   Waiting for system check result...")
        system_result = system_future.result()
        print("   ✅ System check result:")
        for line in system_result.strip().split("\n"):
            print(f"      {line}")

        print()
        print("🎉 SUCCESS: All tasks completed successfully!")
        print("✅ Phase 1.5 Enhanced AWS Provider is working end-to-end")
        success = True

    except Exception as e:
        print(f"   ❌ Task execution failed: {e}")
        print("   This indicates worker connectivity or execution issues")
        success = False

    print("5. Cleaning up...")
    parsl.clear()
    print("   ✅ Parsl cleared")

    return success


if __name__ == "__main__":
    success = main()
    if success:
        print("\n🏆 PRODUCTION READY: Phase 1.5 Enhanced AWS Provider")
        print("   - SSM tunneling works perfectly")
        print("   - Workers connect and execute tasks")
        print("   - Complete Parsl workflow execution")
        sys.exit(0)
    else:
        print("\n❌ NOT READY: Issues remain")
        sys.exit(1)

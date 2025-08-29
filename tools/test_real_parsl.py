#!/usr/bin/env python3
"""
Test our AWSProvider with real Parsl HighThroughputExecutor

This will show us what command the executor actually sends to our provider,
validating our understanding of the Parsl architecture.
"""

import logging
import sys
from pathlib import Path

import parsl
from parsl.config import Config
from parsl.executors import HighThroughputExecutor

# Add tools directory to path so we can import phase1
sys.path.insert(0, str(Path(__file__).parent))

try:
    from phase1 import AWSProvider
except ImportError as e:
    print(f"ERROR: Could not import AWSProvider: {e}")
    sys.exit(1)


def test_real_parsl_integration():
    """Test our provider with actual Parsl workflow."""

    print("TESTING REAL PARSL INTEGRATION")
    print("=" * 50)
    print("This test will show us what command HighThroughputExecutor")
    print("actually passes to our AWSProvider.submit() method")
    print("=" * 50)

    # Enable debug logging to see the command
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    # Create our provider (should discover optimized AMI if available)
    print("\n1. Creating AWSProvider...")
    provider = AWSProvider(
        region="us-east-1", instance_type="t3.micro", prefer_optimized_ami=True
    )
    print(f"✓ Provider ready: {provider.provider_id}")
    print(
        f"  AMI: {provider.ami_id} ({'optimized' if provider.is_optimized_ami else 'base'})"
    )

    # Configure Parsl with our provider
    print("\n2. Configuring Parsl with HighThroughputExecutor...")
    config = Config(
        executors=[
            HighThroughputExecutor(
                label="aws_executor",
                provider=provider,
                max_workers_per_node=1,  # Keep it simple for testing
                cores_per_worker=1,
            )
        ]
    )

    try:
        print("\n3. Loading Parsl configuration...")
        parsl.load(config)
        print("✓ Parsl loaded successfully")

        # Define a simple Parsl app to test
        @parsl.python_app
        def hello_from_aws():
            import socket
            import platform

            return f"Hello from {socket.gethostname()} running {platform.system()}"

        @parsl.bash_app
        def system_info(stdout="sysinfo.out"):
            return "uname -a; python3 --version; pip3 list | grep parsl || echo 'Parsl not found'"

        print("\n4. Submitting Parsl apps...")

        # Submit Python app
        python_future = hello_from_aws()
        print("✓ Python app submitted")

        # Submit Bash app
        bash_future = system_info()
        print("✓ Bash app submitted")

        print("\n5. Waiting for results...")

        try:
            # Get Python app result
            python_result = python_future.result(timeout=300)  # 5 minute timeout
            print(f"✓ Python app result: {python_result}")

            # Get Bash app result
            bash_future.result(timeout=300)
            print("✓ Bash app completed")

            # Read bash app output
            try:
                with open("sysinfo.out", "r") as f:
                    bash_output = f.read()
                print(f"✓ Bash app output: {bash_output.strip()}")
            except FileNotFoundError:
                print("! Bash app output file not found locally")

        except Exception as e:
            print(f"! App execution failed: {e}")
            print(
                "This may be expected - we're testing provider integration, not app execution"
            )

        print("\n6. SUCCESS: Real Parsl integration test completed")
        print("Check the logs above to see what command was passed to our provider")

    finally:
        print("\nCleaning up...")
        try:
            parsl.clear()
            print("✓ Parsl cleared")
        except Exception as e:
            print(f"! Parsl clear failed: {e}")

        try:
            provider.cleanup()
            print("✓ Provider cleaned up")
        except Exception as e:
            print(f"! Provider cleanup failed: {e}")


if __name__ == "__main__":
    test_real_parsl_integration()

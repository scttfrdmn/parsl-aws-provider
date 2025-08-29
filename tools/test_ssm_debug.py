#!/usr/bin/env python3
"""
Standalone SSM command execution test to debug worker startup issues.
This isolates the SSM command debugging from all Parsl complexity.
"""

import logging
import sys
import time
from pathlib import Path

# Add tools directory to path
sys.path.insert(0, str(Path(__file__).parent))

from phase15_enhanced import AWSProvider


def setup_logging():
    """Set up logging for the test."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def test_ssm_command_execution():
    """Test SSM command execution with debugging."""
    print("\n🧪 STANDALONE SSM COMMAND DEBUG TEST")
    print("=" * 50)

    provider = None

    try:
        # Create provider
        print("1. Creating enhanced AWS provider...")
        provider = AWSProvider(
            region="us-east-1",
            instance_type="t3.micro",
            enable_ssm_tunneling=True,
            use_private_subnets=False,
            prefer_optimized_ami=True,
        )

        print(f"✓ Provider: {provider.provider_id}")
        print(f"✓ AMI: {provider.ami_id}")

        # Launch instance manually
        print("\n2. Launching instance...")
        launch_config = provider._get_launch_config("debug-test")

        response = provider.ec2.run_instances(MinCount=1, MaxCount=1, **launch_config)
        instance_id = response["Instances"][0]["InstanceId"]
        print(f"✓ Instance launched: {instance_id}")

        # Wait for instance to be ready
        from phase15_enhanced import wait_for_instance

        wait_for_instance(provider.ec2, instance_id)
        print(f"✓ Instance {instance_id} is visible")

        # Wait for SSM agent
        print("\n3. Waiting for SSM agent...")
        import asyncio

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            loop.run_until_complete(
                provider.tunnel_manager._wait_for_ssm_agent(instance_id, timeout=120)
            )
            print("✓ SSM agent ready")
        except Exception as e:
            print(f"❌ SSM agent not ready: {e}")
            return False
        finally:
            loop.close()

        # Send debug SSM command
        print("\n4. Sending debug SSM command...")

        debug_script = """#!/bin/bash
set -e
echo "=== SSM Command Debug Test ==="
echo "Date: $(date)"
echo "User: $(whoami)"
echo "Home: $HOME"
echo "PATH: $PATH"
echo "Working directory: $(pwd)"

# Test basic commands
echo "=== System Info ==="
uname -a
python3 --version || echo "Python3 not found"

# Test Parsl availability
echo "=== Parsl Test ==="
if python3 -c "import parsl; print(f'Parsl version: {parsl.__version__}')" 2>/dev/null; then
    echo "✓ Parsl is available"

    # Test worker script
    echo "=== Worker Script Test ==="
    WORKER_SCRIPT=""
    if which process_worker_pool.py >/dev/null 2>&1; then
        WORKER_SCRIPT="process_worker_pool.py"
        echo "✓ Found process_worker_pool.py in PATH"
    elif python3 -c "import parsl.executors.high_throughput.process_worker_pool" >/dev/null 2>&1; then
        WORKER_SCRIPT="python3 -m parsl.executors.high_throughput.process_worker_pool"
        echo "✓ Found Parsl module"
    else
        echo "❌ Worker script not found"
    fi

    if [ -n "$WORKER_SCRIPT" ]; then
        echo "Testing worker script with --help..."
        timeout 10s $WORKER_SCRIPT --help || echo "Worker script help failed"
    fi
else
    echo "❌ Parsl not available"
fi

echo "=== Debug Complete ==="
"""

        ssm_client = provider.session.client("ssm")
        ssm_response = ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [debug_script]},
            TimeoutSeconds=300,
        )

        command_id = ssm_response["Command"]["CommandId"]
        print(f"✓ SSM command sent: {command_id}")

        # Wait for command completion
        print("\n5. Waiting for command completion...")
        for i in range(30):  # Wait up to 5 minutes
            try:
                response = ssm_client.get_command_invocation(
                    CommandId=command_id, InstanceId=instance_id
                )

                status = response["Status"]
                print(f"   Check {i+1}: Command status = {status}")

                if status in ["Success", "Failed", "Cancelled", "TimedOut"]:
                    print("\n=== COMMAND OUTPUT ===")
                    print(response.get("StandardOutputContent", "No output"))

                    if response.get("StandardErrorContent"):
                        print("\n=== COMMAND ERROR ===")
                        print(response["StandardErrorContent"])

                    break

                time.sleep(10)
            except Exception as e:
                print(f"   Check {i+1}: Error getting command status: {e}")
                time.sleep(10)

        print("\n6. Test completed")
        return True

    except Exception as e:
        print(f"❌ Test failed: {e}")
        import traceback

        traceback.print_exc()
        return False

    finally:
        if provider:
            print("\n7. Cleaning up...")
            provider.cleanup()
            print("✓ Cleanup completed")


if __name__ == "__main__":
    setup_logging()
    success = test_ssm_command_execution()
    sys.exit(0 if success else 1)

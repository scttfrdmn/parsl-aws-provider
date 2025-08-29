#!/usr/bin/env python3
"""
Final diagnostic test to check actual worker execution on remote instance.
"""

import logging
import sys
import time
from pathlib import Path

# Add tools directory to path
sys.path.insert(0, str(Path(__file__).parent))

from phase15_enhanced import AWSProvider
from ssm_tunnel import ParslWorkerCommandParser


def setup_logging():
    """Set up logging for the test."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def test_final_worker_execution():
    """Test actual worker execution with full diagnostics."""
    print("\n🔬 FINAL WORKER DIAGNOSTIC TEST")
    print("=" * 60)

    provider = None

    try:
        # Create provider
        print("1. Creating provider...")
        provider = AWSProvider(
            region="us-east-1",
            instance_type="t3.micro",
            enable_ssm_tunneling=True,
            use_private_subnets=False,
            prefer_optimized_ami=True,
        )
        print(f"✓ Provider: {provider.provider_id}")

        # Launch instance manually
        print("\n2. Launching instance...")
        launch_config = provider._get_launch_config("final-test")

        response = provider.ec2.run_instances(MinCount=1, MaxCount=1, **launch_config)
        instance_id = response["Instances"][0]["InstanceId"]
        print(f"✓ Instance launched: {instance_id}")

        # Wait for SSM agent
        print("\n3. Waiting for SSM agent...")
        import asyncio

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            from phase15_enhanced import wait_for_instance

            wait_for_instance(provider.ec2, instance_id)
            loop.run_until_complete(
                provider.tunnel_manager._wait_for_ssm_agent(instance_id, timeout=120)
            )
            print("✓ SSM agent ready")
        finally:
            loop.close()

        # Create test worker command with our fixes
        print("\n4. Creating test worker command...")
        original_command = "process_worker_pool.py --max_workers_per_node=1 -a 127.0.0.1,192.168.1.245 -p 0 -c 1 -m None --poll 10 --port=55000 --cert_dir None --logdir=/Users/test/logs --block_id=test-block --hb_period=30 --hb_threshold=120 --drain_period=None --cpu-affinity none --mpi-launcher=mpiexec --available-accelerators"

        # Apply our fixes
        fixed_command = ParslWorkerCommandParser.modify_for_tunnel(
            original_command, 50020
        )

        print(f"   Original: {original_command}")
        print(f"   Fixed: {fixed_command}")

        # Send comprehensive diagnostic SSM command
        print("\n5. Sending diagnostic command...")

        diagnostic_script = f"""#!/bin/bash
set -e
echo "=== FINAL WORKER DIAGNOSTIC ==="
echo "Date: $(date)"
echo "Instance ID: {instance_id}"

# Test the fixed worker command
echo ""
echo "=== TESTING FIXED WORKER COMMAND ==="
echo "Command: {fixed_command}"

# Create log directory
mkdir -p /tmp/parsl_logs

# Test connectivity to tunnel port
echo ""
echo "=== TESTING TUNNEL CONNECTIVITY ==="
echo "Testing connection to localhost:50020..."
timeout 5s bash -c "</dev/tcp/127.0.0.1/50020" 2>/dev/null && echo "✅ Port 50020 reachable" || echo "❌ Port 50020 not reachable"

# Test worker script directly
echo ""
echo "=== TESTING WORKER SCRIPT ==="
echo "Testing process_worker_pool.py --help..."
timeout 10s process_worker_pool.py --help >/dev/null 2>&1 && echo "✅ Worker script functional" || echo "❌ Worker script failed"

# Try to run the actual worker command (with timeout)
echo ""
echo "=== TESTING ACTUAL WORKER EXECUTION ==="
echo "Attempting to run worker (30s timeout)..."

# Run the worker command in background and capture PID
timeout 30s {fixed_command} >/tmp/parsl_logs/worker_test.log 2>&1 &
WORKER_PID=$!

if [ -n "$WORKER_PID" ]; then
    echo "✅ Worker started with PID $WORKER_PID"

    # Wait a moment and check if it's still running
    sleep 5
    if kill -0 $WORKER_PID 2>/dev/null; then
        echo "✅ Worker still running after 5s"

        # Check worker log
        if [ -f /tmp/parsl_logs/worker_test.log ]; then
            echo "✅ Worker log created"
            echo "--- Worker log (first 10 lines) ---"
            head -10 /tmp/parsl_logs/worker_test.log
            echo "--- End worker log ---"
        else
            echo "❌ No worker log found"
        fi

        # Kill the worker
        kill $WORKER_PID 2>/dev/null || true
    else
        echo "❌ Worker exited after 5s"
        if [ -f /tmp/parsl_logs/worker_test.log ]; then
            echo "--- Worker error log ---"
            cat /tmp/parsl_logs/worker_test.log
            echo "--- End error log ---"
        fi
    fi
else
    echo "❌ Failed to start worker"
fi

echo ""
echo "=== DIAGNOSTIC COMPLETE ==="
"""

        ssm_client = provider.session.client("ssm")
        ssm_response = ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [diagnostic_script]},
            TimeoutSeconds=300,
        )

        command_id = ssm_response["Command"]["CommandId"]
        print(f"✓ Diagnostic command sent: {command_id}")

        # Wait for completion and get results
        print("\n6. Waiting for diagnostic results...")
        for i in range(30):
            try:
                response = ssm_client.get_command_invocation(
                    CommandId=command_id, InstanceId=instance_id
                )

                status = response["Status"]
                if status in ["Success", "Failed", "Cancelled", "TimedOut"]:
                    print("\n=== DIAGNOSTIC RESULTS ===")
                    print(response.get("StandardOutputContent", "No output"))

                    if response.get("StandardErrorContent"):
                        print("\n=== DIAGNOSTIC ERRORS ===")
                        print(response["StandardErrorContent"])

                    return status == "Success"

                print(f"   Status: {status} (check {i+1}/30)")
                time.sleep(10)

            except Exception as e:
                print(f"   Error checking status: {e}")
                time.sleep(10)

        print("❌ Diagnostic timed out")
        return False

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
    success = test_final_worker_execution()
    print("\n" + "=" * 60)
    print(f"🔬 FINAL DIAGNOSTIC: {'SUCCESS' if success else 'FAILED'}")
    print("=" * 60)
    sys.exit(0 if success else 1)

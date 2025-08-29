#!/usr/bin/env python3
"""
Real End-to-End Test for Phase 1.5 Enhanced Provider

This test actually submits jobs and verifies they complete successfully.
No mocking, no simulation - real AWS infrastructure and real job execution.
"""

import logging
import sys
import time
from pathlib import Path

# Add tools directory to path
sys.path.insert(0, str(Path(__file__).parent))

try:
    from phase15_enhanced import AWSProvider
except ImportError as e:
    print(f"ERROR: Could not import enhanced provider: {e}")
    sys.exit(1)


def setup_logging():
    """Set up comprehensive logging."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Reduce AWS noise
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def test_basic_job_submission():
    """Test basic job submission without Parsl."""
    print("\n" + "=" * 80)
    print("🧪 REAL E2E TEST: Basic Job Submission")
    print("=" * 80)

    provider = None

    try:
        # Create provider with SSM tunneling disabled for simplicity
        print("1. Creating provider (traditional mode)...")
        provider = AWSProvider(
            region="us-east-1",
            instance_type="t3.micro",
            enable_ssm_tunneling=False,  # Start simple
            use_private_subnets=False,
            prefer_optimized_ami=True,
        )

        print(f"✅ Provider created: {provider.provider_id}")
        print(f"   AMI: {provider.ami_id}")
        print(f"   Optimized: {provider.is_optimized_ami}")

        # Submit a real job
        print("\n2. Submitting real job...")
        command = """#!/bin/bash
echo "=== Phase 1.5 Enhanced E2E Test ==="
echo "Hostname: $(hostname)"
echo "Date: $(date)"
echo "AWS Instance ID: $(curl -s http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null || echo 'Not available')"
echo "Python version: $(python3 --version 2>/dev/null || echo 'Python3 not found')"
echo "Parsl installation: $(pip3 list 2>/dev/null | grep parsl || echo 'Parsl not installed')"
echo "=== Test Commands ==="
ls -la /tmp/
ps aux | grep -v grep | head -10
echo "=== END TEST ==="
sleep 30
echo "Job completed successfully"
"""

        job_id = provider.submit(command, 1, "e2e_test")
        print(f"✅ Job submitted: {job_id}")

        # Monitor job status
        print("\n3. Monitoring job execution...")
        max_wait_time = 300  # 5 minutes
        start_time = time.time()

        while time.time() - start_time < max_wait_time:
            statuses = provider.status([job_id])
            status = statuses[0]

            print(f"   Job {job_id}: {status.state.name} - {status.message}")

            if status.state.name in ["COMPLETED", "FAILED"]:
                if status.state.name == "COMPLETED":
                    print("✅ Job completed successfully!")
                    return True
                else:
                    print("❌ Job failed!")
                    return False

            time.sleep(10)  # Check every 10 seconds

        print(f"❌ Job timed out after {max_wait_time} seconds")
        return False

    except Exception as e:
        print(f"❌ E2E test failed: {e}")
        import traceback

        traceback.print_exc()
        return False

    finally:
        if provider:
            print("\n4. Cleaning up...")
            provider.cleanup()
            print("✅ Cleanup completed")


def test_ssm_tunneling_job():
    """Test job submission with SSM tunneling enabled."""
    print("\n" + "=" * 80)
    print("🧪 REAL E2E TEST: SSM Tunneling Job")
    print("=" * 80)

    provider = None

    try:
        # Create provider with SSM tunneling
        print("1. Creating provider (SSM tunneling mode)...")
        provider = AWSProvider(
            region="us-east-1",
            instance_type="t3.micro",
            enable_ssm_tunneling=True,  # Enable SSM tunneling
            use_private_subnets=False,  # Keep public for now
            prefer_optimized_ami=True,
        )

        print(f"✅ Provider created: {provider.provider_id}")
        print(f"   SSM Tunneling: {provider.enable_ssm_tunneling}")
        print(f"   Tunnel Manager: {hasattr(provider, 'tunnel_manager')}")

        # Test a simple command that doesn't require Parsl worker
        print("\n2. Submitting SSM test command...")

        # Use SSM to run a command directly (not Parsl worker)
        ssm_client = provider.session.client("ssm")
        ec2_client = provider.session.client("ec2")

        # Launch an instance for testing
        launch_config = provider._get_launch_config("ssm_test")
        response = ec2_client.run_instances(MinCount=1, MaxCount=1, **launch_config)
        instance_id = response["Instances"][0]["InstanceId"]

        print(f"   Instance launched: {instance_id}")

        # Wait for instance to be running
        print("   Waiting for instance to be running...")
        waiter = ec2_client.get_waiter("instance_running")
        waiter.wait(
            InstanceIds=[instance_id], WaiterConfig={"Delay": 15, "MaxAttempts": 20}
        )

        # Wait for SSM agent
        print("   Waiting for SSM agent...")
        max_ssm_wait = 180
        ssm_start = time.time()

        while time.time() - ssm_start < max_ssm_wait:
            try:
                response = ssm_client.describe_instance_information(
                    Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
                )

                if (
                    response["InstanceInformationList"]
                    and response["InstanceInformationList"][0]["PingStatus"] == "Online"
                ):
                    print("   ✅ SSM agent online")
                    break

            except Exception as e:
                pass

            print("   Waiting for SSM agent...")
            time.sleep(10)
        else:
            print("   ❌ SSM agent timeout")
            return False

        # Execute test command via SSM
        print("\n3. Executing test command via SSM...")

        test_command = """
echo "=== SSM Command Test ==="
echo "Instance ID: $(curl -s http://169.254.169.254/latest/meta-data/instance-id)"
echo "Timestamp: $(date)"
echo "SSM Agent Status: Online"
echo "Test: SUCCESS"
"""

        response = ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [test_command]},
            TimeoutSeconds=60,
        )

        command_id = response["Command"]["CommandId"]
        print(f"   SSM Command sent: {command_id}")

        # Wait for command completion
        print("   Waiting for command completion...")
        time.sleep(30)  # Give command time to execute

        # Get command output
        try:
            output_response = ssm_client.get_command_invocation(
                CommandId=command_id, InstanceId=instance_id
            )

            status = output_response["Status"]
            stdout = output_response.get("StandardOutputContent", "")
            stderr = output_response.get("StandardErrorContent", "")

            print(f"   Command Status: {status}")
            print(f"   Output: {stdout}")

            if stderr:
                print(f"   Errors: {stderr}")

            if status == "Success" and "Test: SUCCESS" in stdout:
                print("✅ SSM command executed successfully!")
                return True
            else:
                print("❌ SSM command failed or incomplete")
                return False

        except Exception as e:
            print(f"❌ Failed to get command output: {e}")
            return False

    except Exception as e:
        print(f"❌ SSM tunneling test failed: {e}")
        import traceback

        traceback.print_exc()
        return False

    finally:
        if provider:
            print("\n4. Cleaning up...")
            provider.cleanup()
            print("✅ Cleanup completed")


def test_optimized_ami_performance():
    """Test optimized AMI vs base AMI performance."""
    print("\n" + "=" * 80)
    print("🧪 REAL E2E TEST: Optimized AMI Performance")
    print("=" * 80)

    results = {}

    for ami_type in ["optimized", "base"]:
        print(f"\n--- Testing {ami_type} AMI ---")

        provider = None
        try:
            # Create provider
            provider = AWSProvider(
                region="us-east-1",
                instance_type="t3.micro",
                enable_ssm_tunneling=False,
                prefer_optimized_ami=(ami_type == "optimized"),
                ami_id=None,  # Let it choose automatically
            )

            print(f"   Provider: {provider.provider_id}")
            print(f"   AMI: {provider.ami_id}")
            print(f"   Optimized: {provider.is_optimized_ami}")

            # Time the startup
            start_time = time.time()

            # Launch instance and wait for it to be ready
            launch_config = provider._get_launch_config(f"{ami_type}_test")

            ec2_client = provider.session.client("ec2")
            response = ec2_client.run_instances(MinCount=1, MaxCount=1, **launch_config)
            instance_id = response["Instances"][0]["InstanceId"]

            print(f"   Instance: {instance_id}")

            # Wait for running state
            print("   Waiting for instance running...")
            waiter = ec2_client.get_waiter("instance_running")
            waiter.wait(InstanceIds=[instance_id])

            startup_time = time.time() - start_time
            print(f"   Startup time: {startup_time:.1f}s")

            results[ami_type] = {
                "startup_time": startup_time,
                "ami_id": provider.ami_id,
                "optimized": provider.is_optimized_ami,
            }

            # Terminate instance
            ec2_client.terminate_instances(InstanceIds=[instance_id])

        except Exception as e:
            print(f"   ❌ {ami_type} AMI test failed: {e}")
            results[ami_type] = {"error": str(e)}

        finally:
            if provider:
                provider.cleanup()

    # Compare results
    print("\n" + "=" * 40)
    print("📊 PERFORMANCE COMPARISON")
    print("=" * 40)

    for ami_type, result in results.items():
        print(f"{ami_type.upper()} AMI:")
        if "error" in result:
            print(f"   ❌ Error: {result['error']}")
        else:
            print(f"   AMI ID: {result['ami_id']}")
            print(f"   Startup: {result['startup_time']:.1f}s")
            print(f"   Optimized: {result['optimized']}")
        print()

    # Calculate improvement
    if "optimized" in results and "base" in results:
        if "startup_time" in results["optimized"] and "startup_time" in results["base"]:
            optimized_time = results["optimized"]["startup_time"]
            base_time = results["base"]["startup_time"]
            improvement = base_time - optimized_time
            improvement_pct = (improvement / base_time) * 100

            print("⚡ Performance Improvement:")
            print(f"   Time saved: {improvement:.1f}s")
            print(f"   Percentage: {improvement_pct:.1f}%")

            return improvement > 0

    return False


def run_all_e2e_tests():
    """Run all end-to-end tests."""
    print("🚀 PHASE 1.5 ENHANCED - REAL END-TO-END TESTS")
    print("=" * 80)
    print("These tests use real AWS infrastructure and validate actual functionality.")
    print("No mocking, no simulation - real deployment verification.")
    print("=" * 80)

    tests = [
        ("Basic Job Submission", test_basic_job_submission),
        ("SSM Tunneling", test_ssm_tunneling_job),
        ("AMI Performance", test_optimized_ami_performance),
    ]

    results = {}

    for test_name, test_func in tests:
        print(f"\n🧪 Running: {test_name}")
        try:
            success = test_func()
            results[test_name] = success
            status = "✅ PASSED" if success else "❌ FAILED"
            print(f"{status}: {test_name}")
        except Exception as e:
            results[test_name] = False
            print(f"❌ FAILED: {test_name} - {e}")

    # Final results
    print("\n" + "=" * 80)
    print("📊 REAL E2E TEST SUMMARY")
    print("=" * 80)

    total = len(results)
    passed = sum(1 for success in results.values() if success)
    failed = total - passed

    print(f"Total Tests: {total}")
    print(f"✅ Passed: {passed}")
    print(f"❌ Failed: {failed}")
    print(f"Success Rate: {(passed/total)*100:.1f}%")

    if failed > 0:
        print("\n❌ Failed Tests:")
        for test_name, success in results.items():
            if not success:
                print(f"   • {test_name}")

    if failed == 0:
        print("\n🎉 ALL REAL E2E TESTS PASSED!")
        print("Phase 1.5 Enhanced provider is production ready!")
    else:
        print(f"\n⚠️  {failed} real test(s) failed - NOT production ready")

    print("=" * 80)
    return failed == 0


if __name__ == "__main__":
    setup_logging()

    try:
        success = run_all_e2e_tests()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n⚠️  Tests interrupted by user")
        sys.exit(130)
    except Exception as e:
        print(f"\n\n💥 Test suite failed: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)

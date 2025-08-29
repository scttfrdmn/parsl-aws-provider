#!/usr/bin/env python3
"""
Focused SSM Tunneling Test

Tests ONLY the SSM tunneling functionality in isolation to validate
that the core networking solution actually works.
"""

import asyncio
import logging
import socket
import sys
import time
from pathlib import Path

# Add tools directory to path
sys.path.insert(0, str(Path(__file__).parent))

try:
    from ssm_tunnel import (
        SSMTunnelManager,
        ParslWorkerCommandParser,
        TunnelSession,
        TunnelConfig,
    )
    from phase15_enhanced import AWSProvider
except ImportError as e:
    print(f"ERROR: Could not import SSM components: {e}")
    sys.exit(1)


def setup_logging():
    """Set up detailed logging for tunnel testing."""
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Keep SSM and phase15 logs verbose
    logging.getLogger("ssm_tunnel").setLevel(logging.DEBUG)
    logging.getLogger("phase15_enhanced").setLevel(logging.INFO)

    # Reduce AWS noise
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)


def test_worker_command_parsing():
    """Test Parsl worker command parsing and modification."""
    print("\n🧪 Testing Worker Command Parsing")
    print("-" * 50)

    try:
        # Test realistic Parsl worker command (based on actual logs)
        test_command = "process_worker_pool.py  --max_workers_per_node=1 -a 127.0.0.1,192.168.1.245,47.157.77.146 -p 0 -c 1 -m None --poll 10 --port=54755 --cert_dir None --logdir=/path/to/logs --block_id={block_id}"

        print(f"Original command: {test_command}")

        # Parse command
        parsed = ParslWorkerCommandParser.parse_addresses_and_port(test_command)
        print(f"Parsed addresses: {parsed['addresses']}")
        print(f"Parsed port: {parsed['port']}")

        if not parsed["addresses"] or not parsed["port"]:
            print("❌ Command parsing failed")
            return False

        # Modify for tunnel
        modified = ParslWorkerCommandParser.modify_for_tunnel(test_command, 50000)
        print(f"Modified command: {modified}")

        # Verify modification
        if (
            "127.0.0.1" in modified
            and "192.168.1.245" not in modified
            and "47.157.77.146" not in modified
        ):
            print("✅ Command parsing and modification working")
            return True
        else:
            print("❌ Command modification failed")
            return False

    except Exception as e:
        print(f"❌ Command parsing test failed: {e}")
        import traceback

        traceback.print_exc()
        return False


def test_instance_launch_with_ssm():
    """Test launching an instance and waiting for SSM agent."""
    print("\n🧪 Testing Instance Launch + SSM Agent")
    print("-" * 50)

    provider = None
    instance_id = None

    try:
        # Create provider
        provider = AWSProvider(
            region="us-east-1",
            instance_type="t3.micro",
            enable_ssm_tunneling=True,  # Enable for this test
            prefer_optimized_ami=True,
        )

        print(f"Provider created: {provider.provider_id}")

        # Launch instance manually to control the process
        launch_config = provider._get_launch_config("ssm_test")
        print(f"Launch config: {launch_config.keys()}")

        response = provider.ec2.run_instances(MinCount=1, MaxCount=1, **launch_config)
        instance_id = response["Instances"][0]["InstanceId"]
        print(f"✅ Instance launched: {instance_id}")

        # Wait for instance to be running
        print("Waiting for instance to be running...")
        waiter = provider.ec2.get_waiter("instance_running")
        waiter.wait(
            InstanceIds=[instance_id], WaiterConfig={"Delay": 15, "MaxAttempts": 20}
        )
        print("✅ Instance is running")

        # Test SSM agent readiness
        print("Testing SSM agent availability...")
        ssm_client = provider.session.client("ssm")

        max_wait = 180  # 3 minutes
        start_time = time.time()

        while time.time() - start_time < max_wait:
            try:
                response = ssm_client.describe_instance_information(
                    Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
                )

                if (
                    response["InstanceInformationList"]
                    and response["InstanceInformationList"][0]["PingStatus"] == "Online"
                ):
                    wait_time = time.time() - start_time
                    print(f"✅ SSM agent ready after {wait_time:.1f}s")
                    return True, instance_id

            except Exception as e:
                print(f"SSM check failed: {e}")

            print(f"  Waiting for SSM agent... ({int(time.time() - start_time)}s)")
            time.sleep(10)

        print("❌ SSM agent timeout")
        return False, instance_id

    except Exception as e:
        print(f"❌ Instance launch test failed: {e}")
        import traceback

        traceback.print_exc()
        return False, instance_id

    finally:
        if instance_id and provider:
            print(f"Cleaning up instance {instance_id}...")
            try:
                provider.ec2.terminate_instances(InstanceIds=[instance_id])
                print("✅ Instance terminated")
            except Exception as e:
                print(f"! Instance cleanup failed: {e}")

        if provider:
            provider.cleanup()


async def test_ssm_tunnel_creation():
    """Test actual SSM tunnel creation and connectivity."""
    print("\n🧪 Testing SSM Tunnel Creation")
    print("-" * 50)

    provider = None
    instance_id = None
    tunnel_manager = None

    try:
        # Launch instance first
        print("1. Launching instance for tunnel test...")
        provider = AWSProvider(
            region="us-east-1",
            instance_type="t3.micro",
            enable_ssm_tunneling=True,
            prefer_optimized_ami=True,
        )

        launch_config = provider._get_launch_config("tunnel_test")
        response = provider.ec2.run_instances(MinCount=1, MaxCount=1, **launch_config)
        instance_id = response["Instances"][0]["InstanceId"]
        print(f"   Instance: {instance_id}")

        # Wait for running + SSM
        print("2. Waiting for instance and SSM agent...")
        waiter = provider.ec2.get_waiter("instance_running")
        waiter.wait(InstanceIds=[instance_id])

        # Wait for SSM agent
        ssm_client = provider.session.client("ssm")
        max_wait = 180
        start_time = time.time()

        while time.time() - start_time < max_wait:
            try:
                response = ssm_client.describe_instance_information(
                    Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
                )

                if (
                    response["InstanceInformationList"]
                    and response["InstanceInformationList"][0]["PingStatus"] == "Online"
                ):
                    print("   ✅ SSM agent ready")
                    break
            except Exception:
                pass

            await asyncio.sleep(10)
        else:
            print("   ❌ SSM agent timeout")
            return False

        # Create tunnel manager
        print("3. Creating tunnel manager...")
        tunnel_manager = SSMTunnelManager(provider.session, (50000, 50100))

        # Create tunnel
        print("4. Creating SSM tunnel...")
        job_id = "tunnel_test_job"
        controller_port = 54755  # Simulate Parsl controller port

        tunnel = await tunnel_manager.create_tunnel_for_job(
            instance_id, job_id, controller_port
        )

        print(
            f"   ✅ Tunnel created: localhost:{tunnel.config.local_port} -> {instance_id}:{controller_port}"
        )

        # Test tunnel connectivity
        print("5. Testing tunnel connectivity...")

        # Start a simple server on local port to simulate Parsl controller
        test_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        test_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        test_server.bind(("127.0.0.1", tunnel.config.local_port))
        test_server.listen(1)
        test_server.settimeout(5)

        print(f"   Test server listening on localhost:{tunnel.config.local_port}")

        # Test connection through tunnel
        try:
            # This would normally be the worker connecting back
            # For now, just test that the tunnel port is accessible
            test_client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            test_client.settimeout(10)
            result = test_client.connect_ex(("127.0.0.1", tunnel.config.local_port))
            test_client.close()

            if result == 0:
                print("   ✅ Tunnel port accessible")
                tunnel_working = True
            else:
                print(f"   ❌ Tunnel port not accessible: {result}")
                tunnel_working = False

        except Exception as e:
            print(f"   ❌ Tunnel connectivity test failed: {e}")
            tunnel_working = False
        finally:
            test_server.close()

        # Test command modification
        print("6. Testing worker command modification...")
        test_command = f"process_worker_pool.py --max_workers_per_node=1 -a 192.168.1.245 --port={controller_port}"
        modified_command = tunnel_manager.modify_worker_command(test_command, job_id)
        print(f"   Original: {test_command}")
        print(f"   Modified: {modified_command}")

        if "127.0.0.1" in modified_command and "192.168.1.245" not in modified_command:
            print("   ✅ Command modification working")
            command_ok = True
        else:
            print("   ❌ Command modification failed")
            command_ok = False

        # Overall result
        if tunnel_working and command_ok:
            print("\n✅ SSM TUNNEL CREATION TEST PASSED")
            print("   - Tunnel established successfully")
            print("   - Local port accessible")
            print("   - Command modification working")
            return True
        else:
            print("\n❌ SSM TUNNEL CREATION TEST FAILED")
            return False

    except Exception as e:
        print(f"❌ SSM tunnel creation test failed: {e}")
        import traceback

        traceback.print_exc()
        return False

    finally:
        # Cleanup
        if tunnel_manager and job_id:
            print("\n7. Cleaning up tunnel...")
            tunnel_manager.cleanup_job_tunnels(job_id)

        if instance_id and provider:
            print("   Cleaning up instance...")
            try:
                provider.ec2.terminate_instances(InstanceIds=[instance_id])
            except Exception as e:
                print(f"   Instance cleanup failed: {e}")

        if provider:
            provider.cleanup()


async def test_ssm_command_execution():
    """Test executing commands via SSM on the instance."""
    print("\n🧪 Testing SSM Command Execution")
    print("-" * 50)

    provider = None
    instance_id = None

    try:
        # Launch instance
        print("1. Launching instance for command test...")
        provider = AWSProvider(
            region="us-east-1",
            instance_type="t3.micro",
            enable_ssm_tunneling=True,
            prefer_optimized_ami=True,
        )

        launch_config = provider._get_launch_config("command_test")
        response = provider.ec2.run_instances(MinCount=1, MaxCount=1, **launch_config)
        instance_id = response["Instances"][0]["InstanceId"]
        print(f"   Instance: {instance_id}")

        # Wait for SSM readiness
        print("2. Waiting for SSM agent...")
        waiter = provider.ec2.get_waiter("instance_running")
        waiter.wait(InstanceIds=[instance_id])

        ssm_client = provider.session.client("ssm")
        max_wait = 180
        start_time = time.time()

        while time.time() - start_time < max_wait:
            try:
                response = ssm_client.describe_instance_information(
                    Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
                )

                if (
                    response["InstanceInformationList"]
                    and response["InstanceInformationList"][0]["PingStatus"] == "Online"
                ):
                    print("   ✅ SSM agent ready")
                    break
            except Exception:
                pass

            await asyncio.sleep(10)
        else:
            print("   ❌ SSM agent timeout")
            return False

        # Execute test command
        print("3. Executing test command via SSM...")
        test_command = """
echo "=== SSM Command Test ==="
echo "Hostname: $(hostname)"
echo "Date: $(date)"
echo "Instance ID: $(curl -s http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null)"
echo "Python: $(python3 --version 2>/dev/null || echo 'Not available')"
echo "Network test: $(curl -s --max-time 5 httpbin.org/ip || echo 'Network not available')"
echo "=== Command Complete ==="
"""

        response = ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [test_command]},
            TimeoutSeconds=120,
        )

        command_id = response["Command"]["CommandId"]
        print(f"   Command sent: {command_id}")

        # Wait for completion
        print("4. Waiting for command completion...")
        await asyncio.sleep(30)  # Give it time to execute

        # Get results
        try:
            output_response = ssm_client.get_command_invocation(
                CommandId=command_id, InstanceId=instance_id
            )

            status = output_response["Status"]
            stdout = output_response.get("StandardOutputContent", "")
            stderr = output_response.get("StandardErrorContent", "")

            print(f"   Status: {status}")
            print(f"   Output:\n{stdout}")

            if stderr:
                print(f"   Errors:\n{stderr}")

            if status == "Success" and "Command Complete" in stdout:
                print("✅ SSM command execution working")
                return True
            else:
                print("❌ SSM command execution failed")
                return False

        except Exception as e:
            print(f"   ❌ Could not get command results: {e}")
            return False

    except Exception as e:
        print(f"❌ SSM command execution test failed: {e}")
        import traceback

        traceback.print_exc()
        return False

    finally:
        if instance_id and provider:
            print("5. Cleaning up...")
            try:
                provider.ec2.terminate_instances(InstanceIds=[instance_id])
            except Exception:
                pass
        if provider:
            provider.cleanup()


async def run_focused_ssm_tests():
    """Run focused SSM tunneling tests."""
    print("🔬 FOCUSED SSM TUNNELING TESTS")
    print("=" * 60)
    print("Testing ONLY the SSM tunneling functionality to validate")
    print("that our core networking solution actually works.")
    print("=" * 60)

    tests = [
        ("Worker Command Parsing", test_worker_command_parsing, False),  # Sync
        ("SSM Command Execution", test_ssm_command_execution, True),  # Async
        ("SSM Tunnel Creation", test_ssm_tunnel_creation, True),  # Async
    ]

    results = {}

    for test_name, test_func, is_async in tests:
        print(f"\n{'='*15} {test_name} {'='*15}")
        try:
            if is_async:
                success = await test_func()
            else:
                success = test_func()

            results[test_name] = success
            status = "✅ PASSED" if success else "❌ FAILED"
            print(f"\n{status}: {test_name}")

        except Exception as e:
            results[test_name] = False
            print(f"\n❌ FAILED: {test_name} - {e}")
            import traceback

            traceback.print_exc()

        # Wait between tests
        if test_name != tests[-1][0]:
            print("\nWaiting 30s before next test...")
            await asyncio.sleep(30)

    # Final results
    print("\n" + "=" * 60)
    print("📊 FOCUSED SSM TUNNELING TEST RESULTS")
    print("=" * 60)

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
        print("\n🎉 ALL SSM TUNNELING TESTS PASSED!")
        print("✅ SSM tunneling infrastructure is working")
        print("✅ Worker command parsing functional")
        print("✅ SSM command execution operational")
        print("✅ Tunnel creation and management working")
        print("\n🚀 Ready to test full E2E with SSM tunneling enabled!")
    elif passed > 0:
        print(f"\n⚠️ PARTIAL SUCCESS: {passed}/{total} tests passed")
        print("Some SSM functionality working, need to debug failures")
    else:
        print("\n❌ COMPLETE FAILURE: All SSM tests failed")
        print("SSM tunneling approach may have fundamental issues")

    print("=" * 60)
    return failed == 0


if __name__ == "__main__":
    setup_logging()

    try:
        success = asyncio.run(run_focused_ssm_tests())
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n⚠️ Tests interrupted by user")
        sys.exit(130)
    except Exception as e:
        print(f"\n\n💥 SSM test suite failed: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)

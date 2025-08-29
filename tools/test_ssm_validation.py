#!/usr/bin/env python3
"""
SSM Validation Test - Deterministic and Focused

Tests the essential SSM functionality needed for tunneling in a deterministic way.
"""

import asyncio
import logging
import sys
from pathlib import Path

# Add tools directory to path
sys.path.insert(0, str(Path(__file__).parent))

try:
    from ssm_tunnel import SSMTunnelManager, ParslWorkerCommandParser
    from phase15_enhanced import AWSProvider
except ImportError as e:
    print(f"ERROR: Could not import components: {e}")
    sys.exit(1)


def setup_logging():
    """Set up logging for validation test."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


async def validate_ssm_infrastructure():
    """Validate SSM infrastructure readiness."""
    print("\n🔬 SSM Infrastructure Validation")
    print("-" * 60)

    provider = None
    instance_id = None

    try:
        # Create provider
        print("1. Creating provider with SSM tunneling...")
        provider = AWSProvider(
            region="us-east-1",
            instance_type="t3.micro",
            enable_ssm_tunneling=True,
            prefer_optimized_ami=True,
        )
        print(f"   ✅ Provider: {provider.provider_id}")

        # Launch instance
        print("2. Launching instance with SSM configuration...")
        launch_config = provider._get_launch_config("ssm_validation")

        # Verify launch config has IAM instance profile
        if "IamInstanceProfile" in launch_config:
            print(
                f"   ✅ IAM instance profile configured: {launch_config['IamInstanceProfile']}"
            )
        else:
            print("   ⚠️  No IAM instance profile - SSM may not work")

        response = provider.ec2.run_instances(MinCount=1, MaxCount=1, **launch_config)
        instance_id = response["Instances"][0]["InstanceId"]
        print(f"   ✅ Instance launched: {instance_id}")

        # Wait for instance running with explicit waiter
        print("3. Waiting for instance to reach running state...")
        waiter = provider.ec2.get_waiter("instance_running")
        waiter.wait(
            InstanceIds=[instance_id],
            WaiterConfig={
                "Delay": 15,  # Check every 15 seconds
                "MaxAttempts": 30,  # Up to 7.5 minutes
            },
        )
        print("   ✅ Instance is running")

        # Wait for SSM agent with improved method
        print("4. Waiting for SSM agent readiness...")
        print("   This is the critical test - can we establish SSM connectivity?")

        try:
            await provider.tunnel_manager._wait_for_ssm_agent(instance_id, timeout=400)
            print("   ✅ SSM agent is ready and responsive")
            ssm_ready = True
        except Exception as e:
            print(f"   ❌ SSM agent failed: {e}")
            ssm_ready = False

        # Test basic SSM command if agent is ready
        if ssm_ready:
            print("5. Testing basic SSM command execution...")
            ssm_client = provider.session.client("ssm")

            try:
                response = ssm_client.send_command(
                    InstanceIds=[instance_id],
                    DocumentName="AWS-RunShellScript",
                    Parameters={
                        "commands": ['echo "SSM test successful"; whoami; date']
                    },
                    TimeoutSeconds=30,
                )

                command_id = response["Command"]["CommandId"]
                print(f"   ✅ Command sent: {command_id}")

                # Wait for command completion
                await asyncio.sleep(15)

                # Get command results
                try:
                    output = ssm_client.get_command_invocation(
                        CommandId=command_id, InstanceId=instance_id
                    )

                    if output["Status"] == "Success":
                        print("   ✅ SSM command executed successfully")
                        print(
                            f"   Output: {output.get('StandardOutputContent', '')[:100]}..."
                        )
                        return True
                    else:
                        print(f"   ❌ Command failed with status: {output['Status']}")
                        return False

                except Exception as e:
                    print(f"   ❌ Could not get command output: {e}")
                    return False

            except Exception as e:
                print(f"   ❌ SSM command failed: {e}")
                return False
        else:
            print("5. Skipping SSM command test - agent not ready")
            return False

    except Exception as e:
        print(f"❌ Infrastructure validation failed: {e}")
        import traceback

        traceback.print_exc()
        return False

    finally:
        # Cleanup
        if instance_id and provider:
            print("6. Cleaning up resources...")
            try:
                provider.ec2.terminate_instances(InstanceIds=[instance_id])
                print("   ✅ Instance terminated")
            except Exception as e:
                print(f"   ⚠️  Instance cleanup failed: {e}")

        if provider:
            try:
                provider.cleanup()
                print("   ✅ Provider cleaned up")
            except Exception as e:
                print(f"   ⚠️  Provider cleanup failed: {e}")


def validate_command_parsing():
    """Validate Parsl worker command parsing."""
    print("\n🔬 Command Parsing Validation")
    print("-" * 60)

    # Test cases from real Parsl output
    test_cases = [
        {
            "name": "Standard Parsl Command",
            "command": "process_worker_pool.py --max_workers_per_node=1 -a 127.0.0.1,192.168.1.245 -p 0 -c 1 --poll 10 --port=54755",
            "expected_addresses": "127.0.0.1,192.168.1.245",
            "expected_port": "54755",
        },
        {
            "name": "Multi-Address Command",
            "command": "process_worker_pool.py -a 10.0.1.1,192.168.1.100,47.157.77.146 --port=12345 --hb_period=30",
            "expected_addresses": "10.0.1.1,192.168.1.100,47.157.77.146",
            "expected_port": "12345",
        },
    ]

    all_passed = True

    for i, test_case in enumerate(test_cases, 1):
        print(f"{i}. Testing: {test_case['name']}")

        try:
            # Parse command
            parsed = ParslWorkerCommandParser.parse_addresses_and_port(
                test_case["command"]
            )

            # Verify parsing
            if parsed["addresses"] != test_case["expected_addresses"]:
                print(
                    f"   ❌ Address parsing failed: got {parsed['addresses']}, expected {test_case['expected_addresses']}"
                )
                all_passed = False
                continue

            if parsed["port"] != test_case["expected_port"]:
                print(
                    f"   ❌ Port parsing failed: got {parsed['port']}, expected {test_case['expected_port']}"
                )
                all_passed = False
                continue

            # Test command modification
            modified = ParslWorkerCommandParser.modify_for_tunnel(
                test_case["command"], 50000
            )

            # Verify modification
            if (
                "127.0.0.1" not in modified
                or test_case["expected_addresses"].split(",")[1] in modified
            ):
                print(f"   ❌ Command modification failed: {modified}")
                all_passed = False
                continue

            print("   ✅ Parsing and modification successful")
            print(f"      Original addresses: {parsed['addresses']}")
            print("      Modified to: 127.0.0.1")

        except Exception as e:
            print(f"   ❌ Test failed with exception: {e}")
            all_passed = False

    return all_passed


async def run_ssm_validation():
    """Run complete SSM validation."""
    print("🔬 SSM TUNNELING VALIDATION SUITE")
    print("=" * 70)
    print("Deterministic testing of SSM functionality for Phase 1.5 Enhanced")
    print("=" * 70)

    # Test 1: Command Parsing (fast, local)
    print("\n" + "=" * 30 + " TEST 1 " + "=" * 30)
    parsing_success = validate_command_parsing()

    # Test 2: SSM Infrastructure (slow, AWS)
    print("\n" + "=" * 30 + " TEST 2 " + "=" * 30)
    infrastructure_success = await validate_ssm_infrastructure()

    # Results
    print("\n" + "=" * 70)
    print("📊 SSM VALIDATION RESULTS")
    print("=" * 70)

    results = {
        "Command Parsing": parsing_success,
        "SSM Infrastructure": infrastructure_success,
    }

    total = len(results)
    passed = sum(1 for success in results.values() if success)
    failed = total - passed

    print(f"Tests Run: {total}")
    print(f"✅ Passed: {passed}")
    print(f"❌ Failed: {failed}")

    for test_name, success in results.items():
        status = "✅ PASSED" if success else "❌ FAILED"
        print(f"  {status}: {test_name}")

    if failed == 0:
        print("\n🎉 ALL SSM VALIDATION TESTS PASSED!")
        print("✅ Command parsing is working correctly")
        print("✅ SSM infrastructure is operational")
        print("✅ Basic SSM command execution verified")
        print("\n🚀 SSM tunneling foundation is ready!")
        print("Next step: Test actual tunnel creation and port forwarding")
    else:
        print(f"\n⚠️ {failed} validation test(s) failed")
        if not parsing_success:
            print("❌ Command parsing issues - fix regex patterns")
        if not infrastructure_success:
            print(
                "❌ SSM infrastructure issues - check IAM roles, instance config, timeouts"
            )

    print("=" * 70)
    return failed == 0


if __name__ == "__main__":
    setup_logging()

    try:
        success = asyncio.run(run_ssm_validation())
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n⚠️ Validation interrupted by user")
        sys.exit(130)
    except Exception as e:
        print(f"\n\n💥 Validation failed: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)

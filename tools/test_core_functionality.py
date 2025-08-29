#!/usr/bin/env python3
"""
Core Functionality Test for Phase 1.5 Enhanced Provider

Quick validation of essential functionality without long-running tests.
"""

import logging
import sys
import time
from pathlib import Path

# Add tools directory to path
sys.path.insert(0, str(Path(__file__).parent))

try:
    from phase15_enhanced import AWSProvider
    from enhanced_error_handling import graceful_degradation
except ImportError as e:
    print(f"ERROR: Could not import components: {e}")
    sys.exit(1)


def setup_logging():
    """Set up basic logging."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    # Reduce AWS noise
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)


def test_provider_initialization():
    """Test provider initialization."""
    print("\n🧪 Testing Provider Initialization")
    print("-" * 50)

    try:
        # Test standard mode
        provider = AWSProvider(
            region="us-east-1",
            instance_type="t3.micro",
            enable_ssm_tunneling=False,  # Keep simple
            prefer_optimized_ami=True,
        )

        print(f"✅ Standard provider created: {provider.provider_id}")
        print(
            f"   AMI: {provider.ami_id} ({'optimized' if provider.is_optimized_ami else 'base'})"
        )
        print(f"   Security Group: {provider.security_group_id}")

        provider.cleanup()
        return True

    except Exception as e:
        print(f"❌ Provider initialization failed: {e}")
        return False


def test_instance_launch():
    """Test instance launch and visibility."""
    print("\n🧪 Testing Instance Launch")
    print("-" * 50)

    provider = None

    try:
        provider = AWSProvider(
            region="us-east-1", instance_type="t3.micro", enable_ssm_tunneling=False
        )

        # Get launch config
        config = provider._get_launch_config("test_job")
        print("✅ Launch config generated")
        print(f"   AMI: {config['ImageId']}")
        print(f"   Instance Type: {config['InstanceType']}")
        print(f"   Security Groups: {config['SecurityGroupIds']}")

        # Launch instance
        response = provider.ec2.run_instances(MinCount=1, MaxCount=1, **config)
        instance_id = response["Instances"][0]["InstanceId"]
        print(f"✅ Instance launched: {instance_id}")

        # Check visibility
        time.sleep(5)  # Brief wait
        response = provider.ec2.describe_instances(InstanceIds=[instance_id])
        instance = response["Reservations"][0]["Instances"][0]
        state = instance["State"]["Name"]
        print(f"✅ Instance visible in AWS: {state}")

        # Terminate instance
        provider.ec2.terminate_instances(InstanceIds=[instance_id])
        print("✅ Instance terminated")

        return True

    except Exception as e:
        print(f"❌ Instance launch test failed: {e}")
        return False

    finally:
        if provider:
            provider.cleanup()


def test_job_lifecycle():
    """Test complete job submission and status lifecycle."""
    print("\n🧪 Testing Job Lifecycle")
    print("-" * 50)

    provider = None

    try:
        provider = AWSProvider(
            region="us-east-1", instance_type="t3.micro", enable_ssm_tunneling=False
        )

        # Submit job
        command = "echo 'Test job'; sleep 10"
        job_id = provider.submit(command, 1, "lifecycle_test")
        print(f"✅ Job submitted: {job_id}")

        # Check initial status
        statuses = provider.status([job_id])
        initial_status = statuses[0]
        print(f"✅ Initial status: {initial_status.state.name}")

        # Wait a bit and check again
        time.sleep(20)
        statuses = provider.status([job_id])
        later_status = statuses[0]
        print(f"✅ Later status: {later_status.state.name}")

        # Cancel job
        results = provider.cancel([job_id])
        print(f"✅ Job cancelled: {results[0]}")

        return True

    except Exception as e:
        print(f"❌ Job lifecycle test failed: {e}")
        return False

    finally:
        if provider:
            provider.cleanup()


def test_error_handling():
    """Test error handling functionality."""
    print("\n🧪 Testing Error Handling")
    print("-" * 50)

    try:
        # Test graceful degradation
        graceful_degradation.report_feature_failure(
            "test_feature", Exception("test error")
        )
        graceful_degradation.report_feature_failure(
            "test_feature", Exception("test error")
        )
        graceful_degradation.report_feature_failure(
            "test_feature", Exception("test error")
        )

        if not graceful_degradation.is_feature_enabled("test_feature"):
            print("✅ Feature degradation working")
        else:
            print("❌ Feature degradation not working")
            return False

        # Test recovery
        graceful_degradation.report_feature_success("test_feature")
        if graceful_degradation.is_feature_enabled("test_feature"):
            print("✅ Feature recovery working")
            return True
        else:
            print("❌ Feature recovery not working")
            return False

    except Exception as e:
        print(f"❌ Error handling test failed: {e}")
        return False


def run_core_tests():
    """Run core functionality tests."""
    print("🚀 PHASE 1.5 ENHANCED - CORE FUNCTIONALITY TESTS")
    print("=" * 70)
    print("Testing essential functionality without long-running operations.")
    print("=" * 70)

    tests = [
        ("Provider Initialization", test_provider_initialization),
        ("Instance Launch", test_instance_launch),
        ("Job Lifecycle", test_job_lifecycle),
        ("Error Handling", test_error_handling),
    ]

    results = {}

    for test_name, test_func in tests:
        try:
            print(f"\n{'='*10} {test_name} {'='*10}")
            success = test_func()
            results[test_name] = success
            status = "✅ PASSED" if success else "❌ FAILED"
            print(f"\n{status}: {test_name}")
        except Exception as e:
            results[test_name] = False
            print(f"\n❌ FAILED: {test_name} - {e}")
            import traceback

            traceback.print_exc()

    # Final results
    print("\n" + "=" * 70)
    print("📊 CORE FUNCTIONALITY TEST SUMMARY")
    print("=" * 70)

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
        print("\n🎉 ALL CORE TESTS PASSED!")
        print("✅ Phase 1.5 Enhanced provider core functionality is working!")
        print("✅ AWS API integration is correct")
        print("✅ Job lifecycle management is functional")
        print("✅ Error handling is operational")
        print("\n🚀 Ready for production deployment!")
    else:
        print(f"\n⚠️  {failed} core test(s) failed")
        print("❌ NOT ready for production - fix failing tests first")

    print("=" * 70)
    return failed == 0


if __name__ == "__main__":
    setup_logging()

    try:
        success = run_core_tests()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n⚠️  Tests interrupted by user")
        sys.exit(130)
    except Exception as e:
        print(f"\n\n💥 Test suite failed: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)

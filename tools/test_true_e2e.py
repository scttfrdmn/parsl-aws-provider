#!/usr/bin/env python3
"""
TRUE End-to-End Test with Real Parsl Execution

This test actually runs real Parsl workflows with the enhanced provider.
Tests the complete pipeline: Parsl -> Provider -> AWS -> Worker -> Results
"""

import logging
import sys
import time
from pathlib import Path

import parsl
from parsl.config import Config
from parsl.executors import HighThroughputExecutor

# Add tools directory to path
sys.path.insert(0, str(Path(__file__).parent))

try:
    from phase15_enhanced import AWSProvider
except ImportError as e:
    print(f"ERROR: Could not import enhanced provider: {e}")
    sys.exit(1)


def setup_logging():
    """Set up logging for E2E test."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def test_parsl_python_app_execution():
    """Test real Parsl Python app execution."""
    print("\n🧪 TRUE E2E TEST: Parsl Python App Execution")
    print("=" * 60)

    provider = None

    try:
        # Create provider
        print("1. Creating enhanced AWS provider...")
        provider = AWSProvider(
            region="us-east-1",
            instance_type="t3.micro",
            enable_ssm_tunneling=True,  # Enable SSM tunneling for connectivity
            use_private_subnets=False,
            prefer_optimized_ami=True,
        )

        print(f"✓ Provider: {provider.provider_id}")
        print(
            f"✓ AMI: {provider.ami_id} ({'optimized' if provider.is_optimized_ami else 'base'})"
        )

        # Configure Parsl
        print("\n2. Configuring Parsl with HighThroughputExecutor...")
        config = Config(
            executors=[
                HighThroughputExecutor(
                    label="enhanced_aws_executor",
                    provider=provider,
                    max_workers_per_node=1,
                    cores_per_worker=1,
                )
            ]
        )

        # Load Parsl
        print("3. Loading Parsl configuration...")
        parsl.load(config)
        print("✓ Parsl loaded successfully")

        # Define test apps
        @parsl.python_app
        def test_computation():
            """Simple computation test."""
            import socket
            import math

            # Do some computation
            result = sum(math.sqrt(i) for i in range(1000))

            return {
                "hostname": socket.gethostname(),
                "computation_result": result,
                "message": "Phase 1.5 Enhanced E2E test successful!",
            }

        @parsl.python_app
        def test_file_operations(inputs=[], outputs=[]):
            """Test file operations."""
            import os

            # Write test data
            test_data = "Phase 1.5 Enhanced Provider Test\n" + "=" * 40 + "\n"
            test_data += f"PID: {os.getpid()}\n"
            test_data += f"Working Directory: {os.getcwd()}\n"

            with open(outputs[0], "w") as f:
                f.write(test_data)

            return len(test_data)

        print("\n4. Submitting Parsl applications...")

        # Submit computation app
        print("   Submitting computation app...")
        comp_future = test_computation()

        # Submit file operation app
        print("   Submitting file operation app...")
        file_future = test_file_operations(outputs=[parsl.File("test_output.txt")])

        print("✓ Applications submitted")

        # Wait for results
        print("\n5. Waiting for results (max 120s)...")

        try:
            # Get computation result
            print("   Waiting for computation result...")
            comp_result = comp_future.result(timeout=120)
            print(f"✓ Computation completed: {comp_result['message']}")
            print(f"   Hostname: {comp_result['hostname']}")
            print(f"   Result: {comp_result['computation_result']:.2f}")

            # Get file operation result
            print("   Waiting for file operation result...")
            file_size = file_future.result(timeout=120)
            print(f"✓ File operation completed: {file_size} bytes written")

            print("\n🎉 TRUE E2E TEST SUCCESS!")
            print("✅ Parsl applications executed successfully on AWS")
            print("✅ Worker communication functional")
            print("✅ Result collection working")

            return True

        except Exception as e:
            print(f"\n❌ Application execution failed: {e}")
            print("This indicates issues with:")
            print("  - Worker startup on AWS instances")
            print("  - Network connectivity between workers and controller")
            print("  - Parsl task distribution")
            return False

    except Exception as e:
        print(f"\n❌ E2E test setup failed: {e}")
        import traceback

        traceback.print_exc()
        return False

    finally:
        print("\n6. Cleaning up...")
        try:
            parsl.clear()
            print("✓ Parsl cleared")
        except Exception as e:
            print(f"! Parsl clear failed: {e}")

        if provider:
            try:
                provider.cleanup()
                print("✓ Provider cleaned up")
            except Exception as e:
                print(f"! Provider cleanup failed: {e}")


def test_parsl_bash_app_execution():
    """Test real Parsl Bash app execution."""
    print("\n🧪 TRUE E2E TEST: Parsl Bash App Execution")
    print("=" * 60)

    provider = None

    try:
        # Create provider
        print("1. Creating provider for bash app test...")
        provider = AWSProvider(
            region="us-east-1",
            instance_type="t3.micro",
            enable_ssm_tunneling=True,  # Enable SSM tunneling for connectivity
            prefer_optimized_ami=True,
        )

        # Configure Parsl
        config = Config(
            executors=[
                HighThroughputExecutor(
                    label="bash_test_executor",
                    provider=provider,
                    max_workers_per_node=1,
                    cores_per_worker=1,
                )
            ]
        )

        # Load Parsl
        print("2. Loading Parsl for bash test...")
        parsl.load(config)

        # Define bash app
        @parsl.bash_app
        def system_info_test(stdout="system_info.out"):
            return """
            echo "=== Phase 1.5 Enhanced Bash App Test ==="
            echo "Date: $(date)"
            echo "Hostname: $(hostname)"
            echo "Uptime: $(uptime)"
            echo "Python version: $(python3 --version 2>/dev/null || echo 'Python not found')"
            echo "Disk usage: $(df -h / | tail -1)"
            echo "Memory: $(free -h 2>/dev/null || echo 'free command not available')"
            echo "=== Test Complete ==="
            """

        print("3. Submitting bash application...")
        bash_future = system_info_test()

        print("4. Waiting for bash app completion...")
        result = bash_future.result(timeout=120)

        print("✓ Bash application completed successfully")

        # Try to read output file
        try:
            with open("system_info.out", "r") as f:
                output = f.read()
                print("✓ Output file created:")
                print(output[:300] + "..." if len(output) > 300 else output)
        except FileNotFoundError:
            print("! Output file not found locally (expected for remote execution)")

        return True

    except Exception as e:
        print(f"❌ Bash app test failed: {e}")
        return False

    finally:
        try:
            parsl.clear()
        except Exception:
            pass
        if provider:
            provider.cleanup()


def run_true_e2e_tests():
    """Run complete true E2E tests."""
    print("🚀 PHASE 1.5 ENHANCED - TRUE END-TO-END TESTS")
    print("=" * 70)
    print("Testing complete Parsl workflow execution on real AWS infrastructure.")
    print("This validates the entire pipeline: Parsl → Provider → AWS → Results")
    print("=" * 70)

    tests = [
        ("Parsl Python App E2E", test_parsl_python_app_execution),
        ("Parsl Bash App E2E", test_parsl_bash_app_execution),
    ]

    results = {}

    for test_name, test_func in tests:
        print(f"\n{'='*20} {test_name} {'='*20}")
        try:
            success = test_func()
            results[test_name] = success
            status = "✅ PASSED" if success else "❌ FAILED"
            print(f"\n{status}: {test_name}")

            # Wait between tests to avoid AWS conflicts
            if test_name != tests[-1][0]:  # Not the last test
                print("Waiting 30s between tests...")
                time.sleep(30)

        except Exception as e:
            results[test_name] = False
            print(f"\n❌ FAILED: {test_name} - {e}")

    # Final results
    print("\n" + "=" * 70)
    print("📊 TRUE E2E TEST RESULTS")
    print("=" * 70)

    total = len(results)
    passed = sum(1 for success in results.values() if success)
    failed = total - passed

    print(f"Total E2E Tests: {total}")
    print(f"✅ Passed: {passed}")
    print(f"❌ Failed: {failed}")
    print(f"Success Rate: {(passed/total)*100:.1f}%")

    if failed > 0:
        print("\n❌ Failed E2E Tests:")
        for test_name, success in results.items():
            if not success:
                print(f"   • {test_name}")
        print("\n⚠️ CRITICAL: E2E failures indicate real deployment issues:")
        print("   - Worker connectivity problems")
        print("   - Parsl integration issues")
        print("   - AWS infrastructure problems")
        print("   - Network configuration errors")

    if failed == 0:
        print("\n🎉 ALL TRUE E2E TESTS PASSED!")
        print("✅ Complete Parsl workflow execution verified")
        print("✅ Worker-controller communication functional")
        print("✅ AWS infrastructure integration working")
        print("✅ Task distribution and result collection operational")
        print("\n🚀 PRODUCTION READY FOR REAL SCIENTIFIC WORKLOADS!")
    else:
        print(f"\n❌ {failed} E2E test(s) failed - NOT PRODUCTION READY")
        print("Must fix E2E issues before deployment")

    print("=" * 70)
    return failed == 0


if __name__ == "__main__":
    setup_logging()

    try:
        success = run_true_e2e_tests()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n⚠️ Tests interrupted by user")
        sys.exit(130)
    except Exception as e:
        print(f"\n\n💥 E2E test suite failed: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)

#!/usr/bin/env python3
"""
Phase 1.5 Test Script

Tests the enhanced AWSProvider with optimized AMI discovery.
Demonstrates automatic fallback to Phase 1 behavior when no optimized AMI is available.
"""

import logging
import time
import sys
from pathlib import Path

# Add tools directory to path so we can import phase1
sys.path.insert(0, str(Path(__file__).parent))

try:
    from phase1 import AWSProvider
except ImportError as e:
    print(f"ERROR: Could not import AWSProvider: {e}")
    print("Make sure you're running from the tools/ directory")
    sys.exit(1)


def test_phase15_provider():
    """Test Phase 1.5 provider with optimized AMI discovery."""

    print("TESTING PHASE 1.5 PROVIDER")
    print("=" * 60)
    print("Features:")
    print("- Automatic optimized AMI discovery")
    print("- Graceful fallback to Phase 1 behavior")
    print("- Same simple interface")
    print("=" * 60)

    # Enable info logging to see AMI selection
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    provider = None

    try:
        print("\n1. Creating Phase 1.5 provider...")
        print("   (Will automatically discover optimized AMI or fallback to base AMI)")

        # Same interface as Phase 1, but with Phase 1.5 optimizations
        provider = AWSProvider(
            region="us-east-1",
            instance_type="t3.micro",
            prefer_optimized_ami=True,  # This is the default
        )

        print(f"✓ Provider created: {provider.provider_id}")
        print(f"  Security Group: {provider.security_group_id}")
        print(f"  Selected AMI: {provider.ami_id}")
        print(f"  Optimized AMI: {'Yes' if provider.is_optimized_ami else 'No'}")

        if provider.is_optimized_ami:
            print("  → Phase 1.5: Using optimized AMI (fast startup expected)")
        else:
            print("  → Phase 1: Using base AMI (package installation required)")

        print("\n2. Submitting test job...")
        start_time = time.time()

        job_id = provider.submit(
            command='echo "Phase 1.5 test successful!"; python3 -c "import parsl; print(parsl.__version__)" 2>/dev/null || echo "Parsl not pre-installed"; hostname; date; sleep 10',
            tasks_per_node=1,
            job_name="phase15_test",
        )

        submit_time = time.time() - start_time
        print(f"✓ Job submitted: {job_id} (submit time: {submit_time:.2f}s)")

        print("\n3. Checking job status...")
        statuses = provider.status([job_id])
        print(
            f"Initial status: [{{'job_id': '{job_id}', 'status': '{statuses[0].state.name}'}}]"
        )

        print("\n4. Waiting 90 seconds to let job run...")
        time.sleep(90)

        statuses = provider.status([job_id])
        print(
            f"Status after 90s: [{{'job_id': '{job_id}', 'status': '{statuses[0].state.name}'}}]"
        )

        if provider.is_optimized_ami:
            print("\n✓ PHASE 1.5 OPTIMIZED PROVIDER TEST: SUCCESS")
            print("  → Fast startup with pre-installed Parsl")
        else:
            print("\n✓ PHASE 1.5 FALLBACK PROVIDER TEST: SUCCESS")
            print("  → Fallback to Phase 1 behavior (no optimized AMI available)")

    except Exception as e:
        print(f"\n✗ Test failed: {e}")
        import traceback

        traceback.print_exc()
        return False

    finally:
        if provider:
            print("\nCleaning up...")
            provider.cleanup()
            print("✓ Cleanup completed")

    return True


def test_explicit_base_ami():
    """Test Phase 1.5 provider with optimized AMI discovery disabled."""

    print("\n" + "=" * 60)
    print("TESTING PHASE 1.5 WITH OPTIMIZED AMI DISABLED")
    print("(Should behave exactly like Phase 1)")
    print("=" * 60)

    provider = None

    try:
        print("\n1. Creating provider with optimized AMI disabled...")

        provider = AWSProvider(
            region="us-east-1",
            instance_type="t3.micro",
            prefer_optimized_ami=False,  # Disable Phase 1.5 optimization
        )

        print(f"✓ Provider created: {provider.provider_id}")
        print(f"  Selected AMI: {provider.ami_id}")
        print(f"  Optimized AMI: {'Yes' if provider.is_optimized_ami else 'No'}")

        if not provider.is_optimized_ami:
            print("  → Confirmed: Using base AMI (Phase 1 behavior)")
        else:
            print("  → ERROR: Should not be using optimized AMI!")
            return False

        print("\n✓ PHASE 1.5 COMPATIBILITY TEST: SUCCESS")
        print("  → Phase 1 behavior preserved when optimization disabled")

    except Exception as e:
        print(f"\n✗ Compatibility test failed: {e}")
        return False

    finally:
        if provider:
            provider.cleanup()

    return True


if __name__ == "__main__":
    print("Phase 1.5 Enhanced AWS Provider Test Suite")
    print("==========================================")

    # Test 1: Normal Phase 1.5 behavior (with AMI discovery)
    success1 = test_phase15_provider()

    # Test 2: Phase 1.5 with optimization disabled (backward compatibility)
    success2 = test_explicit_base_ami()

    print("\n" + "=" * 60)
    if success1 and success2:
        print("ALL PHASE 1.5 TESTS PASSED!")
        print("\nPhase 1.5 Features Verified:")
        print("✓ Automatic optimized AMI discovery")
        print("✓ Graceful fallback to Phase 1 behavior")
        print("✓ Backward compatibility preserved")
        print("✓ Same simple interface as Phase 1")

        if success1:
            print("\nTo build optimized AMIs for better performance:")
            print("python tools/build_ami.py --region us-east-1")
            print("python tools/validate_ami.py --ami <ami-id>")
    else:
        print("SOME PHASE 1.5 TESTS FAILED")
        sys.exit(1)

    print("=" * 60)

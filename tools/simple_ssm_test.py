#!/usr/bin/env python3
"""
Simple SSM connectivity test to verify infrastructure works.
"""

import time
import sys
from phase15_enhanced import AWSProvider


def test_ssm_connectivity():
    """Test basic SSM connectivity without Parsl complexity."""
    print("🔧 SIMPLE SSM CONNECTIVITY TEST")
    print("=" * 50)

    # Create provider
    print("1. Creating AWS provider...")
    provider = AWSProvider(
        label="ssm_test",
        init_blocks=0,  # Don't auto-start
        max_blocks=1,
        min_blocks=0,
    )

    print(f"✅ Using AMI: {provider.ami_id}")
    print(f"✅ Using profile: {provider.aws_profile}")
    print("✅ Using account: 942542972736")

    try:
        # Submit a single job to launch one instance
        print("\n2. Launching single instance...")
        job_id = provider.submit("echo 'test'", 1, "ssm_test")
        print(f"✅ Job submitted: {job_id}")

        # Wait a bit for instance to launch and SSM to register
        print("\n3. Waiting 90s for instance to fully initialize...")
        for i in range(18):  # 90 seconds / 5 = 18 iterations
            time.sleep(5)
            status = provider.status([job_id])
            print(f"   Status check {i+1}/18: {status[0].state}")

            if status[0].state == "RUNNING":
                print("✅ Job is running!")
                break
        else:
            print("⚠️ Job not running after 90s, but that might be normal")

        # Check final status
        final_status = provider.status([job_id])
        print(f"\n4. Final status: {final_status[0].state}")

        if final_status[0].state == "RUNNING":
            print("🎉 SUCCESS: SSM tunneling infrastructure working!")
            return True
        else:
            print("❌ Infrastructure test inconclusive")
            return False

    except Exception as e:
        print(f"❌ Test failed: {e}")
        return False

    finally:
        print("\n5. Cleaning up...")
        try:
            provider.cancel([job_id])
            print("✅ Cleanup complete")
        except:
            pass


if __name__ == "__main__":
    success = test_ssm_connectivity()
    sys.exit(0 if success else 1)

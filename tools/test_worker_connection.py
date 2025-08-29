#!/usr/bin/env python3
"""
Minimal test to validate worker connection with hardcoded corrected command.
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


def test_worker_connection():
    """Test worker connection with manually corrected command."""
    print("\n🧪 MINIMAL WORKER CONNECTION TEST")
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

        # Submit a job with manually corrected worker command
        print("\n2. Submitting job with corrected worker command...")

        # Create a minimal corrected worker command (remove all problematic paths, use a realistic port)
        corrected_command = "process_worker_pool.py --max_workers_per_node=1 -a 127.0.0.1,192.168.1.245,47.157.77.146 -p 0 -c 1 -m None --poll 10 --port=54573 --cert_dir /tmp --logdir /tmp/parsl_logs --block_id=test-block --hb_period=30 --hb_threshold=120 --drain_period=None --cpu-affinity none --mpi-launcher=mpiexec --available-accelerators"

        job_id = provider.submit(corrected_command, 1, "worker-connection-test")
        print(f"✓ Job submitted: {job_id}")

        # Wait and check for worker connection
        print("\n3. Monitoring worker connection...")
        for i in range(12):  # Wait up to 2 minutes
            status = provider.status([job_id])
            print(f"   Check {i+1}: Job status = {status.get(job_id, 'Unknown')}")

            # Check tunnel health and local port connectivity
            if job_id in provider.job_tunnels:
                tunnel_info = provider.job_tunnels[job_id]
                tunnel = tunnel_info["tunnel"]
                local_port = tunnel.config.local_port

                print(f"   Tunnel local port: {local_port}")
                print(f"   Tunnel healthy: {tunnel.is_healthy()}")

                # Test tunnel connectivity
                import socket

                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                        sock.settimeout(2)
                        result = sock.connect_ex(("127.0.0.1", local_port))
                        print(
                            f"   Tunnel connection test: {'SUCCESS' if result == 0 else 'FAILED'}"
                        )
                except Exception as e:
                    print(f"   Tunnel connection error: {e}")

            time.sleep(10)

        print("\n4. Test completed")

        return True

    except Exception as e:
        print(f"❌ Test failed: {e}")
        return False

    finally:
        if provider:
            print("\n5. Cleaning up...")
            provider.cleanup()
            print("✓ Cleanup completed")


if __name__ == "__main__":
    setup_logging()

    # Run the test
    success = test_worker_connection()
    sys.exit(0 if success else 1)

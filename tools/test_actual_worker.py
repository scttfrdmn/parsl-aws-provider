#!/usr/bin/env python3
"""
Test actual worker connection with real Parsl interchange.
This tests the complete worker connection path.
"""

import logging
import sys
import time
from pathlib import Path

# Add tools directory to path
sys.path.insert(0, str(Path(__file__).parent))

from phase15_enhanced import AWSProvider
import parsl
from parsl.executors import HighThroughputExecutor
from parsl.config import Config


def setup_logging():
    """Set up logging for the test."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


class WorkerConnectionTest:
    """Test class for worker connection validation."""

    def __init__(self):
        self.provider = None
        self.parsl_dfk = None
        self.interchange_port = None

    def test_worker_connection(self):
        """Test complete worker connection path."""
        print("\n🧪 ACTUAL WORKER CONNECTION TEST")
        print("=" * 50)

        try:
            # Step 1: Create provider
            print("1. Creating enhanced AWS provider...")
            self.provider = AWSProvider(
                region="us-east-1",
                instance_type="t3.micro",
                enable_ssm_tunneling=True,
                use_private_subnets=False,
                prefer_optimized_ami=True,
            )
            print(f"✓ Provider: {self.provider.provider_id}")

            # Step 2: Set up minimal Parsl config to get interchange
            print("\n2. Setting up Parsl interchange...")
            config = Config(
                executors=[
                    HighThroughputExecutor(
                        label="test_executor",
                        provider=self.provider,
                        max_workers_per_node=1,
                        cores_per_worker=1,
                    )
                ]
            )

            # Load Parsl to start interchange
            parsl.load(config)
            self.parsl_dfk = parsl.dfk()

            # Get the interchange port from the executor
            executor = self.parsl_dfk.executors["test_executor"]

            # Wait for interchange to start
            time.sleep(5)

            # Get the worker port from interchange
            worker_port = executor.worker_port
            print(f"✓ Interchange started on worker port: {worker_port}")

            # Step 3: Create corrected worker command
            print("\n3. Creating worker command...")

            # Get controller addresses
            addresses = executor.outgoing_q.address_list
            address_str = ",".join(addresses)

            worker_command = f"process_worker_pool.py --max_workers_per_node=1 -a {address_str} -p 0 -c 1 -m None --poll 10 --port={worker_port} --cert_dir /tmp --logdir /tmp/parsl_logs --block_id=test-block --hb_period=30 --hb_threshold=120 --drain_period=None --cpu-affinity none --mpi-launcher=mpiexec --available-accelerators"

            print(f"✓ Worker command: {worker_command}")

            # Step 4: Submit worker via provider
            print("\n4. Submitting worker...")
            job_id = self.provider.submit(worker_command, 1, "worker-test")
            print(f"✓ Job submitted: {job_id}")

            # Step 5: Monitor for worker connection
            print("\n5. Monitoring worker connection...")

            connected_workers = 0
            for i in range(24):  # Wait up to 4 minutes
                # Check if workers connected to interchange
                try:
                    # Get worker count from executor
                    executor_status = executor.status()
                    connected_workers = len(
                        [w for w in executor_status if w["status"] == "RUNNING"]
                    )

                    print(f"   Check {i+1}: Connected workers = {connected_workers}")

                    if connected_workers > 0:
                        print("🎉 SUCCESS! Worker connected to interchange!")
                        return True

                    # Also check tunnel status
                    if job_id in self.provider.job_tunnels:
                        tunnel_info = self.provider.job_tunnels[job_id]
                        tunnel = tunnel_info["tunnel"]
                        print(f"   Tunnel healthy: {tunnel.is_healthy()}")
                        print(f"   Local port: {tunnel.config.local_port}")

                except Exception as e:
                    print(f"   Check {i+1}: Status check error: {e}")

                time.sleep(10)

            print(
                f"❌ No workers connected after 4 minutes (final count: {connected_workers})"
            )
            return False

        except Exception as e:
            print(f"❌ Test failed: {e}")
            import traceback

            traceback.print_exc()
            return False

        finally:
            self.cleanup()

    def cleanup(self):
        """Clean up test resources."""
        print("\n6. Cleaning up...")

        if self.parsl_dfk:
            try:
                parsl.clear()
                print("✓ Parsl cleared")
            except Exception as e:
                print(f"⚠️  Parsl cleanup error: {e}")

        if self.provider:
            try:
                self.provider.cleanup()
                print("✓ Provider cleaned up")
            except Exception as e:
                print(f"⚠️  Provider cleanup error: {e}")


def main():
    """Main test function."""
    setup_logging()

    test = WorkerConnectionTest()
    success = test.test_worker_connection()

    print("\n" + "=" * 50)
    if success:
        print("🎉 WORKER CONNECTION TEST: SUCCESS")
        print("Workers can connect through SSM tunneling!")
    else:
        print("❌ WORKER CONNECTION TEST: FAILED")
        print("Workers are not connecting - need further debugging")
    print("=" * 50)

    return success


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)

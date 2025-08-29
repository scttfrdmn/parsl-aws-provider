#!/usr/bin/env python3
"""
Test command parsing and modification logic.
"""

import sys
from pathlib import Path

# Add tools directory to path
sys.path.insert(0, str(Path(__file__).parent))

from ssm_tunnel import ParslWorkerCommandParser


def test_command_parsing():
    """Test command parsing with real examples."""
    print("🧪 COMMAND PARSING TEST")
    print("=" * 50)

    # Real command from logs
    test_command = "process_worker_pool.py  --max_workers_per_node=1 -a 47.157.77.146,192.168.1.245,127.0.0.1 -p 0 -c 1 -m None --poll 10 --port=54921 --cert_dir None --logdir=/Users/scttfrdmn/src/parsl-aws-provider/runinfo/027/bash_test_executor --block_id={block_id} --hb_period=30  --hb_threshold=120 --drain_period=None --cpu-affinity none  --mpi-launcher=mpiexec --available-accelerators"

    print("1. Original command:")
    print(f"   {test_command}")

    print("\n2. Parsing test:")
    parsed = ParslWorkerCommandParser.parse_addresses_and_port(test_command)
    print(f"   Addresses: {parsed['addresses']}")
    print(f"   Port: {parsed['port']}")

    print("\n3. Modification test:")
    try:
        modified = ParslWorkerCommandParser.modify_for_tunnel(test_command, 50009)
        print(f"   Modified: {modified}")

        print("\n4. Verification:")
        # Check if modifications were applied
        if "--cert_dir None" in modified:
            print("   ❌ --cert_dir None still present")
        else:
            print("   ✅ --cert_dir None fixed")

        if "/Users/" in modified:
            print("   ❌ Local paths still present")
        else:
            print("   ✅ Local paths fixed")

        if "--port=54921" in modified:
            print("   ❌ Original port still present")
        else:
            print("   ✅ Original port replaced")

        if "--port=50009" in modified:
            print("   ✅ Tunnel port present")
        else:
            print("   ❌ Tunnel port missing")

        if "-a 127.0.0.1" in modified:
            print("   ✅ Localhost address present")
        else:
            print("   ❌ Localhost address missing")

    except Exception as e:
        print(f"   ❌ Modification failed: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    test_command_parsing()

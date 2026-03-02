#!/usr/bin/env python3
"""
Real Parsl + AWS Integration Example.

Runs a simple Python function on a real EC2 instance using Parsl's
HighThroughputExecutor with EphemeralAWSProvider.

CONNECTIVITY REQUIREMENT
------------------------
Parsl's HTEX workers (running on EC2) connect *outbound* via ZMQ to the
interchange running on this machine.  For that connection to succeed, this
machine must be reachable from EC2 on TCP ports 54000-55000.

This works when:
  * Running from an EC2 instance (workers connect within AWS)
  * Running from a machine with a direct internet connection (public IP,
    no NAT, or NAT with port-forwarding for 54000-55000)
  * Running over a VPN that routes AWS traffic back to your machine

It does NOT work from a typical home/office NAT without additional setup.
See docs/operating_modes.md for the detached-mode alternative that works
behind any NAT without inbound port requirements.

Usage
-----
    # Ensure AWS credentials are configured:
    export AWS_PROFILE=aws
    export AWS_TEST_REGION=us-west-2   # optional, default us-west-2

    uv run python examples/parsl_aws_integration.py

The script prints the hostname, platform, and Python version of the EC2
instance that ran the function.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import logging
import os
import sys

import parsl
from parsl import python_app
from parsl.addresses import address_by_query, address_by_route
from parsl.config import Config
from parsl.executors import HighThroughputExecutor

from parsl_ephemeral_aws import EphemeralAWSProvider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("parsl-aws-integration")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

AWS_REGION = os.environ.get("AWS_TEST_REGION", "us-west-2")
# None → use instance role (EC2) or default credential chain; never pass ""
AWS_PROFILE: str | None = os.environ.get("AWS_TEST_PROFILE") or None

# Worker init for Amazon Linux 2023 (the default AMI used by EphemeralAWSProvider).
# AL2023's default python3 is 3.9; parsl>=2026.1.5 requires Python 3.10+.
# Install python3.11 from dnf and make it the default python3, then install
# parsl and psutil so the HTEX worker manager can be started.
AL2023_WORKER_INIT = (
    "dnf install -y python3.11 python3.11-pip\n"
    "ln -sf /usr/bin/python3.11 /usr/bin/python3\n"
    "pip3.11 install --quiet 'parsl>=2026.1.5' psutil\n"
)

# HTEX worker ZMQ port range — interchange binds a port in this range.
# These ports must be reachable (inbound) on the machine running this script.
WORKER_PORT_RANGE = (54000, 55000)


# ---------------------------------------------------------------------------
# Python app (runs on EC2)
# ---------------------------------------------------------------------------


@python_app
def hello_from_ec2():
    """Return execution environment info from the EC2 worker."""
    import platform
    import socket

    return {
        "hostname": socket.gethostname(),
        "fqdn": socket.getfqdn(),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
    }


# ---------------------------------------------------------------------------
# Helper: resolve the interchange address
# ---------------------------------------------------------------------------


def _get_interchange_address() -> str:
    """Return the IP address EC2 workers should use to reach this machine.

    Tries the external (public) IP first; falls back to the routing-table
    address for same-VPC / VPN scenarios.
    """
    try:
        addr = address_by_query(timeout=10)
        logger.info("Public IP (from ipify.org): %s", addr)
        return addr
    except Exception:
        addr = address_by_route()
        logger.warning(
            "Could not reach ipify.org; using route address: %s  "
            "(This will only work if EC2 workers can reach this address directly.)",
            addr,
        )
        return addr


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    """Run the integration test; return 0 on success, 1 on failure."""
    interchange_address = _get_interchange_address()

    logger.info("=" * 70)
    logger.info("Parsl + AWS Integration Test")
    logger.info("  Region:               %s", AWS_REGION)
    logger.info("  AWS profile:          %s", AWS_PROFILE)
    logger.info("  Interchange address:  %s", interchange_address)
    logger.info(
        "  Worker port range:    %d-%d", WORKER_PORT_RANGE[0], WORKER_PORT_RANGE[1]
    )
    logger.info("=" * 70)
    logger.warning(
        "Workers on EC2 must be able to reach %s on ports %d-%d.  "
        "If this machine is behind NAT without port forwarding, the test will "
        "time out after ~10 minutes.  Use detached mode for NAT environments.",
        interchange_address,
        WORKER_PORT_RANGE[0],
        WORKER_PORT_RANGE[1],
    )

    provider = EphemeralAWSProvider(
        region=AWS_REGION,
        instance_type="t3.small",  # t3.micro may OOM during pip install
        mode="standard",
        state_store_type="file",
        state_file_path="/tmp/parsl-aws-integration-state.json",  # nosec B108
        auto_shutdown=True,
        auto_create_instance_profile=True,
        profile_name=AWS_PROFILE,
        worker_init=AL2023_WORKER_INIT,
        min_blocks=0,
        max_blocks=1,
        init_blocks=1,
        waiter_delay=15,
        waiter_max_attempts=40,
        additional_tags={"Purpose": "IntegrationTest", "AutoCleanup": "true"},
        debug=True,
    )

    config = Config(
        executors=[
            HighThroughputExecutor(
                label="aws_htex",
                provider=provider,
                address=interchange_address,
                worker_port_range=WORKER_PORT_RANGE,
                max_workers_per_node=1,
            )
        ],
        run_dir="/tmp/parsl-aws-runinfo",  # nosec B108
    )

    try:
        parsl.load(config)
        logger.info("Parsl loaded.  Submitting hello_from_ec2() ...")

        future = hello_from_ec2()

        logger.info("Waiting for result (up to 10 minutes) ...")
        result = future.result(timeout=600)

        logger.info("=" * 70)
        logger.info("SUCCESS — function ran on EC2:")
        for key, val in result.items():
            logger.info("  %-18s %s", key + ":", val)
        logger.info("=" * 70)
        return 0

    except Exception:
        logger.exception("Test FAILED")
        return 1

    finally:
        logger.info("Cleaning up Parsl resources ...")
        parsl.clear()
        try:
            provider.shutdown()
        except Exception:
            logger.warning(
                "Provider shutdown raised an exception (best-effort cleanup)"
            )


if __name__ == "__main__":
    sys.exit(main())

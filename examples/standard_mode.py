#!/usr/bin/env python3
"""Standard mode: EC2 workers that connect back to this machine.

Standard mode launches EC2 instances directly. Parsl's HTEX workers on those
instances connect *outbound* over ZMQ to the interchange running here, so this
machine must accept *inbound* TCP on the worker port range. A laptop behind NAT
cannot; run this from an EC2 instance in the same VPC, or see detached_mode.py
and one_shot_mode.py for alternatives that need no inbound reachability.

Usage
-----
    export AWS_PROFILE=aws
    export AWS_TEST_REGION=us-east-1
    export AWS_TEST_VPC_ID=vpc-...
    export AWS_TEST_SUBNET_ID=subnet-...
    export AWS_TEST_SG_ID=sg-...

    uv run python examples/standard_mode.py

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import logging
import os
import sys

import parsl
from parsl import python_app
from parsl.addresses import address_by_route
from parsl.config import Config
from parsl.executors import HighThroughputExecutor

from parsl_ephemeral_provider import EphemeralProvider

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("standard-mode")

WORKER_PORT_RANGE = (54000, 55000)


@python_app
def report_environment():
    """Return identifying information about the worker that ran this."""
    import os
    import platform
    import socket

    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
    }


def main() -> int:
    """Launch one EC2 worker, run a function on it, and tear it down."""
    try:
        network = {
            "region": os.environ.get("AWS_TEST_REGION", "us-east-1"),
            "vpc_id": os.environ["AWS_TEST_VPC_ID"],
            "subnet_id": os.environ["AWS_TEST_SUBNET_ID"],
            "security_group_id": os.environ["AWS_TEST_SG_ID"],
        }
    except KeyError as exc:
        logger.error("Missing required environment variable: %s", exc)
        logger.error("See the module docstring, and docs/network-prerequisites.md.")
        return 2

    provider = EphemeralProvider(
        instance_type="t3.small",  # t3.micro can OOM during pip install
        mode="standard",
        min_blocks=0,
        max_blocks=2,
        init_blocks=1,
        # image_id is omitted, so an Amazon Linux 2023 AMI matching the
        # instance type's architecture is resolved from SSM.
        # A worker terminates itself when its command finishes. Reclaiming
        # idle-but-running workers is Parsl's max_idletime, not a provider
        # option -- only the interchange knows a worker's task count (#194).
        auto_shutdown=True,
        auto_create_instance_profile=True,
        state_store_type="file",
        state_file_path="standard_mode_state.json",
        additional_tags={"Project": "ParslExample", "Mode": "standard"},
        waiter_delay=15,
        waiter_max_attempts=40,
        debug=True,
        **network,
    )

    config = Config(
        executors=[
            HighThroughputExecutor(
                label="standard_mode_executor",
                provider=provider,
                address=address_by_route(),  # private IP; see the docstring
                worker_port_range=WORKER_PORT_RANGE,
                max_workers_per_node=2,
                # worker_init installs Python 3.11 and Parsl at boot, which takes
                # several minutes. Do not let Parsl declare the worker MISSING first.
                heartbeat_threshold=600,
                heartbeat_period=30,
                # Required: CurveZMQ certificates live in the driver's run_dir,
                # which EC2 workers cannot read. See issue #62.
                encrypted=False,
            )
        ],
        run_dir="runinfo_standard",
    )

    try:
        parsl.load(config)
        logger.info("Submitting work; the first result waits on instance boot.")

        for i, future in enumerate([report_environment() for _ in range(3)]):
            result = future.result(timeout=900)
            logger.info(
                "Task %d ran on %s (%s, Python %s, %d cores)",
                i,
                result["hostname"],
                result["platform"],
                result["python"],
                result["cpu_count"],
            )
        return 0

    except Exception:
        logger.exception("Workflow failed")
        return 1

    finally:
        # parsl.clear() releases Parsl's resources, not AWS ones, and there is
        # no atexit hook. shutdown() is what terminates the instances.
        parsl.clear()
        provider.shutdown()
        logger.info("Provider shut down.")


if __name__ == "__main__":
    sys.exit(main())

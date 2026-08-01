#!/usr/bin/env python3
"""EC2 Fleet: several instance types per block.

Requesting multiple instance types is the single most effective way to reduce
spot interruptions — EC2 draws from whichever pool has capacity. As of v0.7.0
this uses the EC2 Fleet API (`CreateFleet` with `Type="instant"`), not the legacy
Spot Fleet API; the option keeps the name `use_spot_fleet` for compatibility.

Both `use_spot` and `use_spot_fleet` must be set. `use_spot_fleet` alone builds
no fleet manager and the block silently falls through to a single on-demand
instance (issue #137).

Usage
-----
    export AWS_PROFILE=aws
    export AWS_TEST_REGION=us-east-1
    export AWS_TEST_VPC_ID=vpc-...
    export AWS_TEST_SUBNET_ID=subnet-...
    export AWS_TEST_SG_ID=sg-...

    uv run python examples/spot_fleet_example.py

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import logging
import os
import sys

import parsl
from parsl.addresses import address_by_route
from parsl.app.app import python_app
from parsl.config import Config
from parsl.executors import HighThroughputExecutor

from parsl_ephemeral_aws import EphemeralAWSProvider

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("spot-fleet")


@python_app
def hello(name):
    """Report which fleet instance ran this task."""
    import platform
    import urllib.request

    try:
        # IMDSv2 is required, so fetch a token first.
        token = (
            urllib.request.urlopen(  # nosec B310
                urllib.request.Request(
                    "http://169.254.169.254/latest/api/token",
                    headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
                    method="PUT",
                ),
                timeout=5,
            )
            .read()
            .decode()
        )
        instance_type = (
            urllib.request.urlopen(  # nosec B310
                urllib.request.Request(
                    "http://169.254.169.254/latest/meta-data/instance-type",
                    headers={"X-aws-ec2-metadata-token": token},
                ),
                timeout=5,
            )
            .read()
            .decode()
        )
    except Exception:
        instance_type = "unknown"

    return f"Hello, {name}! Ran on {platform.node()} ({instance_type})"


def main() -> int:
    """Run a batch of tasks across a diversified spot fleet."""
    try:
        network = {
            "region": os.environ.get("AWS_TEST_REGION", "us-east-1"),
            "vpc_id": os.environ["AWS_TEST_VPC_ID"],
            "subnet_id": os.environ["AWS_TEST_SUBNET_ID"],
            "security_group_id": os.environ["AWS_TEST_SG_ID"],
        }
    except KeyError as exc:
        logger.error("Missing required environment variable: %s", exc)
        return 2

    provider = EphemeralAWSProvider(
        mode="standard",
        # Both flags are required for the fleet path (#137).
        use_spot=True,
        use_spot_fleet=True,
        # A list of instance type NAMES — not weighted dicts. Keep vCPU and
        # memory comparable; mix families and generations, not sizes.
        instance_types=["m5.large", "m5a.large", "m6i.large", "c5.large"],
        # nodes_per_block is the fleet's target capacity. This is the only launch
        # path where it has any effect.
        nodes_per_block=2,
        spot_max_price_percentage=80,  # cap at 80% of on-demand
        spot_allocation_strategy="price-capacity-optimized",  # the default
        # Interruption detection needs a checkpoint bucket today (#137); it gates
        # whether the monitor is constructed at all. Omit it and you get one
        # WARNING at startup and no detection.
        spot_interruption_handling=True,
        checkpoint_bucket=os.environ.get("AWS_TEST_CHECKPOINT_BUCKET"),
        min_blocks=0,
        max_blocks=4,
        init_blocks=1,
        auto_create_instance_profile=True,
        state_store_type="file",
        state_file_path="spot_fleet_state.json",
        additional_tags={"Project": "ParslSpotFleetDemo"},
        waiter_delay=15,
        waiter_max_attempts=40,
        **network,
    )

    config = Config(
        executors=[
            HighThroughputExecutor(
                label="spot_fleet_executor",
                provider=provider,
                address=address_by_route(),
                worker_port_range=(54000, 55000),
                heartbeat_threshold=600,
                heartbeat_period=30,
                encrypted=False,  # see #62
            )
        ],
        # What actually protects work from a reclaim: the interruption warning
        # currently produces a log line, not task recovery (#137).
        retries=3,
        run_dir="runinfo_spot_fleet",
    )

    try:
        parsl.load(config)
        logger.info("Submitting 10 tasks to the fleet ...")

        for future in [hello(f"Task {i}") for i in range(10)]:
            try:
                logger.info(future.result(timeout=900))
            except Exception as exc:
                logger.error("Task failed: %s", exc)
        return 0

    except Exception:
        logger.exception("Workflow failed")
        return 1

    finally:
        parsl.clear()
        provider.shutdown()
        logger.info("Fleet and instances terminated.")


if __name__ == "__main__":
    sys.exit(main())

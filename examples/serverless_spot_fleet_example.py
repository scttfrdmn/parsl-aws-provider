#!/usr/bin/env python3
"""Serverless mode with an EC2 Fleet instead of Fargate tasks.

`compute_type="ecs"` plus `use_spot_fleet=True` makes serverless mode bypass ECS
and launch an `instant` EC2 Fleet per job. That is a real capability but a
slightly odd one: you are managing instances again, so it is no longer serverless
in any useful sense. It exists because a fleet reaches instance types and sizes
Fargate does not offer.

Note the contrast with `use_spot=True` in the same mode, which stays on Fargate
and switches the cluster's capacity provider to `FARGATE_SPOT`. If what you want
is a diversified spot fleet, standard mode is the more direct route — see
spot_fleet_example.py. Reach for this configuration when the rest of a serverless
deployment is already in place.

Usage
-----
    export AWS_PROFILE=aws
    export AWS_TEST_REGION=us-east-1
    export AWS_TEST_VPC_ID=vpc-...
    export AWS_TEST_SUBNET_ID=subnet-...
    export AWS_TEST_SG_ID=sg-...

    uv run python examples/serverless_spot_fleet_example.py

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import logging
import os
import sys

import parsl
from parsl.config import Config
from parsl.executors import HighThroughputExecutor

from parsl_ephemeral_provider import EphemeralProvider

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("serverless-spot-fleet")


@parsl.python_app
def compute_intensive(iterations=10):
    """Multiply matrices for a while, then report where this ran."""
    import platform
    import time

    import numpy as np

    started = time.time()
    checksum = 0.0
    for _ in range(iterations):
        a = np.random.rand(500, 500)
        b = np.random.rand(500, 500)
        checksum += float(np.sum(np.dot(a, b)))

    return {
        "hostname": platform.node(),
        "processor": platform.processor(),
        "compute_time": time.time() - started,
        "checksum": checksum,
    }


def main() -> int:
    """Run a compute-bound batch on an EC2 Fleet launched by serverless mode."""
    try:
        network = {
            "region": os.environ.get("AWS_TEST_REGION", "us-east-1"),
            # Required: the fleet's overrides name the subnet, and CreateFleet
            # refuses a null one.
            "vpc_id": os.environ["AWS_TEST_VPC_ID"],
            "subnet_id": os.environ["AWS_TEST_SUBNET_ID"],
            "security_group_id": os.environ["AWS_TEST_SG_ID"],
        }
    except KeyError as exc:
        logger.error("Missing required environment variable: %s", exc)
        return 2

    provider = EphemeralProvider(
        mode="serverless",
        # "ecs" is required: the fleet branch lives in the ECS submit path.
        # compute_type="lambda" ignores use_spot_fleet entirely.
        compute_type="ecs",
        use_spot_fleet=True,
        # Mix families and generations at a comparable size. These are type
        # names, not weighted dicts.
        instance_types=[
            "m5.large",
            "m5a.large",
            "m6i.large",
            "c5.large",
            "c5a.large",
        ],
        nodes_per_block=2,  # the fleet's target capacity
        spot_max_price_percentage=80,  # cap at 80% of on-demand
        spot_allocation_strategy="price-capacity-optimized",  # the default
        min_blocks=0,
        max_blocks=4,
        use_public_ips=True,
        # numpy is imported inside compute_intensive, which runs on the fleet
        # instance rather than here, so this is where it has to be installed --
        # the default worker_init installs Parsl alone. Serverless mode dropped
        # worker_init on the fleet path until #198, which is why this example
        # previously had nowhere to declare it.
        worker_init=(
            "dnf install -y python3.11 python3.11-pip\n"
            "ln -sf /usr/bin/python3.11 /usr/bin/python3\n"
            "pip3.11 install --quiet --upgrade parsl numpy\n"
        ),
        state_store_type="parameter_store",
        parameter_store_path="/parsl/serverless-spot-fleet",
        additional_tags={
            "Project": "ParslDemo",
            "Example": "ServerlessSpotFleet",
        },
        waiter_delay=15,
        waiter_max_attempts=40,
        **network,
    )

    config = Config(
        executors=[
            HighThroughputExecutor(
                label="serverless_fleet_executor",
                provider=provider,
                max_workers_per_node=4,
                heartbeat_threshold=600,
                heartbeat_period=30,
                encrypted=False,  # same-VPC; else distribute_certificates=True
            )
        ],
        # A reclaimed instance loses its in-flight tasks, and the interruption
        # warning produces a log line rather than recovery (#137). This is what
        # actually re-runs the work.
        retries=3,
        run_dir="runinfo_serverless_fleet",
    )

    try:
        parsl.load(config)

        futures = [compute_intensive(iterations=5 + i) for i in range(6)]
        logger.info("Submitted %d compute-bound tasks.", len(futures))

        failures = 0
        for i, future in enumerate(futures):
            try:
                result = future.result(timeout=1200)
                logger.info(
                    "Task %d on %s (%s): %.2fs, checksum %.6e",
                    i,
                    result["hostname"],
                    result["processor"] or "unknown",
                    result["compute_time"],
                    result["checksum"],
                )
            except Exception as exc:
                failures += 1
                logger.error("Task %d failed: %s", i, exc)

        return 1 if failures else 0

    except Exception:
        logger.exception("Workflow failed")
        return 1

    finally:
        parsl.clear()
        # Terminates the fleets, their instances, and the launch templates.
        provider.shutdown()
        logger.info("Provider shut down.")


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Spot interruption handling: seeing a reclaim coming, and surviving it.

Two separate jobs, split between the provider and Parsl.

**The provider detects.** With `spot_interruption_handling=True` it creates an
EventBridge rule matching the *EC2 Spot Instance Interruption Warning* event with an
SQS target, and polls it. That gives the full two-minute notice — verified against
real EC2 with a Fault Injection Simulator experiment, where the warning reached the
queue 15.2 s in with the instance still `running`. Polling instance state cannot
see anything until `shutting-down`, far too late to react.

A detected reclaim marks the block `FAILED`, and the marker is deliberately sticky:
an instance AWS is taking back still reports itself `running`, so re-deriving status
on the next poll would overwrite it.

**Parsl recovers.** `retries` on the `Config` is what re-runs the tasks that were on
the lost block. The provider cannot do it: a Parsl provider is handed a command and
returns a block ID, and is never told which tasks a block is running
(https://github.com/scttfrdmn/parsl-aws-provider/issues/137). So this example sets
`retries`, keeps chunks small enough that losing one is cheap, and shows how to
trigger a real interruption to watch the detection fire.

Usage
-----
    export AWS_PROFILE=aws
    export AWS_TEST_REGION=us-east-1
    export AWS_TEST_VPC_ID=vpc-...
    export AWS_TEST_SUBNET_ID=subnet-...
    export AWS_TEST_SG_ID=sg-...

    uv run python examples/spot_interruption_example.py

To force an interruption while it runs, in another shell:

    aws ec2 describe-instances --filters Name=tag:ParslResource,Values=true \
        Name=instance-state-name,Values=running \
        --query 'Reservations[].Instances[].InstanceId' --output text
    aws ec2 send-spot-instance-interruptions --instance-interruptions i-...

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

from parsl_aws_provider import EphemeralAWSProvider

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("parsl_aws_provider.compute.spot_interruption").setLevel(
    logging.DEBUG
)
logger = logging.getLogger("spot-interruption")


@python_app
def chunk_of_work(chunk_id, iterations=20):
    """Grind for a bit, then report where it ran.

    Kept deliberately short. Since a reclaim loses whatever was in flight, the
    unit of work is also the unit of loss — small chunks plus `retries` is the
    recovery strategy that actually exists today.
    """
    import socket
    import time
    import urllib.request

    import numpy as np

    def imds(path):
        """Read an IMDSv2 path, returning None off-EC2.

        Both ``urlopen`` calls carry ``# nosec B310``: the scheme is a literal
        ``http://`` against the link-local metadata address, so there is no
        caller-controlled URL for the ``file:`` scheme to reach through. ``path``
        is supplied by this module, not by submitted work.
        """
        try:
            token_req = urllib.request.Request(
                "http://169.254.169.254/latest/api/token",
                method="PUT",
                headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
            )
            token = urllib.request.urlopen(token_req, timeout=2).read().decode()  # nosec B310
            req = urllib.request.Request(
                f"http://169.254.169.254/latest/meta-data/{path}",
                headers={"X-aws-ec2-metadata-token": token},
            )
            return urllib.request.urlopen(req, timeout=2).read().decode()  # nosec B310
        except Exception:
            return None

    started = time.time()
    total = 0.0
    for _ in range(iterations):
        a = np.random.rand(400, 400)
        b = np.random.rand(400, 400)
        total += float(np.sum(np.dot(a, b)))

    return {
        "chunk_id": chunk_id,
        "hostname": socket.gethostname(),
        "instance_id": imds("instance-id"),
        "instance_type": imds("instance-type"),
        # Non-None means this worker has itself been marked for reclaim.
        "instance_action": imds("spot/instance-action"),
        "runtime": time.time() - started,
        "checksum": total,
    }


def main() -> int:
    """Run a chunked workload on diversified spot capacity."""
    try:
        provider = EphemeralAWSProvider(
            region=os.environ.get("AWS_TEST_REGION", "us-east-1"),
            vpc_id=os.environ["AWS_TEST_VPC_ID"],
            subnet_id=os.environ["AWS_TEST_SUBNET_ID"],
            security_group_id=os.environ["AWS_TEST_SG_ID"],
            mode="standard",
            # Both flags are required for the fleet path; use_spot_fleet alone
            # builds no fleet manager and falls through to one on-demand
            # instance with no error (#137).
            use_spot=True,
            use_spot_fleet=True,
            # Diversify across families and generations at a comparable size.
            # This is the single most effective interruption countermeasure.
            instance_types=["m5.large", "m5a.large", "m6i.large", "c5.large"],
            spot_max_price_percentage=70,
            spot_allocation_strategy="price-capacity-optimized",  # the default
            spot_interruption_handling=True,
            nodes_per_block=2,
            init_blocks=1,
            min_blocks=0,
            max_blocks=4,
            worker_init=(
                "dnf install -y python3.11 python3.11-pip\n"
                "ln -sf /usr/bin/python3.11 /usr/bin/python3\n"
                "pip3.11 install --quiet --upgrade parsl numpy\n"
            ),
            additional_tags={"Project": "ParslDemo", "Example": "SpotInterruption"},
            waiter_delay=15,
            waiter_max_attempts=40,
        )
    except KeyError as exc:
        logger.error("Missing required environment variable: %s", exc)
        logger.error("See the module docstring, and docs/network-prerequisites.md.")
        return 2

    config = Config(
        executors=[
            HighThroughputExecutor(
                label="spot_executor",
                provider=provider,
                address=address_by_route(),
                max_workers_per_node=2,
                heartbeat_threshold=600,
                heartbeat_period=30,
                encrypted=False,  # see #62
                worker_port_range=(54000, 55000),
            )
        ],
        # The whole recovery story. A reclaimed instance loses its in-flight
        # tasks; Parsl re-runs them elsewhere.
        retries=3,
        run_dir="runinfo_spot_interruption",
    )

    try:
        parsl.load(config)

        chunks = [chunk_of_work(i, iterations=20) for i in range(12)]
        logger.info(
            "Submitted %d chunks across diversified spot capacity.", len(chunks)
        )
        logger.info(
            "Watch for 'Spot interruption warning' in the log, or force one with "
            "aws ec2 send-spot-instance-interruptions."
        )

        instances = set()
        failures = 0
        for i, future in enumerate(chunks):
            try:
                result = future.result(timeout=1800)
                instances.add(result["instance_id"])
                logger.info(
                    "Chunk %d on %s (%s) in %.1fs%s",
                    result["chunk_id"],
                    result["instance_id"],
                    result["instance_type"],
                    result["runtime"],
                    " [MARKED FOR RECLAIM]" if result["instance_action"] else "",
                )
            except Exception as exc:
                failures += 1
                logger.error("Chunk %d failed after retries: %s", i, exc)

        logger.info(
            "%d/%d chunks completed across %d instance(s).",
            len(chunks) - failures,
            len(chunks),
            len(instances),
        )
        if len(instances) > 1:
            logger.info(
                "More than one instance ran work — either scaling or a reclaim "
                "with a successful retry."
            )
        return 1 if failures else 0

    except Exception:
        logger.exception("Workflow failed")
        return 1

    finally:
        parsl.clear()
        # Also removes the EventBridge rule and SQS queue created for the
        # interruption warning. Nothing runs at interpreter exit.
        provider.shutdown()
        logger.info("Provider shut down.")


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Detached mode: a bastion owns the worker lifecycle.

A bastion host launched from a CloudFormation stack provisions and terminates the
workers, so this machine never needs to be reachable from EC2. That makes
detached mode the option that works from behind NAT, and it lets a workflow
outlive the process that started it — reconnect later by constructing a provider
against the same state location.

The bastion is deliberately preserved when the client disconnects. Call
`provider.shutdown()` when the workflow is genuinely finished; nothing runs at
interpreter exit.

Usage
-----
    export AWS_PROFILE=aws
    export AWS_TEST_REGION=us-east-1
    export AWS_TEST_VPC_ID=vpc-...
    export AWS_TEST_SUBNET_ID=subnet-...
    export AWS_TEST_SG_ID=sg-...

    uv run python examples/detached_mode.py                    # start a workflow
    uv run python examples/detached_mode.py /parsl/my-workflow # reconnect to one

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import logging
import os
import sys
import time

import parsl
from parsl import python_app
from parsl.config import Config
from parsl.executors import HighThroughputExecutor

from parsl_aws_provider import EphemeralAWSProvider

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("detached-mode")


@python_app
def detached_task(duration=30, task_id=None):
    """Sleep, then report which worker ran this and for how long."""
    import socket
    import time

    started = time.time()
    time.sleep(duration)
    return {
        "task_id": task_id,
        "hostname": socket.gethostname(),
        "runtime": time.time() - started,
    }


def _build_provider(parameter_store_path: str) -> EphemeralAWSProvider:
    """Construct the provider. The same call starts or adopts a workflow.

    Reconnection is not a separate mode or flag: pointing a new provider at an
    existing state location adopts the persisted provider_id, bastion, and job
    map. The corollary is that two concurrent workflows must never share a state
    location, or they will fight over each other's resources.
    """
    return EphemeralAWSProvider(
        region=os.environ.get("AWS_TEST_REGION", "us-east-1"),
        vpc_id=os.environ["AWS_TEST_VPC_ID"],
        subnet_id=os.environ["AWS_TEST_SUBNET_ID"],
        security_group_id=os.environ["AWS_TEST_SG_ID"],
        mode="detached",
        instance_type="t3.small",
        bastion_instance_type="t3.micro",
        min_blocks=0,
        max_blocks=4,
        init_blocks=1,
        # Parameter Store is readable from both this machine and the bastion,
        # which is what makes adoption from a new process work.
        state_store_type="parameter_store",
        parameter_store_path=parameter_store_path,
        auto_create_instance_profile=True,
        worker_init=(
            "dnf install -y python3.11 python3.11-pip\n"
            "ln -sf /usr/bin/python3.11 /usr/bin/python3\n"
            "pip3.11 install --quiet --upgrade parsl\n"
        ),
        additional_tags={"Project": "ParslExample", "Mode": "detached"},
        waiter_delay=15,
        waiter_max_attempts=40,
    )


def main() -> int:
    """Start a detached workflow, or adopt an existing one and report on it."""
    reconnecting = len(sys.argv) > 1
    path = sys.argv[1] if reconnecting else f"/parsl/detached-{int(time.time())}"

    try:
        provider = _build_provider(path)
    except KeyError as exc:
        logger.error("Missing required environment variable: %s", exc)
        logger.error("See the module docstring, and docs/network-prerequisites.md.")
        return 2

    logger.info("State location: %s", path)
    logger.info("provider_id:    %s", provider.provider_id)
    if provider.operating_mode.bastion_id:
        logger.info("bastion:        %s", provider.operating_mode.bastion_id)

    if reconnecting:
        # Adoption restored the job map, so the previous run's jobs can be
        # queried without Parsl in the picture at all.
        job_ids = list(provider.job_map)
        for job_id, status in zip(job_ids, provider.status(job_ids)):
            logger.info("  %s -> %s", job_id, status.state.name)
        logger.info("Call provider.shutdown() to terminate this workflow.")
        return 0

    config = Config(
        executors=[
            HighThroughputExecutor(
                label="detached_mode_executor",
                provider=provider,
                max_workers_per_node=2,
                heartbeat_threshold=600,
                heartbeat_period=30,
                encrypted=False,  # see #62
            )
        ],
        run_dir="runinfo_detached",
    )

    try:
        parsl.load(config)
        tasks = [detached_task(duration=60, task_id=f"task-{i}") for i in range(3)]
        logger.info(
            "Submitted %d tasks; they continue if this process exits.", len(tasks)
        )

        for i, future in enumerate(tasks):
            result = future.result(timeout=1200)
            logger.info(
                "Task %d ran on %s in %.1fs", i, result["hostname"], result["runtime"]
            )
        return 0

    except KeyboardInterrupt:
        logger.info("Disconnected. Tasks continue running in AWS.")
        return 0

    except Exception:
        logger.exception("Workflow failed")
        return 1

    finally:
        # parsl.clear() disconnects the client; the bastion and workers survive
        # so the workflow can be adopted later.
        parsl.clear()
        logger.info("To tear everything down, reconnect and call shutdown():")
        logger.info("  %s %s", sys.argv[0], path)


if __name__ == "__main__":
    sys.exit(main())

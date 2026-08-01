#!/usr/bin/env python3
"""Serverless mode: Lambda functions or Fargate tasks instead of EC2 instances.

Lambda suits short, highly parallel work and needs no network configuration at
all — functions run in the Lambda-managed VPC. Fargate suits longer or
larger-memory tasks and does need a subnet and security group, because
`awsvpcConfiguration` is mandatory for it.

Two things to know before using this mode:

* `worker_init` has no effect on either backend. There is no instance to run it
  on, so dependencies must be in the Lambda deployment package or layer, or in
  the container image.
* The Fargate container image *is* configurable, as of v0.8.0 — pass
  `ecs_container_image` (#136). The default, `python:3.12-slim`, carries the
  standard library only, so set it to an image holding your dependencies. This is
  the usual reason to prefer Fargate over Lambda.

Usage
-----
    export AWS_PROFILE=aws
    export AWS_TEST_REGION=us-east-1

    uv run python examples/serverless_mode.py lambda

    # Fargate additionally needs a subnet and security group:
    export AWS_TEST_VPC_ID=vpc-...
    export AWS_TEST_SUBNET_ID=subnet-...
    export AWS_TEST_SG_ID=sg-...
    uv run python examples/serverless_mode.py ecs

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import logging
import os
import sys

import parsl
from parsl.app.python import python_app
from parsl.config import Config
from parsl.executors import HighThroughputExecutor

from parsl_ephemeral_aws import EphemeralAWSProvider

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("serverless-mode")


@python_app
def serverless_task(task_type="processing", data_size=1):
    """Do a little work and report the execution environment it landed in."""
    import math
    import os
    import platform
    import time

    started = time.time()

    if task_type == "processing":
        detail = {"processed_items": sum(1 for _ in range(data_size * 100_000))}
    elif task_type == "etl":
        records = [{"id": i, "value": i * 2} for i in range(data_size * 100)]
        transformed = [r["value"] * 1.5 for r in records]
        detail = {
            "transformed_count": len(transformed),
            "avg_value": sum(transformed) / len(transformed) if transformed else 0,
        }
    else:
        primes = [
            n
            for n in range(2, 10_000)
            if all(n % d for d in range(2, int(math.sqrt(n)) + 1))
        ]
        detail = {"prime_count": len(primes)}

    # Lambda exports its memory ceiling; Fargate does not.
    memory_limit = os.environ.get("AWS_LAMBDA_FUNCTION_MEMORY_SIZE", "unknown")
    on_lambda = "AWS_LAMBDA_FUNCTION_NAME" in os.environ

    return {
        "task_type": task_type,
        "environment": "Lambda" if on_lambda else "Fargate",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "memory_limit_mb": memory_limit,
        "execution_time": time.time() - started,
        "detail": detail,
    }


def _build_provider(compute_type: str) -> EphemeralAWSProvider:
    """Build a serverless provider for the requested backend."""
    options = {
        "region": os.environ.get("AWS_TEST_REGION", "us-east-1"),
        "mode": "serverless",
        # compute_type accepts "ec2", "lambda", or "ecs". There is no "auto" at
        # the provider level; leaving the "ec2" default in serverless mode leaves
        # ServerlessMode on its own internal heuristic.
        "compute_type": compute_type,
        "memory_size": 1024,  # MB; Lambda CPU scales with this
        "timeout": 300,  # seconds; Lambda's own ceiling is 900
        "min_blocks": 0,
        "max_blocks": 20,
        "state_store_type": "file",
        "state_file_path": f"serverless_{compute_type}_state.json",
        "additional_tags": {
            "Project": "ParslExample",
            "Mode": f"serverless-{compute_type}",
        },
    }

    if compute_type == "ecs":
        # Fargate needs awsvpcConfiguration, so the network IDs are required.
        options.update(
            vpc_id=os.environ["AWS_TEST_VPC_ID"],
            subnet_id=os.environ["AWS_TEST_SUBNET_ID"],
            security_group_id=os.environ["AWS_TEST_SG_ID"],
            # Reachable since v0.8.0 (#136). The tasks below need only the
            # standard library, so the default image would do; it is set
            # explicitly because substituting your own image is the point of
            # choosing Fargate. cpu/memory must be a pair Fargate accepts --
            # an invalid combination fails the task definition, not the task.
            ecs_container_image=os.environ.get(
                "AWS_TEST_ECS_IMAGE", "python:3.12-slim"
            ),
            ecs_task_cpu=512,
            ecs_task_memory=1024,
        )

    return EphemeralAWSProvider(**options)


def main() -> int:
    """Run a batch of short tasks on Lambda or Fargate."""
    compute_type = sys.argv[1] if len(sys.argv) > 1 else "lambda"
    if compute_type not in ("lambda", "ecs"):
        logger.error("Usage: %s [lambda|ecs]", sys.argv[0])
        return 2

    try:
        provider = _build_provider(compute_type)
    except KeyError as exc:
        logger.error("Missing required environment variable: %s", exc)
        return 2

    config = Config(
        executors=[
            HighThroughputExecutor(
                label=f"{compute_type}_executor",
                provider=provider,
                max_workers_per_node=10,
                encrypted=False,  # see #62
            )
        ],
        run_dir=f"runinfo_{compute_type}",
    )

    try:
        parsl.load(config)

        tasks = [
            serverless_task(task_type="processing", data_size=size)
            for size in (1, 5, 10)
        ]
        tasks += [serverless_task(task_type="inference") for _ in range(3)]
        tasks += [serverless_task(task_type="etl", data_size=8) for _ in range(2)]

        logger.info("Submitted %d tasks to %s.", len(tasks), compute_type)

        completed = []
        for i, future in enumerate(tasks):
            try:
                result = future.result(timeout=600)
                completed.append(result)
                logger.info(
                    "Task %d (%s) on %s: %.2fs, %s MB limit, %s",
                    i,
                    result["task_type"],
                    result["environment"],
                    result["execution_time"],
                    result["memory_limit_mb"],
                    result["detail"],
                )
            except Exception as exc:
                logger.error("Task %d failed: %s", i, exc)

        if completed:
            mean = sum(r["execution_time"] for r in completed) / len(completed)
            logger.info("%d/%d completed, mean %.2fs", len(completed), len(tasks), mean)
        return 0 if len(completed) == len(tasks) else 1

    except Exception:
        logger.exception("Workflow failed")
        return 1

    finally:
        parsl.clear()
        # Deletes the Lambda functions or the ECS cluster and task definitions.
        provider.shutdown()
        logger.info("Provider shut down.")


if __name__ == "__main__":
    sys.exit(main())

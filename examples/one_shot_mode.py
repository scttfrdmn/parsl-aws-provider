#!/usr/bin/env python3
"""One-shot mode: one command per instance, dispatched over SSM.

This is the only mode that needs **no inbound reachability at all**. The other
modes run Parsl's HTEX, whose workers connect back to an interchange on this
machine — so a laptop behind NAT cannot use them without port forwarding or a
VPN. One-shot mode instead pushes a command to the instance with SSM
``SendCommand``, and the connection is outbound from the instance to AWS. There
is no interchange, and therefore nothing to reach.

The trade-off is that there is no Parsl app graph: you submit shell commands
through the provider directly rather than decorating Python functions. Each
instance runs its one command, reports its exit code, and terminates. Use this
for batch work — a shell pipeline, a container run, a script already on a baked
AMI — not for a dependency graph of Python tasks.

Two properties are the point of the mode, and both are real-AWS verified in
``tests/aws/test_one_shot_e2e.py``:

* **The exit code determines the job status.** A non-zero exit reports FAILED.
  Status derived from EC2 instance state cannot do this — the instance state is
  identical for success and failure (#66, #76).
* **The instance terminates rather than stopping.** EC2 defaults an
  instance-initiated shutdown to *stop*, which leaves a billed EBS volume behind
  (#76).

``auto_create_instance_profile=True`` is required: SSM ``SendCommand`` needs the
instance to carry a profile holding ``AmazonSSMManagedInstanceCore``. The
provider creates that role and deletes it on shutdown; pass
``iam_instance_profile_arn`` instead to manage the lifecycle yourself.

Usage
-----
    export AWS_PROFILE=aws
    export AWS_TEST_REGION=us-east-1
    export AWS_TEST_VPC_ID=vpc-...
    export AWS_TEST_SUBNET_ID=subnet-...
    export AWS_TEST_SG_ID=sg-...

    uv run python examples/one_shot_mode.py

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import logging
import os
import sys
import time

from parsl.jobs.states import JobState

from parsl_aws_provider import EphemeralAWSProvider

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("one-shot-mode")

POLL_INTERVAL_S = 15
# SSM registration plus worker_init on real hardware; the first status is slow.
MAX_WAIT_S = 900

# The commands to run, one instance each. The second fails deliberately, to show
# that the exit code reaches the job status.
COMMANDS = [
    "echo hello from $(hostname); uname -srm",
    "echo this one fails >&2; exit 3",
]


def _wait_for_terminal(provider, job_id: str) -> JobState:
    """Poll ``status()`` until the job leaves PENDING/RUNNING.

    ``status()`` takes a list and returns a list of ``JobStatus``, which is the
    Parsl interface — not a dict, and not a bare state.
    """
    deadline = time.monotonic() + MAX_WAIT_S
    state = JobState.PENDING

    while time.monotonic() < deadline:
        state = provider.status([job_id])[0].state
        if state not in (JobState.PENDING, JobState.RUNNING):
            return state
        logger.info("  %s: %s", job_id[:8], state.name)
        time.sleep(POLL_INTERVAL_S)

    logger.error("Timed out after %ds with %s still %s", MAX_WAIT_S, job_id, state.name)
    return state


def main() -> int:
    """Dispatch one command per instance and report each exit status."""
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

    provider = EphemeralAWSProvider(
        instance_type="t3.micro",  # nothing is pip-installed here, so micro is fine
        mode="standard",  # one_shot is a standard-mode option
        one_shot=True,
        # Required: SSM SendCommand needs AmazonSSMManagedInstanceCore on the
        # instance. Created here and deleted on shutdown.
        auto_create_instance_profile=True,
        min_blocks=0,
        max_blocks=len(COMMANDS),
        init_blocks=0,  # each submit() launches its own instance
        state_store_type="file",
        state_file_path="one_shot_mode_state.json",
        additional_tags={"Project": "ParslExample", "Mode": "one-shot"},
        waiter_delay=15,
        waiter_max_attempts=40,
        debug=True,
        **network,
    )

    # No Parsl config and no HighThroughputExecutor: there is no interchange in
    # this mode, which is exactly why it works from behind NAT.
    try:
        job_ids = []
        for command in COMMANDS:
            job_id = provider.submit(command, tasks_per_node=1)
            logger.info("Submitted %s: %s", job_id[:8], command)
            job_ids.append(job_id)

        logger.info("Waiting for %d one-shot jobs; the first is slowest.", len(job_ids))

        failures = 0
        for job_id, command in zip(job_ids, COMMANDS):
            state = _wait_for_terminal(provider, job_id)
            # provider._cleanup_resources() drops the record once terminal, so the
            # exit code is read from the mode's copy, written during status().
            resource_id = provider.job_map.get(job_id, {}).get("resource_id")
            resource = provider.operating_mode.resources.get(resource_id or "", {})
            logger.info(
                "%s → %s (exit %s): %s",
                job_id[:8],
                state.name,
                resource.get("exit_code", "unknown"),
                command,
            )
            if state != JobState.COMPLETED:
                failures += 1

        # One command fails on purpose, so a single failure is the expected result
        # and is what demonstrates the mode works.
        logger.info("%d of %d jobs failed (1 expected).", failures, len(job_ids))
        return 0 if failures == 1 else 1

    except Exception:
        logger.exception("Workflow failed")
        return 1

    finally:
        # There is no atexit hook. shutdown() terminates any surviving instance
        # and deletes the launch template and the IAM role created above.
        provider.shutdown()
        logger.info("Provider shut down.")


if __name__ == "__main__":
    sys.exit(main())

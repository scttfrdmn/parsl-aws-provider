"""AWS-specific fixtures for real-AWS E2E tests.

These fixtures create and tear down a real EphemeralAWSProvider backed by
actual AWS infrastructure.  They are only activated when the test is marked
with @pytest.mark.aws and requires the 'aws' profile to be configured in
~/.aws/credentials.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import logging
import os
import uuid

import pytest

from parsl_ephemeral_aws.provider import EphemeralAWSProvider

logger = logging.getLogger(__name__)

AWS_TEST_REGION = os.environ.get("AWS_TEST_REGION", "us-west-2")
AWS_TEST_PROFILE = os.environ.get("AWS_TEST_PROFILE", "aws")


# ---------------------------------------------------------------------------
# Session-scoped region fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def aws_region() -> str:
    """Return the AWS region under test."""
    return AWS_TEST_REGION


# ---------------------------------------------------------------------------
# Per-test run ID — used in tags and state filenames
# ---------------------------------------------------------------------------


@pytest.fixture
def test_run_id() -> str:
    """Return a short unique ID for this test invocation."""
    return uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------------
# Main provider fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def aws_provider(tmp_path, aws_session, test_run_id, aws_region):
    """Create and initialize a real EphemeralAWSProvider.

    Yields a fully initialised provider and tears it down afterwards.
    The teardown is best-effort; the ``cleanup_stray_instances`` autouse
    fixture provides an additional safety net for any leaked instances.
    """
    state_file = str(tmp_path / f"state-{test_run_id}.json")

    provider = EphemeralAWSProvider(
        region=aws_region,
        instance_type="t3.micro",
        mode="standard",
        state_store_type="file",
        state_file_path=state_file,
        auto_shutdown=True,
        auto_create_instance_profile=True,
        profile_name=AWS_TEST_PROFILE,
        additional_tags={
            "E2ETestRunId": test_run_id,
            "AutoCleanup": "true",
        },
        # Allow up to ~10 minutes for real AWS waiters
        waiter_delay=15,
        waiter_max_attempts=40,
        debug=True,
    )

    provider.operating_mode.initialize()

    yield provider

    try:
        provider.shutdown()
    except Exception as exc:
        logger.warning("Provider shutdown raised an exception (best-effort): %s", exc)


# ---------------------------------------------------------------------------
# Safety-net fixture: terminate any stray instances after each test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def cleanup_stray_instances(aws_session, aws_region, test_run_id):
    """Autouse fixture: terminate leaked instances after every test."""
    yield  # test runs here

    try:
        ec2 = aws_session.client("ec2", region_name=aws_region)
        response = ec2.describe_instances(
            Filters=[
                {"Name": "tag:E2ETestRunId", "Values": [test_run_id]},
                {
                    "Name": "instance-state-name",
                    "Values": ["pending", "running", "stopping", "stopped"],
                },
            ]
        )
        instance_ids = [
            inst["InstanceId"]
            for reservation in response.get("Reservations", [])
            for inst in reservation.get("Instances", [])
        ]
        if instance_ids:
            logger.info(
                "cleanup_stray_instances: terminating %d stray instance(s): %s",
                len(instance_ids),
                instance_ids,
            )
            ec2.terminate_instances(InstanceIds=instance_ids)
    except Exception as exc:
        logger.warning("cleanup_stray_instances: exception (ignored): %s", exc)

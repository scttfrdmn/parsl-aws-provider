"""Real-AWS end-to-end tests for one-shot mode (issues #66, #76).

One-shot mode dispatches a single command per instance over SSM ``SendCommand``
and terminates the instance afterwards. Two properties matter and neither can be
verified with mocks:

* a non-zero exit code must surface as ``FAILED``. Under the original UserData
  design the instance state was identical for success and failure, so every job
  reported COMPLETED.
* the instance must reach ``terminated``, not ``stopped``. EC2 defaults an
  instance-initiated shutdown to *stop*, which leaves a billed EBS volume — and
  because ``EC2_STATUS_MAPPING`` maps ``stopped`` to COMPLETED, the provider
  drops the tracking record and the volume is orphaned as well as billed.

Run with::

    AWS_TEST_VPC_ID=vpc-xxx AWS_TEST_SUBNET_ID=subnet-xxx AWS_TEST_SG_ID=sg-xxx \\
    AWS_PROFILE=aws pytest tests/aws/test_one_shot_e2e.py -m aws --no-cov -v

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import logging
import time

import pytest
from parsl.jobs.states import JobState

from parsl_ephemeral_aws.provider import EphemeralAWSProvider

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.aws, pytest.mark.slow]

POLL_INTERVAL_S = 15
MAX_WAIT_S = 900  # 15 minutes — SSM registration plus worker_init on real iron

AWS_TEST_PROFILE = "aws"


def _poll_until_terminal(provider, job_id: str, timeout: int = MAX_WAIT_S):
    """Poll ``status()`` until the job leaves PENDING/RUNNING.

    Returns
    -------
    Optional[JobState]
        The terminal state reached, or None on timeout.
    """
    non_terminal = (JobState.PENDING, JobState.RUNNING)
    deadline = time.time() + timeout
    while time.time() < deadline:
        statuses = provider.status([job_id])
        if statuses and statuses[0].state not in non_terminal:
            return statuses[0].state
        time.sleep(POLL_INTERVAL_S)
    return None


def _wait_for_instance_state(ec2, instance_id: str, wanted, timeout: int = 600):
    """Poll ``describe_instances`` until the instance reaches one of *wanted*."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            response = ec2.describe_instances(InstanceIds=[instance_id])
        except Exception as exc:  # instance may have aged out entirely
            logger.warning("describe_instances raised (treated as gone): %s", exc)
            return "terminated"
        for reservation in response.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                last = instance["State"]["Name"]
                if last in wanted:
                    return last
        time.sleep(POLL_INTERVAL_S)
    return last


@pytest.fixture
def one_shot_provider(tmp_path, test_run_id, aws_region, network_ids):
    """A provider in one-shot mode with SSM dispatch enabled.

    ``auto_create_instance_profile=True`` is required: one-shot dispatch goes
    through SSM ``SendCommand``, which needs the instance to carry a profile
    holding ``AmazonSSMManagedInstanceCore``.
    """
    provider = EphemeralAWSProvider(
        region=aws_region,
        instance_type="t3.micro",
        mode="standard",
        one_shot=True,
        auto_create_instance_profile=True,
        state_store_type="file",
        state_file_path=str(tmp_path / f"one-shot-{test_run_id}.json"),
        profile_name=AWS_TEST_PROFILE,
        additional_tags={"E2ETestRunId": test_run_id, "AutoCleanup": "true"},
        waiter_delay=15,
        waiter_max_attempts=40,
        debug=True,
        **network_ids,
    )

    yield provider

    try:
        provider.shutdown()
    except Exception as exc:
        logger.warning("Provider shutdown raised (best-effort): %s", exc)


class TestOneShotExitCodes:
    """A one-shot job's status must follow its command's exit code (#76b)."""

    def test_successful_command_reports_completed(self, one_shot_provider):
        """``exit 0`` → COMPLETED, with the exit code recorded."""
        job_id = one_shot_provider.submit("echo hello-one-shot", tasks_per_node=1)

        state = _poll_until_terminal(one_shot_provider, job_id)

        assert state == JobState.COMPLETED, f"expected COMPLETED, got {state}"

    def test_failing_command_reports_failed(self, one_shot_provider):
        """``exit 1`` → FAILED.

        This is the defect #66 specified and never implemented: status was
        derived from EC2 instance state, which is identical either way, so a
        failing command reported COMPLETED and Parsl never retried it.
        """
        job_id = one_shot_provider.submit(
            "echo about-to-fail >&2; exit 1", tasks_per_node=1
        )

        state = _poll_until_terminal(one_shot_provider, job_id)

        assert state == JobState.FAILED, f"expected FAILED, got {state}"

    def test_exit_code_is_recorded_on_the_resource(self, one_shot_provider):
        """The SSM response code is stored so a failure is diagnosable."""
        job_id = one_shot_provider.submit("exit 42", tasks_per_node=1)
        resource_id = one_shot_provider.job_map[job_id]["resource_id"]

        state = _poll_until_terminal(one_shot_provider, job_id)
        assert state == JobState.FAILED, f"expected FAILED, got {state}"

        # provider._cleanup_resources() removes the record once terminal, so read
        # the mode's copy, which is written during get_job_status().
        resource = one_shot_provider.operating_mode.resources.get(resource_id, {})
        assert resource.get("exit_code") == 42, (
            f"expected exit_code 42, got {resource.get('exit_code')} "
            f"(resource: {resource})"
        )


class TestOneShotInstanceTermination:
    """One-shot instances must terminate, not stop (#76a)."""

    def test_instance_terminates_rather_than_stopping(
        self, one_shot_provider, aws_session, aws_region
    ):
        """A stopped instance keeps a billed EBS volume the provider forgot about."""
        ec2 = aws_session.client("ec2", region_name=aws_region)

        job_id = one_shot_provider.submit("echo hello-one-shot", tasks_per_node=1)
        resource_id = one_shot_provider.job_map[job_id]["resource_id"]

        state = _poll_until_terminal(one_shot_provider, job_id)
        assert state == JobState.COMPLETED, f"expected COMPLETED, got {state}"

        final = _wait_for_instance_state(
            ec2, resource_id, ("terminated", "shutting-down", "stopped")
        )
        assert final in ("terminated", "shutting-down"), (
            f"instance {resource_id} reached '{final}'; a stopped instance keeps "
            "a billed EBS volume"
        )

    def test_launch_sets_shutdown_behavior_to_terminate(
        self, one_shot_provider, aws_session, aws_region
    ):
        """Verify the attribute on the live instance, not just the API call."""
        ec2 = aws_session.client("ec2", region_name=aws_region)

        job_id = one_shot_provider.submit("echo hello-one-shot", tasks_per_node=1)
        resource_id = one_shot_provider.job_map[job_id]["resource_id"]

        try:
            attribute = ec2.describe_instance_attribute(
                InstanceId=resource_id,
                Attribute="instanceInitiatedShutdownBehavior",
            )
            behavior = attribute["InstanceInitiatedShutdownBehavior"]["Value"]
            assert behavior == "terminate", (
                f"instance {resource_id} would '{behavior}' on shutdown, leaving "
                "a billed EBS volume"
            )
        finally:
            try:
                one_shot_provider.cancel([job_id])
            except Exception as exc:
                logger.warning("teardown cancel raised (ignored): %s", exc)

    def test_no_ebs_volume_is_left_behind(
        self, one_shot_provider, aws_session, aws_region
    ):
        """The root volume must go with the instance."""
        ec2 = aws_session.client("ec2", region_name=aws_region)

        job_id = one_shot_provider.submit("echo hello-one-shot", tasks_per_node=1)
        resource_id = one_shot_provider.job_map[job_id]["resource_id"]

        response = ec2.describe_instances(InstanceIds=[resource_id])
        volume_ids = [
            mapping["Ebs"]["VolumeId"]
            for reservation in response.get("Reservations", [])
            for instance in reservation.get("Instances", [])
            for mapping in instance.get("BlockDeviceMappings", [])
            if "Ebs" in mapping
        ]
        assert volume_ids, f"instance {resource_id} reported no EBS volumes"

        state = _poll_until_terminal(one_shot_provider, job_id)
        assert state == JobState.COMPLETED, f"expected COMPLETED, got {state}"

        _wait_for_instance_state(ec2, resource_id, ("terminated",))

        remaining = ec2.describe_volumes(
            VolumeIds=volume_ids,
            Filters=[{"Name": "status", "Values": ["available", "in-use"]}],
        ).get("Volumes", [])
        assert not remaining, (
            f"volumes still billable after termination: "
            f"{[v['VolumeId'] for v in remaining]}"
        )


class TestOneShotConfiguration:
    """Guards that keep a misconfigured one-shot provider from starting."""

    def test_one_shot_requires_an_instance_profile(
        self, tmp_path, test_run_id, aws_region, network_ids
    ):
        """SSM dispatch is impossible without a profile, so fail at construction."""
        with pytest.raises(ValueError, match="one_shot=True requires"):
            EphemeralAWSProvider(
                region=aws_region,
                mode="standard",
                one_shot=True,
                state_store_type="file",
                state_file_path=str(tmp_path / f"guard-{test_run_id}.json"),
                profile_name=AWS_TEST_PROFILE,
                **network_ids,
            )

    def test_command_is_not_embedded_in_user_data(
        self, one_shot_provider, aws_session, aws_region
    ):
        """The command travels over SSM; UserData only prepares the worker.

        If the command were in UserData it would run before SSM was reachable
        and its exit code would be unobservable.
        """
        import base64

        ec2 = aws_session.client("ec2", region_name=aws_region)
        sentinel = "sentinel-not-in-user-data"

        job_id = one_shot_provider.submit(f"echo {sentinel}", tasks_per_node=1)
        resource_id = one_shot_provider.job_map[job_id]["resource_id"]

        try:
            attribute = ec2.describe_instance_attribute(
                InstanceId=resource_id, Attribute="userData"
            )
            user_data = base64.b64decode(
                attribute.get("UserData", {}).get("Value", "")
            ).decode()

            assert sentinel not in user_data, "command leaked into UserData"
            assert "/var/run/parsl_worker_ready" in user_data
        finally:
            try:
                one_shot_provider.cancel([job_id])
            except Exception as exc:
                logger.warning("teardown cancel raised (ignored): %s", exc)

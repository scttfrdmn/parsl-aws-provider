"""Real-AWS end-to-end tests for spot instance lifecycle and interruption recovery.

These tests create actual AWS infrastructure using spot EC2 instances and verify:

1. Infrastructure (VPC/subnet/SG) is created correctly.
2. The launched instance is a *spot* request (``InstanceLifecycle == 'spot'``).
3. Status transitions work as expected.
4. The spot interruption monitor starts when ``spot_interruption_handling=True``.
5. Force-terminating a spot instance causes the job to leave RUNNING state.

Run with::

    AWS_PROFILE=aws pytest tests/aws/test_spot_e2e.py -m "aws" --no-cov -v

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import ipaddress
import logging
import time

import pytest

from parsl.jobs.states import JobState

from parsl_ephemeral_provider.provider import EphemeralProvider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

POLL_INTERVAL_S = 15
MAX_WAIT_S = 600  # 10 minutes


def _poll_until(
    provider, job_id: str, target_state: JobState, timeout: int = MAX_WAIT_S
) -> bool:
    """Poll provider.status() until the job reaches *target_state* or timeout.

    Returns True if the target state was reached, False on timeout.

    ``status()`` returns ``List[JobStatus]``, not the ``List[Dict[str, str]]``
    this suite was written against, and ``JobStatus`` is not subscriptable —
    see #102.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = provider.status([job_id])
        if result and result[0].state == target_state:
            return True
        time.sleep(POLL_INTERVAL_S)
    return False


def _poll_until_not(provider, job_id: str, current_state: JobState, timeout=MAX_WAIT_S):
    """Poll until the job leaves *current_state*; return the new state or None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = provider.status([job_id])
        if result and result[0].state != current_state:
            return result[0].state
        time.sleep(POLL_INTERVAL_S)
    return None


# ---------------------------------------------------------------------------
# TestSpotInfrastructure
# ---------------------------------------------------------------------------


@pytest.mark.aws
@pytest.mark.slow
class TestSpotInfrastructure:
    """Verify that VPC/subnet/SG resources are created correctly for spot mode.

    Uses the ``spot_provider`` fixture (``use_spot=True``, file-based state).
    """

    def test_vpc_created_and_available(self, spot_provider, aws_session, aws_region):
        """Provider creates a VPC and it reaches 'available' state."""
        vpc_id = spot_provider.operating_mode.vpc_id
        assert vpc_id is not None, "vpc_id should be set after initialize()"

        ec2 = aws_session.client("ec2", region_name=aws_region)
        response = ec2.describe_vpcs(VpcIds=[vpc_id])
        vpcs = response.get("Vpcs", [])
        assert len(vpcs) == 1, f"Expected exactly one VPC for {vpc_id}, got {vpcs}"
        assert vpcs[0]["State"] == "available", (
            f"VPC {vpc_id} is in state '{vpcs[0]['State']}', expected 'available'"
        )

    def test_subnet_created_in_vpc(self, spot_provider, aws_session, aws_region):
        """Provider creates a subnet inside the provider VPC."""
        subnet_id = spot_provider.operating_mode.subnet_id
        vpc_id = spot_provider.operating_mode.vpc_id
        assert subnet_id is not None, "subnet_id should be set after initialize()"

        ec2 = aws_session.client("ec2", region_name=aws_region)
        response = ec2.describe_subnets(SubnetIds=[subnet_id])
        subnets = response.get("Subnets", [])
        assert len(subnets) == 1, f"Expected exactly one subnet for {subnet_id}"
        assert subnets[0]["VpcId"] == vpc_id, (
            f"Subnet {subnet_id} belongs to VPC {subnets[0]['VpcId']}, expected {vpc_id}"
        )

    def test_security_group_created_in_vpc(
        self, spot_provider, aws_session, aws_region
    ):
        """Provider creates a security group inside the provider VPC."""
        sg_id = spot_provider.operating_mode.security_group_id
        vpc_id = spot_provider.operating_mode.vpc_id
        assert sg_id is not None, "security_group_id should be set after initialize()"

        ec2 = aws_session.client("ec2", region_name=aws_region)
        response = ec2.describe_security_groups(GroupIds=[sg_id])
        groups = response.get("SecurityGroups", [])
        assert len(groups) == 1, f"Expected exactly one SG for {sg_id}"
        assert groups[0]["VpcId"] == vpc_id, (
            f"Security group {sg_id} belongs to VPC {groups[0]['VpcId']}, "
            f"expected {vpc_id}"
        )

    def test_vpc_cidr_no_conflict(self, spot_provider, aws_session, aws_region):
        """The provider VPC CIDR does not overlap any other VPC in the account."""
        vpc_id = spot_provider.operating_mode.vpc_id
        assert vpc_id is not None

        ec2 = aws_session.client("ec2", region_name=aws_region)
        response = ec2.describe_vpcs()
        all_vpcs = response.get("Vpcs", [])

        provider_cidr = None
        other_cidrs = []
        for vpc in all_vpcs:
            if vpc["VpcId"] == vpc_id:
                provider_cidr = vpc["CidrBlock"]
            else:
                other_cidrs.append(vpc["CidrBlock"])

        assert provider_cidr is not None, f"Could not find CIDR for VPC {vpc_id}"

        provider_net = ipaddress.IPv4Network(provider_cidr, strict=False)
        for cidr in other_cidrs:
            other_net = ipaddress.IPv4Network(cidr, strict=False)
            assert not provider_net.overlaps(other_net), (
                f"Provider VPC CIDR {provider_cidr} overlaps existing VPC CIDR {cidr}"
            )


# ---------------------------------------------------------------------------
# TestSpotComputeLifecycle
# ---------------------------------------------------------------------------


@pytest.mark.aws
@pytest.mark.slow
class TestSpotComputeLifecycle:
    """Verify spot instance launch, instance lifecycle type, status transitions,
    command completion, and cancellation.

    Each test uses a *fresh* provider instance (function scope) to keep tests
    independent.
    """

    def test_submit_spot_returns_job_id(self, spot_provider):
        """submit() returns a non-empty job ID that appears in job_map."""
        job_id = spot_provider.submit("echo hello-from-spot", tasks_per_node=1)
        try:
            assert job_id, "job_id should be a non-empty string"
            assert job_id in spot_provider.job_map, (
                f"job_id {job_id} not found in provider.job_map"
            )
        finally:
            try:
                spot_provider.cancel([job_id])
            except Exception as exc:
                logger.warning("teardown cancel raised (ignored): %s", exc)

    def test_spot_instance_is_spot_request(
        self, spot_provider, aws_session, aws_region
    ):
        """The EC2 instance launched for a spot job has InstanceLifecycle='spot'."""
        job_id = spot_provider.submit("echo hello-from-spot", tasks_per_node=1)
        try:
            resource_id = spot_provider.job_map[job_id]["resource_id"]

            ec2 = aws_session.client("ec2", region_name=aws_region)
            response = ec2.describe_instances(InstanceIds=[resource_id])
            instances = [
                inst
                for res in response.get("Reservations", [])
                for inst in res.get("Instances", [])
            ]
            assert instances, f"No instance found for {resource_id}"
            lifecycle = instances[0].get("InstanceLifecycle")
            assert lifecycle == "spot", (
                f"Expected InstanceLifecycle='spot', got '{lifecycle}' for {resource_id}"
            )
        finally:
            try:
                spot_provider.cancel([job_id])
            except Exception as exc:
                logger.warning("teardown cancel raised (ignored): %s", exc)

    def test_spot_status_running_after_submit(self, spot_provider):
        """After submit() the spot instance should already be RUNNING.

        submit() internally waits for the instance to reach the running state
        before returning, so RUNNING is the expected status immediately.
        """
        job_id = spot_provider.submit("echo hello-from-spot", tasks_per_node=1)
        try:
            result = spot_provider.status([job_id])
            assert result, "status() returned an empty list"
            state = result[0].state
            assert state == JobState.RUNNING, (
                f"Expected RUNNING immediately after submit, got {state}"
            )
        finally:
            try:
                spot_provider.cancel([job_id])
            except Exception as exc:
                logger.warning("teardown cancel raised (ignored): %s", exc)

    def test_spot_command_completes(self, spot_provider, aws_session, aws_region):
        """A short-lived spot command completes and the instance terminates.

        With ``auto_shutdown=True`` the UserData script ends with 'shutdown -h now'
        so the instance self-terminates once the command finishes.
        """
        job_id = spot_provider.submit("echo hello-from-spot", tasks_per_node=1)
        resource_id = spot_provider.job_map[job_id]["resource_id"]

        reached = _poll_until(spot_provider, job_id, JobState.COMPLETED)
        assert reached, f"Job {job_id} did not reach COMPLETED within {MAX_WAIT_S}s"

        ec2 = aws_session.client("ec2", region_name=aws_region)
        response = ec2.describe_instances(InstanceIds=[resource_id])
        instances = [
            inst
            for res in response.get("Reservations", [])
            for inst in res.get("Instances", [])
        ]
        assert instances, f"No instance record found for {resource_id}"
        state = instances[0]["State"]["Name"]
        assert state in (
            "terminated",
            "shutting-down",
        ), f"Instance {resource_id} is in state '{state}'; expected terminal state"

    def test_spot_cancel_terminates_instance(
        self, spot_provider, aws_session, aws_region
    ):
        """cancel() terminates the backing spot EC2 instance."""
        job_id = spot_provider.submit("sleep 300", tasks_per_node=1)
        resource_id = spot_provider.job_map[job_id]["resource_id"]

        result = spot_provider.status([job_id])
        assert result[0].state == JobState.RUNNING, (
            "Expected RUNNING before cancellation"
        )

        spot_provider.cancel([job_id])

        ec2 = aws_session.client("ec2", region_name=aws_region)
        deadline = time.time() + 120
        final_state = None
        while time.time() < deadline:
            response = ec2.describe_instances(InstanceIds=[resource_id])
            instances = [
                inst
                for res in response.get("Reservations", [])
                for inst in res.get("Instances", [])
            ]
            if instances:
                final_state = instances[0]["State"]["Name"]
                if final_state in ("terminated", "shutting-down"):
                    break
            time.sleep(POLL_INTERVAL_S)

        assert final_state in ("terminated", "shutting-down"), (
            f"Instance {resource_id} did not reach terminal state after cancel(); "
            f"last observed state: {final_state}"
        )


# ---------------------------------------------------------------------------
# TestSpotInterruptionMonitor
# ---------------------------------------------------------------------------


@pytest.mark.aws
@pytest.mark.slow
class TestSpotInterruptionMonitor:
    """Verify spot interruption monitoring behaviour.

    ``spot_interruption_handling=True`` is the only requirement. It used to also
    need an S3 bucket, because the monitor was built only when a
    ``checkpoint_bucket`` was configured -- so a caller who asked for
    interruption handling and gave no bucket silently got none at all. #137
    removed both that gate and the checkpointing API behind it.
    """

    def _make_interruption_provider(
        self, tmp_path, aws_session, test_run_id, aws_region
    ) -> EphemeralProvider:
        """Helper: create a provider with interruption handling enabled."""
        state_file = str(tmp_path / f"state-int-{test_run_id}.json")
        provider = EphemeralProvider(
            region=aws_region,
            instance_type="t3.micro",
            mode="standard",
            use_spot=True,
            spot_interruption_handling=True,
            state_store_type="file",
            state_file_path=state_file,
            auto_shutdown=True,
            auto_create_instance_profile=True,
            profile_name=aws_session.profile_name or "aws",
            additional_tags={
                "E2ETestRunId": test_run_id,
                "AutoCleanup": "true",
            },
            waiter_delay=15,
            waiter_max_attempts=40,
            debug=True,
        )
        return provider

    def test_interruption_monitor_starts_after_initialize(
        self, tmp_path, aws_session, test_run_id, aws_region
    ):
        """After initialize() the spot interruption monitor thread is running."""
        provider = self._make_interruption_provider(
            tmp_path, aws_session, test_run_id, aws_region
        )
        try:
            provider.operating_mode.initialize()

            monitor = provider.operating_mode.spot_interruption_monitor
            assert monitor is not None, (
                "spot_interruption_monitor should be set whenever "
                "spot_interruption_handling=True"
            )
            assert (
                monitor.monitoring_thread is not None
                and monitor.monitoring_thread.is_alive()
            ), "Interruption monitoring thread should be alive after initialize()"
        finally:
            try:
                provider.shutdown()
            except Exception as exc:
                logger.warning("interruption provider shutdown: %s", exc)

    def test_forced_termination_transitions_out_of_running(
        self, spot_provider, aws_session, aws_region
    ):
        """Force-terminating a running spot instance causes the job to leave RUNNING.

        Submits a long-lived ``sleep 300`` job, force-terminates the backing EC2
        instance via boto3, then polls until the provider reports any status other
        than RUNNING (or times out after 3 minutes).

        Note: ``spot_provider`` uses ``spot_interruption_handling=False`` so the
        job reaches COMPLETED (EC2 termination → COMPLETED via status mapping)
        rather than FAILED.  The important assertion is that the job *leaves* the
        RUNNING state promptly after the instance disappears.
        """
        job_id = spot_provider.submit("sleep 300", tasks_per_node=1)
        resource_id = spot_provider.job_map[job_id]["resource_id"]

        # Confirm it is RUNNING first
        result = spot_provider.status([job_id])
        assert result and result[0].state == JobState.RUNNING, (
            "Expected RUNNING status before force-termination"
        )

        # Force-terminate the EC2 instance directly
        ec2 = aws_session.client("ec2", region_name=aws_region)
        ec2.terminate_instances(InstanceIds=[resource_id])
        logger.info(
            "test_forced_termination: force-terminated instance %s for job %s",
            resource_id,
            job_id,
        )

        # Poll until the provider detects the change (3 minute timeout)
        new_state = _poll_until_not(
            spot_provider, job_id, JobState.RUNNING, timeout=180
        )
        assert new_state is not None, (
            f"Job {job_id} did not leave RUNNING state within 180s after "
            f"instance {resource_id} was force-terminated"
        )
        logger.info(
            "test_forced_termination: job %s transitioned from RUNNING to %s",
            job_id,
            new_state,
        )

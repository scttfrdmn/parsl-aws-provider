"""Real-AWS end-to-end tests for DetachedMode (bastion host + SSM tunnel).

These tests create actual AWS infrastructure — VPC, security group, and a
persistent bastion EC2 instance — and verify the full detached provider lifecycle:

    initialise → bastion running → submit → status → cancel / complete → shutdown

Run with::

    AWS_PROFILE=aws pytest tests/aws/test_detached_mode_e2e.py -m "aws" --no-cov -v

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import logging
import time

import pytest
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

POLL_INTERVAL_S = 15
MAX_WAIT_S = 600  # 10 minutes


def _poll_until(
    provider, job_id: str, target_status: str, timeout: int = MAX_WAIT_S
) -> bool:
    """Poll provider.status() until the job reaches *target_status* or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = provider.status([job_id])
        if result and result[0]["status"] == target_status:
            return True
        time.sleep(POLL_INTERVAL_S)
    return False


# ---------------------------------------------------------------------------
# TestDetachedModeInfrastructure
# ---------------------------------------------------------------------------


@pytest.mark.aws
@pytest.mark.slow
class TestDetachedModeInfrastructure:
    """Verify that VPC, security group, and bastion host are created correctly."""

    def test_vpc_created_and_available(
        self, detached_provider, aws_session, aws_region
    ):
        """Provider creates a VPC and it reaches 'available' state."""
        vpc_id = detached_provider.operating_mode.vpc_id
        assert vpc_id is not None, "vpc_id should be set after initialize()"

        ec2 = aws_session.client("ec2", region_name=aws_region)
        response = ec2.describe_vpcs(VpcIds=[vpc_id])
        vpcs = response.get("Vpcs", [])
        assert len(vpcs) == 1, f"Expected exactly one VPC for {vpc_id}, got {vpcs}"
        assert (
            vpcs[0]["State"] == "available"
        ), f"VPC {vpc_id} is in state '{vpcs[0]['State']}', expected 'available'"

    def test_bastion_instance_running(self, detached_provider, aws_session, aws_region):
        """The bastion host EC2 instance exists and is in 'running' state.

        The bastion is identified by ``operating_mode.bastion_id``.  For
        ``bastion_host_type='direct'`` this is an instance ID; for
        ``bastion_host_type='cloudformation'`` it is the CloudFormation stack name
        (the instance ID is retrieved from the stack outputs / resources).
        """
        bastion_id = detached_provider.operating_mode.bastion_id
        assert (
            bastion_id is not None
        ), "bastion_id should be set after initialize() in detached mode"

        ec2 = aws_session.client("ec2", region_name=aws_region)

        # If bastion_id looks like an instance ID, query it directly.
        # If it looks like a CF stack name, list instances tagged with it.
        if bastion_id.startswith("i-"):
            response = ec2.describe_instances(InstanceIds=[bastion_id])
            instances = [
                inst
                for res in response.get("Reservations", [])
                for inst in res.get("Instances", [])
            ]
            assert instances, f"No EC2 instance found for bastion_id={bastion_id}"
            state = instances[0]["State"]["Name"]
            assert (
                state == "running"
            ), f"Bastion instance {bastion_id} is in state '{state}', expected 'running'"
        else:
            # CloudFormation-based bastion: look for instances tagged with the stack
            response = ec2.describe_instances(
                Filters=[
                    {
                        "Name": "tag:aws:cloudformation:stack-name",
                        "Values": [bastion_id],
                    },
                    {
                        "Name": "instance-state-name",
                        "Values": ["pending", "running"],
                    },
                ]
            )
            instances = [
                inst
                for res in response.get("Reservations", [])
                for inst in res.get("Instances", [])
            ]
            assert (
                instances
            ), f"No running EC2 instance found for CF bastion stack '{bastion_id}'"
            state = instances[0]["State"]["Name"]
            assert (
                state in ("pending", "running")
            ), f"Bastion instance is in state '{state}'; expected 'pending' or 'running'"

    def test_bastion_tagged_correctly(self, detached_provider, aws_session, aws_region):
        """Bastion instance (direct) carries the 'ProviderId' tag."""
        bastion_id = detached_provider.operating_mode.bastion_id
        assert bastion_id is not None

        # Only directly verifiable for direct (non-CF) deployments
        if not bastion_id.startswith("i-"):
            pytest.skip(
                "Tag verification for CloudFormation-based bastions requires "
                "looking up the instance inside the stack — not yet implemented."
            )

        ec2 = aws_session.client("ec2", region_name=aws_region)
        response = ec2.describe_instances(InstanceIds=[bastion_id])
        instances = [
            inst
            for res in response.get("Reservations", [])
            for inst in res.get("Instances", [])
        ]
        assert instances, f"No instance found for {bastion_id}"

        tags = {t["Key"]: t["Value"] for t in instances[0].get("Tags", [])}
        assert tags.get("ProviderId") == detached_provider.provider_id, (
            f"Tag 'ProviderId' expected '{detached_provider.provider_id}', "
            f"got '{tags.get('ProviderId')}'"
        )


# ---------------------------------------------------------------------------
# TestDetachedModeComputeLifecycle
# ---------------------------------------------------------------------------


@pytest.mark.aws
@pytest.mark.slow
class TestDetachedModeComputeLifecycle:
    """Verify job submission, status reporting, command completion, and cancellation.

    Each test uses a *fresh* provider instance (function scope) to keep tests
    independent.
    """

    def test_submit_returns_job_id(self, detached_provider):
        """submit() returns a non-empty job ID that appears in job_map."""
        job_id = detached_provider.submit("echo hello-from-detached", tasks_per_node=1)
        try:
            assert job_id, "job_id should be a non-empty string"
            assert (
                job_id in detached_provider.job_map
            ), f"job_id {job_id} not found in provider.job_map"
        finally:
            try:
                detached_provider.cancel([job_id])
            except Exception as exc:
                logger.warning("teardown cancel raised (ignored): %s", exc)

    def test_job_status_after_submit(self, detached_provider):
        """Job is in PENDING or RUNNING state immediately after submit()."""
        job_id = detached_provider.submit("echo hello-from-detached", tasks_per_node=1)
        try:
            result = detached_provider.status([job_id])
            assert result, "status() returned an empty list"
            status = result[0]["status"]
            assert status in (
                "PENDING",
                "RUNNING",
            ), f"Expected PENDING or RUNNING immediately after submit, got {status}"
        finally:
            try:
                detached_provider.cancel([job_id])
            except Exception as exc:
                logger.warning("teardown cancel raised (ignored): %s", exc)

    def test_command_completes(self, detached_provider):
        """A short ``echo`` command reaches COMPLETED within the timeout."""
        job_id = detached_provider.submit("echo hello-from-detached", tasks_per_node=1)

        reached = _poll_until(detached_provider, job_id, "COMPLETED")
        assert reached, f"Job {job_id} did not reach COMPLETED within {MAX_WAIT_S}s"

    def test_cancel_removes_job(self, detached_provider):
        """cancel() transitions the job to a terminal (non-RUNNING) state."""
        job_id = detached_provider.submit("sleep 300", tasks_per_node=1)

        # Verify it starts running
        deadline = time.time() + 120
        started = False
        while time.time() < deadline:
            result = detached_provider.status([job_id])
            if result and result[0]["status"] in ("PENDING", "RUNNING"):
                started = True
                break
            time.sleep(POLL_INTERVAL_S)
        assert started, f"Job {job_id} did not reach PENDING/RUNNING within 120s"

        detached_provider.cancel([job_id])

        # After cancel, the job should not be RUNNING
        result = detached_provider.status([job_id])
        if result:
            status = result[0]["status"]
            assert status not in (
                "RUNNING",
            ), f"Job {job_id} is still RUNNING after cancel(); status={status}"


# ---------------------------------------------------------------------------
# TestDetachedModeCleanup
# ---------------------------------------------------------------------------


@pytest.mark.aws
@pytest.mark.slow
class TestDetachedModeCleanup:
    """Verify that provider.shutdown() removes all created infrastructure."""

    def test_shutdown_removes_bastion(self, detached_provider, aws_session, aws_region):
        """After shutdown() the bastion host reaches a terminal EC2 state.

        For direct-instance bastions the instance should be in
        'terminated' or 'shutting-down'.  For CF-based bastions the stack
        should be deleted (no running instances).
        """
        bastion_id = detached_provider.operating_mode.bastion_id
        assert bastion_id is not None

        detached_provider.shutdown()

        ec2 = aws_session.client("ec2", region_name=aws_region)

        if bastion_id.startswith("i-"):
            # Direct bastion: poll for terminal state (2 min max)
            deadline = time.time() + 120
            final_state = None
            while time.time() < deadline:
                response = ec2.describe_instances(InstanceIds=[bastion_id])
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
                f"Bastion instance {bastion_id} did not reach a terminal state after "
                f"shutdown(); last state: {final_state}"
            )
        else:
            # CF-based bastion: verify the stack no longer has running instances
            response = ec2.describe_instances(
                Filters=[
                    {
                        "Name": "tag:aws:cloudformation:stack-name",
                        "Values": [bastion_id],
                    },
                    {
                        "Name": "instance-state-name",
                        "Values": ["pending", "running", "stopping", "stopped"],
                    },
                ]
            )
            running_instances = [
                inst
                for res in response.get("Reservations", [])
                for inst in res.get("Instances", [])
            ]
            assert running_instances == [], (
                f"Bastion CF stack '{bastion_id}' still has running instances after "
                f"shutdown(): {[i['InstanceId'] for i in running_instances]}"
            )

    def test_shutdown_removes_vpc(self, detached_provider, aws_session, aws_region):
        """After shutdown() the provider VPC no longer exists."""
        vpc_id = detached_provider.operating_mode.vpc_id
        assert vpc_id is not None

        detached_provider.shutdown()

        ec2 = aws_session.client("ec2", region_name=aws_region)
        try:
            response = ec2.describe_vpcs(VpcIds=[vpc_id])
            vpcs = response.get("Vpcs", [])
            assert vpcs == [], f"VPC {vpc_id} still exists after shutdown(): {vpcs}"
        except ClientError as exc:
            error_code = exc.response["Error"]["Code"]
            assert (
                error_code == "InvalidVpcID.NotFound"
            ), f"Unexpected ClientError checking VPC after shutdown(): {exc}"

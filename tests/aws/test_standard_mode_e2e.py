"""Real-AWS end-to-end tests for StandardMode full lifecycle.

These tests create actual AWS infrastructure (VPC, subnet, security group,
EC2 instances) and verify the full provider lifecycle:

    PENDING → RUNNING → COMPLETED

No Parsl interchange is required — the UserData script runs on real EC2 iron
and the provider reports COMPLETED only after the instance terminates.

Run with::

    AWS_PROFILE=aws pytest tests/aws/ -m "aws" --no-cov -v

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import ipaddress
import time
import logging

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
    """Poll provider.status() until the job reaches *target_status* or timeout.

    Parameters
    ----------
    provider:
        An initialised EphemeralAWSProvider instance.
    job_id:
        The job ID returned by provider.submit().
    target_status:
        The status string to wait for (e.g. "COMPLETED", "RUNNING").
    timeout:
        Maximum number of seconds to wait before returning False.

    Returns
    -------
    bool
        True if the target status was reached before the timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = provider.status([job_id])
        if result and result[0]["status"] == target_status:
            return True
        time.sleep(POLL_INTERVAL_S)
    return False


# ---------------------------------------------------------------------------
# TestStandardModeInfrastructure
# ---------------------------------------------------------------------------


@pytest.mark.aws
@pytest.mark.slow
class TestStandardModeInfrastructure:
    """Verify that the VPC/subnet/SG resources are created correctly.

    Uses a single provider instance for the whole class (class-scoped
    fixture) to avoid repeated infrastructure setup/teardown.
    """

    def test_vpc_created_and_available(self, aws_provider, aws_session, aws_region):
        """Provider creates a VPC and it reaches 'available' state."""
        vpc_id = aws_provider.operating_mode.vpc_id
        assert vpc_id is not None, "vpc_id should be set after initialize()"

        ec2 = aws_session.client("ec2", region_name=aws_region)
        response = ec2.describe_vpcs(VpcIds=[vpc_id])
        vpcs = response.get("Vpcs", [])
        assert len(vpcs) == 1, f"Expected exactly one VPC for {vpc_id}, got {vpcs}"
        assert (
            vpcs[0]["State"] == "available"
        ), f"VPC {vpc_id} is in state '{vpcs[0]['State']}', expected 'available'"

    def test_subnet_created_in_vpc(self, aws_provider, aws_session, aws_region):
        """Provider creates a subnet inside the provider VPC."""
        subnet_id = aws_provider.operating_mode.subnet_id
        vpc_id = aws_provider.operating_mode.vpc_id
        assert subnet_id is not None, "subnet_id should be set after initialize()"

        ec2 = aws_session.client("ec2", region_name=aws_region)
        response = ec2.describe_subnets(SubnetIds=[subnet_id])
        subnets = response.get("Subnets", [])
        assert (
            len(subnets) == 1
        ), f"Expected exactly one subnet for {subnet_id}, got {subnets}"
        assert subnets[0]["VpcId"] == vpc_id, (
            f"Subnet {subnet_id} belongs to VPC {subnets[0]['VpcId']}, "
            f"expected {vpc_id}"
        )

    def test_security_group_created_in_vpc(self, aws_provider, aws_session, aws_region):
        """Provider creates a security group inside the provider VPC."""
        sg_id = aws_provider.operating_mode.security_group_id
        vpc_id = aws_provider.operating_mode.vpc_id
        assert sg_id is not None, "security_group_id should be set after initialize()"

        ec2 = aws_session.client("ec2", region_name=aws_region)
        response = ec2.describe_security_groups(GroupIds=[sg_id])
        groups = response.get("SecurityGroups", [])
        assert len(groups) == 1, f"Expected exactly one SG for {sg_id}, got {groups}"
        assert groups[0]["VpcId"] == vpc_id, (
            f"Security group {sg_id} belongs to VPC {groups[0]['VpcId']}, "
            f"expected {vpc_id}"
        )

    def test_vpc_cidr_no_conflict(self, aws_provider, aws_session, aws_region):
        """The provider VPC CIDR does not overlap any other VPC in the account."""
        vpc_id = aws_provider.operating_mode.vpc_id
        assert vpc_id is not None

        ec2 = aws_session.client("ec2", region_name=aws_region)
        response = ec2.describe_vpcs()
        all_vpcs = response.get("Vpcs", [])

        # Find our VPC's CIDR
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
            assert not provider_net.overlaps(
                other_net
            ), f"Provider VPC CIDR {provider_cidr} overlaps existing VPC CIDR {cidr}"


# ---------------------------------------------------------------------------
# TestStandardModeComputeLifecycle
# ---------------------------------------------------------------------------


@pytest.mark.aws
@pytest.mark.slow
class TestStandardModeComputeLifecycle:
    """Verify EC2 instance launch, status transitions, and cancellation.

    Each test uses a *fresh* provider instance (function scope) to keep
    tests independent.
    """

    def test_submit_returns_job_id(self, aws_provider):
        """submit() returns a non-empty job ID that appears in job_map."""
        job_id = aws_provider.submit("echo hello-from-ec2", tasks_per_node=1)
        try:
            assert job_id, "job_id should be a non-empty string"
            assert (
                job_id in aws_provider.job_map
            ), f"job_id {job_id} not found in provider.job_map"
        finally:
            try:
                aws_provider.cancel([job_id])
            except Exception as exc:
                logger.warning("teardown cancel raised (ignored): %s", exc)

    def test_instance_tagged_correctly(self, aws_provider, aws_session, aws_region):
        """The EC2 instance launched for a job carries the expected tags."""
        job_id = aws_provider.submit("echo hello-from-ec2", tasks_per_node=1)
        try:
            resource_id = aws_provider.job_map[job_id]["resource_id"]

            ec2 = aws_session.client("ec2", region_name=aws_region)
            response = ec2.describe_instances(InstanceIds=[resource_id])
            instances = [
                inst
                for res in response.get("Reservations", [])
                for inst in res.get("Instances", [])
            ]
            assert instances, f"No instance found for {resource_id}"

            tags = {t["Key"]: t["Value"] for t in instances[0].get("Tags", [])}
            assert (
                tags.get("ProviderId") == aws_provider.provider_id
            ), f"Tag 'ProviderId' expected '{aws_provider.provider_id}', got '{tags.get('ProviderId')}'"
            assert (
                tags.get("JobId") == job_id
            ), f"Tag 'JobId' expected '{job_id}', got '{tags.get('JobId')}'"
        finally:
            try:
                aws_provider.cancel([job_id])
            except Exception as exc:
                logger.warning("teardown cancel raised (ignored): %s", exc)

    def test_status_running_after_submit(self, aws_provider):
        """After submit() returns the instance should already be RUNNING.

        submit() internally calls wait_for_resource("instance_running") before
        returning, so the instance is already running when submit() returns.
        """
        job_id = aws_provider.submit("echo hello-from-ec2", tasks_per_node=1)
        try:
            result = aws_provider.status([job_id])
            assert result, "status() returned an empty list"
            status = result[0]["status"]
            assert (
                status == "RUNNING"
            ), f"Expected status RUNNING immediately after submit, got {status}"
        finally:
            try:
                aws_provider.cancel([job_id])
            except Exception as exc:
                logger.warning("teardown cancel raised (ignored): %s", exc)

    def test_command_completes_after_shutdown(
        self, aws_provider, aws_session, aws_region
    ):
        """A short-lived command completes and the instance terminates.

        With auto_shutdown=True the UserData script ends with 'shutdown -h now'
        so the instance self-terminates once the command finishes.
        """
        job_id = aws_provider.submit("echo hello-from-ec2", tasks_per_node=1)
        resource_id = aws_provider.job_map[job_id]["resource_id"]

        reached = _poll_until(aws_provider, job_id, "COMPLETED")
        assert reached, f"Job {job_id} did not reach COMPLETED within {MAX_WAIT_S}s"

        # Verify the EC2 instance is in a terminal state
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

    def test_cancel_terminates_instance(self, aws_provider, aws_session, aws_region):
        """cancel() terminates the backing EC2 instance."""
        job_id = aws_provider.submit("sleep 300", tasks_per_node=1)
        resource_id = aws_provider.job_map[job_id]["resource_id"]

        # Confirm it is running first
        result = aws_provider.status([job_id])
        assert result[0]["status"] == "RUNNING", "Expected RUNNING before cancellation"

        aws_provider.cancel([job_id])

        # Poll the EC2 API directly (2 minutes max)
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
            f"Instance {resource_id} did not reach a terminal state after cancel(); "
            f"last observed state: {final_state}"
        )


# ---------------------------------------------------------------------------
# TestStandardModeCleanup
# ---------------------------------------------------------------------------


@pytest.mark.aws
@pytest.mark.slow
class TestStandardModeCleanup:
    """Verify that provider.shutdown() removes all created infrastructure."""

    def test_shutdown_removes_vpc(self, aws_provider, aws_session, aws_region):
        """After shutdown() the provider VPC no longer exists."""
        vpc_id = aws_provider.operating_mode.vpc_id
        assert vpc_id is not None

        aws_provider.shutdown()

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

    def test_shutdown_removes_subnet(self, aws_provider, aws_session, aws_region):
        """After shutdown() the provider subnet no longer exists."""
        subnet_id = aws_provider.operating_mode.subnet_id
        assert subnet_id is not None

        aws_provider.shutdown()

        ec2 = aws_session.client("ec2", region_name=aws_region)
        try:
            response = ec2.describe_subnets(SubnetIds=[subnet_id])
            subnets = response.get("Subnets", [])
            assert (
                subnets == []
            ), f"Subnet {subnet_id} still exists after shutdown(): {subnets}"
        except ClientError as exc:
            error_code = exc.response["Error"]["Code"]
            assert (
                error_code == "InvalidSubnetID.NotFound"
            ), f"Unexpected ClientError checking subnet after shutdown(): {exc}"

    def test_shutdown_removes_security_group(
        self, aws_provider, aws_session, aws_region
    ):
        """After shutdown() the provider security group no longer exists."""
        sg_id = aws_provider.operating_mode.security_group_id
        assert sg_id is not None

        aws_provider.shutdown()

        ec2 = aws_session.client("ec2", region_name=aws_region)
        try:
            response = ec2.describe_security_groups(GroupIds=[sg_id])
            groups = response.get("SecurityGroups", [])
            assert (
                groups == []
            ), f"Security group {sg_id} still exists after shutdown(): {groups}"
        except ClientError as exc:
            error_code = exc.response["Error"]["Code"]
            assert (
                error_code == "InvalidGroup.NotFound"
            ), f"Unexpected ClientError checking SG after shutdown(): {exc}"

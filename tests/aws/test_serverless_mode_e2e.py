"""Real-AWS end-to-end tests for ServerlessMode (Lambda / ECS).

These tests create actual AWS infrastructure and verify the full serverless
provider lifecycle:

    initialise → submit (Lambda) → PENDING/RUNNING → COMPLETED
    shutdown → Lambda functions / VPC removed

The default ``worker_type`` is ``auto``, which:
- Creates VPC/subnet/SG for ECS tasks.
- Initialises ``LambdaManager`` for Lambda jobs.
- Routes short single-task commands to Lambda; everything else to ECS.

Run with::

    AWS_PROFILE=aws pytest tests/aws/test_serverless_mode_e2e.py -m "aws" --no-cov -v

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
# TestServerlessModeInfrastructure
# ---------------------------------------------------------------------------


@pytest.mark.aws
@pytest.mark.slow
class TestServerlessModeInfrastructure:
    """Verify VPC/SG resources are created for the serverless (auto) mode.

    The auto worker_type creates network infrastructure for ECS tasks even when
    the first submitted job is dispatched to Lambda.
    """

    def test_vpc_created_for_ecs(self, serverless_provider, aws_session, aws_region):
        """Provider creates a VPC for ECS task networking in auto mode."""
        vpc_id = serverless_provider.operating_mode.vpc_id
        assert vpc_id is not None, (
            "vpc_id should be set after initialize() for serverless auto mode "
            "(required for ECS tasks)"
        )

        ec2 = aws_session.client("ec2", region_name=aws_region)
        response = ec2.describe_vpcs(VpcIds=[vpc_id])
        vpcs = response.get("Vpcs", [])
        assert len(vpcs) == 1, f"Expected exactly one VPC for {vpc_id}, got {vpcs}"
        assert (
            vpcs[0]["State"] == "available"
        ), f"VPC {vpc_id} is in state '{vpcs[0]['State']}', expected 'available'"

    def test_security_group_created(self, serverless_provider, aws_session, aws_region):
        """Provider creates a security group in the VPC."""
        sg_id = serverless_provider.operating_mode.security_group_id
        vpc_id = serverless_provider.operating_mode.vpc_id
        assert (
            sg_id is not None
        ), "security_group_id should be set after initialize() for serverless mode"
        assert vpc_id is not None, "vpc_id must be set if security_group_id is set"

        ec2 = aws_session.client("ec2", region_name=aws_region)
        response = ec2.describe_security_groups(GroupIds=[sg_id])
        groups = response.get("SecurityGroups", [])
        assert len(groups) == 1, f"Expected exactly one SG for {sg_id}"
        assert groups[0]["VpcId"] == vpc_id, (
            f"Security group {sg_id} belongs to VPC {groups[0]['VpcId']}, "
            f"expected {vpc_id}"
        )


# ---------------------------------------------------------------------------
# TestLambdaLifecycle
# ---------------------------------------------------------------------------


@pytest.mark.aws
@pytest.mark.slow
class TestLambdaLifecycle:
    """Verify Lambda function creation, status transitions, and cancellation.

    Each test submits a short command (``echo``) which the auto worker_type
    routes to Lambda.  The Lambda function is created as a CloudFormation stack
    named ``parsl-lambda-{job_id[:8]}``.
    """

    def test_submit_lambda_returns_job_id(self, serverless_provider):
        """submit() returns a non-empty job ID that appears in job_map."""
        job_id = serverless_provider.submit("echo hello-from-lambda", tasks_per_node=1)
        try:
            assert job_id, "job_id should be a non-empty string"
            assert (
                job_id in serverless_provider.job_map
            ), f"job_id {job_id} not found in provider.job_map"
        finally:
            try:
                serverless_provider.cancel([job_id])
            except Exception as exc:
                logger.warning("teardown cancel raised (ignored): %s", exc)

    def test_lambda_function_exists_after_submit(
        self, serverless_provider, aws_session, aws_region
    ):
        """A Lambda function named ``parsl-lambda-{job_id}`` exists after submit().

        The serverless mode deploys the function via CloudFormation; by the time
        submit() returns the CF stack (and therefore the function) should be
        present.
        """
        job_id = serverless_provider.submit("echo hello-from-lambda", tasks_per_node=1)
        try:
            expected_fn_name = f"parsl-lambda-{job_id}"
            lambda_client = aws_session.client("lambda", region_name=aws_region)

            # Allow up to 2 minutes for the CF stack to finish deploying the function
            deadline = time.time() + 120
            fn_found = False
            while time.time() < deadline:
                try:
                    lambda_client.get_function(FunctionName=expected_fn_name)
                    fn_found = True
                    break
                except ClientError as exc:
                    if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                        time.sleep(POLL_INTERVAL_S)
                    else:
                        raise

            assert fn_found, (
                f"Lambda function '{expected_fn_name}' was not found within 120s "
                f"after submitting job {job_id}"
            )
        finally:
            try:
                serverless_provider.cancel([job_id])
            except Exception as exc:
                logger.warning("teardown cancel raised (ignored): %s", exc)

    def test_lambda_status_transitions_to_completed(self, serverless_provider):
        """A short Lambda command transitions to COMPLETED within the timeout."""
        job_id = serverless_provider.submit("echo hello-from-lambda", tasks_per_node=1)

        reached = _poll_until(serverless_provider, job_id, "COMPLETED")
        assert reached, f"Job {job_id} did not reach COMPLETED within {MAX_WAIT_S}s"

    def test_lambda_cancel_removes_function(
        self, serverless_provider, aws_session, aws_region
    ):
        """cancel() removes the Lambda function (or its CloudFormation stack)."""
        job_id = serverless_provider.submit("sleep 300", tasks_per_node=1)
        expected_fn_name = f"parsl-lambda-{job_id}"

        try:
            serverless_provider.cancel([job_id])
        except Exception as exc:
            logger.warning("cancel raised (expected for long-running job): %s", exc)

        # Allow up to 3 minutes for CF stack to be deleted and function to disappear
        lambda_client = aws_session.client("lambda", region_name=aws_region)
        deadline = time.time() + 180
        fn_deleted = False
        while time.time() < deadline:
            try:
                lambda_client.get_function(FunctionName=expected_fn_name)
                time.sleep(POLL_INTERVAL_S)
            except ClientError as exc:
                if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                    fn_deleted = True
                    break
                raise

        assert (
            fn_deleted
        ), f"Lambda function '{expected_fn_name}' still exists 180s after cancel()"


# ---------------------------------------------------------------------------
# TestServerlessModeCleanup
# ---------------------------------------------------------------------------


@pytest.mark.aws
@pytest.mark.slow
class TestServerlessModeCleanup:
    """Verify that provider.shutdown() removes all created infrastructure."""

    def test_shutdown_removes_vpc(self, serverless_provider, aws_session, aws_region):
        """After shutdown() the provider VPC no longer exists."""
        vpc_id = serverless_provider.operating_mode.vpc_id
        # VPC may be None if worker_type is lambda-only; skip gracefully.
        if vpc_id is None:
            pytest.skip("No VPC was created for this serverless configuration")

        serverless_provider.shutdown()

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

    def test_shutdown_removes_lambda_functions(
        self, serverless_provider, aws_session, aws_region
    ):
        """After shutdown(), no Lambda functions tagged with the provider ID remain.

        Submits a short job then calls shutdown() (which cancels all pending/running
        jobs and cleans up infrastructure).
        """
        job_id = serverless_provider.submit("echo cleanup-test", tasks_per_node=1)
        provider_id = serverless_provider.provider_id

        serverless_provider.shutdown()

        # Verify no Lambda functions with this provider's tag remain
        lambda_client = aws_session.client("lambda", region_name=aws_region)
        paginator = lambda_client.get_paginator("list_functions")
        provider_functions = []
        for page in paginator.paginate():
            for fn in page.get("Functions", []):
                try:
                    tags_resp = lambda_client.list_tags(
                        Resource=fn.get("FunctionArn", "")
                    )
                    if tags_resp.get("Tags", {}).get("ProviderId") == provider_id:
                        provider_functions.append(fn["FunctionName"])
                except Exception:
                    pass

        assert provider_functions == [], (
            f"Lambda functions with ProviderId={provider_id} still exist after "
            f"shutdown(): {provider_functions}"
        )

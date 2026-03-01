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

from parsl_ephemeral_aws import GlobusComputeProvider
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


# ---------------------------------------------------------------------------
# Spot provider fixture (for #55 — spot instance E2E)
# ---------------------------------------------------------------------------


@pytest.fixture
def spot_provider(tmp_path, aws_session, test_run_id, aws_region):
    """Provider configured for spot instances without interruption handling.

    Uses ``use_spot=True`` with ``spot_interruption_handling=False`` to avoid
    requiring a checkpoint S3 bucket in most spot lifecycle tests.
    """
    state_file = str(tmp_path / f"state-{test_run_id}.json")

    provider = EphemeralAWSProvider(
        region=aws_region,
        instance_type="t3.micro",
        mode="standard",
        use_spot=True,
        spot_interruption_handling=False,
        state_store_type="file",
        state_file_path=state_file,
        auto_shutdown=True,
        auto_create_instance_profile=True,
        profile_name=AWS_TEST_PROFILE,
        additional_tags={
            "E2ETestRunId": test_run_id,
            "AutoCleanup": "true",
        },
        waiter_delay=15,
        waiter_max_attempts=40,
        debug=True,
    )

    provider.operating_mode.initialize()

    yield provider

    try:
        provider.shutdown()
    except Exception as exc:
        logger.warning(
            "spot_provider shutdown raised an exception (best-effort): %s", exc
        )


# ---------------------------------------------------------------------------
# Serverless provider fixture (for #61 — serverless / Lambda+ECS E2E)
# ---------------------------------------------------------------------------


@pytest.fixture
def serverless_provider(tmp_path, aws_session, test_run_id, aws_region):
    """Provider configured for serverless (auto worker_type) mode.

    The default worker_type is WORKER_TYPE_AUTO which creates VPC/subnet/SG
    resources for ECS *and* initialises LambdaManager for Lambda jobs.  Short
    single-task commands are dispatched to Lambda; everything else goes to ECS.
    """
    state_file = str(tmp_path / f"state-{test_run_id}.json")

    provider = EphemeralAWSProvider(
        region=aws_region,
        mode="serverless",
        state_store_type="file",
        state_file_path=state_file,
        auto_shutdown=True,
        auto_create_instance_profile=True,
        profile_name=AWS_TEST_PROFILE,
        additional_tags={
            "E2ETestRunId": test_run_id,
            "AutoCleanup": "true",
        },
        waiter_delay=15,
        waiter_max_attempts=40,
        debug=True,
    )

    provider.operating_mode.initialize()

    yield provider

    try:
        provider.shutdown()
    except Exception as exc:
        logger.warning(
            "serverless_provider shutdown raised an exception (best-effort): %s", exc
        )


@pytest.fixture(autouse=True)
def cleanup_stray_lambda_resources(aws_session, aws_region, test_run_id):
    """Autouse fixture: delete leaked Lambda functions / CF stacks after every test."""
    yield  # test runs here

    try:
        lambda_client = aws_session.client("lambda", region_name=aws_region)
        paginator = lambda_client.get_paginator("list_functions")
        for page in paginator.paginate():
            for fn in page.get("Functions", []):
                fn_arn = fn.get("FunctionArn", "")
                fn_name = fn.get("FunctionName", "")
                if not fn_name.startswith("parsl-lambda-"):
                    continue
                try:
                    tags_resp = lambda_client.list_tags(Resource=fn_arn)
                    if tags_resp.get("Tags", {}).get("E2ETestRunId") == test_run_id:
                        lambda_client.delete_function(FunctionName=fn_name)
                        logger.info(
                            "cleanup_stray_lambda_resources: deleted %s", fn_name
                        )
                except Exception:
                    pass
    except Exception as exc:
        logger.warning("cleanup_stray_lambda_resources: exception (ignored): %s", exc)


# ---------------------------------------------------------------------------
# Detached provider fixture (for #54 — detached / bastion E2E)
# ---------------------------------------------------------------------------


@pytest.fixture
def detached_provider(tmp_path, aws_session, test_run_id, aws_region):
    """Provider configured for detached (bastion host) mode."""
    state_file = str(tmp_path / f"state-{test_run_id}.json")

    provider = EphemeralAWSProvider(
        region=aws_region,
        instance_type="t3.micro",
        mode="detached",
        bastion_instance_type="t3.micro",
        state_store_type="file",
        state_file_path=state_file,
        auto_shutdown=True,
        auto_create_instance_profile=True,
        profile_name=AWS_TEST_PROFILE,
        additional_tags={
            "E2ETestRunId": test_run_id,
            "AutoCleanup": "true",
        },
        waiter_delay=15,
        waiter_max_attempts=40,
        debug=True,
    )

    provider.operating_mode.initialize()

    yield provider

    try:
        provider.shutdown()
    except Exception as exc:
        logger.warning(
            "detached_provider shutdown raised an exception (best-effort): %s", exc
        )


# ---------------------------------------------------------------------------
# State backend fixtures (for #57 — Parameter Store + S3 state E2E)
# ---------------------------------------------------------------------------


@pytest.fixture
def parameter_store_provider(aws_session, test_run_id, aws_region):
    """Provider using AWS Parameter Store as the state backend."""
    provider = EphemeralAWSProvider(
        region=aws_region,
        instance_type="t3.micro",
        mode="standard",
        state_store_type="parameter_store",
        parameter_store_path=f"/parsl/e2e-test/{test_run_id}",
        auto_shutdown=True,
        auto_create_instance_profile=True,
        profile_name=AWS_TEST_PROFILE,
        additional_tags={
            "E2ETestRunId": test_run_id,
            "AutoCleanup": "true",
        },
        waiter_delay=15,
        waiter_max_attempts=40,
        debug=True,
    )

    provider.operating_mode.initialize()

    yield provider

    try:
        provider.shutdown()
    except Exception as exc:
        logger.warning(
            "parameter_store_provider shutdown raised an exception (best-effort): %s",
            exc,
        )


@pytest.fixture
def s3_state_bucket(aws_session, test_run_id, aws_region):
    """Create a temporary S3 bucket for state backend tests; delete it after the test."""
    s3 = aws_session.client("s3", region_name=aws_region)
    bucket_name = f"parsl-e2e-state-{test_run_id}"

    if aws_region == "us-east-1":
        s3.create_bucket(Bucket=bucket_name)
    else:
        s3.create_bucket(
            Bucket=bucket_name,
            CreateBucketConfiguration={"LocationConstraint": aws_region},
        )

    yield bucket_name

    # Cleanup: delete all objects first, then the bucket
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket_name):
            objects = page.get("Contents", [])
            if objects:
                s3.delete_objects(
                    Bucket=bucket_name,
                    Delete={"Objects": [{"Key": o["Key"]} for o in objects]},
                )
        s3.delete_bucket(Bucket=bucket_name)
        logger.info("s3_state_bucket: deleted bucket %s", bucket_name)
    except Exception as exc:
        logger.warning("s3_state_bucket cleanup: exception (ignored): %s", exc)


@pytest.fixture
def s3_provider(aws_session, test_run_id, aws_region, s3_state_bucket):
    """Provider using S3 as the state backend."""
    provider = EphemeralAWSProvider(
        region=aws_region,
        instance_type="t3.micro",
        mode="standard",
        state_store_type="s3",
        s3_bucket=s3_state_bucket,
        auto_shutdown=True,
        auto_create_instance_profile=True,
        profile_name=AWS_TEST_PROFILE,
        additional_tags={
            "E2ETestRunId": test_run_id,
            "AutoCleanup": "true",
        },
        waiter_delay=15,
        waiter_max_attempts=40,
        debug=True,
    )

    provider.operating_mode.initialize()

    yield provider

    try:
        provider.shutdown()
    except Exception as exc:
        logger.warning(
            "s3_provider shutdown raised an exception (best-effort): %s", exc
        )


# ---------------------------------------------------------------------------
# Globus Compute provider fixture (for #58 — Globus Compute E2E)
# ---------------------------------------------------------------------------


@pytest.fixture
def globus_compute_provider(tmp_path, aws_session, test_run_id, aws_region):
    """Provider configured for Globus Compute endpoint config generation.

    Creates a ``GlobusComputeProvider`` backed by standard EC2 mode and yields
    it without calling ``operating_mode.initialize()`` — Globus Compute
    endpoint tests manage the lifecycle via the ``globus-compute-endpoint``
    CLI rather than calling the provider directly.

    The provider is shut down best-effort in teardown to release any EC2
    resources that may have been provisioned by lifecycle tests.
    """
    state_file = str(tmp_path / f"state-gc-{test_run_id}.json")

    provider = GlobusComputeProvider(
        region=aws_region,
        instance_type="t3.small",
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
        display_name=f"Parsl E2E Test {test_run_id}",
        waiter_delay=15,
        waiter_max_attempts=40,
        debug=True,
    )

    yield provider

    try:
        provider.shutdown()
    except Exception as exc:
        logger.warning(
            "globus_compute_provider shutdown raised an exception (best-effort): %s",
            exc,
        )


@pytest.fixture(autouse=True)
def cleanup_stray_ssm_parameters(aws_session, aws_region, test_run_id):
    """Autouse fixture: delete leaked SSM parameters after every test."""
    yield  # test runs here

    try:
        ssm = aws_session.client("ssm", region_name=aws_region)
        path = f"/parsl/e2e-test/{test_run_id}"
        paginator = ssm.get_paginator("get_parameters_by_path")
        params_to_delete = []
        for page in paginator.paginate(Path=path, Recursive=True):
            for param in page.get("Parameters", []):
                params_to_delete.append(param["Name"])

        for i in range(0, len(params_to_delete), 10):
            batch = params_to_delete[i : i + 10]
            if batch:
                ssm.delete_parameters(Names=batch)

        if params_to_delete:
            logger.info(
                "cleanup_stray_ssm_parameters: deleted %d parameters under %s",
                len(params_to_delete),
                path,
            )
    except Exception as exc:
        logger.warning("cleanup_stray_ssm_parameters: exception (ignored): %s", exc)

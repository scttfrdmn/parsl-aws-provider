"""Real-AWS end-to-end tests for Parameter Store and S3 state backends.

These tests verify that the provider correctly persists and restores state via
AWS Parameter Store (SSM) and S3, exercising the full state round-trip:

    initialise → state saved → re-create provider → state loaded → shutdown → state deleted

NOTE on known bugs
------------------
As of v0.3.0 the ``ParameterStoreStateStore`` and ``S3StateStore`` classes were
implemented with a different constructor signature than what ``EphemeralAWSProvider``
passes when creating them (``session``, ``path``/``bucket``, ``provider_id`` vs
the original ``provider`` object, ``prefix``/``bucket_name``).  These tests are
written to exercise the **intended** behaviour; they will fail at provider
construction time until those mismatches are resolved.  The failures serve as
regression-test markers for issue #57.

Run with::

    AWS_PROFILE=aws pytest tests/aws/test_state_backends_e2e.py -m "aws" --no-cov -v

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import logging
import time

import pytest

from parsl_ephemeral_aws.provider import EphemeralAWSProvider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

POLL_INTERVAL_S = 15
MAX_WAIT_S = 600  # 10 minutes

AWS_TEST_PROFILE = "aws"


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
# TestParameterStoreState
# ---------------------------------------------------------------------------


@pytest.mark.aws
@pytest.mark.slow
class TestParameterStoreState:
    """Verify state persistence via AWS Systems Manager Parameter Store.

    All tests use the ``parameter_store_provider`` fixture which configures
    ``state_store_type='parameter_store'`` with path ``/parsl/e2e-test/{test_run_id}``.
    """

    def test_state_written_to_parameter_store(
        self, parameter_store_provider, aws_session, aws_region, test_run_id
    ):
        """After initialize() the provider state is present in Parameter Store.

        Checks that at least one SSM parameter exists under
        ``/parsl/e2e-test/{test_run_id}/``.
        """
        ssm = aws_session.client("ssm", region_name=aws_region)
        path = f"/parsl/e2e-test/{test_run_id}"

        paginator = ssm.get_paginator("get_parameters_by_path")
        all_params = []
        for page in paginator.paginate(Path=path, Recursive=True):
            all_params.extend(page.get("Parameters", []))

        assert all_params, (
            f"No SSM parameters found under '{path}' after provider.initialize(). "
            "The ParameterStore state backend may not be writing state correctly."
        )

    def test_submit_appears_in_parameter_store(
        self, parameter_store_provider, aws_session, aws_region, test_run_id
    ):
        """After submit() the state in Parameter Store reflects the job.

        Polls SSM until a parameter value contains the job_id string.
        """
        job_id = parameter_store_provider.submit(
            "echo hello-from-ssm", tasks_per_node=1
        )
        try:
            ssm = aws_session.client("ssm", region_name=aws_region)
            path = f"/parsl/e2e-test/{test_run_id}"

            # Allow up to 60 s for the state write to propagate
            deadline = time.time() + 60
            found = False
            while time.time() < deadline:
                paginator = ssm.get_paginator("get_parameters_by_path")
                for page in paginator.paginate(
                    Path=path, Recursive=True, WithDecryption=True
                ):
                    for param in page.get("Parameters", []):
                        if job_id in param.get("Value", ""):
                            found = True
                            break
                if found:
                    break
                time.sleep(POLL_INTERVAL_S)

            assert found, (
                f"job_id '{job_id}' not found in any SSM parameter under '{path}' "
                "within 60s after submit()."
            )
        finally:
            try:
                parameter_store_provider.cancel([job_id])
            except Exception as exc:
                logger.warning("teardown cancel raised (ignored): %s", exc)

    def test_state_round_trips_through_parameter_store(
        self, aws_session, test_run_id, aws_region
    ):
        """State saved by one provider instance can be loaded by a new instance.

        Creates a first provider, submits a job, then re-creates a second provider
        pointing to the same Parameter Store path.  The second provider should load
        the existing state and have a non-empty job_map.
        """
        path = f"/parsl/e2e-test/{test_run_id}"

        # First provider: initialize and submit
        provider1 = EphemeralAWSProvider(
            region=aws_region,
            instance_type="t3.micro",
            mode="standard",
            state_store_type="parameter_store",
            parameter_store_path=path,
            auto_shutdown=False,  # keep resources so provider2 can inspect them
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
        provider1.operating_mode.initialize()
        job_id = provider1.submit("echo round-trip-test", tasks_per_node=1)

        # Give the state write a moment to propagate
        time.sleep(5)

        # Second provider: load state from the same path
        provider2 = EphemeralAWSProvider(
            region=aws_region,
            instance_type="t3.micro",
            mode="standard",
            state_store_type="parameter_store",
            parameter_store_path=path,
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
        try:
            provider2.operating_mode.initialize()  # should load saved state
            assert job_id in provider2.job_map or len(provider2.job_map) > 0, (
                "provider2.job_map should be populated from state loaded via "
                f"Parameter Store path '{path}'"
            )
        finally:
            try:
                provider1.shutdown()
            except Exception as exc:
                logger.warning("provider1 shutdown: %s (ignored)", exc)
            try:
                provider2.shutdown()
            except Exception as exc:
                logger.warning("provider2 shutdown: %s (ignored)", exc)

    def test_shutdown_removes_parameters(
        self, parameter_store_provider, aws_session, aws_region, test_run_id
    ):
        """After shutdown() the SSM parameters under the provider path are deleted."""
        path = f"/parsl/e2e-test/{test_run_id}"

        # Verify there is at least one parameter before shutdown
        ssm = aws_session.client("ssm", region_name=aws_region)
        pre_params = []
        paginator = ssm.get_paginator("get_parameters_by_path")
        for page in paginator.paginate(Path=path, Recursive=True):
            pre_params.extend(page.get("Parameters", []))
        assert pre_params, f"No parameters found under '{path}' before shutdown — state was never written."

        parameter_store_provider.shutdown()

        # All parameters should be gone
        post_params = []
        for page in paginator.paginate(Path=path, Recursive=True):
            post_params.extend(page.get("Parameters", []))
        assert post_params == [], (
            f"SSM parameters still present under '{path}' after shutdown(): "
            f"{[p['Name'] for p in post_params]}"
        )


# ---------------------------------------------------------------------------
# TestS3State
# ---------------------------------------------------------------------------


@pytest.mark.aws
@pytest.mark.slow
class TestS3State:
    """Verify state persistence via AWS S3.

    All tests use the ``s3_provider`` fixture which configures
    ``state_store_type='s3'`` with a temporary bucket created by the
    ``s3_state_bucket`` fixture.
    """

    def test_state_written_to_s3(
        self, s3_provider, aws_session, aws_region, s3_state_bucket
    ):
        """After initialize() the provider state is present as an S3 object.

        Checks that at least one object exists in the state bucket.
        """
        s3 = aws_session.client("s3", region_name=aws_region)
        response = s3.list_objects_v2(Bucket=s3_state_bucket)
        objects = response.get("Contents", [])
        assert objects, (
            f"No objects found in S3 bucket '{s3_state_bucket}' after "
            "provider.initialize(). The S3 state backend may not be writing state."
        )

    def test_submit_appears_in_s3(
        self, s3_provider, aws_session, aws_region, s3_state_bucket
    ):
        """After submit() the S3 state object contains the job_id string."""
        job_id = s3_provider.submit("echo hello-from-s3", tasks_per_node=1)
        try:
            s3 = aws_session.client("s3", region_name=aws_region)

            # Allow up to 60 s for the state write
            deadline = time.time() + 60
            found = False
            while time.time() < deadline:
                response = s3.list_objects_v2(Bucket=s3_state_bucket)
                for obj in response.get("Contents", []):
                    body = (
                        s3.get_object(Bucket=s3_state_bucket, Key=obj["Key"])["Body"]
                        .read()
                        .decode("utf-8", errors="replace")
                    )
                    if job_id in body:
                        found = True
                        break
                if found:
                    break
                time.sleep(POLL_INTERVAL_S)

            assert found, (
                f"job_id '{job_id}' not found in any S3 object in bucket "
                f"'{s3_state_bucket}' within 60s after submit()."
            )
        finally:
            try:
                s3_provider.cancel([job_id])
            except Exception as exc:
                logger.warning("teardown cancel raised (ignored): %s", exc)

    def test_state_round_trips_through_s3(
        self, aws_session, test_run_id, aws_region, s3_state_bucket
    ):
        """State saved by one provider can be loaded by a new provider from S3.

        Creates a first provider, submits a job, then re-creates a second provider
        pointing to the same bucket.  The second provider should load the existing
        state and have a non-empty job_map.
        """
        # First provider: initialize and submit
        provider1 = EphemeralAWSProvider(
            region=aws_region,
            instance_type="t3.micro",
            mode="standard",
            state_store_type="s3",
            s3_bucket=s3_state_bucket,
            auto_shutdown=False,  # keep resources for provider2
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
        provider1.operating_mode.initialize()
        job_id = provider1.submit("echo round-trip-s3", tasks_per_node=1)

        time.sleep(5)

        # Second provider: load state from the same S3 bucket
        provider2 = EphemeralAWSProvider(
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
        try:
            provider2.operating_mode.initialize()
            assert job_id in provider2.job_map or len(provider2.job_map) > 0, (
                "provider2.job_map should be populated from S3-stored state in "
                f"bucket '{s3_state_bucket}'"
            )
        finally:
            try:
                provider1.shutdown()
            except Exception as exc:
                logger.warning("provider1 shutdown: %s (ignored)", exc)
            try:
                provider2.shutdown()
            except Exception as exc:
                logger.warning("provider2 shutdown: %s (ignored)", exc)

    def test_shutdown_removes_s3_objects(
        self, s3_provider, aws_session, aws_region, s3_state_bucket
    ):
        """After shutdown() the S3 state objects created by the provider are deleted.

        Verifies that the bucket is empty (or has only pre-existing objects) after
        a clean shutdown.
        """
        s3 = aws_session.client("s3", region_name=aws_region)

        # Confirm there is something in the bucket before shutdown
        pre_response = s3.list_objects_v2(Bucket=s3_state_bucket)
        pre_objects = pre_response.get("Contents", [])
        assert pre_objects, (
            f"Bucket '{s3_state_bucket}' is empty before shutdown — "
            "state was never written."
        )

        s3_provider.shutdown()

        # After shutdown the provider-managed objects should be gone
        post_response = s3.list_objects_v2(Bucket=s3_state_bucket)
        post_objects = post_response.get("Contents", [])
        assert post_objects == [], (
            f"S3 objects still present in '{s3_state_bucket}' after shutdown(): "
            f"{[o['Key'] for o in post_objects]}"
        )

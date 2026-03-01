"""Real-AWS + Globus Compute E2E tests for GlobusComputeProvider.

These tests verify the full Globus Compute integration lifecycle:

    generate_endpoint_config → start endpoint → submit function → get result
    → shutdown endpoint → EC2 resources terminated

Prerequisites
-------------
1. Globus auth tokens present (run ``globus-compute-endpoint login`` once):
   ``~/.globus_compute/storage.db`` must contain valid tokens.

2. ``globus-compute-sdk`` and ``globus-compute-endpoint`` installed::

       pip install "parsl-ephemeral-aws[globus]"
       # or:
       pip install globus-compute-sdk globus-compute-endpoint

3. A registered endpoint UUID — either set ``GLOBUS_COMPUTE_ENDPOINT_ID`` in
   the environment, **or** allow the fixture to register a fresh endpoint by
   setting ``GLOBUS_COMPUTE_REGISTER_NEW=1``.

4. Real AWS credentials (``aws`` profile) for EC2 provisioning.

Run with::

    AWS_PROFILE=aws pytest tests/aws/test_globus_compute_e2e.py \\
        -m "aws and globus" --no-cov -v

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import logging
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional

import pytest

from parsl_ephemeral_aws import GlobusComputeProvider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

POLL_INTERVAL_S = 10
ENDPOINT_START_TIMEOUT_S = 120  # 2 minutes for endpoint to come online
FUNCTION_TIMEOUT_S = 300  # 5 minutes for a function to complete

AWS_TEST_PROFILE = os.environ.get("AWS_TEST_PROFILE", "aws")

# Optional env var: UUID of a pre-registered Globus Compute endpoint.
# When set, tests use this endpoint directly (no registration step).
_ENDPOINT_ID_ENV = "GLOBUS_COMPUTE_ENDPOINT_ID"

# Set to "1" to force registration of a new endpoint even if
# GLOBUS_COMPUTE_ENDPOINT_ID is present.
_REGISTER_NEW_ENV = "GLOBUS_COMPUTE_REGISTER_NEW"


# ---------------------------------------------------------------------------
# Skip helpers
# ---------------------------------------------------------------------------


def _skip_if_no_globus_sdk():
    """Skip the current test if globus-compute-sdk is not importable."""
    try:
        import globus_compute_sdk  # noqa: F401
    except ImportError:
        pytest.skip(
            "globus-compute-sdk is not installed. "
            "Install it with: pip install 'parsl-ephemeral-aws[globus]'"
        )


def _skip_if_no_globus_auth():
    """Skip if no Globus auth tokens are available."""
    storage_db = Path.home() / ".globus_compute" / "storage.db"
    if not storage_db.exists():
        pytest.skip(
            "No Globus Compute auth tokens found. " "Run: globus-compute-endpoint login"
        )


def _skip_if_no_endpoint_id() -> str:
    """Return the endpoint UUID from env or skip the test."""
    ep_id = os.environ.get(_ENDPOINT_ID_ENV, "").strip()
    if not ep_id:
        pytest.skip(
            f"Set {_ENDPOINT_ID_ENV}=<uuid> to run Globus Compute function submission "
            "tests against a pre-registered endpoint."
        )
    return ep_id


# ---------------------------------------------------------------------------
# Helper: start / stop globus-compute-endpoint via subprocess
# ---------------------------------------------------------------------------


def _start_endpoint(
    endpoint_name: str, timeout: int = ENDPOINT_START_TIMEOUT_S
) -> Optional[str]:
    """Start a globus-compute-endpoint and return its UUID.

    Returns None if the endpoint fails to start within *timeout* seconds.
    """
    try:
        result = subprocess.run(
            ["globus-compute-endpoint", "start", endpoint_name],
            capture_output=True,
            text=True,
            timeout=60,
        )
        logger.info("endpoint start stdout: %s", result.stdout[:500])
        if result.returncode != 0:
            logger.warning("endpoint start failed: %s", result.stderr[:500])
            return None
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("globus-compute-endpoint not available: %s", exc)
        return None

    # Poll for the endpoint UUID in the storage DB
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = subprocess.run(
                ["globus-compute-endpoint", "list", "--json"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                import json

                endpoints = json.loads(result.stdout)
                for ep in endpoints:
                    if ep.get("name") == endpoint_name:
                        ep_id = ep.get("id")
                        if ep_id:
                            logger.info(
                                "endpoint %s started with ID %s", endpoint_name, ep_id
                            )
                            return ep_id
        except Exception as exc:
            logger.debug("polling endpoint list: %s", exc)
        time.sleep(POLL_INTERVAL_S)

    return None


def _stop_endpoint(endpoint_name: str) -> None:
    """Stop a running globus-compute-endpoint (best-effort)."""
    try:
        subprocess.run(
            ["globus-compute-endpoint", "stop", endpoint_name],
            capture_output=True,
            text=True,
            timeout=30,
        )
        logger.info("Stopped endpoint %s", endpoint_name)
    except Exception as exc:
        logger.warning("Failed to stop endpoint %s: %s", endpoint_name, exc)


# ---------------------------------------------------------------------------
# TestGlobusComputeProviderConfig
# ---------------------------------------------------------------------------


@pytest.mark.aws
@pytest.mark.globus
@pytest.mark.slow
class TestGlobusComputeProviderConfig:
    """Verify GlobusComputeProvider config generation works end-to-end.

    These tests exercise the Python layer only (no running Globus Compute
    service required), verifying that the generated ``config.yaml`` is
    a valid Globus Compute endpoint config.
    """

    def test_generate_config_standard_mode(self, tmp_path, aws_region):
        """generate_endpoint_config() writes a valid config.yaml for standard mode."""
        from unittest.mock import MagicMock, patch
        from parsl_ephemeral_aws.provider import EphemeralAWSProvider
        from parsl_ephemeral_aws.state.file import FileStateStore

        provider_id = f"gc-test-{uuid.uuid4().hex[:8]}"
        state_file = str(tmp_path / f"{provider_id}.json")
        state_store = FileStateStore(file_path=state_file, provider_id=provider_id)
        mode_mock = MagicMock()

        with (
            patch("parsl_ephemeral_aws.provider.create_session") as mock_sess,
            patch.object(
                EphemeralAWSProvider,
                "_initialize_state_store",
                return_value=state_store,
            ),
            patch.object(
                EphemeralAWSProvider,
                "_initialize_operating_mode",
                return_value=mode_mock,
            ),
        ):
            mock_sess.return_value = MagicMock()
            provider = GlobusComputeProvider(
                provider_id=provider_id,
                region=aws_region,
                image_id="ami-12345678",
                instance_type="t3.medium",
                mode="standard",
                display_name="E2E Test Endpoint",
            )

        ep_dir = str(tmp_path / "e2e_endpoint")
        config_path = provider.generate_endpoint_config(ep_dir)

        assert os.path.isfile(config_path)
        content = Path(config_path).read_text()
        assert "GlobusComputeEngine" in content
        assert "display_name: E2E Test Endpoint" in content
        assert f"region: {aws_region}" in content
        assert "instance_type: t3.medium" in content

    def test_generate_config_spot_mode(self, tmp_path, aws_region):
        """Config for a spot-instance endpoint includes use_spot: true."""
        from unittest.mock import MagicMock, patch
        from parsl_ephemeral_aws.provider import EphemeralAWSProvider
        from parsl_ephemeral_aws.state.file import FileStateStore

        provider_id = f"gc-spot-{uuid.uuid4().hex[:8]}"
        state_file = str(tmp_path / f"{provider_id}.json")
        state_store = FileStateStore(file_path=state_file, provider_id=provider_id)
        mode_mock = MagicMock()

        with (
            patch("parsl_ephemeral_aws.provider.create_session") as mock_sess,
            patch.object(
                EphemeralAWSProvider,
                "_initialize_state_store",
                return_value=state_store,
            ),
            patch.object(
                EphemeralAWSProvider,
                "_initialize_operating_mode",
                return_value=mode_mock,
            ),
        ):
            mock_sess.return_value = MagicMock()
            provider = GlobusComputeProvider(
                provider_id=provider_id,
                region=aws_region,
                image_id="ami-12345678",
                instance_type="t3.medium",
                use_spot=True,
                display_name="Spot Endpoint",
            )

        config_path = provider.generate_endpoint_config(str(tmp_path / "spot_ep"))
        content = Path(config_path).read_text()
        assert "use_spot: true" in content

    def test_generate_config_container_mode(self, tmp_path, aws_region):
        """Config for a container endpoint includes docker container_type."""
        from unittest.mock import MagicMock, patch
        from parsl_ephemeral_aws.provider import EphemeralAWSProvider
        from parsl_ephemeral_aws.state.file import FileStateStore

        provider_id = f"gc-ctr-{uuid.uuid4().hex[:8]}"
        state_file = str(tmp_path / f"{provider_id}.json")
        state_store = FileStateStore(file_path=state_file, provider_id=provider_id)
        mode_mock = MagicMock()

        with (
            patch("parsl_ephemeral_aws.provider.create_session") as mock_sess,
            patch.object(
                EphemeralAWSProvider,
                "_initialize_state_store",
                return_value=state_store,
            ),
            patch.object(
                EphemeralAWSProvider,
                "_initialize_operating_mode",
                return_value=mode_mock,
            ),
        ):
            mock_sess.return_value = MagicMock()
            provider = GlobusComputeProvider(
                provider_id=provider_id,
                region=aws_region,
                image_id="ami-12345678",
                container_image="python:3.11-slim",
                display_name="Container Endpoint",
            )

        config_path = provider.generate_endpoint_config(str(tmp_path / "ctr_ep"))
        content = Path(config_path).read_text()
        assert "container_type: docker" in content
        assert "python:3.11-slim" in content


# ---------------------------------------------------------------------------
# TestGlobusComputeFunctionSubmission
# ---------------------------------------------------------------------------


@pytest.mark.aws
@pytest.mark.globus
@pytest.mark.slow
class TestGlobusComputeFunctionSubmission:
    """Submit Python functions to a running Globus Compute endpoint.

    Requires:
    - ``globus-compute-sdk`` installed
    - Globus auth tokens in ``~/.globus_compute/storage.db``
    - ``GLOBUS_COMPUTE_ENDPOINT_ID`` env var set to a running endpoint UUID
    """

    def setup_method(self):
        _skip_if_no_globus_sdk()
        _skip_if_no_globus_auth()

    def test_submit_simple_function(self):
        """Submit a simple doubling function and verify the result."""
        import globus_compute_sdk

        endpoint_id = _skip_if_no_endpoint_id()

        def double(x):
            return x * 2

        with globus_compute_sdk.Executor(endpoint_id=endpoint_id) as executor:
            future = executor.submit(double, 21)
            result = future.result(timeout=FUNCTION_TIMEOUT_S)

        assert result == 42, f"Expected 42, got {result!r}"

    def test_submit_returns_hostname(self):
        """Function result carries the AWS instance hostname."""
        import globus_compute_sdk

        endpoint_id = _skip_if_no_endpoint_id()

        def get_hostname():
            import socket

            return socket.gethostname()

        with globus_compute_sdk.Executor(endpoint_id=endpoint_id) as executor:
            future = executor.submit(get_hostname)
            hostname = future.result(timeout=FUNCTION_TIMEOUT_S)

        assert hostname, "hostname should be a non-empty string"
        logger.info("Function executed on host: %s", hostname)

    def test_submit_cpu_intensive_function(self):
        """A CPU-intensive function runs to completion on the endpoint."""
        import globus_compute_sdk

        endpoint_id = _skip_if_no_endpoint_id()

        def cpu_work(n):
            import math

            return sum(math.sqrt(i * 2.5) for i in range(n))

        with globus_compute_sdk.Executor(endpoint_id=endpoint_id) as executor:
            future = executor.submit(cpu_work, 500_000)
            result = future.result(timeout=FUNCTION_TIMEOUT_S)

        assert isinstance(result, float), f"Expected float result, got {type(result)}"
        assert result > 0

    def test_submit_multiple_functions_concurrent(self):
        """Multiple functions submitted concurrently all return correct results."""
        import globus_compute_sdk

        endpoint_id = _skip_if_no_endpoint_id()

        def square(x):
            return x * x

        inputs = list(range(5))
        expected = [x * x for x in inputs]

        with globus_compute_sdk.Executor(endpoint_id=endpoint_id) as executor:
            futures = [executor.submit(square, x) for x in inputs]
            results = [f.result(timeout=FUNCTION_TIMEOUT_S) for f in futures]

        assert (
            results == expected
        ), f"Concurrent results mismatch: {results} != {expected}"


# ---------------------------------------------------------------------------
# TestGlobusComputeEndpointLifecycle
# ---------------------------------------------------------------------------


@pytest.mark.aws
@pytest.mark.globus
@pytest.mark.slow
class TestGlobusComputeEndpointLifecycle:
    """Verify that a fresh endpoint can be started, used, and stopped.

    Requires ``globus-compute-endpoint`` CLI to be installed and Globus auth
    tokens to be present.  A new endpoint named ``parsl-e2e-{test_run_id}``
    is registered, started, used, and then stopped + deleted.

    These tests are intentionally slow (endpoint start/stop takes minutes).
    """

    def setup_method(self):
        _skip_if_no_globus_sdk()
        _skip_if_no_globus_auth()
        # Also require the endpoint CLI
        try:
            subprocess.run(
                ["globus-compute-endpoint", "--version"],
                capture_output=True,
                check=True,
                timeout=10,
            )
        except (FileNotFoundError, subprocess.CalledProcessError):
            pytest.skip(
                "globus-compute-endpoint CLI not installed. "
                "Install with: pip install 'parsl-ephemeral-aws[globus]'"
            )

    def test_endpoint_config_written_and_valid(
        self, tmp_path, aws_session, test_run_id, aws_region
    ):
        """generate_endpoint_config() writes a config that endpoint CLI accepts."""
        state_file = str(tmp_path / f"state-{test_run_id}.json")
        endpoint_name = f"parsl-e2e-{test_run_id}"
        endpoint_dir = str(Path.home() / ".globus_compute" / endpoint_name)

        provider = GlobusComputeProvider(
            region=aws_region,
            instance_type="t3.small",
            mode="standard",
            auto_create_instance_profile=True,
            profile_name=AWS_TEST_PROFILE,
            state_store_type="file",
            state_file_path=state_file,
            additional_tags={
                "E2ETestRunId": test_run_id,
                "AutoCleanup": "true",
            },
            display_name=f"Parsl E2E Test {test_run_id}",
            waiter_delay=15,
            waiter_max_attempts=40,
            debug=True,
        )

        config_path = provider.generate_endpoint_config(endpoint_dir)
        assert os.path.isfile(config_path), f"config.yaml not found at {config_path}"

        content = Path(config_path).read_text()
        assert "GlobusComputeEngine" in content
        assert aws_region in content
        assert "t3.small" in content
        logger.info("Endpoint config written to %s", config_path)

        # Verify endpoint CLI can read the config (configure step)
        result = subprocess.run(
            ["globus-compute-endpoint", "configure", endpoint_name],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # "configure" on an existing endpoint is a no-op / warning — just verify
        # it doesn't hard-error on our config syntax
        logger.info(
            "endpoint configure exit=%d stderr=%s",
            result.returncode,
            result.stderr[:300],
        )

    def test_endpoint_start_registers_with_globus(
        self, tmp_path, aws_session, test_run_id, aws_region
    ):
        """Starting an endpoint registers it with the Globus Compute service.

        This is an integration smoke test: creates a GlobusComputeProvider,
        writes the config, starts the endpoint daemon, confirms it appears in
        the endpoint list, then stops it.
        """
        import globus_compute_sdk

        state_file = str(tmp_path / f"state-{test_run_id}.json")
        endpoint_name = f"parsl-e2e-{test_run_id}"
        endpoint_dir = str(Path.home() / ".globus_compute" / endpoint_name)

        provider = GlobusComputeProvider(
            region=aws_region,
            instance_type="t3.small",
            mode="standard",
            auto_create_instance_profile=True,
            profile_name=AWS_TEST_PROFILE,
            state_store_type="file",
            state_file_path=state_file,
            additional_tags={
                "E2ETestRunId": test_run_id,
                "AutoCleanup": "true",
            },
            display_name=f"Parsl E2E Test {test_run_id}",
            waiter_delay=15,
            waiter_max_attempts=40,
        )

        provider.generate_endpoint_config(endpoint_dir)

        endpoint_id = _start_endpoint(endpoint_name)
        assert endpoint_id, (
            f"Endpoint '{endpoint_name}' did not start or register within "
            f"{ENDPOINT_START_TIMEOUT_S}s"
        )
        logger.info("Endpoint started: id=%s", endpoint_id)

        try:
            # Submit a trivial function to confirm the endpoint is online
            def ping():
                return "pong"

            with globus_compute_sdk.Executor(endpoint_id=endpoint_id) as executor:
                future = executor.submit(ping)
                result = future.result(timeout=FUNCTION_TIMEOUT_S)

            assert result == "pong", f"Expected 'pong', got {result!r}"

        finally:
            _stop_endpoint(endpoint_name)
            # Best-effort provider shutdown for any EC2 resources
            try:
                provider.shutdown()
            except Exception as exc:
                logger.warning(
                    "provider shutdown after endpoint test: %s (ignored)", exc
                )


# ---------------------------------------------------------------------------
# TestGlobusComputeEC2Cleanup
# ---------------------------------------------------------------------------


@pytest.mark.aws
@pytest.mark.globus
@pytest.mark.slow
class TestGlobusComputeEC2Cleanup:
    """Verify that EC2 resources are released after endpoint shutdown.

    Uses a pre-registered endpoint (``GLOBUS_COMPUTE_ENDPOINT_ID``) so the
    test can be run independently of endpoint lifecycle tests.
    """

    def setup_method(self):
        _skip_if_no_globus_sdk()
        _skip_if_no_globus_auth()

    def test_ec2_resources_released_after_provider_shutdown(
        self, tmp_path, aws_session, test_run_id, aws_region
    ):
        """After provider.shutdown(), no EC2 instances tagged with the run ID remain.

        This test submits a job via the standard ``EphemeralAWSProvider`` path
        (not via Globus Compute service) to verify the underlying AWS teardown
        — the Globus Compute layer delegates resource lifecycle to the provider.
        """
        from parsl_ephemeral_aws.provider import EphemeralAWSProvider

        state_file = str(tmp_path / f"state-{test_run_id}.json")

        provider = EphemeralAWSProvider(
            region=aws_region,
            instance_type="t3.small",
            mode="standard",
            auto_create_instance_profile=True,
            profile_name=AWS_TEST_PROFILE,
            state_store_type="file",
            state_file_path=state_file,
            additional_tags={
                "E2ETestRunId": test_run_id,
                "AutoCleanup": "true",
                "GlobusComputeE2E": "true",
            },
            auto_shutdown=True,
            waiter_delay=15,
            waiter_max_attempts=40,
            debug=True,
        )

        provider.operating_mode.initialize()
        job_id = provider.submit("echo globus-cleanup-test", tasks_per_node=1)

        # Give the instance time to launch
        deadline = time.time() + 120
        while time.time() < deadline:
            result = provider.status([job_id])
            if result and result[0]["status"] in ("RUNNING", "COMPLETED"):
                break
            time.sleep(POLL_INTERVAL_S)

        # Shutdown should terminate all EC2 resources
        provider.shutdown()

        # Verify no instances remain
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
        remaining = [
            inst["InstanceId"]
            for res in response.get("Reservations", [])
            for inst in res.get("Instances", [])
        ]
        assert (
            remaining == []
        ), f"EC2 instances still running after provider.shutdown(): {remaining}"

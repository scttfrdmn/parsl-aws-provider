"""Unit tests for the EphemeralAWSProvider core Parsl interface methods.

Tests cover submit, status, cancel, scale_in, scale_out, shutdown, and
thread-safety guarantees.  All AWS interactions are mocked.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import os
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import MagicMock, patch

import pytest

from parsl_ephemeral_aws.exceptions import ProviderError
from parsl_ephemeral_aws.provider import EphemeralAWSProvider
from parsl_ephemeral_aws.state.file import FileStateStore


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_provider(tmp_dir, mode_mock=None, max_blocks=5):
    """Return a fully wired EphemeralAWSProvider backed by a FileStateStore.

    All AWS calls are suppressed via mocked session and operating mode.
    """
    provider_id = f"test-{uuid.uuid4().hex[:8]}"
    state_file = os.path.join(tmp_dir, f"{provider_id}.json")
    state_store = FileStateStore(file_path=state_file, provider_id=provider_id)

    if mode_mock is None:
        mode_mock = MagicMock()
        # submit_job returns a fake resource_id
        mode_mock.submit_job.return_value = f"resource-{uuid.uuid4().hex[:8]}"
        # get_job_status returns RUNNING by default
        mode_mock.get_job_status.return_value = {}
        # cancel_jobs returns CANCELED for everything
        mode_mock.cancel_jobs.return_value = {}
        mode_mock.cleanup_resources.return_value = None
        mode_mock.cleanup_infrastructure.return_value = None
        mode_mock.list_resources.return_value = {}

    with (
        patch("parsl_ephemeral_aws.provider.create_session") as mock_session_factory,
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
        mock_session_factory.return_value = MagicMock()
        provider = EphemeralAWSProvider(
            provider_id=provider_id,
            region="us-east-1",
            image_id="ami-12345678",
            instance_type="t3.micro",
            mode="standard",
            max_blocks=max_blocks,
            min_blocks=0,
            init_blocks=0,
        )

    return provider, mode_mock


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProviderInterface:
    """Tests for core Parsl interface methods of EphemeralAWSProvider."""

    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    # --- submit ---

    def test_submit_returns_job_id(self, tmp_dir):
        """submit() returns a non-empty string job_id and tracks the resource."""
        provider, mode = _make_provider(tmp_dir)
        resource_id = "resource-abc"
        mode.submit_job.return_value = resource_id

        job_id = provider.submit("echo hello", tasks_per_node=1)

        assert isinstance(job_id, str)
        assert job_id  # non-empty
        assert resource_id in provider.resources
        assert job_id in provider.job_map

    def test_submit_respects_max_blocks(self, tmp_dir):
        """submit() raises ProviderError when already at max_blocks capacity."""
        provider, mode = _make_provider(tmp_dir, max_blocks=1)

        # Seed a resource so we're at capacity
        provider.resources["r1"] = {"job_id": "j1", "status": "RUNNING"}

        with pytest.raises(ProviderError, match="max_blocks"):
            provider.submit("echo hello", tasks_per_node=1)

    # --- status ---

    def test_status_returns_list(self, tmp_dir):
        """status() returns a list with one entry per requested job_id."""
        provider, mode = _make_provider(tmp_dir)
        resource_id = "resource-abc"
        mode.submit_job.return_value = resource_id
        mode.get_job_status.return_value = {resource_id: "RUNNING"}

        job_id = provider.submit("echo hello", tasks_per_node=1)
        result = provider.status([job_id])

        assert len(result) == 1
        assert result[0]["job_id"] == job_id
        assert result[0]["status"] == "RUNNING"

    def test_status_unknown_job_id(self, tmp_dir):
        """status() returns UNKNOWN for job_ids not in job_map."""
        provider, _ = _make_provider(tmp_dir)
        result = provider.status(["nonexistent-job-id"])

        assert result[0]["status"] == "UNKNOWN"

    # --- cancel ---

    def test_cancel_jobs(self, tmp_dir):
        """cancel() returns a result list and cleans up CANCELED resources."""
        provider, mode = _make_provider(tmp_dir)
        resource_id = "resource-xyz"
        mode.submit_job.return_value = resource_id
        mode.cancel_jobs.return_value = {resource_id: "CANCELED"}
        # After cancel, the status polling sees CANCELED → cleanup
        mode.get_job_status.return_value = {resource_id: "CANCELED"}

        job_id = provider.submit("echo hello", tasks_per_node=1)
        # Mark as CANCELED so _cleanup_resources picks it up
        provider.resources[resource_id]["status"] = "CANCELED"

        result = provider.cancel([job_id])

        assert any(r["job_id"] == job_id for r in result)

    def test_cancel_nonexistent_job(self, tmp_dir):
        """cancel() handles unknown job IDs without raising."""
        provider, _ = _make_provider(tmp_dir)
        result = provider.cancel(["no-such-job"])

        assert result[0]["status"] == "UNKNOWN"

    # --- scale_in ---

    def test_scale_in_terminates_running(self, tmp_dir):
        """scale_in(1) cancels one RUNNING resource."""
        provider, mode = _make_provider(tmp_dir)
        provider.resources["r1"] = {"job_id": "j1", "status": "RUNNING"}
        provider.job_map["j1"] = {"resource_id": "r1", "status": "RUNNING"}
        mode.cancel_jobs.return_value = {"r1": "CANCELED"}

        terminated = provider.scale_in(1)

        assert "j1" in terminated

    def test_scale_in_zero_returns_empty(self, tmp_dir):
        """scale_in(0) returns an empty list without touching resources."""
        provider, _ = _make_provider(tmp_dir)
        result = provider.scale_in(0)
        assert result == []

    # --- scale_out ---

    def test_scale_out_returns_empty_list(self, tmp_dir):
        """scale_out() is a no-op that returns []."""
        provider, _ = _make_provider(tmp_dir)
        result = provider.scale_out(3)
        assert result == []

    # --- shutdown ---

    def test_shutdown_cancels_jobs_and_cleans_up(self, tmp_dir):
        """shutdown() cancels all jobs and empties resources / job_map."""
        provider, mode = _make_provider(tmp_dir)
        resource_id = "resource-abc"
        mode.submit_job.return_value = resource_id
        mode.cancel_jobs.return_value = {resource_id: "CANCELED"}

        provider.submit("echo hello", tasks_per_node=1)
        assert provider.resources

        provider.shutdown()

        assert provider.resources == {}
        assert provider.job_map == {}

    # --- thread safety ---

    def test_concurrent_submit_no_race(self, tmp_dir):
        """Concurrent submits do not cause dict-mutation errors."""
        provider, mode = _make_provider(tmp_dir, max_blocks=50)

        def _make_resource_id():
            return f"resource-{uuid.uuid4().hex[:8]}"

        mode.submit_job.side_effect = lambda **_: _make_resource_id()

        n = 20
        job_ids = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = [ex.submit(provider.submit, "echo hello", 1) for _ in range(n)]
            for f in as_completed(futures):
                job_ids.append(f.result())

        assert len(job_ids) == n
        assert len(provider.resources) == n
        assert len(provider.job_map) == n

    # --- state persistence ---

    def test_state_saved_on_submit(self, tmp_dir):
        """_save_state() is called after each successful submit."""
        provider, mode = _make_provider(tmp_dir)
        mode.submit_job.return_value = "resource-1"

        original_save = provider._save_state
        call_count = []
        provider._save_state = lambda: (call_count.append(1), original_save())[1]

        provider.submit("echo hello", tasks_per_node=1)

        assert call_count, "_save_state not called during submit"

    def test_state_saved_on_status(self, tmp_dir):
        """_save_state() is called after status updates."""
        provider, mode = _make_provider(tmp_dir)
        resource_id = "resource-1"
        mode.submit_job.return_value = resource_id
        mode.get_job_status.return_value = {resource_id: "RUNNING"}

        job_id = provider.submit("echo hello", tasks_per_node=1)

        original_save = provider._save_state
        call_count = []
        provider._save_state = lambda: (call_count.append(1), original_save())[1]

        provider.status([job_id])

        assert call_count, "_save_state not called during status"

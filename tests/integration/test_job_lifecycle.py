"""Integration tests for full job lifecycle and state recovery.

These tests exercise the complete submit → status → cancel → shutdown cycle
of EphemeralAWSProvider with a real FileStateStore (no LocalStack required).
The operating mode is mocked to avoid EC2/Lambda API calls, letting us focus
on the provider's state management and concurrency guarantees.

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
from parsl_ephemeral_aws.utils.localstack import is_localstack_available


# Skip the entire module only if LocalStack is unavailable AND a test
# explicitly needs it.  The helper below lets individual tests opt-in.
_LOCALSTACK_AVAILABLE = is_localstack_available()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider_with_file_state(tmp_dir, provider_id=None, max_blocks=10):
    """Create an EphemeralAWSProvider backed by a FileStateStore.

    The operating mode is replaced with a MagicMock after construction so that
    no real AWS calls are made.  Returns (provider, mock_mode, state_store).
    """
    if provider_id is None:
        provider_id = f"test-{uuid.uuid4().hex[:8]}"

    state_file = os.path.join(tmp_dir, f"{provider_id}.json")
    state_store = FileStateStore(file_path=state_file, provider_id=provider_id)

    mode_mock = MagicMock()
    mode_mock.submit_job.side_effect = lambda **_: f"resource-{uuid.uuid4().hex[:8]}"
    mode_mock.get_job_status.return_value = {}
    mode_mock.cancel_jobs.return_value = {}
    mode_mock.cleanup_resources.return_value = None
    mode_mock.cleanup_infrastructure.return_value = None
    mode_mock.list_resources.return_value = {}

    with (
        patch("parsl_ephemeral_aws.provider.create_session") as mock_sf,
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
        mock_sf.return_value = MagicMock()
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

    return provider, mode_mock, state_store


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestJobLifecycle:
    """Integration tests for the full job lifecycle."""

    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    # ------------------------------------------------------------------
    # 1. Full lifecycle: submit → status → cleanup
    # ------------------------------------------------------------------

    @pytest.mark.localstack
    def test_full_lifecycle_standard_mode_file_state(self, tmp_dir):
        """Submit a job, poll status until COMPLETED, verify cleanup."""
        provider, mode, state_store = _make_provider_with_file_state(tmp_dir)
        resource_id = "resource-001"
        # Clear side_effect set by helper so return_value takes precedence
        mode.submit_job.side_effect = None
        mode.submit_job.return_value = resource_id

        # Submit
        job_id = provider.submit("echo hello", tasks_per_node=1)
        assert job_id in provider.job_map
        assert resource_id in provider.resources

        # Simulate job completing
        provider.resources[resource_id]["status"] = "COMPLETED"
        mode.get_job_status.return_value = {resource_id: "COMPLETED"}

        # Poll status
        statuses = provider.status([job_id])
        assert statuses[0]["status"] == "COMPLETED"

        # Trigger cleanup
        provider._cleanup_resources()

        # After cleanup of a COMPLETED resource, it should be removed
        assert resource_id not in provider.resources
        assert job_id not in provider.job_map

    # ------------------------------------------------------------------
    # 2. State recovery across provider restart
    # ------------------------------------------------------------------

    @pytest.mark.localstack
    def test_provider_restart_recovers_state(self, tmp_dir):
        """State persisted by one provider instance is loaded by a new one."""
        provider_id = f"test-{uuid.uuid4().hex[:8]}"
        state_file = os.path.join(tmp_dir, f"{provider_id}.json")
        state_store_1 = FileStateStore(file_path=state_file, provider_id=provider_id)

        mode_mock = MagicMock()
        resource_id = "resource-restart"
        mode_mock.submit_job.return_value = resource_id
        mode_mock.get_job_status.return_value = {}
        mode_mock.cancel_jobs.return_value = {}
        mode_mock.cleanup_resources.return_value = None
        mode_mock.cleanup_infrastructure.return_value = None

        with (
            patch("parsl_ephemeral_aws.provider.create_session") as mock_sf,
            patch.object(
                EphemeralAWSProvider,
                "_initialize_state_store",
                return_value=state_store_1,
            ),
            patch.object(
                EphemeralAWSProvider,
                "_initialize_operating_mode",
                return_value=mode_mock,
            ),
        ):
            mock_sf.return_value = MagicMock()
            provider1 = EphemeralAWSProvider(
                provider_id=provider_id,
                region="us-east-1",
                image_id="ami-12345678",
                instance_type="t3.micro",
                mode="standard",
                max_blocks=5,
            )

        job_id = provider1.submit("echo hello", tasks_per_node=1)
        assert job_id in provider1.job_map

        # State is persisted by _save_state after submit; load it in a new instance
        state_store_2 = FileStateStore(file_path=state_file, provider_id=provider_id)
        state = state_store_2.load_state()

        assert state is not None
        assert job_id in state.get("job_map", {})
        assert resource_id in state.get("resources", {})

    # ------------------------------------------------------------------
    # 3. No orphaned entries on submit failure
    # ------------------------------------------------------------------

    @pytest.mark.localstack
    def test_cleanup_on_submit_failure(self, tmp_dir):
        """If operating-mode submit_job() raises, no entry is left in resources."""
        provider, mode, _ = _make_provider_with_file_state(tmp_dir)
        mode.submit_job.side_effect = RuntimeError("EC2 launch failed")

        with pytest.raises(ProviderError):
            provider.submit("echo hello", tasks_per_node=1)

        # No orphaned resource tracking
        assert provider.resources == {}
        assert provider.job_map == {}

    # ------------------------------------------------------------------
    # 4. Concurrent submits — all tracked
    # ------------------------------------------------------------------

    @pytest.mark.localstack
    def test_concurrent_submits_all_tracked(self, tmp_dir):
        """5 concurrent submits via ThreadPoolExecutor all appear in resources."""
        provider, mode, _ = _make_provider_with_file_state(tmp_dir, max_blocks=20)
        mode.submit_job.side_effect = lambda **_: f"resource-{uuid.uuid4().hex[:8]}"

        n = 5
        job_ids = []
        with ThreadPoolExecutor(max_workers=n) as ex:
            futures = [ex.submit(provider.submit, f"echo {i}", 1) for i in range(n)]
            for f in as_completed(futures):
                job_ids.append(f.result())

        assert len(job_ids) == n
        assert len(provider.resources) == n
        assert len(provider.job_map) == n
        # All returned job_ids must be in the map
        for jid in job_ids:
            assert jid in provider.job_map

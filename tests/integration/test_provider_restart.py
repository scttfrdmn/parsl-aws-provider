"""Integration tests for provider restart and state recovery (closes #45).

Exercises the full save → reload cycle: a provider submits jobs, its state is
persisted to a FileStateStore, a second provider instance loads the same state
file, and the recovered provider sees all original jobs and resources.

No LocalStack or real AWS required — the operating mode is mocked.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import os
import tempfile
import uuid
from unittest.mock import MagicMock, patch

import pytest

from parsl_ephemeral_aws.provider import EphemeralAWSProvider
from parsl_ephemeral_aws.state.file import FileStateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider(tmp_dir, provider_id, state_file, max_blocks=10):
    """Build a provider backed by a shared FileStateStore.

    The operating mode is replaced by a MagicMock after construction.
    """
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
class TestProviderRestart:
    """Provider restart and state recovery (closes #45)."""

    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    def test_restart_recovers_jobs_and_resources(self, tmp_dir):
        """Second provider instance recovers all jobs and resources from state."""
        provider_id = f"restart-test-{uuid.uuid4().hex[:8]}"
        state_file = os.path.join(tmp_dir, f"{provider_id}.json")

        # --- Provider 1: submit 3 jobs and shut down ---
        p1, mode1, store1 = _make_provider(tmp_dir, provider_id, state_file)
        resource_ids = []
        job_ids = []
        for _ in range(3):
            jid = p1.submit("echo hello", tasks_per_node=1)
            job_ids.append(jid)
            # Find the resource_id for this job
            resource_ids.append(p1.job_map[jid]["resource_id"])

        # Persist final state
        p1._save_state()

        # --- Provider 2: load same state file ---
        p2, mode2, store2 = _make_provider(tmp_dir, provider_id, state_file)
        # Manually trigger state load (as provider would on initialize)
        p2._load_state()

        # All 3 jobs must be in the recovered job_map
        for jid in job_ids:
            assert jid in p2.job_map, f"job_id {jid} missing from recovered job_map"

        # All 3 resource_ids must be in the recovered resources dict
        for rid in resource_ids:
            assert (
                rid in p2.resources
            ), f"resource_id {rid} missing from recovered resources"

    def test_restart_preserves_job_status(self, tmp_dir):
        """Recovered provider sees the same statuses as the original."""
        provider_id = f"status-test-{uuid.uuid4().hex[:8]}"
        state_file = os.path.join(tmp_dir, f"{provider_id}.json")

        p1, mode1, _ = _make_provider(tmp_dir, provider_id, state_file)
        jid = p1.submit("echo hello", tasks_per_node=1)
        rid = p1.job_map[jid]["resource_id"]

        # Manually mark one job as RUNNING in state
        p1.resources[rid]["status"] = "RUNNING"
        p1.job_map[jid]["status"] = "RUNNING"
        p1._save_state()

        p2, _, _ = _make_provider(tmp_dir, provider_id, state_file)
        p2._load_state()

        assert (
            p2.job_map.get(jid, {}).get("status") == "RUNNING"
        ), "Recovered provider should see RUNNING status"
        assert (
            p2.resources.get(rid, {}).get("status") == "RUNNING"
        ), "Recovered provider should see RUNNING resource status"

    def test_empty_state_file_handled_gracefully(self, tmp_dir):
        """Provider restart with a missing state file returns False gracefully."""
        provider_id = f"empty-test-{uuid.uuid4().hex[:8]}"
        state_file = os.path.join(tmp_dir, f"{provider_id}.json")

        p, _, _ = _make_provider(tmp_dir, provider_id, state_file)
        # No state saved — _load_state should return False without raising
        result = p._load_state()
        assert (
            result is False or result is None
        ), "Loading nonexistent state should return falsy"

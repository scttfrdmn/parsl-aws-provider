"""Unit tests for EphemeralAWSProvider edge cases.

Covers issue #49 test gaps, plus #37/#39 configurability assertions.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import os
import tempfile
import uuid
from unittest.mock import MagicMock, patch

import pytest

from parsl_ephemeral_aws.exceptions import ProviderError
from parsl_ephemeral_aws.provider import EphemeralAWSProvider
from parsl_ephemeral_aws.state.file import FileStateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider(tmp_dir, max_blocks=5, **extra_kwargs):
    """Return a fully wired EphemeralAWSProvider backed by a FileStateStore.

    AWS interactions are suppressed via mocked session and operating mode.
    """
    provider_id = f"test-{uuid.uuid4().hex[:8]}"
    state_file = os.path.join(tmp_dir, f"{provider_id}.json")
    state_store = FileStateStore(file_path=state_file, provider_id=provider_id)

    mode_mock = MagicMock()
    mode_mock.submit_job.return_value = f"resource-{uuid.uuid4().hex[:8]}"
    mode_mock.get_job_status.return_value = {}
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
            **extra_kwargs,
        )

    return provider, mode_mock


# ---------------------------------------------------------------------------
# TestProviderEdgeCases
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProviderEdgeCases:
    """Edge-case tests for EphemeralAWSProvider."""

    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    def test_submit_at_zero_max_blocks_raises(self, tmp_dir):
        """submit() raises ProviderError immediately when max_blocks=0."""
        provider, _ = _make_provider(tmp_dir, max_blocks=0)

        with pytest.raises(ProviderError, match="max_blocks"):
            provider.submit("echo hello", tasks_per_node=1)

    def test_scale_in_more_than_running_caps_at_running_count(self, tmp_dir):
        """scale_in(N) terminates at most the number of RUNNING resources."""
        provider, mode = _make_provider(tmp_dir, max_blocks=10)

        # Seed 2 RUNNING resources
        provider.resources["r1"] = {"job_id": "j1", "status": "RUNNING"}
        provider.resources["r2"] = {"job_id": "j2", "status": "RUNNING"}
        provider.job_map["j1"] = {"resource_id": "r1", "status": "RUNNING"}
        provider.job_map["j2"] = {"resource_id": "r2", "status": "RUNNING"}

        # Request to scale in 100 — should cap at 2
        terminated = provider.scale_in(100)

        assert len(terminated) <= 2

    def test_status_empty_list_returns_empty(self, tmp_dir):
        """status([]) returns an empty list without error."""
        provider, _ = _make_provider(tmp_dir)

        result = provider.status([])

        assert result == []

    def test_cancel_empty_list_returns_empty(self, tmp_dir):
        """cancel([]) returns an empty list without error."""
        provider, _ = _make_provider(tmp_dir)

        result = provider.cancel([])

        assert result == []

    def test_status_polling_interval_configurable(self, tmp_dir):
        """status_polling_interval reflects the value passed to __init__."""
        provider, _ = _make_provider(tmp_dir, status_polling_interval=30)

        assert provider.status_polling_interval == 30

    def test_status_polling_interval_default(self, tmp_dir):
        """status_polling_interval defaults to 60 when not specified."""
        provider, _ = _make_provider(tmp_dir)

        assert provider.status_polling_interval == 60

    def test_waiter_params_stored_on_provider(self, tmp_dir):
        """waiter_delay and waiter_max_attempts are stored as provider attributes."""
        provider, _ = _make_provider(tmp_dir, waiter_delay=10, waiter_max_attempts=120)

        assert provider.waiter_delay == 10
        assert provider.waiter_max_attempts == 120

    def test_waiter_params_defaults(self, tmp_dir):
        """waiter_delay defaults to 5 and waiter_max_attempts to 60."""
        provider, _ = _make_provider(tmp_dir)

        assert provider.waiter_delay == 5
        assert provider.waiter_max_attempts == 60

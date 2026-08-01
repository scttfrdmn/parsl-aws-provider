"""Unit tests for the EphemeralAWSProvider core Parsl interface methods.

Tests cover submit, status, cancel, scale_in, scale_out, shutdown, and
thread-safety guarantees.  All AWS interactions are mocked.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import os
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import MagicMock, patch

import pytest

from parsl.jobs.states import JobState, JobStatus

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
            vpc_id="vpc-test00001",
            subnet_id="subnet-test001",
            security_group_id="sg-test00001",
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
        """status() returns a list of JobStatus objects, one per job_id."""
        provider, mode = _make_provider(tmp_dir)
        resource_id = "resource-abc"
        mode.submit_job.return_value = resource_id
        mode.get_job_status.return_value = {resource_id: "RUNNING"}

        job_id = provider.submit("echo hello", tasks_per_node=1)
        result = provider.status([job_id])

        assert len(result) == 1
        assert isinstance(result[0], JobStatus)
        assert result[0].state == JobState.RUNNING

    def test_status_unknown_job_id(self, tmp_dir):
        """status() returns UNKNOWN JobState for job_ids not in job_map."""
        provider, _ = _make_provider(tmp_dir)
        result = provider.status(["nonexistent-job-id"])

        assert isinstance(result[0], JobStatus)
        assert result[0].state == JobState.UNKNOWN

    # --- cancel ---

    def test_cancel_jobs(self, tmp_dir):
        """cancel() returns List[bool] with True for cancelled jobs."""
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

        assert isinstance(result, list)
        assert result[0] is True

    def test_cancel_nonexistent_job(self, tmp_dir):
        """cancel() returns False for unknown job IDs without raising."""
        provider, _ = _make_provider(tmp_dir)
        result = provider.cancel(["no-such-job"])

        assert isinstance(result, list)
        assert result[0] is False

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

    # --- concurrent stress tests (closes #46) ---

    def test_concurrent_submit_50_threads(self, tmp_dir):
        """50 concurrent submits all complete without data races (closes #46)."""
        provider, mode = _make_provider(tmp_dir, max_blocks=100)
        mode.submit_job.side_effect = lambda **_: f"resource-{uuid.uuid4().hex[:8]}"

        n = 50
        futures_list = []
        with ThreadPoolExecutor(max_workers=20) as ex:
            futures_list = [
                ex.submit(provider.submit, "echo hello", 1) for _ in range(n)
            ]
            job_ids = [f.result() for f in as_completed(futures_list)]

        assert len(job_ids) == n
        assert len(provider.resources) == n
        assert len(provider.job_map) == n
        # All job_ids must be unique
        assert len(set(job_ids)) == n

    def test_concurrent_status_and_submit(self, tmp_dir):
        """Simultaneous submits and status calls do not deadlock or corrupt state."""
        provider, mode = _make_provider(tmp_dir, max_blocks=100)
        mode.submit_job.side_effect = lambda **_: f"resource-{uuid.uuid4().hex[:8]}"
        mode.get_job_status.return_value = {}

        errors = []

        def do_submit():
            try:
                provider.submit("echo hello", 1)
            except Exception as exc:
                errors.append(exc)

        def do_status():
            try:
                provider.status([])
            except Exception as exc:
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=20) as ex:
            submit_futs = [ex.submit(do_submit) for _ in range(20)]
            status_futs = [ex.submit(do_status) for _ in range(20)]
            for f in as_completed(submit_futs + status_futs):
                f.result()

        assert not errors, f"Errors during concurrent submit+status: {errors}"


@pytest.mark.unit
class TestExecutionProviderConformance:
    """The provider must satisfy Parsl's ExecutionProvider contract (closes #82).

    `@typechecked` on EphemeralAWSProvider makes the annotations load-bearing at
    runtime, so a signature narrower than the base class's is not a typing nit —
    it raises TypeCheckError on inputs Parsl is entitled to pass.
    """

    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    def test_signatures_match_base_class(self):
        """submit/status/cancel accept everything ExecutionProvider declares."""
        import inspect

        from parsl.providers.base import ExecutionProvider

        for name in ("submit", "status", "cancel"):
            base = inspect.signature(getattr(ExecutionProvider, name))
            ours = inspect.signature(getattr(EphemeralAWSProvider, name))
            assert list(ours.parameters) == list(base.parameters), (
                f"{name}() parameter names diverge from ExecutionProvider"
            )
            for pname, bparam in base.parameters.items():
                assert ours.parameters[pname].default == bparam.default, (
                    f"{name}() default for {pname} diverges from ExecutionProvider"
                )

    def test_submit_default_job_name_accepted(self, tmp_dir):
        """The base class's "parsl.auto" default is treated as "no name given"."""
        provider, mode = _make_provider(tmp_dir)
        mode.submit_job.return_value = "resource-auto"

        job_id = provider.submit("echo hello", tasks_per_node=1)

        # Not the literal sentinel: a generated per-job name.
        assert provider.job_map[job_id]["job_name"] != "parsl.auto"
        assert provider.job_map[job_id]["job_name"].startswith("parsl-job-")

    def test_status_accepts_non_string_ids(self, tmp_dir):
        """status() reports UNKNOWN for opaque IDs rather than raising.

        `Sequence[object]` is what the base declares, so a non-string ID must
        not reach the string-keyed job_map.
        """
        provider, _ = _make_provider(tmp_dir)

        result = provider.status([1, None, object()])

        assert len(result) == 3
        assert all(s.state == JobState.UNKNOWN for s in result)

    def test_cancel_accepts_non_string_ids(self, tmp_dir):
        """cancel() reports False for opaque IDs rather than raising."""
        provider, _ = _make_provider(tmp_dir)

        result = provider.cancel([1, None])

        assert result == [False, False]

    def test_status_accepts_unhashable_ids(self, tmp_dir):
        """An unhashable ID reports UNKNOWN instead of raising TypeError.

        ``Sequence[object]`` permits unhashable objects, so results cannot be
        collected in a dict keyed by the ID itself — that raised
        ``TypeError: unhashable type: 'list'`` before results were keyed by
        position.
        """
        provider, _ = _make_provider(tmp_dir)

        result = provider.status([["a"], {"b": 1}])

        assert [s.state for s in result] == [JobState.UNKNOWN, JobState.UNKNOWN]

    def test_cancel_accepts_unhashable_ids(self, tmp_dir):
        """cancel() reports False for unhashable IDs instead of raising."""
        provider, _ = _make_provider(tmp_dir)

        assert provider.cancel([["a"], {"b": 1}]) == [False, False]

    def test_status_duplicate_ids_returns_one_entry_each(self, tmp_dir):
        """Repeated IDs each get their own positional entry.

        Keying results by ID would collapse duplicates; Parsl indexes the
        returned list positionally, so the lengths must match.
        """
        provider, mode = _make_provider(tmp_dir)
        resource_id = "resource-dup"
        mode.submit_job.return_value = resource_id
        mode.get_job_status.return_value = {resource_id: "RUNNING"}

        job_id = provider.submit("echo hello", tasks_per_node=1)
        result = provider.status([job_id, job_id, job_id])

        assert len(result) == 3
        assert all(s.state == JobState.RUNNING for s in result)

    def test_status_mixed_ids_preserves_order(self, tmp_dir):
        """A real ID alongside opaque ones still resolves positionally."""
        provider, mode = _make_provider(tmp_dir)
        resource_id = "resource-mixed"
        mode.submit_job.return_value = resource_id
        mode.get_job_status.return_value = {resource_id: "RUNNING"}

        job_id = provider.submit("echo hello", tasks_per_node=1)
        result = provider.status([None, job_id, 42])

        assert [s.state for s in result] == [
            JobState.UNKNOWN,
            JobState.RUNNING,
            JobState.UNKNOWN,
        ]

    def test_scale_in_skips_resources_without_job_id(self, tmp_dir):
        """A resource missing job_id is skipped, not passed to cancel() as None.

        An interrupted submit or a partially-restored state document can leave
        job_id absent; passing None into cancel() raised TypeCheckError.
        """
        provider, mode = _make_provider(tmp_dir)
        # No "job_id" key at all — the partial-state shape.
        provider.resources["r-orphan"] = {"status": "RUNNING"}
        provider.resources["r-good"] = {"job_id": "j-good", "status": "RUNNING"}
        provider.job_map["j-good"] = {"resource_id": "r-good", "status": "RUNNING"}
        mode.cancel_jobs.return_value = {"r-good": "CANCELED"}

        terminated = provider.scale_in(2)

        assert None not in terminated
        assert terminated == ["j-good"]

    def test_cores_and_mem_per_node_resolved(self, tmp_dir):
        """The base class declares both; EC2 modes populate them from the API."""
        with patch(
            "parsl_ephemeral_aws.provider.describe_instance_capacity",
            return_value=(2, 1.0),
        ):
            provider, _ = _make_provider(tmp_dir)

        assert provider.cores_per_node == 2
        assert provider.mem_per_node == 1.0

    def test_explicit_cores_and_mem_override_lookup(self, tmp_dir):
        """Caller-supplied values win; no EC2 call is made."""
        provider_id = f"test-{uuid.uuid4().hex[:8]}"
        state_file = os.path.join(tmp_dir, f"{provider_id}.json")
        state_store = FileStateStore(file_path=state_file, provider_id=provider_id)

        with (
            patch("parsl_ephemeral_aws.provider.create_session"),
            patch(
                "parsl_ephemeral_aws.provider.describe_instance_capacity"
            ) as mock_lookup,
            patch.object(
                EphemeralAWSProvider,
                "_initialize_state_store",
                return_value=state_store,
            ),
            patch.object(
                EphemeralAWSProvider,
                "_initialize_operating_mode",
                return_value=MagicMock(),
            ),
        ):
            provider = EphemeralAWSProvider(
                provider_id=provider_id,
                region="us-east-1",
                image_id="ami-12345678",
                instance_type="t3.micro",
                mode="standard",
                init_blocks=0,
                vpc_id="vpc-test00001",
                subnet_id="subnet-test001",
                security_group_id="sg-test00001",
                cores_per_node=8,
                mem_per_node=16.0,
            )

        assert provider.cores_per_node == 8
        assert provider.mem_per_node == 16.0
        mock_lookup.assert_not_called()

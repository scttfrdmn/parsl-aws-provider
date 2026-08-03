"""Unit tests for EphemeralProvider edge cases.

Covers issue #49 test gaps, plus #37/#39 configurability assertions.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import os
import tempfile
import time
import uuid
import warnings
from unittest.mock import MagicMock, patch

import pytest

from parsl.jobs.states import JobState

from parsl_ephemeral_provider.constants import (
    DEFAULT_BASTION_HOST_TYPE,
    DEFAULT_BASTION_IDLE_TIMEOUT,
    DEFAULT_ECS_CONTAINER_IMAGE,
    DEFAULT_ECS_CPU,
    DEFAULT_ECS_MEMORY,
    DEFAULT_LAMBDA_RUNTIME,
    DEFAULT_MAX_IDLE_TIME,
    DEFAULT_PRESERVE_BASTION,
    DEFAULT_WARM_POOL_SIZE,
    DEFAULT_WARM_POOL_TTL,
    STATUS_INTERRUPTED,
)
from parsl_ephemeral_provider.exceptions import (
    ProviderConfigurationError,
    ProviderError,
)
from parsl_ephemeral_provider.provider import EphemeralProvider
from parsl_ephemeral_provider.state.base import STATE_KEY_MODE, STATE_KEY_PROVIDER
from parsl_ephemeral_provider.state.file import FileStateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider(tmp_dir, max_blocks=5, **extra_kwargs):
    """Return a fully wired EphemeralProvider backed by a FileStateStore.

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
        patch(
            "parsl_ephemeral_provider.provider.create_session"
        ) as mock_session_factory,
        patch.object(
            EphemeralProvider,
            "_initialize_state_store",
            return_value=state_store,
        ),
        patch.object(
            EphemeralProvider,
            "_initialize_operating_mode",
            return_value=mode_mock,
        ),
    ):
        mock_session_factory.return_value = MagicMock()
        provider = EphemeralProvider(
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
            **extra_kwargs,
        )

    return provider, mode_mock


# ---------------------------------------------------------------------------
# TestProviderEdgeCases
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProviderEdgeCases:
    """Edge-case tests for EphemeralProvider."""

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


# ---------------------------------------------------------------------------
# TestWarmPool
# ---------------------------------------------------------------------------


_FAKE_IAM_ARN = "arn:aws:iam::123456789012:instance-profile/ParslSSMProfile"


@pytest.mark.unit
class TestWarmPool:
    """Unit tests for the warm pool instance-reuse feature."""

    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    # --- parameter / guard tests ---

    def test_warm_pool_disabled_by_default(self, tmp_dir):
        """The pool is off by default, and a warm instance's TTL is short.

        A warm instance is *Running*, not Stopped, so it bills at the full rate
        for the whole TTL -- AWS calls keeping warm instances running "highly
        discouraged to avoid incurring unnecessary charges". This package cannot
        use the native ASG warm pool that holds them Stopped, because dispatch is
        SSM ``SendCommand`` and a Stopped instance runs no agent (#130 tracks the
        pull model that would allow it). So the cost is bounded instead: the TTL
        default was cut from the 600s v0.6.0 shipped with to
        ``DEFAULT_WARM_POOL_TTL`` (#86).

        Asserted against the constant rather than a literal, so the intent -- off
        by default, and short-lived when on -- survives the next revision of the
        number.
        """
        provider, _ = _make_provider(tmp_dir)

        assert provider.warm_pool_size == DEFAULT_WARM_POOL_SIZE == 0
        assert provider.warm_pool_ttl == DEFAULT_WARM_POOL_TTL
        assert DEFAULT_WARM_POOL_TTL <= 120

    def test_warm_pool_iam_guard_raises(self, tmp_dir):
        """warm_pool_size > 0 without an IAM profile raises ValueError."""
        with pytest.raises(ValueError, match="iam_instance_profile_arn"):
            _make_provider(tmp_dir, warm_pool_size=1)

    def test_warm_pool_iam_guard_passes_with_arn(self, tmp_dir):
        """warm_pool_size > 0 with iam_instance_profile_arn does not raise."""
        provider, _ = _make_provider(
            tmp_dir,
            warm_pool_size=2,
            iam_instance_profile_arn=_FAKE_IAM_ARN,
        )

        assert provider.warm_pool_size == 2

    def test_warm_pool_iam_guard_passes_with_auto_create(self, tmp_dir):
        """warm_pool_size > 0 with auto_create_instance_profile=True does not raise."""
        provider, _ = _make_provider(
            tmp_dir,
            warm_pool_size=1,
            auto_create_instance_profile=True,
        )

        assert provider.warm_pool_size == 1

    # --- lifecycle: COMPLETED → WARM transition ---

    def _make_warm_provider(self, tmp_dir, warm_pool_size=2, warm_pool_ttl=600):
        """Create a provider with warm pool enabled and a mode mock with _warm_instances."""
        provider, mode = _make_provider(
            tmp_dir,
            warm_pool_size=warm_pool_size,
            warm_pool_ttl=warm_pool_ttl,
            iam_instance_profile_arn=_FAKE_IAM_ARN,
        )
        mode._warm_instances = []
        return provider, mode

    def test_completed_warm_pool_instance_transitions_to_warm(self, tmp_dir):
        """_cleanup_resources() moves a COMPLETED warm-pool resource to WARM state."""
        provider, mode = self._make_warm_provider(tmp_dir)

        provider.resources["i-001"] = {
            "job_id": "j-001",
            "status": "COMPLETED",
            "warm_pool": True,
            "timestamp": time.time(),
        }
        provider.job_map["j-001"] = {"resource_id": "i-001", "status": "COMPLETED"}

        provider._cleanup_resources()

        assert "i-001" in provider.resources
        assert provider.resources["i-001"]["status"] == "WARM"
        assert "warm_since" in provider.resources["i-001"]
        assert "i-001" in mode._warm_instances

    def test_completed_to_warm_transition_is_persisted(self, tmp_dir):
        """The COMPLETED → WARM transition must reach the state file.

        Regression test for the defect the #65 E2E suite caught: every other
        warm-pool test in this class asserts only in-memory state, and
        ``_cleanup_resources()`` used to call ``_save_state()`` solely inside its
        ``if resources_to_cleanup:`` branch. A transition into a pool with room
        terminates nothing, so it took that branch zero times and the file kept
        saying ``COMPLETED`` with an empty ``warm_instances``.

        That is not a cosmetic staleness. A provider reconstructed from the file
        -- the whole point of the state store -- restores no warm instances, so
        it neither reuses them nor applies their TTL, while AWS keeps billing
        them at the full Running rate until something else notices. Asserted by
        reading the file rather than the object, which is the only way to tell
        the two apart.

        The mode's ``save_state()`` must be driven too, and that half is the one a
        restart actually depends on: ``__init__`` restores ``_warm_instances``
        from the provider key, then ``operating_mode.initialize()`` calls
        ``load_state()``, which overwrites it from the mode key. Persisting only
        the provider document looked right on the file and still lost the pool on
        restart — which is what the live #65 run showed.
        """
        provider, mode = self._make_warm_provider(tmp_dir)

        provider.resources["i-001"] = {
            "job_id": "j-001",
            "status": "COMPLETED",
            "warm_pool": True,
            "timestamp": time.time(),
        }
        provider.job_map["j-001"] = {"resource_id": "i-001", "status": "COMPLETED"}

        provider._cleanup_resources()

        persisted = provider.state_store.load_state(STATE_KEY_PROVIDER)
        assert persisted["resources"]["i-001"]["status"] == "WARM"
        assert persisted["warm_instances"] == ["i-001"]
        mode.save_state.assert_called()

    def test_warm_instance_not_cleaned_up_before_ttl(self, tmp_dir):
        """A WARM instance within its TTL is not terminated."""
        provider, mode = self._make_warm_provider(tmp_dir, warm_pool_ttl=600)
        mode._warm_instances = ["i-001"]

        provider.resources["i-001"] = {
            "job_id": "j-001",
            "status": "WARM",
            "warm_pool": True,
            "warm_since": time.time() - 60,  # 60s old, TTL is 600s
        }
        provider.job_map["j-001"] = {"resource_id": "i-001", "status": "COMPLETED"}

        provider._cleanup_resources()

        assert "i-001" in provider.resources  # still alive
        assert mode.cleanup_resources.call_count == 0

    # --- TTL eviction ---

    def test_ttl_eviction_terminates_expired_warm_instance(self, tmp_dir):
        """A WARM instance past its TTL is terminated by _cleanup_resources()."""
        provider, mode = self._make_warm_provider(tmp_dir, warm_pool_ttl=60)
        mode._warm_instances = ["i-001"]

        provider.resources["i-001"] = {
            "job_id": "j-001",
            "status": "WARM",
            "warm_pool": True,
            "warm_since": time.time() - 120,  # 120s old > 60s TTL
        }
        provider.job_map["j-001"] = {"resource_id": "i-001", "status": "COMPLETED"}

        provider._cleanup_resources()

        assert "i-001" not in provider.resources
        assert "i-001" not in mode._warm_instances
        mode.cleanup_resources.assert_called_once_with(["i-001"])

    # --- pool-full eviction ---

    def test_pool_full_evicts_oldest_warm_instance(self, tmp_dir):
        """When the pool is full, the oldest warm instance is evicted for the newcomer."""
        provider, mode = self._make_warm_provider(tmp_dir, warm_pool_size=1)
        now = time.time()

        # Existing warm instance (old)
        mode._warm_instances = ["i-old"]
        provider.resources["i-old"] = {
            "job_id": "j-old",
            "status": "WARM",
            "warm_pool": True,
            "warm_since": now - 300,
        }
        provider.job_map["j-old"] = {"resource_id": "i-old", "status": "COMPLETED"}

        # New instance whose job just completed
        provider.resources["i-new"] = {
            "job_id": "j-new",
            "status": "COMPLETED",
            "warm_pool": True,
            "timestamp": now,
        }
        provider.job_map["j-new"] = {"resource_id": "i-new", "status": "COMPLETED"}

        provider._cleanup_resources()

        # Oldest evicted, newcomer kept
        assert "i-old" not in provider.resources
        assert "i-new" in provider.resources
        assert provider.resources["i-new"]["status"] == "WARM"
        assert "i-new" in mode._warm_instances
        assert "i-old" not in mode._warm_instances


# ---------------------------------------------------------------------------
# TestAMIBaking
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAMIBaking:
    """Unit tests for the AMI baking feature (issue #67)."""

    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    # -----------------------------------------------------------------------
    # 1. Default state
    # -----------------------------------------------------------------------

    def test_bake_ami_disabled_by_default(self, tmp_dir):
        """bake_ami defaults to False and _baked_ami_id is None."""
        provider, _ = _make_provider(tmp_dir)

        assert provider.bake_ami is False
        assert provider.baked_ami_id is None

    # -----------------------------------------------------------------------
    # 2. Pre-supplied baked AMI skips baking
    # -----------------------------------------------------------------------

    def test_pre_supplied_baked_ami_skips_baking(self, tmp_dir):
        """When baked_ami_id is supplied, image_id is set and no create_image call is made."""
        import os

        provider_id = f"test-{uuid.uuid4().hex[:8]}"
        state_file = os.path.join(tmp_dir, f"{provider_id}.json")

        from parsl_ephemeral_provider.state.file import FileStateStore
        from parsl_ephemeral_provider.modes.standard import StandardMode

        state_store = FileStateStore(file_path=state_file, provider_id=provider_id)

        ec2_mock = MagicMock()
        session_mock = MagicMock()
        session_mock.client.return_value = ec2_mock

        mode = StandardMode(
            provider_id=provider_id,
            session=session_mock,
            state_store=state_store,
            image_id="ami-base",
            baked_ami_id="ami-prefab",
            vpc_id="vpc-123",
            subnet_id="subnet-123",
            security_group_id="sg-123",
        )
        # Run just the baking branch by calling initialize() with network already set
        with (
            patch.object(mode, "save_state"),
            patch.object(mode, "load_state", return_value=False),
            patch.object(mode, "_verify_resources"),
        ):
            mode.initialize()

        assert mode.image_id == "ami-prefab"
        assert mode._baked_ami_id == "ami-prefab"
        # No EC2 create_image call should have been made
        ec2_mock.create_image.assert_not_called()

    # -----------------------------------------------------------------------
    # 3. _bake_ami() launches builder and creates image
    # -----------------------------------------------------------------------

    def test_bake_ami_launches_builder_and_creates_image(self, tmp_dir):
        """_bake_ami() calls run_instances, waits for stop, create_image, waits for available."""
        import os

        provider_id = f"test-{uuid.uuid4().hex[:8]}"
        state_file = os.path.join(tmp_dir, f"{provider_id}.json")

        from parsl_ephemeral_provider.state.file import FileStateStore
        from parsl_ephemeral_provider.modes.standard import StandardMode

        state_store = FileStateStore(file_path=state_file, provider_id=provider_id)

        ec2_mock = MagicMock()
        session_mock = MagicMock()
        session_mock.client.return_value = ec2_mock

        ec2_mock.run_instances.return_value = {
            "Instances": [{"InstanceId": "i-builder001"}]
        }
        ec2_mock.create_image.return_value = {"ImageId": "ami-baked001"}

        mode = StandardMode(
            provider_id=provider_id,
            session=session_mock,
            state_store=state_store,
            image_id="ami-base",
            bake_ami=True,
            vpc_id="vpc-123",
            subnet_id="subnet-123",
            security_group_id="sg-123",
        )

        with patch(
            "parsl_ephemeral_provider.modes.standard.wait_for_resource"
        ) as mock_wait:
            ami_id = mode._bake_ami()

        assert ami_id == "ami-baked001"
        ec2_mock.run_instances.assert_called_once()
        ec2_mock.create_image.assert_called_once()
        # wait_for_resource should have been called at least twice
        # (instance_stopped and image_available)
        assert mock_wait.call_count >= 2
        # Builder should be terminated
        ec2_mock.terminate_instances.assert_called_with(InstanceIds=["i-builder001"])

    # -----------------------------------------------------------------------
    # 4. save_state() persists baked_ami_id
    # -----------------------------------------------------------------------

    def test_baked_ami_persisted_in_save_state(self, tmp_dir):
        """save_state() includes baked_ami_id and owns_baked_ami in the state dict."""
        import os

        provider_id = f"test-{uuid.uuid4().hex[:8]}"
        state_file = os.path.join(tmp_dir, f"{provider_id}.json")

        from parsl_ephemeral_provider.state.file import FileStateStore
        from parsl_ephemeral_provider.modes.standard import StandardMode

        state_store = FileStateStore(file_path=state_file, provider_id=provider_id)
        session_mock = MagicMock()

        mode = StandardMode(
            provider_id=provider_id,
            session=session_mock,
            state_store=state_store,
            image_id="ami-base",
            vpc_id="vpc-test00001",
            subnet_id="subnet-test001",
            security_group_id="sg-test00001",
        )
        mode._baked_ami_id = "ami-saved001"
        mode._owns_baked_ami = True

        saved = {}
        keys_written = []

        def _capture_state(state_key, state):
            keys_written.append(state_key)
            saved.update(state)

        with patch.object(state_store, "save_state", side_effect=_capture_state):
            mode.save_state()

        assert keys_written == [STATE_KEY_MODE]
        assert saved.get("baked_ami_id") == "ami-saved001"
        assert saved.get("owns_baked_ami") is True

    # -----------------------------------------------------------------------
    # 5. load_state() restores baked_ami_id
    # -----------------------------------------------------------------------

    def test_baked_ami_restored_from_load_state(self, tmp_dir):
        """load_state() restores _baked_ami_id and sets image_id to the baked AMI."""
        import os

        provider_id = f"test-{uuid.uuid4().hex[:8]}"
        state_file = os.path.join(tmp_dir, f"{provider_id}.json")

        from parsl_ephemeral_provider.state.file import FileStateStore
        from parsl_ephemeral_provider.modes.standard import StandardMode

        state_store = FileStateStore(file_path=state_file, provider_id=provider_id)
        session_mock = MagicMock()

        mode = StandardMode(
            provider_id=provider_id,
            session=session_mock,
            state_store=state_store,
            image_id="ami-base",
            vpc_id="vpc-test00001",
            subnet_id="subnet-test001",
            security_group_id="sg-test00001",
        )
        persisted_state = {
            "provider_id": provider_id,
            "resources": {},
            "vpc_id": "vpc-test00001",
            "subnet_id": "subnet-test001",
            "security_group_id": "sg-test00001",
            "initialized": True,
            "use_spot_fleet": False,
            "spot_interruption_handling": False,
            "warm_instances": [],
            "baked_ami_id": "ami-restored001",
            "owns_baked_ami": True,
        }

        with patch.object(state_store, "load_state", return_value=persisted_state):
            mode.load_state()

        assert mode._baked_ami_id == "ami-restored001"
        assert mode._owns_baked_ami is True
        assert mode.image_id == "ami-restored001"

    # -----------------------------------------------------------------------
    # 6. cleanup_infrastructure() deregisters baked AMI
    # -----------------------------------------------------------------------

    def test_deregister_baked_ami_on_cleanup(self, tmp_dir):
        """cleanup_infrastructure() calls deregister_image and delete_snapshot when _owns_baked_ami."""
        import os

        provider_id = f"test-{uuid.uuid4().hex[:8]}"
        state_file = os.path.join(tmp_dir, f"{provider_id}.json")

        from parsl_ephemeral_provider.state.file import FileStateStore
        from parsl_ephemeral_provider.modes.standard import StandardMode

        state_store = FileStateStore(file_path=state_file, provider_id=provider_id)

        ec2_mock = MagicMock()
        ec2_mock.describe_images.return_value = {
            "Images": [
                {
                    "BlockDeviceMappings": [
                        {"Ebs": {"SnapshotId": "snap-abc123"}},
                    ]
                }
            ]
        }
        session_mock = MagicMock()
        session_mock.client.return_value = ec2_mock

        mode = StandardMode(
            provider_id=provider_id,
            session=session_mock,
            state_store=state_store,
            image_id="ami-base",
            vpc_id="vpc-test00001",
            subnet_id="subnet-test001",
            security_group_id="sg-test00001",
        )
        mode._baked_ami_id = "ami-cleanup001"
        mode._owns_baked_ami = True
        mode.initialized = True

        with patch.object(mode, "cleanup_all"), patch.object(mode, "save_state"):
            mode.cleanup_infrastructure()

        ec2_mock.deregister_image.assert_called_once_with(ImageId="ami-cleanup001")
        ec2_mock.delete_snapshot.assert_called_once_with(SnapshotId="snap-abc123")
        assert mode._baked_ami_id is None


# ---------------------------------------------------------------------------
# TestOneShotMode
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOneShotMode:
    """Unit tests for the one_shot parameter (issue #66)."""

    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    def test_one_shot_disabled_by_default(self, tmp_dir):
        """one_shot defaults to False on the provider."""
        provider, _ = _make_provider(tmp_dir)
        assert provider.one_shot is False

    def test_one_shot_warm_pool_guard_raises(self, tmp_dir):
        """one_shot=True combined with warm_pool_size > 0 raises ValueError."""
        with pytest.raises(ValueError, match="one_shot"):
            _make_provider(
                tmp_dir,
                one_shot=True,
                warm_pool_size=1,
                iam_instance_profile_arn=_FAKE_IAM_ARN,
            )

    def test_one_shot_compatible_with_zero_warm_pool(self, tmp_dir):
        """one_shot=True with warm_pool_size=0 (default) constructs without error."""
        provider, _ = _make_provider(
            tmp_dir,
            one_shot=True,
            warm_pool_size=0,
            iam_instance_profile_arn=_FAKE_IAM_ARN,
        )
        assert provider.one_shot is True

    def test_one_shot_iam_guard_raises(self, tmp_dir):
        """One-shot dispatches over SSM, so it needs an instance profile too."""
        with pytest.raises(ValueError, match="one_shot=True requires"):
            _make_provider(tmp_dir, one_shot=True)

    def test_one_shot_iam_guard_passes_with_auto_create(self, tmp_dir):
        """auto_create_instance_profile=True satisfies the one-shot IAM guard."""
        provider, _ = _make_provider(
            tmp_dir, one_shot=True, auto_create_instance_profile=True
        )
        assert provider.one_shot is True

    def _one_shot_mode(self, tmp_dir, **overrides):
        """Build a StandardMode with one_shot=True and a mocked session."""
        from parsl_ephemeral_provider.modes.standard import StandardMode

        provider_id = f"test-{uuid.uuid4().hex[:8]}"
        state_file = os.path.join(tmp_dir, f"{provider_id}.json")
        state_store = FileStateStore(file_path=state_file, provider_id=provider_id)

        params = dict(
            provider_id=provider_id,
            session=MagicMock(),
            state_store=state_store,
            image_id="ami-12345678",
            auto_shutdown=False,
            one_shot=True,
            vpc_id="vpc-test00001",
            subnet_id="subnet-test001",
            security_group_id="sg-test00001",
        )
        params.update(overrides)
        return StandardMode(**params)

    def test_one_shot_dispatches_over_ssm_not_userdata(self, tmp_dir):
        """One-shot uses SSM so the command's exit code is observable.

        UserData cannot report an exit code — the instance state is identical
        whether the command succeeded or failed (#76).
        """
        mode = self._one_shot_mode(tmp_dir)

        assert mode._uses_ssm_dispatch() is True

        script = mode._prepare_init_script("echo hi", "job-1")
        assert "echo hi" not in script
        assert "/var/run/parsl_worker_ready" in script
        # No UserData shutdown: it would race the SSM dispatch and kill the
        # instance before the command was ever delivered.
        assert "shutdown -h now" not in script

    def test_one_shot_ssm_command_carries_shutdown_backstop(self, tmp_dir):
        """The dispatched command shuts the instance down if the driver dies."""
        mode = self._one_shot_mode(tmp_dir)
        ssm = mode.session.client.return_value
        ssm.send_command.return_value = {"Command": {"CommandId": "cmd-1"}}

        mode._dispatch_ssm_command("i-123", "echo hi", "job-1")

        script = ssm.send_command.call_args.kwargs["Parameters"]["commands"][0]
        assert "echo hi" in script
        assert "shutdown -h now" in script
        # The exit code is captured before the shutdown is scheduled, and
        # re-raised, so a failing command still reports FAILED.
        assert "_parsl_rc=$?" in script
        assert "exit $_parsl_rc" in script

    def test_non_one_shot_ssm_command_has_no_shutdown(self, tmp_dir):
        """Warm-pool instances must survive the command for reuse."""
        mode = self._one_shot_mode(
            tmp_dir, one_shot=False, warm_pool_size=2, auto_shutdown=True
        )
        ssm = mode.session.client.return_value
        ssm.send_command.return_value = {"Command": {"CommandId": "cmd-1"}}

        mode._dispatch_ssm_command("i-123", "echo hi", "job-1")

        script = ssm.send_command.call_args.kwargs["Parameters"]["commands"][0]
        assert "shutdown" not in script

    def test_instances_terminate_rather_than_stop_on_shutdown(self, tmp_dir):
        """EC2 defaults an instance-initiated shutdown to *stop*, billing EBS.

        Regression for #76: a stopped instance keeps a billed volume, and
        EC2_STATUS_MAPPING maps "stopped" to COMPLETED, so the provider drops
        the tracking record and the volume is orphaned as well as billed.
        """
        mode = self._one_shot_mode(tmp_dir)
        ec2 = mode.session.client.return_value
        ec2.run_instances.return_value = {"Instances": [{"InstanceId": "i-123"}]}

        mode._create_instance("#!/bin/bash\n", "job-1")

        run_args = ec2.run_instances.call_args.kwargs
        assert run_args["InstanceInitiatedShutdownBehavior"] == "terminate"

    def test_spot_launch_spec_omits_run_instances_only_keys(self, tmp_dir):
        """RequestSpotInstances rejects RunInstances-only keys (#97).

        ``run_args`` is built for ``run_instances`` and reused as the spot
        ``LaunchSpecification``; boto3 validates parameter names client-side, so
        leaving any of these in makes every spot request raise before it reaches
        AWS.
        """
        mode = self._one_shot_mode(tmp_dir, use_spot=True)
        ec2 = mode.session.client.return_value
        ec2.request_spot_instances.return_value = {
            "SpotInstanceRequests": [{"SpotInstanceRequestId": "sir-1"}]
        }
        ec2.describe_spot_instance_requests.return_value = {
            "SpotInstanceRequests": [{"InstanceId": "i-123"}]
        }

        mode._create_instance("#!/bin/bash\n", "job-1")

        spec = ec2.request_spot_instances.call_args.kwargs["LaunchSpecification"]
        for key in ("MinCount", "MaxCount", "InstanceInitiatedShutdownBehavior"):
            assert key not in spec, f"{key} is not valid in a LaunchSpecification"

    def test_one_shot_status_comes_from_the_ssm_exit_code(self, tmp_dir):
        """A non-zero exit must report FAILED, not COMPLETED."""
        from parsl_ephemeral_provider.constants import STATUS_COMPLETED, STATUS_FAILED

        mode = self._one_shot_mode(tmp_dir)
        mode.resources["i-123"] = {
            "type": "ec2",
            "one_shot": True,
            "ssm_command_id": "cmd-1",
            "status": "RUNNING",
        }
        ssm = mode.session.client.return_value
        ssm.get_command_invocation.return_value = {
            "Status": "Failed",
            "ResponseCode": 1,
            "StandardErrorContent": "boom",
        }

        assert mode.get_job_status(["i-123"]) == {"i-123": STATUS_FAILED}
        assert mode.resources["i-123"]["exit_code"] == 1

        ssm.get_command_invocation.return_value = {
            "Status": "Success",
            "ResponseCode": 0,
        }
        assert mode.get_job_status(["i-123"]) == {"i-123": STATUS_COMPLETED}
        assert mode.resources["i-123"]["exit_code"] == 0

    def test_failed_dispatch_terminates_the_instance(self, tmp_dir):
        """With no command in UserData there is nothing to fall back to.

        Leaving the instance up would idle it until max_idle_time while
        reporting RUNNING, so the submit fails loudly and the instance goes.
        """
        mode = self._one_shot_mode(tmp_dir)
        mode.initialized = True
        ec2 = mode.session.client.return_value
        ec2.run_instances.return_value = {"Instances": [{"InstanceId": "i-123"}]}

        with patch.object(
            mode, "_wait_for_ssm_online", side_effect=RuntimeError("no SSM")
        ):
            with pytest.raises(Exception, match="no SSM"):
                mode.submit_job(job_id="job-1", command="echo hi", tasks_per_node=1)

        ec2.terminate_instances.assert_called_once_with(InstanceIds=["i-123"])
        assert "i-123" not in mode.resources


# ---------------------------------------------------------------------------
# TestStateKeySeparation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStateKeySeparation:
    """The provider and its mode must not overwrite each other's state (#78)."""

    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    @staticmethod
    def _mode(provider_id, state_store):
        """Return a StandardMode sharing *state_store* with the provider."""
        from parsl_ephemeral_provider.modes.standard import StandardMode

        return StandardMode(
            provider_id=provider_id,
            session=MagicMock(),
            state_store=state_store,
            image_id="ami-base",
            vpc_id="vpc-test00001",
            subnet_id="subnet-test001",
            security_group_id="sg-test00001",
        )

    def test_provider_and_mode_writes_coexist(self, tmp_dir):
        """A mode write must not destroy job_map, nor a provider write the baked AMI.

        Both writers previously issued full-document overwrites into one slot, so
        whichever went last erased the other's fields: the baked AMI ID (leaking
        the AMI and its snapshots, since cleanup could no longer see it) or
        job_map (losing the job-to-resource mapping across restart).
        """
        provider, _ = _make_provider(tmp_dir)
        state_store = provider.state_store
        mode = self._mode(provider.provider_id, state_store)
        mode._baked_ami_id = "ami-baked001"
        mode._owns_baked_ami = True

        mode.save_state()
        provider.job_map["job-1"] = "resource-1"
        provider._save_state()
        # A second mode write is the point at which the provider's fields used
        # to disappear.
        mode.save_state()

        mode_state = state_store.load_state(STATE_KEY_MODE)
        provider_state = state_store.load_state(STATE_KEY_PROVIDER)

        assert mode_state["baked_ami_id"] == "ami-baked001"
        assert mode_state["owns_baked_ami"] is True
        assert provider_state["job_map"] == {"job-1": "resource-1"}

    def test_both_writers_survive_a_round_trip(self, tmp_dir):
        """Reloading restores the mode's baked AMI and the provider's job_map."""
        provider, _ = _make_provider(tmp_dir)
        state_store = provider.state_store
        mode = self._mode(provider.provider_id, state_store)
        mode._baked_ami_id = "ami-baked001"
        mode._owns_baked_ami = True
        mode.initialized = True

        mode.save_state()
        provider.job_map["job-1"] = "resource-1"
        provider._save_state()

        # A fresh mode and provider reading the same file, as after a restart.
        reloaded_mode = self._mode(provider.provider_id, state_store)
        assert reloaded_mode.load_state() is True
        assert reloaded_mode._baked_ami_id == "ami-baked001"
        assert reloaded_mode._owns_baked_ami is True
        assert reloaded_mode.image_id == "ami-baked001"

        provider.job_map.clear()
        provider._load_state()
        assert provider.job_map == {"job-1": "resource-1"}

    def test_mode_load_ignores_the_provider_document(self, tmp_dir):
        """The mode must not pick up the provider's document as its own.

        The provider writes provider_id too, so an unkeyed read would match the
        mode's own provider_id check and load the wrong shape.
        """
        provider, _ = _make_provider(tmp_dir)
        provider.job_map["job-1"] = "resource-1"
        provider._save_state()

        mode = self._mode(provider.provider_id, provider.state_store)
        assert mode.load_state() is False
        assert mode.initialized is False

    def test_shutdown_deletes_both_documents(self, tmp_dir):
        """shutdown() must delete both keys, not save an empty document.

        ``delete_state`` was implemented in all three stores and the ABC but
        called from nowhere; shutdown wrote an empty document instead, which on
        the AWS backends left SSM parameters and S3 objects behind and on any
        backend left a document describing resources that no longer exist.
        """
        provider, _ = _make_provider(tmp_dir)
        state_store = provider.state_store
        mode = self._mode(provider.provider_id, state_store)
        mode._baked_ami_id = "ami-baked001"

        mode.save_state()
        provider.job_map["job-1"] = "resource-1"
        provider._save_state()
        assert state_store.load_state(STATE_KEY_MODE) is not None
        assert state_store.load_state(STATE_KEY_PROVIDER) is not None

        # The provider's operating_mode is a MagicMock, so route the mode
        # deletion at the real store the way the live mode would.
        provider.operating_mode.delete_state.side_effect = mode.delete_state

        provider.shutdown()

        assert state_store.load_state(STATE_KEY_MODE) is None
        assert state_store.load_state(STATE_KEY_PROVIDER) is None

    @staticmethod
    def _provider_sharing(state_file, **extra):
        """Build a provider at *state_file* without passing a provider_id.

        ``_make_provider`` always supplies one, which is exactly the case that
        skips adoption — so restart has to be modelled with a provider that lets
        the ID default, as a real successor process would.
        """
        with (
            patch(
                "parsl_ephemeral_provider.provider.create_session"
            ) as mock_session_factory,
            patch.object(
                EphemeralProvider,
                "_initialize_operating_mode",
                return_value=MagicMock(),
            ),
        ):
            mock_session_factory.return_value = MagicMock()
            return EphemeralProvider(
                region="us-east-1",
                image_id="ami-12345678",
                instance_type="t3.micro",
                mode="standard",
                state_store_type="file",
                state_file_path=state_file,
                min_blocks=0,
                init_blocks=0,
                vpc_id="vpc-test00001",
                subnet_id="subnet-test001",
                security_group_id="sg-test00001",
                **extra,
            )

    def test_a_successor_provider_loads_state_from_the_same_path(self, tmp_dir):
        """Restart must restore job_map given nothing but a shared state path.

        Two independent defects made this impossible: ``_load_state()`` was
        never called from ``__init__`` at all, and it refuses a document whose
        ``provider_id`` differs from its own -- while ``provider_id`` defaults to
        a fresh UUID and does not affect *where* state is stored. So a successor
        rejected the very state it was pointed at.
        """
        state_file = os.path.join(tmp_dir, "shared.json")

        first = self._provider_sharing(state_file)
        first.job_map["job-1"] = {"resource_id": "resource-1"}
        first._save_state()

        second = self._provider_sharing(state_file)

        assert second.provider_id == first.provider_id
        assert second.job_map == {"job-1": {"resource_id": "resource-1"}}

    def test_the_id_is_adopted_from_mode_state_when_no_provider_state_exists(
        self, tmp_dir
    ):
        """Only the mode key exists until a job is submitted.

        The provider writes its own key from ``_save_state()``, which runs on
        submit/status/cancel — so a provider that constructed and exited without
        submitting leaves *only* the mode document behind. Adoption originally
        read the provider key alone and found nothing, so the successor kept its
        fresh UUID and ``OperatingMode.load_state()`` rejected the mode document
        on the ID gate. Found against real AWS: a resumed provider silently took
        the create path and never noticed its security group had been deleted.
        """
        state_file = os.path.join(tmp_dir, "mode-only.json")

        first = self._provider_sharing(state_file)
        mode = self._mode(first.provider_id, first.state_store)
        mode.save_state()

        # Only the mode key is present — exactly the no-submit case.
        assert first.state_store.load_state(STATE_KEY_PROVIDER) is None
        assert first.state_store.load_state(STATE_KEY_MODE) is not None

        second = self._provider_sharing(state_file)

        assert second.provider_id == first.provider_id
        # And with the ID adopted, the mode's own load now clears its gate.
        successor_mode = self._mode(second.provider_id, second.state_store)
        assert successor_mode.load_state() is True

    def test_an_explicit_provider_id_is_never_overridden(self, tmp_dir):
        """A caller-supplied ID is a deliberate choice; adoption must skip it.

        Otherwise a provider asked for a specific identity would silently answer
        to whichever ID happened to be sitting at that state location.
        """
        state_file = os.path.join(tmp_dir, "shared.json")

        first = self._provider_sharing(state_file)
        first._save_state()

        second = self._provider_sharing(state_file, provider_id="explicit-id")

        assert second.provider_id == "explicit-id"
        # And it declines the other provider's state rather than adopting it.
        assert second.job_map == {}

    def test_a_fresh_path_leaves_the_generated_id_alone(self, tmp_dir):
        """With nothing persisted there is no ID to adopt and no state to load."""
        provider = self._provider_sharing(os.path.join(tmp_dir, "empty.json"))

        assert provider.provider_id
        assert provider.job_map == {}

    def test_shutdown_survives_a_mode_without_delete_state(self, tmp_dir):
        """A mode that predates delete_state must not break shutdown.

        ``_delete_state`` guards with ``callable(getattr(...))``; without it a
        third-party mode -- or a test double built with ``spec=`` -- would turn
        shutdown into an error path.
        """
        provider, _ = _make_provider(tmp_dir)
        provider.job_map["job-1"] = "resource-1"
        provider._save_state()
        del provider.operating_mode.delete_state

        provider.shutdown()

        assert provider.state_store.load_state(STATE_KEY_PROVIDER) is None


# ---------------------------------------------------------------------------
# TestStandardOnlyOptionGuard
# ---------------------------------------------------------------------------


#: The options StandardMode alone implements, with a non-default value for each.
#: ``_initialize_operating_mode()`` forwards them only on the STANDARD branch --
#: correct -- but the provider acts on them regardless of mode, which is what
#: makes the mismatch leak rather than merely no-op. See #80.
STANDARD_ONLY_OPTIONS = [
    ("warm_pool_size", 2),
    ("warm_pool_ttl", 900),
    ("bake_ami", True),
    ("baked_ami_id", "ami-prebaked01"),
    ("one_shot", True),
]

NON_STANDARD_MODES = ["detached", "serverless"]


def _construct(mode, **extra_kwargs):
    """Construct a provider in *mode*, with AWS and the operating mode mocked.

    Unlike ``_make_provider`` this does not pin ``mode="standard"``, and it
    patches ``initialize`` so construction does not reach AWS. The guard under
    test runs in ``__init__`` before either the state store or the mode is
    built, so nothing here can mask it.
    """
    with (
        patch(
            "parsl_ephemeral_provider.provider.create_session"
        ) as mock_session_factory,
        patch.object(
            EphemeralProvider, "_initialize_state_store", return_value=MagicMock()
        ),
        patch.object(
            EphemeralProvider, "_initialize_operating_mode", return_value=MagicMock()
        ),
        patch.object(EphemeralProvider, "_load_state", return_value=None),
    ):
        mock_session_factory.return_value = MagicMock()
        return EphemeralProvider(
            region="us-east-1",
            image_id="ami-12345678",
            mode=mode,
            vpc_id="vpc-test00001",
            subnet_id="subnet-test001",
            security_group_id="sg-test00001",
            **extra_kwargs,
        )


def _construct_with_real_mode(mode, **extra_kwargs):
    """Construct a provider whose operating mode is the real class, not a mock.

    ``_construct`` patches ``_initialize_operating_mode`` wholesale, which is
    right for testing the guards but useless for testing *forwarding*: a
    MagicMock accepts any keyword, including ones the real signature would
    reject, so an assertion against it proves nothing about whether the value
    arrives. Here only ``initialize()`` is patched -- it is what reaches AWS --
    so the mode is really constructed with whatever the provider passed.
    """
    with (
        patch(
            "parsl_ephemeral_provider.provider.create_session"
        ) as mock_session_factory,
        patch.object(
            EphemeralProvider, "_initialize_state_store", return_value=MagicMock()
        ),
        patch.object(EphemeralProvider, "_load_state", return_value=None),
        patch("parsl_ephemeral_provider.modes.standard.StandardMode.initialize"),
        patch("parsl_ephemeral_provider.modes.detached.DetachedMode.initialize"),
        patch("parsl_ephemeral_provider.modes.serverless.ServerlessMode.initialize"),
    ):
        mock_session_factory.return_value = MagicMock()
        return EphemeralProvider(
            region="us-east-1",
            image_id="ami-12345678",
            mode=mode,
            vpc_id="vpc-test00001",
            subnet_id="subnet-test001",
            security_group_id="sg-test00001",
            **extra_kwargs,
        )


@pytest.mark.unit
class TestStandardOnlyOptionGuard:
    """StandardMode-only options must be refused on other modes (#80)."""

    @pytest.mark.parametrize("mode", NON_STANDARD_MODES)
    @pytest.mark.parametrize("option,value", STANDARD_ONLY_OPTIONS)
    def test_the_option_is_refused_on_a_non_standard_mode(self, mode, option, value):
        """Each option, on each mode that cannot honour it, must raise.

        Silently ignoring it is what #80 is about: with
        ``mode="detached", warm_pool_size=2`` the provider tagged resources
        ``warm_pool=True``, took the warm-pool branch in ``_cleanup_resources()``,
        and set ``STATUS_WARM`` -- a status no other mode's
        ``get_job_status()`` recognises -- so the instances were never cleaned
        up and leaked with no error at all.
        """
        extra = {option: value}
        # warm_pool_size and one_shot also trip the SSM IAM guard; supply a
        # profile so a pass here cannot be the wrong guard firing.
        extra["iam_instance_profile_arn"] = _FAKE_IAM_ARN

        with pytest.raises(ProviderConfigurationError, match=option):
            _construct(mode, **extra)

    @pytest.mark.parametrize("option,value", STANDARD_ONLY_OPTIONS)
    def test_the_option_is_accepted_on_standard_mode(self, option, value):
        """The same option must still be accepted where it is implemented.

        A guard that rejected these everywhere would pass the test above while
        breaking the feature.
        """
        extra = {option: value, "iam_instance_profile_arn": _FAKE_IAM_ARN}

        provider = _construct("standard", **extra)

        assert getattr(provider, option) == value

    @pytest.mark.parametrize("mode", NON_STANDARD_MODES)
    def test_a_non_standard_mode_with_no_such_option_constructs(self, mode):
        """The guard must not fire on the defaults.

        Every provider passes through it, so a wrong comparison here would
        break detached and serverless mode outright.
        """
        provider = _construct(mode)

        assert provider.mode_type.value == mode

    @pytest.mark.parametrize("mode", NON_STANDARD_MODES)
    def test_explicitly_passing_a_default_is_not_refused(self, mode):
        """Passing the documented default is a no-op, not a misconfiguration.

        The guard compares against the default rather than testing presence, so
        ``warm_pool_size=0`` -- which asks for nothing -- must be allowed.

        The values come from the constants, not literals: the TTL default moved
        600 -> 120 in #86, and a hardcoded 600 here stopped being "the default"
        without ceasing to look like it.
        """
        provider = _construct(
            mode,
            warm_pool_size=DEFAULT_WARM_POOL_SIZE,
            warm_pool_ttl=DEFAULT_WARM_POOL_TTL,
            bake_ami=False,
            baked_ami_id=None,
            one_shot=False,
        )

        assert provider.warm_pool_size == 0

    def test_every_offending_option_is_named_at_once(self):
        """The message must list all of them, not just the first found.

        Reporting one at a time means a caller who set three fixes them one
        construction at a time.
        """
        with pytest.raises(ProviderConfigurationError) as excinfo:
            _construct(
                "detached",
                warm_pool_size=2,
                bake_ami=True,
                one_shot=True,
                iam_instance_profile_arn=_FAKE_IAM_ARN,
            )

        message = str(excinfo.value)
        for option in ("warm_pool_size", "bake_ami", "one_shot"):
            assert option in message

    def test_the_message_names_the_mode_that_was_asked_for(self):
        """Naming the mode is what makes the error actionable."""
        with pytest.raises(ProviderConfigurationError, match="serverless"):
            _construct("serverless", bake_ami=True)

    def test_the_mode_guard_precedes_the_ssm_iam_guard(self):
        """Mode rejection must come first, or the error misdirects.

        ``one_shot=True`` on detached mode with no IAM profile trips two guards.
        The IAM one would tell the caller to set
        ``auto_create_instance_profile`` -- which cannot help, because detached
        mode does not implement one-shot at all.
        """
        with pytest.raises(ProviderConfigurationError, match="one_shot"):
            _construct("detached", one_shot=True)

    def test_a_standard_only_option_does_not_leak_through_kwargs(self):
        """The guard must catch the option itself, not just the named parameter.

        All five are real ``__init__`` parameters today, so this is a
        regression fence: were one ever demoted to ``**kwargs`` it would reach
        the mode silently again.
        """
        with pytest.raises(ProviderConfigurationError, match="warm_pool_size"):
            _construct(
                "detached", warm_pool_size=1, iam_instance_profile_arn=_FAKE_IAM_ARN
            )


# ---------------------------------------------------------------------------
# TestModeSpecificOptionForwarding (#136)
# ---------------------------------------------------------------------------

#: Options only DetachedMode implements, with a non-default value for each.
DETACHED_ONLY_OPTIONS = [
    ("idle_timeout", 5),
    ("preserve_bastion", False),
    ("bastion_host_type", "direct"),
    ("workflow_id", "wf-136"),
]

#: Options only ServerlessMode implements, with a non-default value for each.
SERVERLESS_ONLY_OPTIONS = [
    ("lambda_runtime", "python3.11"),
    ("ecs_task_cpu", 256),
    ("ecs_task_memory", 512),
    ("ecs_container_image", "myrepo/myimage:latest"),
]


@pytest.mark.unit
class TestModeSpecificOptionForwarding:
    """The eight mode options the provider never forwarded (#136).

    Both modes accepted all eight and read them, but
    ``_initialize_operating_mode()`` passed none of them, so the mode defaults
    always won. Since #105 rejects unknown kwargs these were not merely ignored
    but unreachable: there was no way to set them through the public API at all.

    ``ecs_container_image`` is the one with teeth. Every Fargate task ran the
    same fixed image, so serverless mode could not run a workload with its own
    dependencies -- the usual reason to pick Fargate over Lambda.
    """

    @pytest.mark.parametrize("option,value", DETACHED_ONLY_OPTIONS)
    def test_a_detached_option_is_accepted_and_stored(self, option, value):
        """The provider must accept it: before #136 this raised."""
        provider = _construct("detached", **{option: value})

        assert getattr(provider, option) == value

    @pytest.mark.parametrize("option,value", SERVERLESS_ONLY_OPTIONS)
    def test_a_serverless_option_is_accepted_and_stored(self, option, value):
        """The provider must accept it: before #136 this raised."""
        provider = _construct("serverless", **{option: value})

        assert getattr(provider, option) == value

    @pytest.mark.parametrize("option,value", DETACHED_ONLY_OPTIONS)
    def test_a_detached_option_reaches_the_mode(self, option, value):
        """Accepting the option is worthless if it stops at the provider.

        This is the assertion that would have failed before the fix even had the
        parameters existed, because ``_initialize_operating_mode()`` built
        ``DetachedMode`` without them. Constructing the *real* mode rather than a
        MagicMock is the point: a mock records any kwarg, including ones the real
        signature would reject.
        """
        provider = _construct_with_real_mode("detached", **{option: value})

        assert getattr(provider.operating_mode, option) == value

    @pytest.mark.parametrize("option,value", SERVERLESS_ONLY_OPTIONS)
    def test_a_serverless_option_reaches_the_mode(self, option, value):
        """As above, for the serverless branch."""
        provider = _construct_with_real_mode(
            "serverless", compute_type="ecs", **{option: value}
        )

        assert getattr(provider.operating_mode, option) == value

    @pytest.mark.parametrize("option,value", DETACHED_ONLY_OPTIONS)
    @pytest.mark.parametrize("mode", ["standard", "serverless"])
    def test_a_detached_option_is_refused_elsewhere(self, mode, option, value):
        """Set on a mode that never receives it, it must raise rather than no-op.

        ``preserve_bastion`` and ``idle_timeout`` are the two cost controls in
        detached mode, so honouring the default while appearing to accept the
        request is how a caller pays for a bastion they asked to have torn down.

        Matching "supported only by" rather than just the option name is
        deliberate: the unknown-kwarg check raises the same exception type and
        also names the option, so a looser assertion passes on an unfixed
        provider -- for entirely the wrong reason.
        """
        with pytest.raises(ProviderConfigurationError, match="supported only by") as e:
            _construct(mode, **{option: value})

        assert option in str(e.value)

    @pytest.mark.parametrize("option,value", SERVERLESS_ONLY_OPTIONS)
    @pytest.mark.parametrize("mode", ["standard", "detached"])
    def test_a_serverless_option_is_refused_elsewhere(self, mode, option, value):
        """As above, for the serverless-only options."""
        with pytest.raises(ProviderConfigurationError, match="supported only by") as e:
            _construct(mode, **{option: value})

        assert option in str(e.value)

    @pytest.mark.parametrize("mode", ["standard", "detached", "serverless"])
    def test_the_defaults_construct_on_every_mode(self, mode):
        """Every provider passes through both new guards.

        A wrong comparison in either would break two of the three modes
        outright, so this is the cheapest way to catch that.
        """
        assert _construct(mode).mode_type.value == mode

    @pytest.mark.parametrize("mode", ["standard", "detached", "serverless"])
    def test_explicitly_passing_a_default_is_not_refused(self, mode):
        """Passing the documented default asks for nothing, so it must be allowed.

        The values come from the constants rather than literals for the reason
        the constants exist: ``DEFAULT_LAMBDA_RUNTIME`` and
        ``DEFAULT_ECS_CONTAINER_IMAGE`` both moved off python3.9 in this same
        change, and a hardcoded "python3.9" here would have stopped being "the
        default" without ceasing to look like it.
        """
        provider = _construct(
            mode,
            idle_timeout=DEFAULT_BASTION_IDLE_TIMEOUT,
            preserve_bastion=DEFAULT_PRESERVE_BASTION,
            bastion_host_type=DEFAULT_BASTION_HOST_TYPE,
            workflow_id=None,
            lambda_runtime=DEFAULT_LAMBDA_RUNTIME,
            ecs_task_cpu=DEFAULT_ECS_CPU,
            ecs_task_memory=DEFAULT_ECS_MEMORY,
            ecs_container_image=DEFAULT_ECS_CONTAINER_IMAGE,
        )

        assert provider.mode_type.value == mode

    def test_every_offending_option_is_named_at_once(self):
        """A caller who set three should not fix them one construction at a time."""
        with pytest.raises(
            ProviderConfigurationError, match="supported only by"
        ) as excinfo:
            _construct("standard", ecs_task_cpu=256, ecs_task_memory=512)

        message = str(excinfo.value)
        assert "ecs_task_cpu" in message and "ecs_task_memory" in message

    def test_the_message_names_both_modes(self):
        """Naming the owning mode and the requested one is what makes it fixable."""
        with pytest.raises(
            ProviderConfigurationError, match="supported only by"
        ) as excinfo:
            _construct("standard", idle_timeout=5)

        message = str(excinfo.value)
        assert "detached" in message and "standard" in message

    def test_a_workflow_id_of_none_still_gets_a_generated_id(self):
        """Forwarding ``None`` must not defeat the mode's own UUID default.

        ``workflow_id`` is the one of the eight whose default is not a constant:
        ``DetachedMode`` substitutes a fresh UUID. Defaulting it in the provider
        instead would have pinned every workflow to the literal ``None``.
        """
        provider = _construct_with_real_mode("detached")

        assert provider.operating_mode.workflow_id


# ---------------------------------------------------------------------------
# TestSpotInterruptionStatus
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSpotInterruptionStatus:
    """The provider half of the interruption response (#137).

    The mode marks the resource ``STATUS_INTERRUPTED``; these are the three
    things the provider must then do with it. Before #137 an interrupted
    instance went to "shutting-down", which ``EC2_STATUS_MAPPING`` renders
    COMPLETED, so the block reported success and its tasks were dropped.
    """

    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    def test_interrupted_reports_failed_to_parsl(self, tmp_dir):
        """FAILED, not COMPLETED: the block did not finish its work.

        FAILED is what stops the executor dispatching to the block and lets
        Parsl's own ``retries`` re-run the lost tasks. COMPLETED was the bug.
        """
        provider, mode = _make_provider(tmp_dir)
        provider.resources["i-001"] = {
            "job_id": "j-001",
            "status": STATUS_INTERRUPTED,
            "timestamp": time.time(),
        }
        provider.job_map["j-001"] = {
            "resource_id": "i-001",
            "status": STATUS_INTERRUPTED,
        }
        mode.get_job_status.return_value = {"i-001": STATUS_INTERRUPTED}

        statuses = provider.status(["j-001"])

        assert statuses[0].state == JobState.FAILED

    def test_interrupted_is_terminal(self, tmp_dir):
        """Once interrupted, a later poll cannot walk the job back.

        ``status()`` short-circuits terminal states, so a reclaimed instance
        whose EC2 state has since become "terminated" -- which maps to
        COMPLETED -- must not be re-reported as a success.
        """
        provider, mode = _make_provider(tmp_dir)
        provider.resources["i-001"] = {
            "job_id": "j-001",
            "status": STATUS_INTERRUPTED,
            "timestamp": time.time(),
        }
        provider.job_map["j-001"] = {
            "resource_id": "i-001",
            "status": STATUS_INTERRUPTED,
        }
        mode.get_job_status.return_value = {"i-001": "COMPLETED"}

        statuses = provider.status(["j-001"])

        assert statuses[0].state == JobState.FAILED
        assert mode.get_job_status.call_count == 0

    def test_interrupted_resource_is_cleaned_up(self, tmp_dir):
        """It must be terminated like any other terminal state.

        Omitting ``STATUS_INTERRUPTED`` from the cleanup list would trade a
        silently-successful reclaim for a silent cost leak: an instance the
        provider has stopped tracking but never terminated.
        """
        provider, mode = _make_provider(tmp_dir)
        provider.resources["i-001"] = {
            "job_id": "j-001",
            "status": STATUS_INTERRUPTED,
            "timestamp": time.time(),
        }
        provider.job_map["j-001"] = {
            "resource_id": "i-001",
            "status": STATUS_INTERRUPTED,
        }

        provider._cleanup_resources()

        mode.cleanup_resources.assert_called_once_with(["i-001"])
        assert "i-001" not in provider.resources

    def test_an_interrupted_warm_instance_is_not_recycled(self, tmp_dir):
        """AWS is taking the instance, so it must not go back into the pool.

        The warm-pool branch runs before the general one, so this needs its own
        case: recycling a doomed instance would hand the next job an instance
        about to vanish.
        """
        provider, mode = _make_provider(
            tmp_dir,
            warm_pool_size=2,
            iam_instance_profile_arn=_FAKE_IAM_ARN,
        )
        mode._warm_instances = []
        provider.resources["i-001"] = {
            "job_id": "j-001",
            "status": STATUS_INTERRUPTED,
            "warm_pool": True,
            "timestamp": time.time(),
        }
        provider.job_map["j-001"] = {
            "resource_id": "i-001",
            "status": STATUS_INTERRUPTED,
        }

        provider._cleanup_resources()

        mode.cleanup_resources.assert_called_once_with(["i-001"])
        assert "i-001" not in mode._warm_instances
        assert "i-001" not in provider.resources


@pytest.mark.unit
class TestRunningResourcesSurviveCleanup:
    """A RUNNING resource is never reaped by age (#194).

    The removed branch compared ``time.time() - resource["timestamp"]`` against
    ``max_idle_time``, but "timestamp" is stamped once at submit and never
    refreshed. That made it age-since-submission, so any task running longer
    than the limit was terminated mid-flight. These cases pin the absence of
    that branch; each one fails against the old code.
    """

    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    def test_a_long_running_job_is_not_terminated(self, tmp_dir):
        """The regression itself: busy work, far past the limit, left alone."""
        provider, mode = _make_provider(tmp_dir, auto_shutdown=True)
        # Submitted an hour ago and still running -- a perfectly healthy job
        # for any real workload, and 12x the old 300s default.
        provider.resources["i-busy"] = {
            "job_id": "j-busy",
            "status": "RUNNING",
            "timestamp": time.time() - 3600,
        }
        provider.job_map["j-busy"] = {"resource_id": "i-busy", "status": "RUNNING"}

        provider._cleanup_resources()

        mode.cleanup_resources.assert_not_called()
        assert "i-busy" in provider.resources
        assert provider.resources["i-busy"]["status"] == "RUNNING"

    def test_age_does_not_reap_even_at_the_extreme(self, tmp_dir):
        """No threshold survives: a timestamp of 0 is maximally "old"."""
        provider, mode = _make_provider(tmp_dir, auto_shutdown=True)
        provider.resources["i-ancient"] = {
            "job_id": "j-ancient",
            "status": "RUNNING",
            "timestamp": 0,
        }
        provider.job_map["j-ancient"] = {
            "resource_id": "i-ancient",
            "status": "RUNNING",
        }

        provider._cleanup_resources()

        mode.cleanup_resources.assert_not_called()
        assert "i-ancient" in provider.resources

    def test_a_pending_resource_is_also_left_alone(self, tmp_dir):
        """A slow-booting instance must not be reclaimed before it registers."""
        provider, mode = _make_provider(tmp_dir, auto_shutdown=True)
        provider.resources["i-booting"] = {
            "job_id": "j-booting",
            "status": "PENDING",
            "timestamp": time.time() - 3600,
        }
        provider.job_map["j-booting"] = {
            "resource_id": "i-booting",
            "status": "PENDING",
        }

        provider._cleanup_resources()

        mode.cleanup_resources.assert_not_called()
        assert "i-booting" in provider.resources

    def test_terminal_resources_are_still_collected(self, tmp_dir):
        """Removing the age branch must not stop real cleanup.

        Guards against "fixing" the reap by disabling ``_cleanup_resources``
        altogether, which would leak every finished instance's record.
        """
        provider, mode = _make_provider(tmp_dir, auto_shutdown=True)
        for idx, status in enumerate(
            ["COMPLETED", "FAILED", "CANCELED", STATUS_INTERRUPTED]
        ):
            rid = f"i-{idx}"
            provider.resources[rid] = {
                "job_id": f"j-{idx}",
                "status": status,
                "timestamp": time.time(),
            }
            provider.job_map[f"j-{idx}"] = {"resource_id": rid, "status": status}

        provider._cleanup_resources()

        mode.cleanup_resources.assert_called_once()
        assert sorted(mode.cleanup_resources.call_args[0][0]) == [
            "i-0",
            "i-1",
            "i-2",
            "i-3",
        ]
        assert provider.resources == {}

    def test_setting_max_idle_time_warns_that_it_is_ignored(self, tmp_dir):
        """Silently ignoring a tuned value would be the worse failure.

        The option used to terminate running work, so somebody may have raised
        it as a workaround; they need to learn it no longer applies.
        """
        with pytest.warns(DeprecationWarning, match="max_idle_time is deprecated"):
            provider, mode = _make_provider(tmp_dir, max_idle_time=900)

        # Still recorded: it is persisted in the state document and forwarded to
        # every mode, so dropping the attribute would break older state files.
        assert provider.max_idle_time == 900

        provider.resources["i-busy"] = {
            "job_id": "j-busy",
            "status": "RUNNING",
            "timestamp": time.time() - 9000,  # 10x the value that was set
        }
        provider.job_map["j-busy"] = {"resource_id": "i-busy", "status": "RUNNING"}

        provider._cleanup_resources()

        mode.cleanup_resources.assert_not_called()

    def test_leaving_max_idle_time_at_its_default_is_silent(self, tmp_dir):
        """Warning on a value nobody set would be noise on every construction."""
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            provider, _ = _make_provider(tmp_dir)

        assert provider.max_idle_time == DEFAULT_MAX_IDLE_TIME

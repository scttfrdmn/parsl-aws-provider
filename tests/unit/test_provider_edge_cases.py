"""Unit tests for EphemeralAWSProvider edge cases.

Covers issue #49 test gaps, plus #37/#39 configurability assertions.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import os
import tempfile
import time
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
        """warm_pool_size defaults to 0 and warm_pool_ttl to 600."""
        provider, _ = _make_provider(tmp_dir)

        assert provider.warm_pool_size == 0
        assert provider.warm_pool_ttl == 600

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

        from parsl_ephemeral_aws.state.file import FileStateStore
        from parsl_ephemeral_aws.modes.standard import StandardMode

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

        from parsl_ephemeral_aws.state.file import FileStateStore
        from parsl_ephemeral_aws.modes.standard import StandardMode

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

        with patch("parsl_ephemeral_aws.modes.standard.wait_for_resource") as mock_wait:
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

        from parsl_ephemeral_aws.state.file import FileStateStore
        from parsl_ephemeral_aws.modes.standard import StandardMode

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

        def _capture_state(state):
            saved.update(state)

        with patch.object(state_store, "save_state", side_effect=_capture_state):
            mode.save_state()

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

        from parsl_ephemeral_aws.state.file import FileStateStore
        from parsl_ephemeral_aws.modes.standard import StandardMode

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

        from parsl_ephemeral_aws.state.file import FileStateStore
        from parsl_ephemeral_aws.modes.standard import StandardMode

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
        from parsl_ephemeral_aws.modes.standard import StandardMode

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
        from parsl_ephemeral_aws.constants import STATUS_COMPLETED, STATUS_FAILED

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

"""Integration tests for provider lifecycle.

These tests drive ``EphemeralAWSProvider`` through construction, submit, status,
cancel and shutdown against the substrate emulator, with the operating mode
replaced by a double so no instances are launched.

Every test here previously passed ``_test_session=``/``_test_state_store=``,
kwargs that never existed in the package (``git log -S`` finds no commit adding
them) and which #105 turned from silently-ignored into hard errors. They are
replaced by the real seams: ``endpoint_url`` binds the session to the emulator,
and ``_initialize_operating_mode`` is patched the way the unit suite does it.

That mattered more than a kwarg rename. The old tests also assigned
``provider._operating_mode``, an attribute the provider never reads -- it uses
``operating_mode`` -- so the doubles were inert and every assertion about them
was vacuous. Several also asserted an API this provider does not have:
``initialize_blocks()``, ``save_state()``, ``load_state()``, a dict-returning
``status()``, and ``submit(job_id, command)``. The real contract is
``submit(command, tasks_per_node)`` returning a job ID, ``status()`` returning
``List[JobStatus]`` and ``cancel()`` returning ``List[bool]`` (the Parsl
compliance fix), so the assertions are rewritten against that.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from parsl.jobs.states import JobState

from parsl_ephemeral_aws.exceptions import ProviderConfigurationError
from parsl_ephemeral_aws.modes.detached import DetachedMode
from parsl_ephemeral_aws.modes.serverless import ServerlessMode
from parsl_ephemeral_aws.modes.standard import StandardMode
from parsl_ephemeral_aws.provider import EphemeralAWSProvider
from parsl_ephemeral_aws.state.file import FileStateStore
from tests.substrate_support import get_substrate_endpoint, is_substrate_available

# Skip all tests if the substrate emulator is not available
pytestmark = pytest.mark.skipif(
    not is_substrate_available(),
    reason="substrate not available - start with 'make substrate-up'",
)


@contextmanager
def build_provider(mode_spec, state_file, network, **config):
    """Construct a provider whose operating mode is a double, and yield both.

    The mode is substituted through ``_initialize_operating_mode`` rather than by
    assigning the attribute afterwards, because ``__init__`` itself calls
    ``operating_mode.initialize()`` -- a post-hoc swap lets the real mode
    provision infrastructure first, which is what the previous version of these
    tests did.

    ``spec=`` is deliberate: a bare ``MagicMock`` answers to any attribute, so a
    test asserting on a method the mode no longer has would keep passing. The
    state store is real, backed by ``tmp_path``, since the provider round-trips
    ``provider_id`` through it during construction.
    """
    mode = MagicMock(spec=mode_spec)
    mode.submit_job.return_value = "resource-1"
    mode.get_job_status.return_value = {}
    mode.cancel_jobs.return_value = {}
    mode.list_resources.return_value = {}

    provider_id = config.pop("provider_id", f"test-provider-{uuid.uuid4().hex[:8]}")
    store = FileStateStore(file_path=str(state_file), provider_id=provider_id)

    with (
        patch.object(
            EphemeralAWSProvider, "_initialize_state_store", return_value=store
        ),
        patch.object(
            EphemeralAWSProvider, "_initialize_operating_mode", return_value=mode
        ),
    ):
        provider = EphemeralAWSProvider(
            provider_id=provider_id,
            region="us-east-1",
            endpoint_url=get_substrate_endpoint(),
            state_file_path=str(state_file),
            vpc_id=network["vpc_id"],
            subnet_id=network["subnet_id"],
            security_group_id=network["security_group_id"],
            **config,
        )
    try:
        yield provider, mode
    finally:
        provider.shutdown()


@pytest.mark.integration
@pytest.mark.substrate
class TestProviderLifecycle:
    """Integration tests for provider lifecycle."""

    @pytest.fixture
    def state_file(self, tmp_path):
        """Path for the provider's state document, inside the test's sandbox."""
        return tmp_path / f"state-{uuid.uuid4().hex[:8]}.json"

    def test_standard_mode_full_lifecycle(self, substrate_network, state_file):
        """Submit, poll, cancel and shut down in standard mode."""
        with build_provider(
            StandardMode,
            state_file,
            substrate_network,
            instance_type="t3.micro",
            image_id="ami-12345678",
            mode="standard",
            max_blocks=2,
        ) as (provider, mode):
            # __init__ initializes the mode; no separate call exists.
            mode.initialize.assert_called_once()

            job_id = provider.submit("echo hello", tasks_per_node=1)
            assert mode.submit_job.called
            assert job_id in provider.job_map

            mode.get_job_status.return_value = {"resource-1": "RUNNING"}
            statuses = provider.status([job_id])
            assert [s.state for s in statuses] == [JobState.RUNNING]

            mode.cancel_jobs.return_value = {"resource-1": "CANCELED"}
            assert provider.cancel([job_id]) == [True]

        mode.cleanup_infrastructure.assert_called_once()

    def test_detached_mode_full_lifecycle(self, substrate_network, state_file):
        """Detached mode carries a workflow_id and cleans up on shutdown."""
        workflow_id = f"test-workflow-{uuid.uuid4().hex[:8]}"
        with build_provider(
            DetachedMode,
            state_file,
            substrate_network,
            instance_type="t3.micro",
            image_id="ami-12345678",
            mode="detached",
            workflow_id=workflow_id,
            bastion_instance_type="t3.micro",
            max_blocks=2,
        ) as (provider, mode):
            assert provider.workflow_id == workflow_id
            mode.initialize.assert_called_once()

            job_id = provider.submit("echo hello", tasks_per_node=1)
            mode.get_job_status.return_value = {"resource-1": "RUNNING"}
            assert [s.state for s in provider.status([job_id])] == [JobState.RUNNING]

        mode.cleanup_infrastructure.assert_called_once()

    def test_serverless_mode_full_lifecycle(self, substrate_network, state_file):
        """Serverless mode submits without provisioning blocks.

        No network IDs are required for Lambda-only serverless -- functions run in
        the Lambda-managed VPC -- but passing them is harmless and keeps one
        construction helper for all three modes.
        """
        with build_provider(
            ServerlessMode,
            state_file,
            substrate_network,
            mode="serverless",
            max_blocks=10,
            init_blocks=0,
        ) as (provider, mode):
            mode.initialize.assert_called_once()

            job_id = provider.submit("echo hello", tasks_per_node=1)
            mode.get_job_status.return_value = {"resource-1": "RUNNING"}
            assert [s.state for s in provider.status([job_id])] == [JobState.RUNNING]

            # scale_out is Parsl's strategy's job, not the provider's.
            assert provider.scale_out(blocks=1) == []

        mode.cleanup_infrastructure.assert_called_once()

    @pytest.mark.parametrize(
        "config,expected_message",
        [
            ({"mode": "invalid_mode"}, "Invalid operating mode"),
            ({"region": "mars-west-9"}, "Invalid region"),
            ({"state_store_type": "quantum"}, "Invalid state store type"),
            ({"state_store_type": "s3"}, "s3_bucket is required"),
            ({"min_blocks": 5, "max_blocks": 1}, "cannot be less than min_blocks"),
            # A detached-only option on standard mode: refused rather than
            # half-honoured, since the mode would ignore it (#136).
            ({"idle_timeout": 5}, "supported only by mode='detached'"),
            # #105: an unknown kwarg is an error, not silently dropped. This is
            # what retired the _test_session injection these tests used to do.
            ({"nonexistent_option": True}, "Unknown configuration option"),
        ],
    )
    def test_provider_validation(self, substrate_network, config, expected_message):
        """Invalid configuration is refused at construction, with a reason.

        The message is asserted, not just the type: every case below raises
        ``ProviderConfigurationError``, so matching only the class would let a
        config error pass this test for the wrong reason.
        """
        base = {
            "region": "us-east-1",
            "instance_type": "t3.micro",
            "image_id": "ami-12345678",
            "endpoint_url": get_substrate_endpoint(),
            "vpc_id": substrate_network["vpc_id"],
            "subnet_id": substrate_network["subnet_id"],
            "security_group_id": substrate_network["security_group_id"],
        }
        with pytest.raises(ProviderConfigurationError, match=expected_message):
            EphemeralAWSProvider(**{**base, **config})

    def test_network_ids_are_required(self, substrate_network):
        """Omitting the network IDs is refused (#69 removed VPC creation)."""
        with pytest.raises(
            ValueError, match="vpc_id, subnet_id, and security_group_id"
        ):
            EphemeralAWSProvider(
                region="us-east-1",
                instance_type="t3.micro",
                image_id="ami-12345678",
                endpoint_url=get_substrate_endpoint(),
            )

    def test_configuration_defaults(self, substrate_network, state_file):
        """Defaults are what the constants declare, not what a docstring claims."""
        with build_provider(
            StandardMode,
            state_file,
            substrate_network,
            instance_type="t3.micro",
            image_id="ami-12345678",
        ) as (provider, _mode):
            assert provider.mode_type.value == "standard"
            assert provider.max_blocks == 10  # DEFAULT_MAX_BLOCKS
            assert provider.min_blocks == 0
            assert provider.init_blocks == 0
            assert not provider.use_spot
            assert not provider.use_spot_fleet
            assert not provider.spot_interruption_handling
            assert provider.label.startswith("ephemeral-aws")

    def test_state_persistence_configuration(self, substrate_network, state_file):
        """The file state store is wired to the path the caller gave."""
        with build_provider(
            DetachedMode,
            state_file,
            substrate_network,
            instance_type="t3.micro",
            image_id="ami-12345678",
            mode="detached",
            workflow_id=f"test-workflow-{uuid.uuid4().hex[:8]}",
            state_store_type="file",
        ) as (provider, _mode):
            assert provider.state_store_type.value == "file"
            assert provider.state_file_path == str(state_file)

            # submit() persists; the document must land at that path.
            provider.submit("echo hello", tasks_per_node=1)
            assert state_file.exists()

    def test_job_status_mapping(self, substrate_network, state_file):
        """Mode status strings map onto Parsl's JobState enum.

        Asserted through the real ``_STRING_TO_JOB_STATE`` table rather than
        against strings: ``status()`` returns ``List[JobStatus]`` positionally, so
        an unknown status must resolve to ``UNKNOWN`` rather than raise.
        """
        with build_provider(
            StandardMode,
            state_file,
            substrate_network,
            instance_type="t3.micro",
            image_id="ami-12345678",
            max_blocks=5,
        ) as (provider, mode):
            wanted = ["RUNNING", "PENDING", "COMPLETED", "nonsense"]
            job_ids = []
            for i, status in enumerate(wanted):
                mode.submit_job.return_value = f"resource-{i}"
                job_ids.append(provider.submit(f"echo {i}", tasks_per_node=1))

            mode.get_job_status.return_value = {
                f"resource-{i}": status for i, status in enumerate(wanted)
            }

            states = [s.state for s in provider.status(job_ids)]
            assert states == [
                JobState.RUNNING,
                JobState.PENDING,
                JobState.COMPLETED,
                JobState.UNKNOWN,
            ]

            # An ID this provider never issued resolves to UNKNOWN, not KeyError.
            assert provider.status(["never-issued"])[0].state == JobState.UNKNOWN

    def test_provider_tags(self, substrate_network, state_file):
        """Caller tags survive alongside the provider's own defaults."""
        custom_tags = {
            "Project": "TestProject",
            "Environment": "Testing",
            "CostCenter": "R&D-123",
        }
        with build_provider(
            StandardMode,
            state_file,
            substrate_network,
            instance_type="t3.micro",
            image_id="ami-12345678",
            additional_tags=custom_tags,
        ) as (provider, _mode):
            for key, value in custom_tags.items():
                assert provider.additional_tags[key] == value

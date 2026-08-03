"""Integration tests for scaling behaviour against the substrate emulator.

These drive the *real* provider and the *real* operating modes -- nothing about
the scaling path is mocked -- so the assertions are about instances that actually
exist in the emulator, not about calls made to a double.

The previous version of this file could not have tested that. It patched a dozen
methods that do not exist on any mode (``_create_vpc``, ``_create_subnet``,
``_create_security_group``, ``_create_ec2_instance``, ``_delete_ec2_instance``,
``_delete_vpc``, ``_create_ssm_parameter``, ``_create_tags``) and called two more
(``mode._init_blocks()``, ``mode.scale_out()``/``mode.scale_in()``) that live on
the provider, not the mode -- ``grep`` finds none of them in
``parsl_ephemeral_provider/``. ``unittest.mock.patch.object`` raises
``AttributeError`` for a missing attribute, so every one of those was a hard
error rather than a passing test, and the network-creating shape they described
was removed in #69 anyway.

It also shadowed the ``substrate_session`` fixture with a class-scoped one built
from ``get_substrate_session()``, which binds no endpoint: clients made from it
reach real AWS and fail on auth. conftest's fixture wraps ``session.client`` to
inject ``endpoint_url``, which is what makes code under test hit the emulator.

What scaling actually means here:

* ``max_blocks`` is enforced in ``provider.submit()``, which raises
  ``ProviderError`` rather than silently over-provisioning.
* ``provider.scale_in(n)`` cancels up to *n* RUNNING blocks and drops them.
* ``provider.scale_out()`` returns ``[]`` by design -- growing the pool is
  Parsl's strategy's job, and the provider's docstring says so. A test asserting
  otherwise would be asserting a feature that does not exist.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import uuid

import pytest
from parsl.jobs.states import JobState

from parsl_ephemeral_provider.constants import WORKER_TYPE_ECS, WORKER_TYPE_LAMBDA
from parsl_ephemeral_provider.exceptions import ProviderError
from parsl_ephemeral_provider.modes.serverless import ServerlessMode
from parsl_ephemeral_provider.provider import EphemeralProvider
from parsl_ephemeral_provider.state.file import FileStateStore
from tests.substrate_support import get_substrate_endpoint, is_substrate_available

pytestmark = [
    pytest.mark.integration,
    pytest.mark.substrate,
    pytest.mark.skipif(
        not is_substrate_available(),
        reason="substrate not available - start with 'make substrate-up'",
    ),
]


@pytest.fixture
def state_file(tmp_path):
    """Path for the provider's state document, inside the test's sandbox."""
    return tmp_path / f"state-{uuid.uuid4().hex[:8]}.json"


def running_instance_ids(session, provider):
    """Return the live instance IDs this provider owns.

    Scoped by the ``ProviderId`` tag rather than by subnet: every launch here goes
    through the mode's launch template (#85), and substrate does not apply the
    template's network interface -- it places the instance in a subnet of its own
    choosing, so a ``subnet-id`` filter matches nothing even though the launch
    succeeded. The tag is set per launch in ``_create_instance``, so it survives
    that gap and also keeps a concurrent test from inflating the count.

    ``terminated`` is excluded because a torn-down instance lingers in
    ``describe_instances`` for a while.
    """
    ec2 = session.client("ec2")
    reservations = ec2.describe_instances(
        Filters=[
            {"Name": "tag:ProviderId", "Values": [provider.provider_id]},
            {
                "Name": "instance-state-name",
                "Values": ["pending", "running", "stopping", "stopped"],
            },
        ]
    )["Reservations"]
    return [i["InstanceId"] for r in reservations for i in r["Instances"]]


def build_provider(session, state_file, network, **config):
    """Construct a provider with its real operating mode, bound to the emulator.

    ``region`` follows the session rather than being hardcoded. The provider
    builds its own session from ``region``, and substrate partitions resources by
    region exactly as AWS does -- so a fixed ``us-east-1`` here against a
    ``substrate_network`` provisioned in ``AWS_TEST_REGION`` (default
    ``us-west-2``) makes the caller's own security group report
    ``InvalidGroup.NotFound``.
    """
    return EphemeralProvider(
        provider_id=f"test-provider-{uuid.uuid4().hex[:8]}",
        region=session.region_name,
        endpoint_url=get_substrate_endpoint(),
        state_file_path=str(state_file),
        instance_type="t3.micro",
        image_id="ami-12345678",
        vpc_id=network["vpc_id"],
        subnet_id=network["subnet_id"],
        security_group_id=network["security_group_id"],
        **config,
    )


class TestStandardModeScaling:
    """Scaling in standard mode, where each block is an EC2 instance."""

    def test_submit_stops_at_max_blocks(
        self, substrate_session, substrate_network, state_file
    ):
        """The cap is enforced by refusing the submit, not by over-provisioning."""
        provider = build_provider(
            substrate_session, state_file, substrate_network, max_blocks=2, min_blocks=0
        )
        try:
            job_ids = [provider.submit(f"echo {i}", tasks_per_node=1) for i in range(2)]
            assert len(provider.resources) == 2
            assert len(running_instance_ids(substrate_session, provider)) == 2

            with pytest.raises(ProviderError, match="already at max_blocks = 2"):
                provider.submit("echo 3", tasks_per_node=1)

            # The refused submit must not have launched anything.
            assert len(running_instance_ids(substrate_session, provider)) == 2
            assert [s.state for s in provider.status(job_ids)] == [
                JobState.RUNNING,
                JobState.RUNNING,
            ]
        finally:
            provider.shutdown()

    def test_scale_in_releases_running_blocks(
        self, substrate_session, substrate_network, state_file
    ):
        """scale_in cancels the requested number of blocks and forgets them.

        ``status()`` is polled first on purpose: ``scale_in`` selects blocks whose
        recorded status is RUNNING, and a freshly submitted block is still
        PENDING until something asks the mode.
        """
        provider = build_provider(
            substrate_session, state_file, substrate_network, max_blocks=3, min_blocks=0
        )
        try:
            job_ids = [provider.submit(f"echo {i}", tasks_per_node=1) for i in range(3)]
            provider.status(job_ids)

            released = provider.scale_in(2)

            assert len(released) == 2
            assert set(released) <= set(job_ids)
            assert len(provider.resources) == 1
            # The surviving job is the one not released.
            assert set(provider.job_map) == set(job_ids) - set(released)
        finally:
            provider.shutdown()

    def test_scale_in_of_zero_or_fewer_is_a_no_op(
        self, substrate_session, substrate_network, state_file
    ):
        """A non-positive request touches nothing rather than draining the pool."""
        provider = build_provider(
            substrate_session, state_file, substrate_network, max_blocks=2, min_blocks=0
        )
        try:
            job_ids = [provider.submit(f"echo {i}", tasks_per_node=1) for i in range(2)]
            provider.status(job_ids)

            assert provider.scale_in(0) == []
            assert provider.scale_in(-1) == []
            assert len(provider.resources) == 2
        finally:
            provider.shutdown()

    def test_scale_out_is_delegated_to_parsl(
        self, substrate_session, substrate_network, state_file
    ):
        """scale_out reports nothing and provisions nothing.

        This is the provider's documented contract -- Parsl's strategy decides
        when to grow -- so the test pins it rather than treating it as a gap. If
        that ever changes, this failing is the correct signal.
        """
        provider = build_provider(
            substrate_session, state_file, substrate_network, max_blocks=4, min_blocks=0
        )
        try:
            assert provider.scale_out(2) == []

            assert running_instance_ids(substrate_session, provider) == []
            assert provider.resources == {}
        finally:
            provider.shutdown()


class TestDetachedModeScaling:
    """Scaling in detached mode, where a bastion fronts the blocks.

    ``bastion_host_type="direct"`` throughout: the CloudFormation bastion needs
    an endpoint substrate does not serve. No ``key_name`` is passed, which is the
    ordinary configuration -- SSM needs none -- and which used to fail botocore's
    parameter validation before any request was sent (#158).
    """

    def _provider(self, session, state_file, network, **config):
        return build_provider(
            session,
            state_file,
            network,
            mode="detached",
            bastion_host_type="direct",
            bastion_instance_type="t3.micro",
            workflow_id=f"test-workflow-{uuid.uuid4().hex[:8]}",
            preserve_bastion=False,
            **config,
        )

    def test_submit_stops_at_max_blocks(
        self, substrate_session, substrate_network, state_file
    ):
        """The cap applies to worker blocks and leaves the bastion alone."""
        provider = self._provider(
            substrate_session, state_file, substrate_network, max_blocks=2, min_blocks=0
        )
        try:
            bastion_id = provider.operating_mode.bastion_id
            assert bastion_id is not None

            for i in range(2):
                provider.submit(f"echo {i}", tasks_per_node=1)
            assert len(provider.resources) == 2

            with pytest.raises(ProviderError, match="already at max_blocks = 2"):
                provider.submit("echo 3", tasks_per_node=1)

            # The bastion is infrastructure, not a block, so it is not counted
            # against max_blocks and must still be running. It carries the same
            # ProviderId tag as a worker, which is why it shows up here.
            assert bastion_id in running_instance_ids(substrate_session, provider)
        finally:
            provider.shutdown()

    def test_shutdown_removes_the_bastion(
        self, substrate_session, substrate_network, state_file
    ):
        """With preserve_bastion=False the bastion goes with the provider."""
        provider = self._provider(
            substrate_session, state_file, substrate_network, max_blocks=1, min_blocks=0
        )
        bastion_id = provider.operating_mode.bastion_id
        assert bastion_id in running_instance_ids(substrate_session, provider)

        provider.shutdown()

        assert provider.operating_mode.bastion_id is None
        assert bastion_id not in running_instance_ids(substrate_session, provider)


class TestServerlessWorkerSelection:
    """Which backend a serverless block lands on, given its shape.

    Driven through the real ``_select_worker_type`` rather than a patched one. The
    old test patched it with a fixed ``side_effect`` list and then asserted the
    resources matched that list, which tested the mock.
    """

    def _mode(self, substrate_session, network, tmp_path, **config):
        provider_id = f"test-provider-{uuid.uuid4().hex[:8]}"
        return ServerlessMode(
            provider_id=provider_id,
            session=substrate_session,
            state_store=FileStateStore(
                file_path=str(tmp_path / "state.json"), provider_id=provider_id
            ),
            region=substrate_session.region_name,
            vpc_id=network["vpc_id"],
            subnet_id=network["subnet_id"],
            security_group_id=network["security_group_id"],
            **config,
        )

    def test_auto_routes_by_job_shape(
        self, substrate_session, substrate_network, tmp_path
    ):
        """A single-task, short command goes to Lambda; anything else to ECS."""
        mode = self._mode(
            substrate_session, substrate_network, tmp_path, worker_type="auto"
        )
        try:
            mode.initialize()

            assert mode._select_worker_type("echo hello", 1) == WORKER_TYPE_LAMBDA
            # More than one task per node cannot be a Lambda invocation.
            assert mode._select_worker_type("echo hello", 4) == WORKER_TYPE_ECS
            # Nor can a command past Lambda's practical payload size.
            assert mode._select_worker_type("x" * 6000, 1) == WORKER_TYPE_ECS
        finally:
            mode.cleanup_infrastructure()

    @pytest.mark.parametrize("worker_type", [WORKER_TYPE_LAMBDA, WORKER_TYPE_ECS])
    def test_an_explicit_worker_type_is_never_overridden(
        self, substrate_session, substrate_network, tmp_path, worker_type
    ):
        """Only ``auto`` inspects the job; a named backend is honoured verbatim."""
        mode = self._mode(
            substrate_session, substrate_network, tmp_path, worker_type=worker_type
        )
        try:
            mode.initialize()

            # The same two jobs that ``auto`` routes differently above.
            assert mode._select_worker_type("echo hello", 1) == worker_type
            assert mode._select_worker_type("x" * 6000, 8) == worker_type
        finally:
            mode.cleanup_infrastructure()

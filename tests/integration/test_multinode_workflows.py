"""Integration tests for what ``nodes_per_block`` actually does.

The value has exactly one effect in this package: it becomes an EC2 Fleet's
``TotalTargetCapacity``. Every other launch path ignores it and launches one
instance per block. ``docs/examples.md:562`` says so under "Not supported", and
``docs/spot_fleet.md:79`` repeats it -- so these tests pin the contract from both
sides, asserting that the single-instance paths *ignore* the value rather than
leaving that half untested.

There is no MPI support to test. The previous version of this file was written
against a design that does not exist: it passed ``launcher=MpiExecLauncher()`` to
modes that accept no such argument, then asserted ``mpirun`` had been spliced
into the submitted command by launcher wiring the package has never had --
``grep -rn launcher parsl_ephemeral_provider/`` finds nothing. Since #105 the provider
*rejects* unknown kwargs rather than absorbing them, so that construction now
raises, which is asserted below as the honest answer to "how do I run MPI here".

It also patched twelve methods that exist on no mode -- ``_create_vpc``,
``_create_subnet``, ``_create_security_group``, ``_create_ec2_instance``,
``_create_ec2_instances_as_block``, ``_create_ec2_instances_as_block_impl``,
``_create_bastion_host``, ``_create_ssm_parameter``, ``_create_tags``,
``_delete_ec2_instance``, ``_delete_subnet``, ``_delete_vpc`` -- and
``patch.object`` raises ``AttributeError`` on a missing attribute, so all four
tests were hard errors and none of their assertions ever ran. Several were also
wrong on their face: no resource record has an ``instances`` list, there is no
``provider.initialize_blocks()``, and ``provider.status()`` returns
``List[JobStatus]`` rather than a job-id-keyed dict.

"Multi-node" here therefore means "a block holding more than one instance", which
is a fleet. Everything runs against the emulator with nothing on the modes
stubbed.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import json
import uuid

import pytest

from parsl_ephemeral_provider.constants import (
    RESOURCE_TYPE_EC2,
    RESOURCE_TYPE_SPOT_FLEET,
    STATUS_CANCELED,
    WORKER_TYPE_ECS,
)
from parsl_ephemeral_provider.exceptions import ProviderConfigurationError
from parsl_ephemeral_provider.modes.detached import DetachedMode
from parsl_ephemeral_provider.modes.serverless import ServerlessMode
from parsl_ephemeral_provider.modes.standard import StandardMode
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

#: Large enough that "one per block" and "one per node" cannot be confused, small
#: enough to stay quick against the emulator.
NODES = 4

MPI_COMMAND = "mpirun -n 16 -ppn 4 python /path/to/mpi_script.py"


def job_name():
    """A job name unique to this run.

    Not cosmetic: ``ServerlessMode._create_job_fleet`` derives its launch
    template name from the job ID, and a name that repeats across runs finds the
    previous run's template still in the emulator. The mode then tries to add a
    version to it, and substrate serves no ``CreateLaunchTemplateVersion`` --
    so a fixed name passes once and fails every time after, which is the worst
    kind of test.
    """
    return f"mpi-job-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def state_store(tmp_path):
    """A real file-backed state store inside the test's sandbox.

    Real rather than mocked: the fleet paths persist their block map through
    ``spot_fleet_state``, and a mock hides a serialization error behind a
    recorded call. It also keeps ``state/file.py``'s ``fcntl.flock`` on a genuine
    descriptor, which a MagicMock handle cannot supply.
    """
    provider_id = f"test-provider-{uuid.uuid4().hex[:8]}"
    return FileStateStore(
        file_path=str(tmp_path / f"state-{provider_id}.json"), provider_id=provider_id
    )


def _standard(session, state_store, network, **overrides):
    """A StandardMode bound to the emulator, using the caller's network."""
    kwargs = dict(
        provider_id=f"test-provider-{uuid.uuid4().hex[:8]}",
        session=session,
        state_store=state_store,
        region=session.region_name,
        instance_type="t3.micro",
        image_id="ami-12345678",
        vpc_id=network["vpc_id"],
        subnet_id=network["subnet_id"],
        security_group_id=network["security_group_id"],
    )
    kwargs.update(overrides)
    return StandardMode(**kwargs)


def _fleet_mode(session, state_store, network, **overrides):
    """A StandardMode whose blocks are EC2 Fleets -- the only multi-node path."""
    kwargs = dict(
        use_spot_fleet=True,
        instance_types=["t3.micro", "t3.small"],
        nodes_per_block=NODES,
    )
    kwargs.update(overrides)
    return _standard(session, state_store, network, **kwargs)


def instance_states(session, instance_ids):
    """Return the set of EC2 state names covering *instance_ids*."""
    described = session.client("ec2").describe_instances(InstanceIds=list(instance_ids))
    return {
        instance["State"]["Name"]
        for reservation in described["Reservations"]
        for instance in reservation["Instances"]
    }


def live_instance_ids(session, tag, value):
    """Live instance IDs carrying ``tag=value``.

    Scoped by tag rather than by subnet, as in ``test_autoscaling_workflows.py``:
    substrate does not apply the launch template's network interface, so a
    ``subnet-id`` filter matches nothing even for a launch that succeeded.

    Which tag depends on the path, and they do not overlap: ``_create_instance``
    sets ``ProviderId``, while ``SpotFleetManager`` sets ``WorkflowId`` and
    ``BlockId`` (``compute/spot_fleet.py``) and ``ServerlessMode``'s own fleet
    sets ``ParslWorkflowId``. Callers name the one they mean rather than have this
    helper guess, so a rename on one path cannot silently make the count zero
    here and still pass.

    ``terminated`` and ``shutting-down`` are excluded: a torn-down instance
    lingers in ``describe_instances`` for a while.
    """
    reservations = session.client("ec2").describe_instances(
        Filters=[
            {"Name": f"tag:{tag}", "Values": [value]},
            {
                "Name": "instance-state-name",
                "Values": ["pending", "running", "stopping", "stopped"],
            },
        ]
    )["Reservations"]
    return [
        instance["InstanceId"]
        for reservation in reservations
        for instance in reservation["Instances"]
    ]


class TestFleetTargetCapacity:
    """The one path where ``nodes_per_block`` is honoured."""

    def test_nodes_per_block_becomes_the_fleets_target_capacity(
        self, substrate_session, substrate_network, state_store
    ):
        """One block is one fleet holding ``nodes_per_block`` instances.

        Asserted against EC2 as well as against the block map, because the two
        can disagree: substrate honours ``TotalTargetCapacity`` (substrate#387),
        whereas moto launches exactly one instance no matter what is requested,
        which is why this cannot be covered there at all.
        """
        mode = _fleet_mode(substrate_session, state_store, substrate_network)
        try:
            mode.initialize()
            block_id = mode.submit_job(job_name(), MPI_COMMAND, NODES)

            block = mode.spot_fleet_manager.blocks[block_id]
            assert len(block["instance_ids"]) == NODES

            capacity = substrate_session.client("ec2").describe_fleets(
                FleetIds=[block["fleet_request_id"]]
            )["Fleets"][0]["TargetCapacitySpecification"]
            assert capacity["TotalTargetCapacity"] == NODES
            assert capacity["DefaultTargetCapacityType"] == "spot"

            # Every node exists, not just every ID: a fleet that only partially
            # filled would still report the capacity that was requested.
            live = live_instance_ids(substrate_session, "BlockId", block_id)
            assert sorted(live) == sorted(block["instance_ids"])
        finally:
            mode.cleanup_infrastructure()

    def test_a_multi_node_block_is_tracked_as_one_resource(
        self, substrate_session, substrate_network, state_store
    ):
        """One resource per block, keyed by block ID -- not one per node.

        This is the shape the old tests got wrong: they expected
        ``resources[block]["instances"]`` to hold a list of per-node dicts. The
        node IDs live on the manager's block map instead, and reach the state
        document through ``spot_fleet_state`` rather than through ``resources``.
        A provider resumed from state needs them from there, so the round-trip is
        asserted too.
        """
        mode = _fleet_mode(substrate_session, state_store, substrate_network)
        try:
            mode.initialize()
            block_id = mode.submit_job(job_name(), MPI_COMMAND, NODES)

            assert list(mode.resources) == [block_id]
            record = mode.resources[block_id]
            assert record["type"] == RESOURCE_TYPE_SPOT_FLEET
            assert record["fleet_request_id"].startswith("fleet-")
            assert "instances" not in record

            with open(state_store.file_path) as handle:
                saved = json.load(handle)["_states"]["mode"]
            saved_block = saved["spot_fleet_state"]["blocks"][block_id]
            assert len(saved_block["instance_ids"]) == NODES
        finally:
            mode.cleanup_infrastructure()

    def test_the_fleet_manager_inherits_the_modes_session(
        self, substrate_session, substrate_network, state_store
    ):
        """The manager must not build its own session from the environment.

        ``resolve_manager_session()`` prefers ``provider.session`` and falls back
        to ambient environment credentials only when there is none (#117). The
        stand-in ``StandardMode`` builds for the manager carried no ``session``,
        so the fallback always fired: every fleet went to the default account for
        the region while the rest of the mode used the session the caller
        configured. The endpoint is the visible symptom here; against real AWS it
        would be the account, which is why this is asserted rather than left to
        the fleet tests above -- they would keep passing while the fleet was
        created somewhere else entirely.
        """
        mode = _fleet_mode(substrate_session, state_store, substrate_network)

        assert mode.spot_fleet_manager.aws_session is substrate_session
        assert (
            mode.spot_fleet_manager.ec2_client.meta.endpoint_url
            == get_substrate_endpoint()
        )

    def test_the_price_cap_is_fleet_wide_so_it_scales_with_capacity(
        self, substrate_session, substrate_network, state_store
    ):
        """``MaxTotalPrice`` covers the whole fleet, unlike the legacy ``SpotPrice``.

        The legacy per-instance-hour ``SpotPrice`` needed no scaling; sending that
        same number as a fleet-wide maximum would cap a 4-node fleet at one node's
        budget and it would never fill. Asserted as a ratio rather than an
        absolute because the figure derives from live spot-price history.

        Read off the manager rather than off the created fleet: substrate accepts
        ``SpotOptions`` but omits it from ``describe_fleets``, so the value is not
        observable on the fleet itself.
        """
        mode = _fleet_mode(
            substrate_session,
            state_store,
            substrate_network,
            spot_max_price_percentage=50,
        )
        resolve = mode.spot_fleet_manager._resolve_max_total_price

        single = float(resolve(1))
        assert single > 0
        assert float(resolve(NODES)) == pytest.approx(single * NODES)

    def test_no_cap_is_sent_when_none_was_asked_for(
        self, substrate_session, substrate_network, state_store
    ):
        """The default, and the configuration AWS recommends: no maximum at all."""
        mode = _fleet_mode(substrate_session, state_store, substrate_network)

        assert mode.spot_fleet_manager._resolve_max_total_price(NODES) is None


class TestSingleInstancePathsIgnoreNodesPerBlock:
    """The other half of the contract, and the half that surprises people."""

    def test_standard_mode_launches_one_instance_per_block(
        self, substrate_session, substrate_network, state_store
    ):
        """``nodes_per_block=4`` on the on-demand path still launches one.

        Not a defect to fix here -- ``_create_instance`` hardcodes
        ``MinCount=1, MaxCount=1`` and ``docs/examples.md`` documents the
        limitation -- but worth a test that fails the day someone wires the value
        into those counts without also teaching ``get_job_status`` and
        ``cleanup_resources`` about a multi-instance record.
        """
        mode = _standard(
            substrate_session, state_store, substrate_network, nodes_per_block=NODES
        )
        try:
            mode.initialize()
            instance_id = mode.submit_job(job_name(), MPI_COMMAND, NODES)

            assert list(mode.resources) == [instance_id]
            assert mode.resources[instance_id]["type"] == RESOURCE_TYPE_EC2
            assert live_instance_ids(
                substrate_session, "ProviderId", mode.provider_id
            ) == [instance_id]
        finally:
            mode.cleanup_infrastructure()

    def test_detached_mode_delegates_the_value_rather_than_acting_on_it(
        self, substrate_session, substrate_network, state_store
    ):
        """The value is written into the work order; the bastion acts on it.

        ``DetachedMode.submit_job`` launches no workers -- it writes a JSON work
        order to Parameter Store and returns. The bastion manager script polls
        that path and calls ``launch_spot_fleet()``, which reads
        ``nodes_per_block`` as the fleet's target capacity
        (``modes/detached.py:965``). So the mode's whole obligation is to *carry*
        the value, and the only instance in the account after a submit is the
        bastion -- both halves asserted, since a driver that quietly launched
        workers too would double the bill.

        ``bastion_host_type="direct"``: the default CloudFormation path needs a
        service substrate does not emulate, and the real stack is covered in
        ``tests/aws/``.
        """
        workflow_id = f"test-workflow-{uuid.uuid4().hex[:8]}"
        mode = DetachedMode(
            provider_id=f"test-provider-{uuid.uuid4().hex[:8]}",
            session=substrate_session,
            state_store=state_store,
            region=substrate_session.region_name,
            workflow_id=workflow_id,
            bastion_host_type="direct",
            bastion_instance_type="t3.micro",
            preserve_bastion=False,
            image_id="ami-12345678",
            instance_type="c5.large",
            nodes_per_block=NODES,
            use_spot_fleet=True,
            instance_types=["c5.large", "c5.xlarge"],
            vpc_id=substrate_network["vpc_id"],
            subnet_id=substrate_network["subnet_id"],
            security_group_id=substrate_network["security_group_id"],
        )
        try:
            mode.initialize()
            job_id = job_name()
            resource_id = mode.submit_job(job_id, MPI_COMMAND, NODES)

            work_order = json.loads(
                substrate_session.client("ssm").get_parameter(
                    Name=f"/parsl/workflows/{workflow_id}/jobs/{job_id}"
                )["Parameter"]["Value"]
            )
            assert work_order["nodes_per_block"] == NODES
            assert work_order["use_spot_fleet"] is True
            assert work_order["instance_types"] == ["c5.large", "c5.xlarge"]

            assert mode.resources[resource_id]["type"] == RESOURCE_TYPE_EC2
            assert live_instance_ids(
                substrate_session, "ProviderId", mode.provider_id
            ) == [mode.bastion_id]
        finally:
            mode.cleanup_infrastructure()


class TestServerlessFleetCapacity:
    """The serverless mode carries its own copy of the capacity semantics."""

    def test_the_ecs_fleet_path_honours_nodes_per_block(
        self, substrate_session, substrate_network, state_store
    ):
        """``use_spot_fleet`` on the ECS worker type trades Fargate for a fleet.

        A second, independent implementation of the same behaviour --
        ``ServerlessMode._create_job_fleet`` builds its own launch template and
        calls ``create_ec2_fleet`` directly instead of going through
        ``SpotFleetManager`` -- so it needs its own coverage. The instance IDs
        land on the resource record here, not on a block map, and there is no
        stack: ``get_job_status`` and ``cleanup_resources`` both branch on
        ``stack_name``, so recording one would send them down the CloudFormation
        path for a fleet that has none.
        """
        mode = ServerlessMode(
            provider_id=f"test-provider-{uuid.uuid4().hex[:8]}",
            session=substrate_session,
            state_store=state_store,
            region=substrate_session.region_name,
            worker_type=WORKER_TYPE_ECS,
            image_id="ami-12345678",
            instance_types=["t3.micro", "t3.small"],
            nodes_per_block=NODES,
            use_spot_fleet=True,
            vpc_id=substrate_network["vpc_id"],
            subnet_id=substrate_network["subnet_id"],
            security_group_id=substrate_network["security_group_id"],
        )
        try:
            mode.initialize()
            resource_id = mode.submit_job(job_name(), MPI_COMMAND, NODES)

            record = mode.resources[resource_id]
            assert record["resource_type"] == RESOURCE_TYPE_SPOT_FLEET
            assert len(record["instance_ids"]) == NODES
            assert record["use_spot_fleet"] is True
            assert "stack_name" not in record

            capacity = substrate_session.client("ec2").describe_fleets(
                FleetIds=[record["fleet_request_id"]]
            )["Fleets"][0]["TargetCapacitySpecification"]
            assert capacity["TotalTargetCapacity"] == NODES
        finally:
            mode.cleanup_infrastructure()


class TestThroughTheProvider:
    """The same behaviour, reached the way a Parsl config reaches it."""

    def test_nodes_per_block_reaches_the_fleet_through_the_provider(
        self, substrate_session, substrate_network, tmp_path
    ):
        """Three hops, each of which has been broken before.

        The provider's ``common_params`` (the fleet path was unreachable through
        the provider at all until #105), the mode's constructor, and the stand-in
        the mode builds for the manager. Only the last hop is what the fleet
        actually reads, so the assertion follows the value all the way to the
        instances rather than stopping at ``provider.nodes_per_block``.
        """
        provider = EphemeralProvider(
            provider_id=f"test-provider-{uuid.uuid4().hex[:8]}",
            region=substrate_session.region_name,
            endpoint_url=get_substrate_endpoint(),
            state_file_path=str(tmp_path / "state.json"),
            mode="standard",
            image_id="ami-12345678",
            instance_type="c5.large",
            nodes_per_block=NODES,
            use_spot_fleet=True,
            instance_types=["c5.large", "c5.xlarge"],
            max_blocks=2,
            vpc_id=substrate_network["vpc_id"],
            subnet_id=substrate_network["subnet_id"],
            security_group_id=substrate_network["security_group_id"],
        )
        try:
            job_id = provider.submit(MPI_COMMAND, tasks_per_node=NODES)

            mode = provider.operating_mode
            assert mode.nodes_per_block == NODES
            assert mode.spot_fleet_manager.provider.nodes_per_block == NODES

            block_id = provider.job_map[job_id]["resource_id"]
            block = mode.spot_fleet_manager.blocks[block_id]
            assert len(block["instance_ids"]) == NODES

            # One block, not NODES of them: max_blocks counts blocks, so a
            # provider that conflated the two would refuse the first submit.
            assert len(provider.resources) == 1
        finally:
            provider.shutdown()

    def test_a_launcher_is_not_a_provider_option(self, substrate_network, tmp_path):
        """``launcher`` is a Parsl executor concept this provider does not take.

        The old tests passed ``MpiExecLauncher()`` to both the provider and the
        modes, then asserted the launcher had rewritten the submitted command. No
        launcher wiring exists -- the command goes into UserData or over SSM
        verbatim -- and since #105 an unrecognised kwarg is refused rather than
        dropped. That refusal is the behaviour worth pinning: a config that looks
        like it requests MPI fails loudly instead of running single-node and
        reporting success.
        """
        with pytest.raises(ProviderConfigurationError, match="launcher"):
            EphemeralProvider(
                region="us-west-2",
                endpoint_url=get_substrate_endpoint(),
                state_file_path=str(tmp_path / "state.json"),
                image_id="ami-12345678",
                launcher="MpiExecLauncher",
                vpc_id=substrate_network["vpc_id"],
                subnet_id=substrate_network["subnet_id"],
                security_group_id=substrate_network["security_group_id"],
            )


class TestMultiNodeTeardown:
    """Cancelling one block must reclaim all of its nodes."""

    def test_cancelling_a_block_terminates_every_node(
        self, substrate_session, substrate_network, state_store
    ):
        """A partial teardown leaves billed instances nothing points at.

        Deleting the fleet is what terminates the instances; ``cancel_jobs`` does
        no per-node bookkeeping. So this asserts that the block-level operation
        really covers all ``nodes_per_block`` of them, which is the failure that
        would otherwise show up only on an AWS bill.
        """
        mode = _fleet_mode(substrate_session, state_store, substrate_network)
        try:
            mode.initialize()
            block_id = mode.submit_job("mpi-job", MPI_COMMAND, NODES)
            instance_ids = list(
                mode.spot_fleet_manager.blocks[block_id]["instance_ids"]
            )
            assert len(instance_ids) == NODES

            assert mode.cancel_jobs([block_id]) == {block_id: STATUS_CANCELED}

            assert instance_states(substrate_session, instance_ids) <= {
                "shutting-down",
                "terminated",
            }
        finally:
            mode.cleanup_infrastructure()

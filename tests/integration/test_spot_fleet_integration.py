"""Integration tests for SpotFleetManager against the substrate emulator.

These drive the manager's real ``CreateFleet`` path end to end: it resolves the
network, builds a per-block launch template, issues ``CreateFleet``, reads back
the instances, and deletes the fleet on teardown. Nothing on the manager is
stubbed.

This file used to run on moto and was written against the *legacy*
``RequestSpotFleet`` API that #86 removed -- it asserted ``sfr-``-prefixed
request IDs, read state back with ``describe_spot_fleet_requests``, and checked
that an IAM fleet service role had been created with the
``AmazonEC2SpotFleetTaggingRole`` policy attached. ``CreateFleet`` has no
``IamFleetRole`` member at all, so ``iam_fleet_role_arn`` is now only retained to
clean up a role named by a pre-#86 state document, and nothing sets it. Those
assertions could not pass against the shipping code.

Substrate rather than moto because substrate grew a ``CreateFleet`` handler in
its 0.82.0 (substrate#387) and models the parts that matter here, where moto does
not:

* **Target capacity is honoured.** A request for ``nodes_per_block`` instances
  yields that many. moto's ``create_fleet`` launches exactly one instance
  regardless of ``TotalTargetCapacity`` -- verified -- so a multi-node fleet was
  indistinguishable from a single-instance one and the one path where
  ``nodes_per_block`` has any effect could not be tested.
* **``Lifecycle`` reflects the request.** A spot fleet reports ``spot``; moto
  reports ``on-demand`` for the same request.
* **The override's ``SubnetId`` is applied**, so instances are findable by
  subnet.

``RequestSpotFleet`` and ``RequestSpotInstances`` remain unimplemented in
substrate and are not needed: #86 moved this provider onto ``CreateFleet``, so
nothing calls them.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

from types import SimpleNamespace

import pytest

from parsl_ephemeral_provider.compute.spot_fleet import SpotFleetManager
from parsl_ephemeral_provider.constants import STATUS_CANCELLED, STATUS_RUNNING
from parsl_ephemeral_provider.exceptions import ResourceCreationError
from tests.substrate_support import is_substrate_available

pytestmark = [
    pytest.mark.integration,
    pytest.mark.substrate,
    pytest.mark.skipif(
        not is_substrate_available(),
        reason="substrate not available - start with 'make substrate-up'",
    ),
]


@pytest.fixture
def manager(substrate_session, substrate_network):
    """A SpotFleetManager whose clients are bound to the emulator.

    The manager builds its own session in ``__init__`` from the provider's region
    and credentials, which would reach real AWS, so ``aws_session`` and
    ``ec2_client`` are replaced with the fixture's endpoint-bound pair
    afterwards. Every fleet this creates is deleted on teardown.
    """
    manager = SpotFleetManager(_provider(substrate_session, substrate_network))
    manager.aws_session = substrate_session
    manager.ec2_client = substrate_session.client("ec2")
    try:
        yield manager
    finally:
        for block_id in list(manager.blocks):
            try:
                manager.terminate_block(block_id)
            except Exception:  # noqa: BLE001 - teardown must not mask a failure
                pass


def _provider(session, network, **overrides):
    """Build the object the manager reads its configuration from.

    A ``SimpleNamespace``, not a ``MagicMock``. ``_setup_security_config()`` reads
    ``vpc_cidr``/``security_environment``/``admin_cidr_blocks``/
    ``strict_security_mode`` with ``getattr(..., <default>)``, and a MagicMock
    answers all four -- so the defaults never apply and ``SecurityConfig`` rejects
    the mock with ``ValueError: Invalid VPC CIDR: <MagicMock ...>`` before the
    constructor returns.
    """
    attrs = {
        "workflow_id": "test-workflow-id",
        "region": session.region_name,
        "aws_access_key_id": "substrate-test",
        "aws_secret_access_key": "substrate-test-secret",  # nosec B106
        "aws_session_token": None,
        "aws_profile": None,
        "vpc_id": network["vpc_id"],
        "subnet_id": network["subnet_id"],
        "security_group_id": network["security_group_id"],
        "image_id": "ami-12345678",
        "instance_type": "t3.micro",
        "instance_types": [],
        # Required: each launch-template override is built inside a blanket
        # `except Exception`, so a missing attribute here yields zero overrides
        # and the fleet goes out empty rather than failing loudly.
        "key_name": None,
        "use_public_ips": True,
        "nodes_per_block": 1,
        "tags": {"ProjectTag": "TestProject"},
        "spot_max_price_percentage": 100,
        "worker_init": "echo 'Worker init script'",
        "iam_instance_profile_arn": None,
    }
    attrs.update(overrides)
    return SimpleNamespace(**attrs)


class TestNetworkResolution:
    """What the manager does with the network IDs it is handed."""

    def test_the_callers_ids_are_adopted_verbatim(self, manager, substrate_network):
        """No network is created; the supplied IDs are passed straight through."""
        network = manager._setup_network_resources()

        assert network["vpc_id"] == substrate_network["vpc_id"]
        assert network["subnet_id"] == substrate_network["subnet_id"]
        assert network["security_group_id"] == substrate_network["security_group_id"]

    def test_a_missing_id_is_a_configuration_error(
        self, substrate_session, substrate_network
    ):
        """Since #69 a missing ID fails loudly rather than creating a VPC."""
        manager = SpotFleetManager(
            _provider(substrate_session, substrate_network, subnet_id=None)
        )
        manager.aws_session = substrate_session
        manager.ec2_client = substrate_session.client("ec2")

        with pytest.raises(ResourceCreationError, match="subnet_id"):
            manager._setup_network_resources()


class TestCreateBlocks:
    """Each block becomes one real EC2 Fleet."""

    def test_each_block_becomes_a_fleet_that_exists_in_ec2(self, manager):
        """Two blocks, two fleets, both active and holding an instance."""
        blocks = manager.create_blocks(2)

        assert len(blocks) == 2
        for block in blocks.values():
            # An instant fleet returns its instance IDs synchronously, so the
            # block is RUNNING by the time create_blocks returns rather than
            # sitting at the initial PENDING.
            assert block["status"] == STATUS_RUNNING
            assert "created_at" in block
            # A fleet ID, not the legacy `sfr-` spot-fleet-request ID (#86).
            assert block["fleet_request_id"].startswith("fleet-")
            assert block["instance_ids"]

        fleet_ids = sorted(b["fleet_request_id"] for b in blocks.values())
        fleets = manager.ec2_client.describe_fleets(FleetIds=fleet_ids)["Fleets"]
        assert len(fleets) == 2
        assert {f["FleetState"] for f in fleets} == {"active"}

    def test_nodes_per_block_becomes_the_fleets_target_capacity(
        self, substrate_session, substrate_network
    ):
        """The one path where nodes_per_block has any effect.

        The single-instance launch paths ignore it and launch one instance per
        block -- ``docs/spot_fleet.md`` says so -- so this is the only place the
        value is observable.
        """
        manager = SpotFleetManager(
            _provider(substrate_session, substrate_network, nodes_per_block=3)
        )
        manager.aws_session = substrate_session
        manager.ec2_client = substrate_session.client("ec2")

        try:
            block = next(iter(manager.create_blocks(1).values()))

            assert len(block["instance_ids"]) == 3

            fleet = manager.ec2_client.describe_fleets(
                FleetIds=[block["fleet_request_id"]]
            )["Fleets"][0]
            assert fleet["TargetCapacitySpecification"]["TotalTargetCapacity"] == 3
            assert (
                fleet["TargetCapacitySpecification"]["DefaultTargetCapacityType"]
                == "spot"
            )

            described = manager.ec2_client.describe_instances(
                InstanceIds=block["instance_ids"]
            )["Reservations"]
            launched = [i for r in described for i in r["Instances"]]
            assert len(launched) == 3
            assert {i["SubnetId"] for i in launched} == {substrate_network["subnet_id"]}
        finally:
            for block_id in list(manager.blocks):
                manager.terminate_block(block_id)

    def test_no_iam_fleet_service_role_is_created(self, manager):
        """CreateFleet has no IamFleetRole, so none is fetched or made (#86).

        The attribute survives only so that a workflow resumed from a pre-#86
        state document can still delete the role that document names.
        """
        manager.create_blocks(1)

        assert manager.iam_fleet_role_arn is None


class TestTerminateBlock:
    """Deleting a block's fleet, which is what stops the instances."""

    def test_terminating_a_block_deletes_its_fleet_and_instances(self, manager):
        """Instance termination is not optional for an instant fleet."""
        blocks = manager.create_blocks(1)
        block_id, block = next(iter(blocks.items()))
        fleet_id = block["fleet_request_id"]
        instance_ids = block["instance_ids"]

        manager.terminate_block(block_id)

        assert manager.blocks[block_id]["status"] == STATUS_CANCELLED
        assert manager.fleet_requests[fleet_id]["status"] == STATUS_CANCELLED

        fleet = manager.ec2_client.describe_fleets(FleetIds=[fleet_id])["Fleets"][0]
        assert fleet["FleetState"] in (
            "deleted",
            "deleted_terminating",
            "deleted_running",
        )

        described = manager.ec2_client.describe_instances(InstanceIds=instance_ids)
        states = {
            i["State"]["Name"]
            for r in described["Reservations"]
            for i in r["Instances"]
        }
        assert states <= {"shutting-down", "terminated"}, states

    def test_the_blocks_launch_template_is_reclaimed(self, manager):
        """One template is built per block (#85); it goes with the block."""
        block_id = next(iter(manager.create_blocks(1)))
        # Read before terminating: the entry is popped as it is deleted.
        template_id = manager.launch_templates[block_id]

        manager.terminate_block(block_id)

        remaining = manager.ec2_client.describe_launch_templates()["LaunchTemplates"]
        assert template_id not in [t["LaunchTemplateId"] for t in remaining]

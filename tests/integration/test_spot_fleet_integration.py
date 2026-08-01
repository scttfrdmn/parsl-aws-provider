"""Integration tests for the SpotFleetManager class.

These tests use the moto library to mock AWS services for realistic integration
testing: moto intercepts the HTTP layer, so the manager builds its own boto3
session and issues real API calls against a simulated AWS.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import boto3
import pytest

try:
    # moto 5 replaced the per-service decorators (mock_ec2, mock_iam, ...) with a
    # single mock_aws. Importing the removed names made this whole module
    # uncollectable while moto was installed and working.
    from moto import mock_aws

    MOTO_AVAILABLE = True
except ImportError:
    MOTO_AVAILABLE = False

from parsl_ephemeral_aws.compute.spot_fleet import SpotFleetManager
from parsl_ephemeral_aws.constants import STATUS_CANCELLED, STATUS_RUNNING
from parsl_ephemeral_aws.exceptions import ResourceCreationError


# Skip all tests if moto is not installed
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not MOTO_AVAILABLE, reason="moto not installed"),
]


@mock_aws
class TestSpotFleetManagerIntegration(unittest.TestCase):
    """Integration tests for SpotFleetManager using moto."""

    def setUp(self):
        """Set up test environment."""
        self.ec2_client = boto3.client("ec2", region_name="us-east-1")
        self.iam_client = boto3.client("iam", region_name="us-east-1")

        # Network resources are pre-provisioned by the caller since #69; the
        # manager only ever adopts the IDs it is handed.
        self.vpc_id = self.ec2_client.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"][
            "VpcId"
        ]
        self.subnet_id = self.ec2_client.create_subnet(
            VpcId=self.vpc_id, CidrBlock="10.0.0.0/24"
        )["Subnet"]["SubnetId"]
        self.security_group_id = self.ec2_client.create_security_group(
            GroupName="test-sg", Description="Test security group", VpcId=self.vpc_id
        )["GroupId"]

    def _provider(self, **overrides):
        """Build the provider object the manager reads its configuration from.

        A ``SimpleNamespace``, not a ``MagicMock``. ``_setup_security_config()``
        reads ``vpc_cidr``/``security_environment``/``admin_cidr_blocks``/
        ``strict_security_mode`` with ``getattr(..., <default>)``, and a MagicMock
        answers all four -- so the defaults never apply and ``SecurityConfig``
        rejects the mock with ``ValueError: Invalid VPC CIDR: <MagicMock ...>``
        before the constructor returns.
        """
        attrs = {
            "workflow_id": "test-workflow-id",
            "region": "us-east-1",
            "aws_access_key_id": "testing",
            "aws_secret_access_key": "testing",
            "aws_session_token": None,
            "aws_profile": None,
            "vpc_id": self.vpc_id,
            "subnet_id": self.subnet_id,
            "security_group_id": self.security_group_id,
            "image_id": "ami-12345678",
            "instance_type": "t2.micro",
            "instance_types": [],
            # Required: _create_spot_fleet_request builds each launch spec inside a
            # blanket `except Exception: logger.warning("Skipping instance type")`,
            # so a missing attribute here yields *zero* launch specifications and
            # the fleet request goes out empty rather than failing loudly.
            "key_name": None,
            "use_public_ips": True,
            "nodes_per_block": 1,
            "tags": {"ProjectTag": "TestProject"},
            "spot_max_price_percentage": 100,
            "worker_init": "echo 'Worker init script'",
        }
        attrs.update(overrides)
        return SimpleNamespace(**attrs)

    def test_setup_network_resources_with_existing(self):
        """The caller's VPC, subnet, and security group are adopted verbatim."""
        manager = SpotFleetManager(self._provider())

        network = manager._setup_network_resources()

        self.assertEqual(network["vpc_id"], self.vpc_id)
        self.assertEqual(network["subnet_id"], self.subnet_id)
        self.assertEqual(network["security_group_id"], self.security_group_id)

    def test_setup_network_resources_requires_all_three_ids(self):
        """A missing ID is a configuration error, not a silent create."""
        manager = SpotFleetManager(self._provider(subnet_id=None))

        with self.assertRaises(ResourceCreationError) as ctx:
            manager._setup_network_resources()

        self.assertIn("subnet_id", str(ctx.exception))

    @patch("parsl_ephemeral_aws.compute.spot_fleet.time.sleep")
    def test_create_blocks(self, mock_sleep):
        """Each block becomes a real Spot Fleet request.

        Nothing on the manager is stubbed out here: ``create_blocks`` resolves the
        network, creates the IAM fleet role, and issues ``RequestSpotFleet`` twice
        against moto. Only ``time.sleep`` is patched, because
        ``_get_iam_fleet_role`` waits 10 seconds for IAM propagation.
        """
        manager = SpotFleetManager(self._provider())

        blocks = manager.create_blocks(2)

        self.assertEqual(len(blocks), 2)
        for block_info in blocks.values():
            # A block is recorded PENDING and promoted by
            # _wait_for_fleet_instances, which runs before create_blocks returns;
            # moto reports the fleet active immediately, so RUNNING is reached
            # here rather than the initial PENDING.
            self.assertEqual(block_info["status"], STATUS_RUNNING)
            self.assertIn("created_at", block_info)
            self.assertTrue(block_info["fleet_request_id"].startswith("sfr-"))
            self.assertTrue(block_info["instance_ids"])

        # The requests exist in EC2, not just in the manager's bookkeeping.
        fleet_request_ids = sorted(
            block["fleet_request_id"] for block in blocks.values()
        )
        configs = self.ec2_client.describe_spot_fleet_requests(
            SpotFleetRequestIds=fleet_request_ids
        )["SpotFleetRequestConfigs"]
        self.assertEqual(len(configs), 2)
        for config in configs:
            self.assertEqual(config["SpotFleetRequestState"], "active")

        # And the fleet role was created with the AWS-managed tagging policy
        # attached -- which needs MOTO_IAM_LOAD_MANAGED_POLICIES (set session-wide
        # in tests/conftest.py), or attach_role_policy fails with NoSuchEntity.
        role_name = manager.iam_fleet_role_arn.split("/")[-1]
        attached = self.iam_client.list_attached_role_policies(RoleName=role_name)[
            "AttachedPolicies"
        ]
        self.assertEqual(
            [p["PolicyName"] for p in attached], ["AmazonEC2SpotFleetTaggingRole"]
        )

    @patch("parsl_ephemeral_aws.compute.spot_fleet.time.sleep")
    def test_terminate_block_cancels_the_fleet_request(self, mock_sleep):
        """Terminating a block cancels its fleet request and its instances."""
        manager = SpotFleetManager(self._provider())
        blocks = manager.create_blocks(1)
        block_id, block = next(iter(blocks.items()))
        instance_ids = block["instance_ids"]

        manager.terminate_block(block_id)

        self.assertEqual(manager.blocks[block_id]["status"], STATUS_CANCELLED)
        self.assertEqual(
            manager.fleet_requests[block["fleet_request_id"]]["status"], "cancelled"
        )

        # The manager cancels with TerminateInstances=True, and moto drops the
        # fleet record entirely in that case rather than leaving a cancelled one.
        self.assertEqual(
            self.ec2_client.describe_spot_fleet_requests()["SpotFleetRequestConfigs"],
            [],
        )
        states = {
            instance["State"]["Name"]
            for reservation in self.ec2_client.describe_instances(
                InstanceIds=instance_ids
            )["Reservations"]
            for instance in reservation["Instances"]
        }
        self.assertTrue(states <= {"shutting-down", "terminated"}, states)


if __name__ == "__main__":
    unittest.main()

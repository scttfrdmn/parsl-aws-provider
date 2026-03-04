"""Unit tests for VPC manager and security group manager.

Tests #48 coverage.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import ipaddress
from unittest.mock import MagicMock, patch

import pytest

from parsl_ephemeral_aws.network.vpc import VPCManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider_mock(workflow_id="wf-abc123"):
    """Return a minimal provider mock with the attributes VPCManager reads."""
    provider = MagicMock()
    provider.workflow_id = workflow_id
    provider.region = "us-east-1"
    provider.aws_access_key_id = None
    provider.aws_secret_access_key = None
    provider.aws_session_token = None
    provider.aws_profile = None
    provider.tags = {}
    return provider


def _make_vpc_manager(ec2_client_mock=None):
    """Return a VPCManager wired to a mock EC2 client (no real boto3)."""
    provider = _make_provider_mock()
    with patch("boto3.Session") as mock_session_cls:
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        if ec2_client_mock is None:
            ec2_client_mock = MagicMock()
        mock_session.client.return_value = ec2_client_mock
        mock_session.resource.return_value = MagicMock()
        mgr = VPCManager(provider)
    return mgr, ec2_client_mock


# ---------------------------------------------------------------------------
# TestVPCManagerCreation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestVPCManagerCreation:
    """Tests for VPCManager create_vpc, create_subnet."""

    def test_create_vpc_returns_vpc_id(self):
        """create_vpc() returns the VPC ID from the API response."""
        ec2 = MagicMock()
        ec2.create_vpc.return_value = {"Vpc": {"VpcId": "vpc-111aaa"}}
        mgr, _ = _make_vpc_manager(ec2)

        vpc_id = mgr.create_vpc("10.0.0.0/16")

        assert vpc_id == "vpc-111aaa"
        assert mgr.vpc_id == "vpc-111aaa"

    def test_create_vpc_applies_tags(self):
        """create_vpc() passes TagSpecifications with the workflow_id."""
        ec2 = MagicMock()
        ec2.create_vpc.return_value = {"Vpc": {"VpcId": "vpc-222bbb"}}
        mgr, _ = _make_vpc_manager(ec2)

        mgr.create_vpc("10.0.0.0/16")

        call_kwargs = ec2.create_vpc.call_args[1]
        tag_specs = call_kwargs.get("TagSpecifications", [])
        assert tag_specs, "TagSpecifications should be present"
        # At least one tag spec should reference the vpc resource type
        resource_types = [ts["ResourceType"] for ts in tag_specs]
        assert "vpc" in resource_types

    def test_create_vpc_idempotent_returns_existing(self):
        """create_vpc() returns existing vpc_id without calling the API again."""
        ec2 = MagicMock()
        mgr, _ = _make_vpc_manager(ec2)
        mgr.vpc_id = "vpc-existing"

        result = mgr.create_vpc("10.0.0.0/16")

        assert result == "vpc-existing"
        ec2.create_vpc.assert_not_called()

    def test_create_subnet_returns_subnet_id(self):
        """create_subnet() returns the subnet ID from the API response."""
        ec2 = MagicMock()
        ec2.create_vpc.return_value = {"Vpc": {"VpcId": "vpc-333ccc"}}
        ec2.create_subnet.return_value = {"Subnet": {"SubnetId": "subnet-aaa111"}}
        mgr, _ = _make_vpc_manager(ec2)
        mgr.vpc_id = "vpc-333ccc"  # pre-seed so create_vpc isn't called

        subnet_id = mgr.create_subnet("10.0.1.0/24")

        assert subnet_id == "subnet-aaa111"
        assert "subnet-aaa111" in mgr.subnet_ids

    def test_generate_subnet_cidrs_no_overlap(self):
        """_generate_subnet_cidrs() returns non-overlapping CIDRs within the VPC."""
        mgr, _ = _make_vpc_manager()
        cidrs = mgr._generate_subnet_cidrs("10.0.0.0/16", num_subnets=3)

        assert len(cidrs) == 3
        networks = [ipaddress.ip_network(c) for c in cidrs]
        # Verify each CIDR is a subnet of 10.0.0.0/16
        vpc_net = ipaddress.ip_network("10.0.0.0/16")
        for net in networks:
            assert net.subnet_of(vpc_net), f"{net} is not within {vpc_net}"
        # Verify no two CIDRs overlap
        for i, a in enumerate(networks):
            for b in networks[i + 1 :]:
                assert not a.overlaps(b), f"{a} overlaps {b}"

"""
Unit tests for ECSManager network resource resolution.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from parsl_ephemeral_aws.compute.ecs import ECSManager
from parsl_ephemeral_aws.constants import TAG_MANAGED
from parsl_ephemeral_aws.exceptions import ResourceCreationError


pytestmark = pytest.mark.unit


def _manager(**provider_attrs):
    """Build an ECSManager with only the attributes the method under test reads.

    ``__init__`` pulls in the audit logger and credential manager, neither of
    which is relevant to network resolution, so it is bypassed deliberately.
    """
    attrs = {
        "vpc_id": None,
        "subnet_id": None,
        "subnet_ids": None,
        "security_group_id": None,
        "workflow_id": "test-workflow",
    }
    attrs.update(provider_attrs)

    manager = object.__new__(ECSManager)
    manager.provider = SimpleNamespace(**attrs)
    manager.ec2_client = MagicMock()
    manager.ec2_client.describe_security_groups.return_value = {
        "SecurityGroups": [{"GroupId": "sg-existing"}]
    }
    return manager


class TestECSNetworkResolution:
    """Tests for ``ECSManager._get_or_create_network_resources``."""

    def test_explicit_subnet_id_is_used_without_discovery(self):
        """An explicit subnet_id must be honoured verbatim.

        Regression test for the leftover line that unconditionally
        dereferenced ``subnet_response`` — bound only in the discovery
        branch — raising ``UnboundLocalError`` on every ECS submission once
        subnet_id became a required argument.
        """
        manager = _manager(
            vpc_id="vpc-explicit",
            subnet_id="subnet-explicit",
            security_group_id="sg-explicit",
        )

        result = manager._get_or_create_network_resources()

        assert result["vpc_id"] == "vpc-explicit"
        assert result["subnet_ids"] == ["subnet-explicit"]
        manager.ec2_client.describe_vpcs.assert_not_called()
        manager.ec2_client.describe_subnets.assert_not_called()

    def test_explicit_subnet_ids_list_takes_precedence(self):
        """A ``subnet_ids`` list wins over the singular ``subnet_id``."""
        manager = _manager(
            vpc_id="vpc-explicit",
            subnet_id="subnet-singular",
            subnet_ids=["subnet-a", "subnet-b"],
        )

        result = manager._get_or_create_network_resources()

        assert result["subnet_ids"] == ["subnet-a", "subnet-b"]
        manager.ec2_client.describe_subnets.assert_not_called()

    def test_subnets_discovered_when_not_supplied(self):
        """With no explicit subnet, subnets are discovered from the VPC."""
        manager = _manager(vpc_id="vpc-explicit")
        manager.ec2_client.describe_subnets.return_value = {
            "Subnets": [{"SubnetId": "subnet-found-1"}, {"SubnetId": "subnet-found-2"}]
        }

        result = manager._get_or_create_network_resources()

        assert result["subnet_ids"] == ["subnet-found-1", "subnet-found-2"]
        manager.ec2_client.describe_subnets.assert_called_once()

    def test_no_subnets_in_vpc_raises(self):
        """An empty discovery result is an error, not an empty subnet list."""
        manager = _manager(vpc_id="vpc-empty")
        manager.ec2_client.describe_subnets.return_value = {"Subnets": []}

        with pytest.raises(ResourceCreationError, match="No subnets found"):
            manager._get_or_create_network_resources()

    def test_falls_back_to_default_vpc(self):
        """Without an explicit vpc_id, the account's default VPC is used."""
        manager = _manager()
        manager.ec2_client.describe_vpcs.return_value = {
            "Vpcs": [{"VpcId": "vpc-default"}]
        }
        manager.ec2_client.describe_subnets.return_value = {
            "Subnets": [{"SubnetId": "subnet-default"}]
        }

        result = manager._get_or_create_network_resources()

        assert result["vpc_id"] == "vpc-default"
        assert result["subnet_ids"] == ["subnet-default"]

    def test_no_default_vpc_raises(self):
        """No explicit vpc_id and no default VPC is a configuration error."""
        manager = _manager()
        manager.ec2_client.describe_vpcs.return_value = {"Vpcs": []}

        with pytest.raises(ResourceCreationError, match="No default VPC found"):
            manager._get_or_create_network_resources()

    def test_created_security_group_is_not_reauthorized_for_egress(self):
        """A freshly created security group must not have egress authorized (#110).

        EC2 attaches allow-all-outbound to every new security group, so
        authorizing it again raises ``InvalidPermission.Duplicate`` — which was
        re-raised as ``ResourceCreationError`` and wrapped as
        ``JobSubmissionError``, making this branch impossible to complete.
        """
        manager = _manager(vpc_id="vpc-explicit", subnet_id="subnet-explicit")
        manager.ec2_client.describe_security_groups.return_value = {
            "SecurityGroups": []
        }
        manager.ec2_client.create_security_group.return_value = {"GroupId": "sg-new"}

        result = manager._get_or_create_network_resources()

        assert result["security_group_id"] == "sg-new"
        manager.ec2_client.authorize_security_group_egress.assert_not_called()

        # The provider-managed marker is its own key, never EC2's reserved
        # "Name" (#109).
        tags = manager.ec2_client.create_security_group.call_args.kwargs[
            "TagSpecifications"
        ][0]["Tags"]
        assert {"Key": TAG_MANAGED, "Value": "true"} in tags
        assert not any(tag["Key"] == "Name" for tag in tags)

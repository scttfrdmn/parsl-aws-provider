"""Tests for network security framework.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import pytest

from parsl_aws_provider.security import NetworkSecurityPolicy, SecurityEnvironment
from parsl_aws_provider.security.cidr_manager import CIDRManager, CIDRValidationError
from parsl_aws_provider.config import SecurityConfig

pytestmark = pytest.mark.unit


class TestCIDRManager:
    """Tests for CIDR validation and management."""

    def test_validate_cidr_block_valid(self):
        """Test CIDR validation with valid blocks."""
        manager = CIDRManager()

        assert manager.validate_cidr_block("10.0.0.0/16")
        assert manager.validate_cidr_block("172.16.0.0/12")
        assert manager.validate_cidr_block("192.168.1.0/24")
        assert manager.validate_cidr_block("0.0.0.0/0")  # Valid but prohibited

    def test_validate_cidr_block_invalid(self):
        """Test CIDR validation with invalid blocks."""
        manager = CIDRManager()

        assert not manager.validate_cidr_block("10.0.0.0/33")  # Invalid mask
        assert not manager.validate_cidr_block("256.0.0.0/16")  # Invalid IP
        assert not manager.validate_cidr_block("invalid")  # Not CIDR format

    def test_is_prohibited_cidr(self):
        """Test prohibited CIDR detection."""
        manager = CIDRManager()

        is_prohibited, reason = manager.is_prohibited_cidr("0.0.0.0/0")
        assert is_prohibited
        assert "security risk" in reason.lower()

        is_prohibited, reason = manager.is_prohibited_cidr("10.0.0.0/16")
        assert not is_prohibited
        assert reason is None

    def test_is_private_cidr(self):
        """Test private CIDR detection."""
        manager = CIDRManager()

        assert manager.is_private_cidr("10.0.0.0/16")
        assert manager.is_private_cidr("172.16.0.0/12")
        assert manager.is_private_cidr("192.168.1.0/24")
        assert not manager.is_private_cidr("8.8.8.8/32")  # Public IP

    def test_get_subnet_cidrs(self):
        """Test subnet CIDR generation."""
        manager = CIDRManager()

        # Test generating 4 subnets from /16 VPC
        subnets = manager.get_subnet_cidrs("10.0.0.0/16", 4)
        assert len(subnets) == 4
        assert all(manager.validate_cidr_block(subnet) for subnet in subnets)

        # Test that subnets don't overlap
        for i, subnet1 in enumerate(subnets):
            for j, subnet2 in enumerate(subnets):
                if i != j:
                    assert not manager.is_overlapping_cidr(subnet1, subnet2)

    def test_get_subnet_cidrs_too_many(self):
        """Test error when requesting too many subnets."""
        manager = CIDRManager()

        with pytest.raises(CIDRValidationError, match="Cannot create .* subnets"):
            manager.get_subnet_cidrs("10.0.0.0/30", 10)  # /30 can't fit 10 subnets

    def test_analyze_security_rules(self):
        """An internet-open rule is reported as prohibited, a private one as private.

        ``0.0.0.0/0`` is a key of ``PROHIBITED_CIDRS``, so it is classified as
        *prohibited* -- an error -- and never reaches the ``public_access_rules``
        branch, which tests for the same literal one ``elif`` later and is
        therefore unreachable. This asserted ``public_access_rules == 1``, which
        would only hold if the CIDR were downgraded from an error to a warning.
        """
        manager = CIDRManager()

        rules = [
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
            },
            {
                "IpProtocol": "tcp",
                "FromPort": 80,
                "ToPort": 80,
                "IpRanges": [{"CidrIp": "10.0.0.0/16"}],
            },
        ]

        analysis = manager.analyze_security_rules(rules)

        assert analysis["total_rules"] == 2

        # World-open SSH is an error, not a warning, and names the rule and reason.
        assert len(analysis["prohibited_cidrs"]) == 1
        prohibited = analysis["prohibited_cidrs"][0]
        assert prohibited["cidr"] == "0.0.0.0/0"
        assert prohibited["rule_index"] == 0
        assert prohibited["ports"] == "22-22"
        assert len(analysis["errors"]) == 1
        assert "0.0.0.0/0" in analysis["errors"][0]

        # The VPC-internal rule is fine.
        assert len(analysis["private_rules"]) == 1
        assert analysis["private_rules"][0]["cidr"] == "10.0.0.0/16"

    def test_suggest_secure_alternatives(self):
        """Test secure alternative suggestions."""
        manager = CIDRManager()

        suggestions = manager.suggest_secure_alternatives("0.0.0.0/0")
        assert len(suggestions) > 0
        assert any("10.0.0.0/16" in suggestion for suggestion in suggestions)


class TestNetworkSecurityPolicy:
    """Tests for network security policy."""

    def test_development_policy(self):
        """Test development environment policy."""
        policy = NetworkSecurityPolicy.create_development_policy()

        assert policy.environment == SecurityEnvironment.DEVELOPMENT
        assert not policy.strict_mode
        assert policy.public_access_ports  # Dev allows public ports

    def test_production_policy(self):
        """Test production environment policy."""
        admin_cidrs = ["10.0.0.0/8", "172.16.0.0/12"]
        policy = NetworkSecurityPolicy.create_production_policy(admin_cidrs=admin_cidrs)

        assert policy.environment == SecurityEnvironment.PRODUCTION
        assert policy.strict_mode
        assert not policy.public_access_ports  # Prod blocks public ports
        assert policy.admin_cidr_blocks == admin_cidrs

    def test_strict_mode_validation(self):
        """A prohibited CIDR is rejected at construction, not on later inspection.

        ``__post_init__`` runs ``_validate_configuration()``, which calls
        ``_check_strict_mode_violations()`` in strict mode -- so the constructor
        itself raises and no policy object is ever handed back. This test built
        the policy *outside* ``pytest.raises`` and then called the private
        checker inside it, so the error escaped from the wrong line and the test
        failed even though the package behaves correctly. Rejecting at
        construction is the stronger guarantee: an invalid policy cannot exist.
        """
        with pytest.raises(CIDRValidationError, match="not allowed in strict mode"):
            NetworkSecurityPolicy(
                environment=SecurityEnvironment.PRODUCTION,
                admin_cidr_blocks=["0.0.0.0/0"],  # This should fail
                strict_mode=True,
            )

    def test_strict_mode_allows_scoped_admin_cidrs(self):
        """Strict mode is not a blanket ban -- a scoped admin CIDR is accepted.

        Guards the complement of the case above: the check must reject only the
        prohibited literals, so a production policy with real admin ranges still
        constructs.
        """
        policy = NetworkSecurityPolicy(
            environment=SecurityEnvironment.PRODUCTION,
            admin_cidr_blocks=["10.0.0.0/8"],
            strict_mode=True,
        )

        assert policy.strict_mode
        assert policy.admin_cidr_blocks == ["10.0.0.0/8"]

    def test_get_compute_worker_rules(self):
        """Test compute worker security rules."""
        policy = NetworkSecurityPolicy.create_development_policy()
        rules = policy.get_compute_worker_rules()

        assert len(rules) > 0
        # Should have SSH and Parsl communication rules in dev
        ssh_rules = [r for r in rules if r["FromPort"] == 22]
        parsl_rules = [r for r in rules if r["FromPort"] == 54000]
        assert len(ssh_rules) > 0
        assert len(parsl_rules) > 0

    def test_get_bastion_host_rules(self):
        """Test bastion host security rules."""
        admin_cidrs = ["10.0.0.0/8"]
        policy = NetworkSecurityPolicy(
            environment=SecurityEnvironment.PRODUCTION, admin_cidr_blocks=admin_cidrs
        )

        rules = policy.get_bastion_host_rules()
        assert len(rules) > 0

        # All rules should be from admin networks only
        for rule in rules:
            for ip_range in rule["IpRanges"]:
                assert ip_range["CidrIp"] in admin_cidrs

    def test_validate_security_group_rules(self):
        """Test security rule validation."""
        policy = NetworkSecurityPolicy(
            environment=SecurityEnvironment.PRODUCTION, strict_mode=True
        )

        # Valid rules
        valid_rules = [
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": [{"CidrIp": "10.0.0.0/16"}],
            }
        ]
        assert policy.validate_security_group_rules(valid_rules)

        # Invalid rules (0.0.0.0/0 in strict mode)
        invalid_rules = [
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
            }
        ]
        assert not policy.validate_security_group_rules(invalid_rules)


class TestSecurityConfig:
    """Tests for security configuration."""

    def test_development_config(self):
        """Test development configuration."""
        config = SecurityConfig.create_development_config()

        assert config.environment == SecurityEnvironment.DEVELOPMENT
        assert not config.strict_mode
        assert config.use_security_templates

    def test_production_config(self):
        """Test production configuration."""
        admin_cidrs = ["10.0.0.0/8"]
        config = SecurityConfig.create_production_config(admin_cidrs=admin_cidrs)

        assert config.environment == SecurityEnvironment.PRODUCTION
        assert config.strict_mode
        assert config.admin_cidr_blocks == admin_cidrs

    def test_get_network_security_policy(self):
        """Test network policy creation from config."""
        config = SecurityConfig.create_development_config()
        policy = config.get_network_security_policy()

        assert isinstance(policy, NetworkSecurityPolicy)
        assert policy.environment == config.environment
        assert policy.vpc_cidr == config.vpc_cidr

    def test_get_security_group_rules(self):
        """Test security group rule retrieval."""
        config = SecurityConfig.create_development_config()

        # Test compute worker rules
        compute_rules = config.get_security_group_rules("compute_worker")
        assert len(compute_rules) > 0

        # Test bastion rules
        bastion_rules = config.get_security_group_rules("bastion")
        assert len(bastion_rules) >= 0  # May be empty if no admin CIDRs

    def test_analyze_security_posture(self):
        """Test security posture analysis."""
        config = SecurityConfig.create_development_config()
        config.public_access_ports = [80, 443]

        analysis = config.analyze_security_posture()

        assert "environment" in analysis
        assert "strict_mode" in analysis
        assert "warnings" in analysis
        assert "recommendations" in analysis

    def test_validate_security_rules(self):
        """Test security rule validation."""
        config = SecurityConfig.create_production_config(admin_cidrs=["10.0.0.0/8"])

        # Valid rules
        valid_rules = [
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": [{"CidrIp": "10.0.0.0/8"}],
            }
        ]
        assert config.validate_security_rules(valid_rules)

        # Invalid rules in production (0.0.0.0/0)
        invalid_rules = [
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
            }
        ]
        assert not config.validate_security_rules(invalid_rules)

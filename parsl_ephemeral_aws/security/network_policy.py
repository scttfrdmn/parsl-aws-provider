"""Network security policy engine for Parsl Ephemeral AWS Provider.

This module provides configurable security policies to replace hardcoded 0.0.0.0/0
CIDR blocks with environment-appropriate security rules.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Any

from .cidr_manager import CIDRManager, CIDRValidationError

logger = logging.getLogger(__name__)


class SecurityEnvironment(Enum):
    """Security environment profiles."""

    DEVELOPMENT = "dev"
    STAGING = "staging"
    PRODUCTION = "prod"


@dataclass
class NetworkSecurityPolicy:
    """Network security policy configuration.

    Provides environment-specific security rules to replace hardcoded
    0.0.0.0/0 CIDR blocks with appropriate restrictions.
    """

    # Environment type determines default security posture
    environment: SecurityEnvironment = SecurityEnvironment.DEVELOPMENT

    # Administrative access CIDR blocks (e.g., office networks, VPN)
    admin_cidr_blocks: List[str] = field(default_factory=lambda: ["10.0.0.0/8"])

    # SSH access CIDR blocks (should be restricted in production)
    ssh_allowed_cidrs: List[str] = field(default_factory=list)

    # Parsl communication CIDR blocks (internal cluster communication)
    parsl_communication_cidrs: List[str] = field(default_factory=list)

    # Ports that may have public access (empty by default for security)
    public_access_ports: List[int] = field(default_factory=list)

    # VPC CIDR block for internal communication
    vpc_cidr: str = "10.0.0.0/16"

    # Allow internal VPC communication
    allow_vpc_internal: bool = True

    # Strict mode prevents any 0.0.0.0/0 rules (enabled for production)
    strict_mode: bool = field(default=False)

    def __post_init__(self):
        """Validate and configure policy based on environment."""
        self.cidr_manager = CIDRManager()

        # Set strict mode for production
        if self.environment == SecurityEnvironment.PRODUCTION:
            self.strict_mode = True
            logger.info("Production environment detected - enabling strict mode")

        # Validate all CIDR blocks
        self._validate_configuration()

        # Set environment-specific defaults
        self._set_environment_defaults()

    def _validate_configuration(self) -> None:
        """Validate the security policy configuration."""
        try:
            # Validate VPC CIDR
            if not self.cidr_manager.validate_cidr_block(self.vpc_cidr):
                raise CIDRValidationError(f"Invalid VPC CIDR: {self.vpc_cidr}")

            # Validate admin CIDRs
            for cidr in self.admin_cidr_blocks:
                if not self.cidr_manager.validate_cidr_block(cidr):
                    raise CIDRValidationError(f"Invalid admin CIDR: {cidr}")

            # Validate SSH CIDRs
            for cidr in self.ssh_allowed_cidrs:
                if not self.cidr_manager.validate_cidr_block(cidr):
                    raise CIDRValidationError(f"Invalid SSH CIDR: {cidr}")

            # Validate Parsl communication CIDRs
            for cidr in self.parsl_communication_cidrs:
                if not self.cidr_manager.validate_cidr_block(cidr):
                    raise CIDRValidationError(f"Invalid Parsl CIDR: {cidr}")

            # Check for prohibited CIDRs in strict mode
            if self.strict_mode:
                self._check_strict_mode_violations()

        except Exception as e:
            logger.error(f"Security policy validation failed: {e}")
            raise

    def _check_strict_mode_violations(self) -> None:
        """Check for security violations in strict mode."""
        prohibited_cidrs = ["0.0.0.0/0"]

        all_cidrs = (
            self.admin_cidr_blocks
            + self.ssh_allowed_cidrs
            + self.parsl_communication_cidrs
        )

        for cidr in all_cidrs:
            if cidr in prohibited_cidrs:
                raise CIDRValidationError(
                    f"Prohibited CIDR {cidr} not allowed in strict mode "
                    f"(environment: {self.environment.value})"
                )

    def _set_environment_defaults(self) -> None:
        """Set environment-specific defaults."""
        if self.environment == SecurityEnvironment.DEVELOPMENT:
            # Development: More permissive for testing
            if not self.ssh_allowed_cidrs:
                self.ssh_allowed_cidrs = [
                    "10.0.0.0/8",
                    "172.16.0.0/12",
                    "192.168.0.0/16",
                ]
            if not self.parsl_communication_cidrs:
                self.parsl_communication_cidrs = [self.vpc_cidr]

        elif self.environment == SecurityEnvironment.STAGING:
            # Staging: Moderate security
            if not self.ssh_allowed_cidrs:
                self.ssh_allowed_cidrs = ["10.0.0.0/8"]
            if not self.parsl_communication_cidrs:
                self.parsl_communication_cidrs = [self.vpc_cidr]

        elif self.environment == SecurityEnvironment.PRODUCTION:
            # Production: Strict security - no defaults, must be explicit
            if not self.ssh_allowed_cidrs:
                logger.warning(
                    "No SSH CIDRs configured for production environment - SSH access will be denied"
                )
            if not self.parsl_communication_cidrs:
                self.parsl_communication_cidrs = [self.vpc_cidr]

    def get_ssh_security_group_rules(self) -> List[Dict[str, Any]]:
        """Get security group rules for SSH access."""
        rules = []

        if not self.ssh_allowed_cidrs:
            logger.warning("No SSH CIDRs configured - SSH access denied")
            return rules

        for cidr in self.ssh_allowed_cidrs:
            rules.append(
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "IpRanges": [{"CidrIp": cidr}],
                    "Description": f"SSH access from {cidr}",
                }
            )

        return rules

    def get_parsl_communication_rules(self) -> List[Dict[str, Any]]:
        """Get security group rules for Parsl internal communication."""
        rules = []

        for cidr in self.parsl_communication_cidrs:
            rules.append(
                {
                    "IpProtocol": "tcp",
                    "FromPort": 54000,
                    "ToPort": 55000,
                    "IpRanges": [{"CidrIp": cidr}],
                    "Description": f"Parsl communication from {cidr}",
                }
            )

        return rules

    def get_public_access_rules(self) -> List[Dict[str, Any]]:
        """Get security group rules for public access (use with caution)."""
        rules = []

        if self.strict_mode and self.public_access_ports:
            raise CIDRValidationError("Public access ports not allowed in strict mode")

        for port in self.public_access_ports:
            logger.warning(f"Creating public access rule for port {port}")
            rules.append(
                {
                    "IpProtocol": "tcp",
                    "FromPort": port,
                    "ToPort": port,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                    "Description": f"Public access to port {port} (WARNING: Insecure)",
                }
            )

        return rules

    def get_outbound_rules(self) -> List[Dict[str, Any]]:
        """Get outbound security group rules."""
        # For most use cases, allow all outbound traffic
        # In highly secure environments, this could be restricted
        return [
            {
                "IpProtocol": "-1",
                "FromPort": -1,
                "ToPort": -1,
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                "Description": "Allow all outbound traffic",
            }
        ]

    def get_compute_worker_rules(self) -> List[Dict[str, Any]]:
        """Get security group rules for compute workers."""
        rules = []

        # Add SSH rules
        rules.extend(self.get_ssh_security_group_rules())

        # Add Parsl communication rules
        rules.extend(self.get_parsl_communication_rules())

        # Add DNS resolution rules (restricted to VPC or specific DNS servers)
        if self.allow_vpc_internal:
            rules.extend(
                [
                    {
                        "IpProtocol": "tcp",
                        "FromPort": 53,
                        "ToPort": 53,
                        "IpRanges": [{"CidrIp": self.vpc_cidr}],
                        "Description": "DNS TCP within VPC",
                    },
                    {
                        "IpProtocol": "udp",
                        "FromPort": 53,
                        "ToPort": 53,
                        "IpRanges": [{"CidrIp": self.vpc_cidr}],
                        "Description": "DNS UDP within VPC",
                    },
                ]
            )

        # Add HTTP/HTTPS for package downloads (consider restricting further)
        if self.environment == SecurityEnvironment.DEVELOPMENT:
            rules.extend(
                [
                    {
                        "IpProtocol": "tcp",
                        "FromPort": 80,
                        "ToPort": 80,
                        "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                        "Description": "HTTP for package downloads (DEV only)",
                    },
                    {
                        "IpProtocol": "tcp",
                        "FromPort": 443,
                        "ToPort": 443,
                        "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                        "Description": "HTTPS for package downloads (DEV only)",
                    },
                ]
            )
        else:
            # In staging/prod, consider using VPC endpoints or NAT Gateway
            logger.info(
                "HTTP/HTTPS access restricted - consider VPC endpoints for package downloads"
            )

        return rules

    def get_bastion_host_rules(self) -> List[Dict[str, Any]]:
        """Get security group rules for bastion host."""
        rules = []

        # SSH access from admin networks only
        for cidr in self.admin_cidr_blocks:
            rules.append(
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "IpRanges": [{"CidrIp": cidr}],
                    "Description": f"SSH access from admin network {cidr}",
                }
            )

        return rules

    def validate_security_group_rules(self, rules: List[Dict[str, Any]]) -> bool:
        """Validate security group rules against policy."""
        for rule in rules:
            ip_ranges = rule.get("IpRanges", [])
            for ip_range in ip_ranges:
                cidr = ip_range.get("CidrIp")
                if cidr == "0.0.0.0/0" and self.strict_mode:
                    logger.error(
                        "Security policy violation: 0.0.0.0/0 CIDR not allowed in strict mode"
                    )
                    return False

        return True

    @classmethod
    def create_development_policy(
        cls, vpc_cidr: str = "10.0.0.0/16"
    ) -> "NetworkSecurityPolicy":
        """Create a development environment security policy."""
        return cls(
            environment=SecurityEnvironment.DEVELOPMENT,
            vpc_cidr=vpc_cidr,
            admin_cidr_blocks=["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"],
            public_access_ports=[80, 443],  # Allowed in dev for testing
        )

    @classmethod
    def create_production_policy(
        cls, vpc_cidr: str = "10.0.0.0/16", admin_cidrs: List[str] = None
    ) -> "NetworkSecurityPolicy":
        """Create a production environment security policy."""
        if admin_cidrs is None:
            raise ValueError("admin_cidrs must be specified for production environment")

        return cls(
            environment=SecurityEnvironment.PRODUCTION,
            vpc_cidr=vpc_cidr,
            admin_cidr_blocks=admin_cidrs,
            ssh_allowed_cidrs=admin_cidrs,  # Only admin networks can SSH
            public_access_ports=[],  # No public access by default
            strict_mode=True,
        )

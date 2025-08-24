"""CIDR block validation and management for Parsl Ephemeral AWS Provider.

This module provides utilities for validating and managing CIDR blocks
to ensure secure network configurations.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import ipaddress
import logging
from typing import List, Set, Tuple, Optional

logger = logging.getLogger(__name__)


class CIDRValidationError(Exception):
    """Exception raised for CIDR validation errors."""

    pass


class CIDRManager:
    """Manager for CIDR block validation and security analysis."""

    # Prohibited CIDR blocks that should never be used in production
    PROHIBITED_CIDRS = {
        "0.0.0.0/0": "Global internet access - security risk",
        "::/0": "Global IPv6 access - security risk",
    }

    # RFC 1918 private address ranges
    PRIVATE_CIDRS = {
        "10.0.0.0/8": "Private Class A",
        "172.16.0.0/12": "Private Class B",
        "192.168.0.0/16": "Private Class C",
    }

    # AWS reserved ranges
    AWS_RESERVED_CIDRS = {
        "169.254.0.0/16": "AWS Link-local",
        "100.64.0.0/10": "AWS Carrier-grade NAT",
    }

    def __init__(self):
        """Initialize CIDR manager."""
        self.validation_cache: Set[str] = set()

    def validate_cidr_block(self, cidr: str) -> bool:
        """Validate a CIDR block format.

        Parameters
        ----------
        cidr : str
            CIDR block to validate (e.g., "10.0.0.0/16")

        Returns
        -------
        bool
            True if valid, False otherwise
        """
        if cidr in self.validation_cache:
            return True

        try:
            # Parse CIDR block
            network = ipaddress.ip_network(cidr, strict=False)

            # Cache valid CIDR
            self.validation_cache.add(cidr)

            logger.debug(f"Validated CIDR block: {cidr}")
            return True

        except ipaddress.AddressValueError as e:
            logger.error(f"Invalid CIDR block '{cidr}': {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error validating CIDR '{cidr}': {e}")
            return False

    def is_prohibited_cidr(self, cidr: str) -> Tuple[bool, Optional[str]]:
        """Check if a CIDR block is prohibited.

        Parameters
        ----------
        cidr : str
            CIDR block to check

        Returns
        -------
        Tuple[bool, Optional[str]]
            (is_prohibited, reason)
        """
        if cidr in self.PROHIBITED_CIDRS:
            return True, self.PROHIBITED_CIDRS[cidr]
        return False, None

    def is_private_cidr(self, cidr: str) -> bool:
        """Check if a CIDR block is in private address space.

        Parameters
        ----------
        cidr : str
            CIDR block to check

        Returns
        -------
        bool
            True if in private address space
        """
        try:
            network = ipaddress.ip_network(cidr, strict=False)
            return network.is_private
        except ipaddress.AddressValueError:
            return False

    def is_overlapping_cidr(self, cidr1: str, cidr2: str) -> bool:
        """Check if two CIDR blocks overlap.

        Parameters
        ----------
        cidr1 : str
            First CIDR block
        cidr2 : str
            Second CIDR block

        Returns
        -------
        bool
            True if CIDR blocks overlap
        """
        try:
            net1 = ipaddress.ip_network(cidr1, strict=False)
            net2 = ipaddress.ip_network(cidr2, strict=False)
            return net1.overlaps(net2)
        except ipaddress.AddressValueError:
            return False

    def get_subnet_cidrs(self, vpc_cidr: str, subnet_count: int) -> List[str]:
        """Generate subnet CIDR blocks within a VPC CIDR.

        Parameters
        ----------
        vpc_cidr : str
            VPC CIDR block (e.g., "10.0.0.0/16")
        subnet_count : int
            Number of subnets to create

        Returns
        -------
        List[str]
            List of subnet CIDR blocks

        Raises
        ------
        CIDRValidationError
            If VPC CIDR is invalid or subnet count is too large
        """
        try:
            vpc_network = ipaddress.ip_network(vpc_cidr, strict=False)

            # Calculate subnet prefix length
            # For example, /16 VPC with 4 subnets would use /18 subnets
            import math

            bits_needed = math.ceil(math.log2(subnet_count))
            subnet_prefix = vpc_network.prefixlen + bits_needed

            if subnet_prefix > 32:  # IPv4 limit
                raise CIDRValidationError(
                    f"Cannot create {subnet_count} subnets in {vpc_cidr} - "
                    f"would require /{subnet_prefix} prefix"
                )

            # Generate subnet CIDRs
            subnets = list(vpc_network.subnets(new_prefix=subnet_prefix))

            if len(subnets) < subnet_count:
                raise CIDRValidationError(
                    f"Cannot create {subnet_count} subnets in {vpc_cidr} - "
                    f"only {len(subnets)} possible"
                )

            return [str(subnets[i]) for i in range(subnet_count)]

        except ipaddress.AddressValueError as e:
            raise CIDRValidationError(f"Invalid VPC CIDR '{vpc_cidr}': {e}")
        except Exception as e:
            raise CIDRValidationError(f"Error generating subnets: {e}")

    def analyze_security_rules(self, rules: List[dict]) -> dict:
        """Analyze security group rules for security issues.

        Parameters
        ----------
        rules : List[dict]
            List of security group rules

        Returns
        -------
        dict
            Analysis results with security findings
        """
        analysis = {
            "total_rules": len(rules),
            "prohibited_cidrs": [],
            "public_access_rules": [],
            "private_rules": [],
            "warnings": [],
            "errors": [],
        }

        for i, rule in enumerate(rules):
            ip_ranges = rule.get("IpRanges", [])
            protocol = rule.get("IpProtocol", "unknown")
            from_port = rule.get("FromPort", 0)
            to_port = rule.get("ToPort", 0)

            for ip_range in ip_ranges:
                cidr = ip_range.get("CidrIp", "")

                # Check for prohibited CIDRs
                is_prohibited, reason = self.is_prohibited_cidr(cidr)
                if is_prohibited:
                    analysis["prohibited_cidrs"].append(
                        {
                            "rule_index": i,
                            "cidr": cidr,
                            "reason": reason,
                            "protocol": protocol,
                            "ports": f"{from_port}-{to_port}",
                        }
                    )
                    analysis["errors"].append(
                        f"Rule {i}: Prohibited CIDR {cidr} ({reason})"
                    )

                # Check for public access
                elif cidr == "0.0.0.0/0":
                    analysis["public_access_rules"].append(
                        {
                            "rule_index": i,
                            "protocol": protocol,
                            "ports": f"{from_port}-{to_port}",
                        }
                    )
                    analysis["warnings"].append(
                        f"Rule {i}: Public access on {protocol}:{from_port}-{to_port}"
                    )

                # Check if private
                elif self.is_private_cidr(cidr):
                    analysis["private_rules"].append(
                        {
                            "rule_index": i,
                            "cidr": cidr,
                            "protocol": protocol,
                            "ports": f"{from_port}-{to_port}",
                        }
                    )

        return analysis

    def suggest_secure_alternatives(self, cidr: str) -> List[str]:
        """Suggest secure alternatives for a given CIDR block.

        Parameters
        ----------
        cidr : str
            CIDR block to find alternatives for

        Returns
        -------
        List[str]
            List of suggested secure alternatives
        """
        suggestions = []

        if cidr == "0.0.0.0/0":
            suggestions.extend(
                [
                    "10.0.0.0/16 (Private VPC range)",
                    "172.16.0.0/12 (Private Class B range)",
                    "192.168.0.0/16 (Private Class C range)",
                    "Your organization's specific CIDR blocks",
                    "VPC internal communication only",
                ]
            )

        return suggestions

    def validate_vpc_cidr_recommendations(self, vpc_cidr: str) -> List[str]:
        """Provide recommendations for VPC CIDR block selection.

        Parameters
        ----------
        vpc_cidr : str
            Proposed VPC CIDR block

        Returns
        -------
        List[str]
            List of recommendations
        """
        recommendations = []

        try:
            network = ipaddress.ip_network(vpc_cidr, strict=False)

            # Check if it's private
            if not network.is_private:
                recommendations.append(
                    "WARNING: VPC CIDR is not in private address space"
                )

            # Check prefix length
            if network.prefixlen > 24:
                recommendations.append(
                    f"VPC CIDR /{network.prefixlen} may be too small for multiple subnets"
                )
            elif network.prefixlen < 16:
                recommendations.append(
                    f"VPC CIDR /{network.prefixlen} is very large - consider smaller range"
                )

            # Check for common overlaps
            common_ranges = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
            for common_range in common_ranges:
                if self.is_overlapping_cidr(vpc_cidr, common_range):
                    recommendations.append(
                        f"VPC CIDR overlaps with common range {common_range}"
                    )

        except ipaddress.AddressValueError as e:
            recommendations.append(f"Invalid VPC CIDR: {e}")

        return recommendations

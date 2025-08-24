"""Security group management for Parsl Ephemeral AWS Provider.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import logging
from typing import List, Optional, Any

import boto3
from botocore.exceptions import ClientError

from ..exceptions import ResourceCreationError, ResourceCleanupError
from ..constants import TAG_NAME, TAG_WORKFLOW_ID, DEFAULT_SG_NAME, DEFAULT_VPC_CIDR
from ..config import SecurityConfig


logger = logging.getLogger(__name__)


class SecurityGroupManager:
    """Manager for AWS security group resources."""

    def __init__(self, provider: Any) -> None:
        """Initialize the security group manager.

        Parameters
        ----------
        provider : EphemeralAWSProvider
            The provider instance
        """
        self.provider = provider

        # Initialize AWS session
        session_kwargs = {}
        if self.provider.aws_access_key_id and self.provider.aws_secret_access_key:
            session_kwargs["aws_access_key_id"] = self.provider.aws_access_key_id
            session_kwargs[
                "aws_secret_access_key"
            ] = self.provider.aws_secret_access_key

        if self.provider.aws_session_token:
            session_kwargs["aws_session_token"] = self.provider.aws_session_token

        if self.provider.aws_profile:
            session_kwargs["profile_name"] = self.provider.aws_profile

        self.aws_session = boto3.Session(
            region_name=self.provider.region, **session_kwargs
        )

        # Initialize clients
        self.ec2_client = self.aws_session.client("ec2")

        # Track resources for cleanup
        self.security_groups = {}  # type: Dict[str, Dict[str, Any]]

        # Initialize security configuration
        self._setup_security_config()

    def _setup_security_config(self) -> None:
        """Set up security configuration from provider settings."""
        # Get security settings from provider if available
        security_env = getattr(self.provider, "security_environment", "dev")
        vpc_cidr = getattr(self.provider, "vpc_cidr", DEFAULT_VPC_CIDR)
        admin_cidrs = getattr(self.provider, "admin_cidr_blocks", None)
        strict_mode = getattr(self.provider, "strict_security_mode", None)

        # Create security configuration
        if security_env == "prod" and admin_cidrs:
            self.security_config = SecurityConfig.create_production_config(
                vpc_cidr=vpc_cidr, admin_cidrs=admin_cidrs
            )
        else:
            # Default to development configuration
            self.security_config = SecurityConfig.create_development_config(
                vpc_cidr=vpc_cidr
            )
            if strict_mode is not None:
                self.security_config.strict_mode = strict_mode

        logger.info(
            f"Security Group Manager configuration: environment={self.security_config.environment.value}, "
            f"strict_mode={self.security_config.strict_mode}"
        )

        # Analyze security posture
        analysis = self.security_config.analyze_security_posture()
        for warning in analysis.get("warnings", []):
            logger.warning(f"Security Group Manager warning: {warning}")
        for rec in analysis.get("recommendations", []):
            logger.info(f"Security Group Manager recommendation: {rec}")

    def create_security_group(
        self, vpc_id: str, name: Optional[str] = None, description: Optional[str] = None
    ) -> str:
        """Create a security group.

        Parameters
        ----------
        vpc_id : str
            ID of the VPC to create the security group in
        name : Optional[str], optional
            Name of the security group, by default None (auto-generated)
        description : Optional[str], optional
            Description of the security group, by default None (auto-generated)

        Returns
        -------
        str
            ID of the created security group
        """
        try:
            # Generate name and description if not provided
            if not name:
                name = f"{DEFAULT_SG_NAME}-{self.provider.workflow_id}"

            if not description:
                description = f"Security group for Parsl Ephemeral AWS Provider ({self.provider.workflow_id})"

            # Create security group
            sg_response = self.ec2_client.create_security_group(
                GroupName=name,
                Description=description,
                VpcId=vpc_id,
                TagSpecifications=[
                    {
                        "ResourceType": "security-group",
                        "Tags": [
                            {"Key": "Name", "Value": name},
                            {"Key": TAG_NAME, "Value": "true"},
                            {
                                "Key": TAG_WORKFLOW_ID,
                                "Value": self.provider.workflow_id,
                            },
                        ],
                    }
                ],
            )
            security_group_id = sg_response["GroupId"]

            # Store security group information
            self.security_groups[security_group_id] = {
                "id": security_group_id,
                "name": name,
                "vpc_id": vpc_id,
                "rules": [],
            }

            logger.info(f"Created security group: {security_group_id}")

            # Add provider tags
            if self.provider.tags:
                self.ec2_client.create_tags(
                    Resources=[security_group_id],
                    Tags=[
                        {"Key": k, "Value": v} for k, v in self.provider.tags.items()
                    ],
                )

            return security_group_id

        except ClientError as e:
            logger.error(f"Error creating security group: {e}")
            raise ResourceCreationError(f"Failed to create security group: {e}")

    def add_ingress_rule(
        self,
        security_group_id: str,
        ip_protocol: str,
        from_port: int,
        to_port: int,
        cidr_blocks: Optional[List[str]] = None,
        source_group_ids: Optional[List[str]] = None,
        description: Optional[str] = None,
    ) -> None:
        """Add an ingress rule to a security group.

        Parameters
        ----------
        security_group_id : str
            ID of the security group
        ip_protocol : str
            IP protocol ('tcp', 'udp', 'icmp', or '-1' for all)
        from_port : int
            Start of port range
        to_port : int
            End of port range
        cidr_blocks : Optional[List[str]], optional
            CIDR blocks to allow, by default None
        source_group_ids : Optional[List[str]], optional
            Source security group IDs to allow, by default None
        description : Optional[str], optional
            Description of the rule, by default None
        """
        try:
            # Prepare IP permissions
            ip_permissions = [
                {"IpProtocol": ip_protocol, "FromPort": from_port, "ToPort": to_port}
            ]

            # Add CIDR blocks if provided
            if cidr_blocks:
                ip_permissions[0]["IpRanges"] = [
                    {"CidrIp": cidr, "Description": description}
                    if description
                    else {"CidrIp": cidr}
                    for cidr in cidr_blocks
                ]

            # Add source security groups if provided
            if source_group_ids:
                ip_permissions[0]["UserIdGroupPairs"] = [
                    {"GroupId": group_id, "Description": description}
                    if description
                    else {"GroupId": group_id}
                    for group_id in source_group_ids
                ]

            # Add ingress rule
            self.ec2_client.authorize_security_group_ingress(
                GroupId=security_group_id, IpPermissions=ip_permissions
            )

            # Update security group information
            if security_group_id in self.security_groups:
                self.security_groups[security_group_id]["rules"].append(
                    {
                        "type": "ingress",
                        "ip_protocol": ip_protocol,
                        "from_port": from_port,
                        "to_port": to_port,
                        "cidr_blocks": cidr_blocks,
                        "source_group_ids": source_group_ids,
                        "description": description,
                    }
                )

            logger.info(
                f"Added ingress rule to security group {security_group_id}: {ip_protocol} {from_port}-{to_port}"
            )

        except ClientError as e:
            # If rule already exists, log but don't raise
            if e.response["Error"]["Code"] == "InvalidPermission.Duplicate":
                logger.warning(
                    f"Rule already exists in security group {security_group_id}: {e}"
                )
                return

            logger.error(
                f"Error adding ingress rule to security group {security_group_id}: {e}"
            )
            raise ResourceCreationError(
                f"Failed to add ingress rule to security group {security_group_id}: {e}"
            )

    def add_egress_rule(
        self,
        security_group_id: str,
        ip_protocol: str,
        from_port: int,
        to_port: int,
        cidr_blocks: Optional[List[str]] = None,
        destination_group_ids: Optional[List[str]] = None,
        description: Optional[str] = None,
    ) -> None:
        """Add an egress rule to a security group.

        Parameters
        ----------
        security_group_id : str
            ID of the security group
        ip_protocol : str
            IP protocol ('tcp', 'udp', 'icmp', or '-1' for all)
        from_port : int
            Start of port range
        to_port : int
            End of port range
        cidr_blocks : Optional[List[str]], optional
            CIDR blocks to allow, by default None
        destination_group_ids : Optional[List[str]], optional
            Destination security group IDs to allow, by default None
        description : Optional[str], optional
            Description of the rule, by default None
        """
        try:
            # Prepare IP permissions
            ip_permissions = [
                {"IpProtocol": ip_protocol, "FromPort": from_port, "ToPort": to_port}
            ]

            # Add CIDR blocks if provided
            if cidr_blocks:
                ip_permissions[0]["IpRanges"] = [
                    {"CidrIp": cidr, "Description": description}
                    if description
                    else {"CidrIp": cidr}
                    for cidr in cidr_blocks
                ]

            # Add destination security groups if provided
            if destination_group_ids:
                ip_permissions[0]["UserIdGroupPairs"] = [
                    {"GroupId": group_id, "Description": description}
                    if description
                    else {"GroupId": group_id}
                    for group_id in destination_group_ids
                ]

            # Add egress rule
            self.ec2_client.authorize_security_group_egress(
                GroupId=security_group_id, IpPermissions=ip_permissions
            )

            # Update security group information
            if security_group_id in self.security_groups:
                self.security_groups[security_group_id]["rules"].append(
                    {
                        "type": "egress",
                        "ip_protocol": ip_protocol,
                        "from_port": from_port,
                        "to_port": to_port,
                        "cidr_blocks": cidr_blocks,
                        "destination_group_ids": destination_group_ids,
                        "description": description,
                    }
                )

            logger.info(
                f"Added egress rule to security group {security_group_id}: {ip_protocol} {from_port}-{to_port}"
            )

        except ClientError as e:
            # If rule already exists, log but don't raise
            if e.response["Error"]["Code"] == "InvalidPermission.Duplicate":
                logger.warning(
                    f"Rule already exists in security group {security_group_id}: {e}"
                )
                return

            logger.error(
                f"Error adding egress rule to security group {security_group_id}: {e}"
            )
            raise ResourceCreationError(
                f"Failed to add egress rule to security group {security_group_id}: {e}"
            )

    def configure_default_rules(
        self,
        security_group_id: str,
        group_type: str = "compute_worker",
        allow_self_traffic: bool = True,
        allow_all_outbound: bool = True,
    ) -> None:
        """Configure security rules using the security framework.

        Parameters
        ----------
        security_group_id : str
            ID of the security group
        group_type : str, optional
            Type of security group ("compute_worker", "bastion", "public_access"),
            by default "compute_worker"
        allow_self_traffic : bool, optional
            Whether to allow all traffic within the security group, by default True
        allow_all_outbound : bool, optional
            Whether to allow all outbound traffic, by default True
        """
        try:
            # Get security rules from configuration
            security_rules = self.security_config.get_security_group_rules(group_type)

            # Apply each rule
            for rule in security_rules:
                self.add_ingress_rule(
                    security_group_id=security_group_id,
                    ip_protocol=rule["IpProtocol"],
                    from_port=rule["FromPort"],
                    to_port=rule["ToPort"],
                    cidr_blocks=[ip_range["CidrIp"] for ip_range in rule["IpRanges"]],
                    description=rule.get("Description", f"{group_type} rule"),
                )

            # Allow all traffic within security group
            if allow_self_traffic:
                self.add_ingress_rule(
                    security_group_id=security_group_id,
                    ip_protocol="-1",  # All protocols
                    from_port=-1,  # All ports
                    to_port=-1,  # All ports
                    source_group_ids=[security_group_id],
                    description="All traffic within security group",
                )

            # Allow all outbound traffic
            if allow_all_outbound:
                # Security groups allow all outbound traffic by default in AWS,
                # but we'll add it explicitly for clarity and in case it was removed
                self.add_egress_rule(
                    security_group_id=security_group_id,
                    ip_protocol="-1",  # All protocols
                    from_port=-1,  # All ports
                    to_port=-1,  # All ports
                    cidr_blocks=["0.0.0.0/0"],
                    description="All outbound traffic",
                )

            logger.info(
                f"Configured {len(security_rules)} {group_type} rules for security group {security_group_id} "
                f"(environment: {self.security_config.environment.value})"
            )

        except Exception as e:
            logger.error(
                f"Error configuring default rules for security group {security_group_id}: {e}"
            )
            raise ResourceCreationError(
                f"Failed to configure default rules for security group {security_group_id}: {e}"
            )

    def revoke_ingress_rule(
        self,
        security_group_id: str,
        ip_protocol: str,
        from_port: int,
        to_port: int,
        cidr_blocks: Optional[List[str]] = None,
        source_group_ids: Optional[List[str]] = None,
    ) -> None:
        """Revoke an ingress rule from a security group.

        Parameters
        ----------
        security_group_id : str
            ID of the security group
        ip_protocol : str
            IP protocol ('tcp', 'udp', 'icmp', or '-1' for all)
        from_port : int
            Start of port range
        to_port : int
            End of port range
        cidr_blocks : Optional[List[str]], optional
            CIDR blocks to revoke, by default None
        source_group_ids : Optional[List[str]], optional
            Source security group IDs to revoke, by default None
        """
        try:
            # Prepare IP permissions
            ip_permissions = [
                {"IpProtocol": ip_protocol, "FromPort": from_port, "ToPort": to_port}
            ]

            # Add CIDR blocks if provided
            if cidr_blocks:
                ip_permissions[0]["IpRanges"] = [
                    {"CidrIp": cidr} for cidr in cidr_blocks
                ]

            # Add source security groups if provided
            if source_group_ids:
                ip_permissions[0]["UserIdGroupPairs"] = [
                    {"GroupId": group_id} for group_id in source_group_ids
                ]

            # Revoke ingress rule
            self.ec2_client.revoke_security_group_ingress(
                GroupId=security_group_id, IpPermissions=ip_permissions
            )

            # Update security group information
            if security_group_id in self.security_groups:
                # Filter out the revoked rule
                updated_rules = []
                for rule in self.security_groups[security_group_id]["rules"]:
                    if (
                        rule["type"] == "ingress"
                        and rule["ip_protocol"] == ip_protocol
                        and rule["from_port"] == from_port
                        and rule["to_port"] == to_port
                        and (
                            (cidr_blocks and rule.get("cidr_blocks") == cidr_blocks)
                            or (
                                source_group_ids
                                and rule.get("source_group_ids") == source_group_ids
                            )
                        )
                    ):
                        continue
                    updated_rules.append(rule)
                self.security_groups[security_group_id]["rules"] = updated_rules

            logger.info(
                f"Revoked ingress rule from security group {security_group_id}: {ip_protocol} {from_port}-{to_port}"
            )

        except ClientError as e:
            logger.error(
                f"Error revoking ingress rule from security group {security_group_id}: {e}"
            )
            raise ResourceCleanupError(
                f"Failed to revoke ingress rule from security group {security_group_id}: {e}"
            )

    def revoke_egress_rule(
        self,
        security_group_id: str,
        ip_protocol: str,
        from_port: int,
        to_port: int,
        cidr_blocks: Optional[List[str]] = None,
        destination_group_ids: Optional[List[str]] = None,
    ) -> None:
        """Revoke an egress rule from a security group.

        Parameters
        ----------
        security_group_id : str
            ID of the security group
        ip_protocol : str
            IP protocol ('tcp', 'udp', 'icmp', or '-1' for all)
        from_port : int
            Start of port range
        to_port : int
            End of port range
        cidr_blocks : Optional[List[str]], optional
            CIDR blocks to revoke, by default None
        destination_group_ids : Optional[List[str]], optional
            Destination security group IDs to revoke, by default None
        """
        try:
            # Prepare IP permissions
            ip_permissions = [
                {"IpProtocol": ip_protocol, "FromPort": from_port, "ToPort": to_port}
            ]

            # Add CIDR blocks if provided
            if cidr_blocks:
                ip_permissions[0]["IpRanges"] = [
                    {"CidrIp": cidr} for cidr in cidr_blocks
                ]

            # Add destination security groups if provided
            if destination_group_ids:
                ip_permissions[0]["UserIdGroupPairs"] = [
                    {"GroupId": group_id} for group_id in destination_group_ids
                ]

            # Revoke egress rule
            self.ec2_client.revoke_security_group_egress(
                GroupId=security_group_id, IpPermissions=ip_permissions
            )

            # Update security group information
            if security_group_id in self.security_groups:
                # Filter out the revoked rule
                updated_rules = []
                for rule in self.security_groups[security_group_id]["rules"]:
                    if (
                        rule["type"] == "egress"
                        and rule["ip_protocol"] == ip_protocol
                        and rule["from_port"] == from_port
                        and rule["to_port"] == to_port
                        and (
                            (cidr_blocks and rule.get("cidr_blocks") == cidr_blocks)
                            or (
                                destination_group_ids
                                and rule.get("destination_group_ids")
                                == destination_group_ids
                            )
                        )
                    ):
                        continue
                    updated_rules.append(rule)
                self.security_groups[security_group_id]["rules"] = updated_rules

            logger.info(
                f"Revoked egress rule from security group {security_group_id}: {ip_protocol} {from_port}-{to_port}"
            )

        except ClientError as e:
            logger.error(
                f"Error revoking egress rule from security group {security_group_id}: {e}"
            )
            raise ResourceCleanupError(
                f"Failed to revoke egress rule from security group {security_group_id}: {e}"
            )

    def delete_security_group(self, security_group_id: str) -> None:
        """Delete a security group.

        Parameters
        ----------
        security_group_id : str
            ID of the security group to delete
        """
        try:
            # Check if security group exists
            try:
                self.ec2_client.describe_security_groups(GroupIds=[security_group_id])
            except ClientError as e:
                if e.response["Error"]["Code"] == "InvalidGroup.NotFound":
                    logger.warning(
                        f"Security group {security_group_id} not found, skipping delete"
                    )
                    if security_group_id in self.security_groups:
                        self.security_groups.pop(security_group_id)
                    return
                raise

            # Delete security group
            self.ec2_client.delete_security_group(GroupId=security_group_id)

            # Remove from tracked security groups
            if security_group_id in self.security_groups:
                self.security_groups.pop(security_group_id)

            logger.info(f"Deleted security group: {security_group_id}")

        except ClientError as e:
            logger.error(f"Error deleting security group {security_group_id}: {e}")
            raise ResourceCleanupError(
                f"Failed to delete security group {security_group_id}: {e}"
            )

    def cleanup_security_groups(self) -> None:
        """Clean up all security groups."""
        for security_group_id in list(self.security_groups.keys()):
            try:
                self.delete_security_group(security_group_id)
            except Exception as e:
                logger.error(
                    f"Error cleaning up security group {security_group_id}: {e}"
                )

    def find_security_groups_by_tag(self, tag_key: str, tag_value: str) -> List[str]:
        """Find security groups by tag.

        Parameters
        ----------
        tag_key : str
            Tag key to search for
        tag_value : str
            Tag value to search for

        Returns
        -------
        List[str]
            List of security group IDs matching the tag
        """
        try:
            response = self.ec2_client.describe_security_groups(
                Filters=[{"Name": f"tag:{tag_key}", "Values": [tag_value]}]
            )

            security_group_ids = [sg["GroupId"] for sg in response["SecurityGroups"]]

            # Add to tracked security groups
            for sg in response["SecurityGroups"]:
                sg_id = sg["GroupId"]
                if sg_id not in self.security_groups:
                    self.security_groups[sg_id] = {
                        "id": sg_id,
                        "name": sg.get("GroupName", ""),
                        "vpc_id": sg.get("VpcId", ""),
                        "rules": [],
                    }

            return security_group_ids

        except ClientError as e:
            logger.error(
                f"Error finding security groups by tag {tag_key}={tag_value}: {e}"
            )
            return []

    def find_workflow_security_groups(self) -> List[str]:
        """Find security groups for the current workflow.

        Returns
        -------
        List[str]
            List of security group IDs for the current workflow
        """
        return self.find_security_groups_by_tag(
            TAG_WORKFLOW_ID, self.provider.workflow_id
        )

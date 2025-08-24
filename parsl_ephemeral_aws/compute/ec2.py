"""EC2 compute resource implementation for Parsl Ephemeral AWS Provider.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import logging
import uuid
import time
from typing import Dict, Any

from botocore.exceptions import ClientError, NoCredentialsError

from ..exceptions import ResourceCreationError, ResourceCleanupError
from ..constants import (
    TAG_PREFIX,
    TAG_NAME,
    TAG_WORKFLOW_ID,
    TAG_BLOCK_ID,
    DEFAULT_VPC_CIDR,
)
from ..config import SecurityConfig
from ..security import CredentialManager, CredentialConfiguration
from ..error_handling import (
    RobustErrorHandler,
    ErrorContext,
    retry_with_backoff,
    RetryConfig,
)


logger = logging.getLogger(__name__)


class EC2Manager:
    """Manager for EC2 compute resources."""

    def __init__(self, provider: Any) -> None:
        """Initialize the EC2 manager.

        Parameters
        ----------
        provider : EphemeralAWSProvider
            The provider instance
        """
        self.provider = provider

        # Initialize error handling
        self.error_handler = RobustErrorHandler(
            retry_config=RetryConfig(
                max_attempts=5, base_delay=2.0, exponential_backoff=True, jitter=True
            )
        )
        logger.info("Error handler initialized for EC2 operations")

        # Initialize security configuration and credential management
        self._setup_security_config()

        # Initialize credential manager
        credential_config = self.security_config.get_credential_configuration()

        # Override credential config with provider-specific settings if provided
        if hasattr(provider, "aws_access_key_id") or hasattr(provider, "aws_profile"):
            # Legacy credential handling - create credential config from provider settings
            credential_config = self._create_credential_config_from_provider()

        try:
            self.credential_manager = CredentialManager(credential_config)
            logger.info("Credential manager initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize credential manager: {e}")
            raise ResourceCreationError(f"Credential initialization failed: {e}")

        # Initialize AWS session using credential manager
        try:
            self.aws_session = self.credential_manager.create_boto3_session(
                region=self.provider.region
            )
        except NoCredentialsError as e:
            logger.error(f"No valid AWS credentials found: {e}")
            raise ResourceCreationError(f"AWS credential error: {e}")

        # Initialize clients
        self.ec2_client = self.aws_session.client("ec2")
        self.ec2_resource = self.aws_session.resource("ec2")

        # Track resources for cleanup
        self.vpc_id = None
        self.subnet_id = None
        self.security_group_id = None
        self.key_pair_name = None
        self.instances = {}
        self.blocks = {}
        self.spot_requests = {}

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
            f"Security configuration: environment={self.security_config.environment.value}, "
            f"strict_mode={self.security_config.strict_mode}"
        )

        # Analyze security posture
        analysis = self.security_config.analyze_security_posture()
        for warning in analysis.get("warnings", []):
            logger.warning(f"Security warning: {warning}")
        for rec in analysis.get("recommendations", []):
            logger.info(f"Security recommendation: {rec}")

    def _create_credential_config_from_provider(self) -> CredentialConfiguration:
        """Create credential configuration from provider settings.

        Returns
        -------
        CredentialConfiguration
            Credential configuration based on provider settings
        """
        # Extract credential settings from provider
        role_arn = getattr(self.provider, "role_arn", None)
        aws_profile = getattr(self.provider, "aws_profile", None)
        use_env_vars = (
            hasattr(self.provider, "aws_access_key_id")
            and self.provider.aws_access_key_id is not None
        )

        # Create credential configuration
        config = CredentialConfiguration(
            role_arn=role_arn,
            enable_sanitization=True,
            sanitize_logs=True,
            use_environment_variables=use_env_vars,
            use_profile=aws_profile,
            auto_refresh_tokens=True,
        )

        # Set security-based defaults
        if self.security_config.environment.value == "production":
            config.use_environment_variables = False
            config.use_profile = None
            config.require_mfa = False

        logger.info(
            f"Created credential config: role_arn={bool(role_arn)}, "
            f"profile={aws_profile}, use_env={use_env_vars}"
        )

        return config

    def _create_vpc_with_retry(self, context: ErrorContext) -> Dict[str, Any]:
        """Create VPC with error handling and retry logic.

        Parameters
        ----------
        context : ErrorContext
            Error context for tracking

        Returns
        -------
        Dict[str, Any]
            VPC creation response
        """
        try:
            vpc_response = self.ec2_client.create_vpc(
                CidrBlock=self.security_config.vpc_cidr,
                TagSpecifications=[
                    {
                        "ResourceType": "vpc",
                        "Tags": [
                            {
                                "Key": "Name",
                                "Value": f"{TAG_PREFIX}-vpc-{self.provider.workflow_id}",
                            },
                            {"Key": TAG_NAME, "Value": "true"},
                            {
                                "Key": TAG_WORKFLOW_ID,
                                "Value": self.provider.workflow_id,
                            },
                        ],
                    }
                ],
            )
            return vpc_response
        except Exception as e:
            error_record = self.error_handler.handle_error(e, context)
            raise ResourceCreationError(f"Failed to create VPC: {e}")

    def _setup_instance_with_retry(
        self, instance_config: Dict[str, Any], context: ErrorContext
    ) -> Dict[str, Any]:
        """Create EC2 instance with error handling and retry logic.

        Parameters
        ----------
        instance_config : Dict[str, Any]
            Instance configuration
        context : ErrorContext
            Error context for tracking

        Returns
        -------
        Dict[str, Any]
            Instance creation response
        """
        try:
            response = self.ec2_client.run_instances(**instance_config)
            return response
        except Exception as e:
            error_record = self.error_handler.handle_error(e, context)
            raise ResourceCreationError(f"Failed to create instance: {e}")

    @retry_with_backoff()
    def _setup_network_resources(self) -> Dict[str, str]:
        """Set up VPC, subnet, and security group for EC2 instances.

        Returns
        -------
        Dict[str, str]
            Dictionary containing VPC ID, subnet ID, and security group ID
        """
        # Check if we already have network resources
        if self.vpc_id and self.subnet_id and self.security_group_id:
            return {
                "vpc_id": self.vpc_id,
                "subnet_id": self.subnet_id,
                "security_group_id": self.security_group_id,
            }

        context = ErrorContext(
            operation="setup_network_resources",
            resource_type="vpc",
            resource_id=f"workflow-{self.provider.workflow_id}",
            region=self.provider.region,
        )

        try:
            # Create VPC with configured CIDR
            vpc_response = self._create_vpc_with_retry(context)
            self.vpc_id = vpc_response["Vpc"]["VpcId"]
            logger.info(f"Created VPC: {self.vpc_id}")

            # Wait for VPC to be available
            self.ec2_client.get_waiter("vpc_available").wait(VpcIds=[self.vpc_id])

            # Enable DNS hostnames in VPC
            self.ec2_client.modify_vpc_attribute(
                VpcId=self.vpc_id, EnableDnsHostnames={"Value": True}
            )

            # Create subnet using CIDR manager
            from ..security.cidr_manager import CIDRManager

            cidr_manager = CIDRManager()
            subnet_cidrs = cidr_manager.get_subnet_cidrs(
                self.security_config.vpc_cidr, 1
            )

            subnet_response = self.ec2_client.create_subnet(
                VpcId=self.vpc_id,
                CidrBlock=subnet_cidrs[0],
                TagSpecifications=[
                    {
                        "ResourceType": "subnet",
                        "Tags": [
                            {
                                "Key": "Name",
                                "Value": f"{TAG_PREFIX}-subnet-{self.provider.workflow_id}",
                            },
                            {"Key": TAG_NAME, "Value": "true"},
                            {
                                "Key": TAG_WORKFLOW_ID,
                                "Value": self.provider.workflow_id,
                            },
                        ],
                    }
                ],
            )
            self.subnet_id = subnet_response["Subnet"]["SubnetId"]
            logger.info(f"Created subnet: {self.subnet_id}")

            # Create internet gateway and attach to VPC
            igw_response = self.ec2_client.create_internet_gateway(
                TagSpecifications=[
                    {
                        "ResourceType": "internet-gateway",
                        "Tags": [
                            {
                                "Key": "Name",
                                "Value": f"{TAG_PREFIX}-igw-{self.provider.workflow_id}",
                            },
                            {"Key": TAG_NAME, "Value": "true"},
                            {
                                "Key": TAG_WORKFLOW_ID,
                                "Value": self.provider.workflow_id,
                            },
                        ],
                    }
                ]
            )
            igw_id = igw_response["InternetGateway"]["InternetGatewayId"]

            self.ec2_client.attach_internet_gateway(
                InternetGatewayId=igw_id, VpcId=self.vpc_id
            )
            logger.info(f"Created and attached internet gateway: {igw_id}")

            # Create route table and associate with subnet
            route_table_response = self.ec2_client.create_route_table(
                VpcId=self.vpc_id,
                TagSpecifications=[
                    {
                        "ResourceType": "route-table",
                        "Tags": [
                            {
                                "Key": "Name",
                                "Value": f"{TAG_PREFIX}-rt-{self.provider.workflow_id}",
                            },
                            {"Key": TAG_NAME, "Value": "true"},
                            {
                                "Key": TAG_WORKFLOW_ID,
                                "Value": self.provider.workflow_id,
                            },
                        ],
                    }
                ],
            )
            route_table_id = route_table_response["RouteTable"]["RouteTableId"]

            # Add route to internet
            self.ec2_client.create_route(
                RouteTableId=route_table_id,
                DestinationCidrBlock="0.0.0.0/0",
                GatewayId=igw_id,
            )

            # Associate route table with subnet
            self.ec2_client.associate_route_table(
                RouteTableId=route_table_id, SubnetId=self.subnet_id
            )
            logger.info(f"Created and configured route table: {route_table_id}")

            # Create security group
            sg_response = self.ec2_client.create_security_group(
                GroupName=f"{TAG_PREFIX}-sg-{self.provider.workflow_id}",
                Description=f"Security group for Parsl Ephemeral AWS Provider ({self.provider.workflow_id})",
                VpcId=self.vpc_id,
                TagSpecifications=[
                    {
                        "ResourceType": "security-group",
                        "Tags": [
                            {
                                "Key": "Name",
                                "Value": f"{TAG_PREFIX}-sg-{self.provider.workflow_id}",
                            },
                            {"Key": TAG_NAME, "Value": "true"},
                            {
                                "Key": TAG_WORKFLOW_ID,
                                "Value": self.provider.workflow_id,
                            },
                        ],
                    }
                ],
            )
            self.security_group_id = sg_response["GroupId"]
            logger.info(f"Created security group: {self.security_group_id}")

            # Add inbound rules using security configuration
            security_rules = self.security_config.get_security_group_rules(
                "compute_worker"
            )

            # Convert to EC2 IpPermissions format
            ip_permissions = []
            for rule in security_rules:
                ip_permission = {
                    "IpProtocol": rule["IpProtocol"],
                    "FromPort": rule["FromPort"],
                    "ToPort": rule["ToPort"],
                    "IpRanges": rule["IpRanges"],
                }
                if "Description" in rule:
                    # Note: Description goes in IpRanges for ingress rules
                    for ip_range in ip_permission["IpRanges"]:
                        ip_range["Description"] = rule["Description"]

                ip_permissions.append(ip_permission)

            # Add self-referencing rule for internal communication
            ip_permissions.append(
                {
                    "IpProtocol": "-1",
                    "UserIdGroupPairs": [
                        {
                            "GroupId": self.security_group_id,
                            "Description": "Allow all traffic within security group",
                        }
                    ],
                }
            )

            if ip_permissions:
                self.ec2_client.authorize_security_group_ingress(
                    GroupId=self.security_group_id, IpPermissions=ip_permissions
                )
                logger.info(
                    f"Configured security group rules: {self.security_group_id} "
                    f"({len(ip_permissions)} rules)"
                )

                # Log security rule summary
                for rule in security_rules:
                    logger.debug(
                        f"Applied security rule: {rule['IpProtocol']}:"
                        f"{rule['FromPort']}-{rule['ToPort']} from "
                        f"{[r['CidrIp'] for r in rule['IpRanges']]}"
                    )
            else:
                logger.warning(
                    "No security rules configured - instance may be unreachable"
                )

            # Enable public IP assignment for the subnet if requested
            if self.provider.use_public_ips:
                self.ec2_client.modify_subnet_attribute(
                    SubnetId=self.subnet_id, MapPublicIpOnLaunch={"Value": True}
                )
                logger.info(
                    f"Enabled public IP assignment for subnet: {self.subnet_id}"
                )

            return {
                "vpc_id": self.vpc_id,
                "subnet_id": self.subnet_id,
                "security_group_id": self.security_group_id,
            }

        except Exception as e:
            # Record error for analysis
            error_record = self.error_handler.handle_error(e, context)
            logger.error(f"Error setting up network resources: {e}")

            # Attempt to clean up any created resources
            self._cleanup_network_resources()

            raise ResourceCreationError(f"Failed to set up network resources: {e}")

    def _cleanup_network_resources(self) -> None:
        """Clean up VPC, subnet, and security group."""
        try:
            # Delete security group
            if self.security_group_id:
                try:
                    self.ec2_client.delete_security_group(
                        GroupId=self.security_group_id
                    )
                    logger.info(f"Deleted security group: {self.security_group_id}")
                except ClientError as e:
                    logger.error(
                        f"Error deleting security group {self.security_group_id}: {e}"
                    )
                self.security_group_id = None

            # Delete subnet
            if self.subnet_id:
                try:
                    self.ec2_client.delete_subnet(SubnetId=self.subnet_id)
                    logger.info(f"Deleted subnet: {self.subnet_id}")
                except ClientError as e:
                    logger.error(f"Error deleting subnet {self.subnet_id}: {e}")
                self.subnet_id = None

            # Detach and delete internet gateways
            if self.vpc_id:
                try:
                    # Find internet gateways attached to VPC
                    igws = self.ec2_client.describe_internet_gateways(
                        Filters=[{"Name": "attachment.vpc-id", "Values": [self.vpc_id]}]
                    )

                    # Detach and delete each internet gateway
                    for igw in igws.get("InternetGateways", []):
                        igw_id = igw["InternetGatewayId"]
                        try:
                            self.ec2_client.detach_internet_gateway(
                                InternetGatewayId=igw_id, VpcId=self.vpc_id
                            )
                            self.ec2_client.delete_internet_gateway(
                                InternetGatewayId=igw_id
                            )
                            logger.info(f"Deleted internet gateway: {igw_id}")
                        except ClientError as e:
                            logger.error(
                                f"Error deleting internet gateway {igw_id}: {e}"
                            )
                except ClientError as e:
                    logger.error(
                        f"Error finding internet gateways for VPC {self.vpc_id}: {e}"
                    )

            # Delete route tables
            if self.vpc_id:
                try:
                    # Find route tables associated with VPC
                    route_tables = self.ec2_client.describe_route_tables(
                        Filters=[{"Name": "vpc-id", "Values": [self.vpc_id]}]
                    )

                    # Delete each custom route table (skip main route table)
                    for rt in route_tables.get("RouteTables", []):
                        rt_id = rt["RouteTableId"]
                        # Skip main route table
                        if any(
                            assoc.get("Main", False)
                            for assoc in rt.get("Associations", [])
                        ):
                            continue

                        # Delete route table
                        try:
                            # First disassociate all subnets
                            for assoc in rt.get("Associations", []):
                                if "SubnetId" in assoc:
                                    self.ec2_client.disassociate_route_table(
                                        AssociationId=assoc["RouteTableAssociationId"]
                                    )

                            # Then delete the route table
                            self.ec2_client.delete_route_table(RouteTableId=rt_id)
                            logger.info(f"Deleted route table: {rt_id}")
                        except ClientError as e:
                            logger.error(f"Error deleting route table {rt_id}: {e}")
                except ClientError as e:
                    logger.error(
                        f"Error finding route tables for VPC {self.vpc_id}: {e}"
                    )

            # Delete VPC
            if self.vpc_id:
                try:
                    self.ec2_client.delete_vpc(VpcId=self.vpc_id)
                    logger.info(f"Deleted VPC: {self.vpc_id}")
                except ClientError as e:
                    logger.error(f"Error deleting VPC {self.vpc_id}: {e}")
                self.vpc_id = None

        except Exception as e:
            logger.error(f"Error cleaning up network resources: {e}")
            raise ResourceCleanupError(f"Failed to clean up network resources: {e}")

    def create_blocks(self, count: int) -> Dict[str, Dict[str, Any]]:
        """Create compute blocks.

        Parameters
        ----------
        count : int
            Number of blocks to create

        Returns
        -------
        Dict[str, Dict[str, Any]]
            Dictionary mapping block IDs to block information
        """
        blocks = {}

        try:
            # Ensure network resources exist
            network = self._setup_network_resources()

            # Create blocks
            for _ in range(count):
                block_id = str(uuid.uuid4())

                # Create instances for the block
                instances = []

                for i in range(self.provider.nodes_per_block):
                    node_id = f"{block_id}-node-{i}"

                    # Create instance
                    if self.provider.use_spot_instances:
                        instance_id = self._create_spot_instance(
                            node_id, network, block_id
                        )
                    else:
                        instance_id = self._create_on_demand_instance(
                            node_id, network, block_id
                        )

                    instances.append(instance_id)

                # Record block information
                self.blocks[block_id] = {
                    "id": block_id,
                    "instance_ids": instances,
                    "status": "PENDING",
                    "created_at": time.time(),
                }

                blocks[block_id] = self.blocks[block_id]

            return blocks

        except Exception as e:
            logger.error(f"Error creating blocks: {e}")

            # Clean up any partially created resources
            for block_id, block_info in blocks.items():
                try:
                    self.terminate_block(block_id)
                except Exception as cleanup_e:
                    logger.error(f"Error cleaning up block {block_id}: {cleanup_e}")

            raise ResourceCreationError(f"Failed to create blocks: {e}")

    def _create_on_demand_instance(
        self, node_id: str, network: Dict[str, str], block_id: str
    ) -> str:
        """Create an on-demand EC2 instance.

        Parameters
        ----------
        node_id : str
            ID for the node
        network : Dict[str, str]
            Network configuration
        block_id : str
            ID of the block this instance belongs to

        Returns
        -------
        str
            Instance ID
        """
        # Prepare EC2 instance parameters
        instance_params = {
            "ImageId": self.provider.image_id,
            "InstanceType": self.provider.instance_type,
            "MinCount": 1,
            "MaxCount": 1,
            "SecurityGroupIds": [network["security_group_id"]],
            "SubnetId": network["subnet_id"],
            "UserData": self._generate_user_data(),
            "TagSpecifications": [
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "Name", "Value": f"{TAG_PREFIX}-node-{node_id}"},
                        {"Key": TAG_NAME, "Value": "true"},
                        {"Key": TAG_WORKFLOW_ID, "Value": self.provider.workflow_id},
                        {"Key": TAG_BLOCK_ID, "Value": block_id},
                    ],
                }
            ],
        }

        # Add provider tags
        for key, value in self.provider.tags.items():
            instance_params["TagSpecifications"][0]["Tags"].append(
                {"Key": key, "Value": value}
            )

        # Launch instance
        response = self.ec2_client.run_instances(**instance_params)
        instance_id = response["Instances"][0]["InstanceId"]

        # Record instance information
        self.instances[instance_id] = {
            "id": instance_id,
            "node_id": node_id,
            "block_id": block_id,
            "type": "on-demand",
            "public_ip": None,
            "private_ip": None,
            "status": "pending",
        }

        logger.info(f"Created on-demand instance: {instance_id} for block {block_id}")

        return instance_id

    def _create_spot_instance(
        self, node_id: str, network: Dict[str, str], block_id: str
    ) -> str:
        """Create a spot EC2 instance.

        Parameters
        ----------
        node_id : str
            ID for the node
        network : Dict[str, str]
            Network configuration
        block_id : str
            ID of the block this instance belongs to

        Returns
        -------
        str
            Instance ID
        """
        # Get current spot price
        spot_price_response = self.ec2_client.describe_spot_price_history(
            InstanceTypes=[self.provider.instance_type],
            ProductDescriptions=["Linux/UNIX"],
            MaxResults=1,
        )

        current_spot_price = (
            float(spot_price_response["SpotPriceHistory"][0]["SpotPrice"])
            if spot_price_response["SpotPriceHistory"]
            else None
        )

        # If we couldn't get the current spot price, use a reasonable default
        if current_spot_price is None:
            # Get on-demand price through price list API or another method
            # For now, we'll just use a placeholder
            current_spot_price = 0.10

        # Calculate max spot price as percentage of on-demand price
        max_spot_price = str(
            current_spot_price * (self.provider.spot_max_price_percentage / 100.0)
        )

        # Prepare spot request parameters
        spot_params = {
            "InstanceCount": 1,
            "Type": "one-time",
            "LaunchSpecification": {
                "ImageId": self.provider.image_id,
                "InstanceType": self.provider.instance_type,
                "SecurityGroupIds": [network["security_group_id"]],
                "SubnetId": network["subnet_id"],
                "UserData": self._generate_user_data_base64(),
            },
            "TagSpecifications": [
                {
                    "ResourceType": "spot-instances-request",
                    "Tags": [
                        {"Key": "Name", "Value": f"{TAG_PREFIX}-spot-{node_id}"},
                        {"Key": TAG_NAME, "Value": "true"},
                        {"Key": TAG_WORKFLOW_ID, "Value": self.provider.workflow_id},
                        {"Key": TAG_BLOCK_ID, "Value": block_id},
                    ],
                }
            ],
        }

        # Add max price if we have it
        if max_spot_price:
            spot_params["SpotPrice"] = max_spot_price

        # Add provider tags
        for key, value in self.provider.tags.items():
            spot_params["TagSpecifications"][0]["Tags"].append(
                {"Key": key, "Value": value}
            )

        # Submit spot request
        response = self.ec2_client.request_spot_instances(**spot_params)
        spot_request_id = response["SpotInstanceRequests"][0]["SpotInstanceRequestId"]

        # Record spot request
        self.spot_requests[spot_request_id] = {
            "id": spot_request_id,
            "node_id": node_id,
            "block_id": block_id,
            "status": "pending",
        }

        logger.info(f"Created spot request: {spot_request_id} for block {block_id}")

        # Wait for spot request to be fulfilled
        waiter = self.ec2_client.get_waiter("spot_instance_request_fulfilled")
        waiter.wait(SpotInstanceRequestIds=[spot_request_id])

        # Get instance ID from spot request
        spot_response = self.ec2_client.describe_spot_instance_requests(
            SpotInstanceRequestIds=[spot_request_id]
        )
        instance_id = spot_response["SpotInstanceRequests"][0]["InstanceId"]

        # Tag the instance
        self.ec2_client.create_tags(
            Resources=[instance_id],
            Tags=[
                {"Key": "Name", "Value": f"{TAG_PREFIX}-node-{node_id}"},
                {"Key": TAG_NAME, "Value": "true"},
                {"Key": TAG_WORKFLOW_ID, "Value": self.provider.workflow_id},
                {"Key": TAG_BLOCK_ID, "Value": block_id},
            ],
        )

        # Add provider tags to instance
        if self.provider.tags:
            self.ec2_client.create_tags(
                Resources=[instance_id],
                Tags=[{"Key": k, "Value": v} for k, v in self.provider.tags.items()],
            )

        # Record instance information
        self.instances[instance_id] = {
            "id": instance_id,
            "node_id": node_id,
            "block_id": block_id,
            "type": "spot",
            "spot_request_id": spot_request_id,
            "public_ip": None,
            "private_ip": None,
            "status": "pending",
        }

        logger.info(
            f"Spot request fulfilled with instance: {instance_id} for block {block_id}"
        )

        return instance_id

    def _generate_user_data(self) -> str:
        """Generate user data script for instance initialization.

        Returns
        -------
        str
            User data script
        """
        user_data = "#!/bin/bash\n"
        user_data += (
            f"echo 'Starting Parsl worker setup for {self.provider.workflow_id}'\n"
        )

        # Add worker initialization commands
        if self.provider.worker_init:
            user_data += f"\n# User-provided worker initialization\n{self.provider.worker_init}\n"

        # Add Parsl worker setup commands
        # In a real implementation, this would configure and start the Parsl worker

        return user_data

    def _generate_user_data_base64(self) -> str:
        """Generate base64-encoded user data script for instance initialization.

        Returns
        -------
        str
            Base64-encoded user data script
        """
        import base64

        return base64.b64encode(self._generate_user_data().encode()).decode()

    def terminate_block(self, block_id: str) -> None:
        """Terminate a compute block.

        Parameters
        ----------
        block_id : str
            ID of the block to terminate
        """
        if block_id not in self.blocks:
            logger.warning(f"Block {block_id} not found")
            return

        try:
            # Get instance IDs for the block
            instance_ids = self.blocks[block_id].get("instance_ids", [])

            # Terminate instances
            if instance_ids:
                self.ec2_client.terminate_instances(InstanceIds=instance_ids)

                # Update instance status
                for instance_id in instance_ids:
                    if instance_id in self.instances:
                        self.instances[instance_id]["status"] = "terminated"

                logger.info(
                    f"Terminated instances for block {block_id}: {instance_ids}"
                )

            # Update block status
            self.blocks[block_id]["status"] = "TERMINATED"

        except Exception as e:
            logger.error(f"Error terminating block {block_id}: {e}")
            raise

    def terminate_instance(self, instance_id: str) -> None:
        """Terminate an EC2 instance.

        Parameters
        ----------
        instance_id : str
            ID of the instance to terminate
        """
        try:
            # Terminate instance
            self.ec2_client.terminate_instances(InstanceIds=[instance_id])

            # Update instance status
            if instance_id in self.instances:
                self.instances[instance_id]["status"] = "terminated"

            logger.info(f"Terminated instance: {instance_id}")

        except Exception as e:
            logger.error(f"Error terminating instance {instance_id}: {e}")
            raise

    def get_instance_status(self, instance_id: str) -> str:
        """Get the status of an EC2 instance.

        Parameters
        ----------
        instance_id : str
            ID of the instance to check

        Returns
        -------
        str
            Instance status
        """
        try:
            response = self.ec2_client.describe_instances(InstanceIds=[instance_id])

            if response["Reservations"] and response["Reservations"][0]["Instances"]:
                status = response["Reservations"][0]["Instances"][0]["State"]["Name"]

                # Update instance information
                if instance_id in self.instances:
                    instance = self.instances[instance_id]
                    instance["status"] = status

                    if "PublicIpAddress" in response["Reservations"][0]["Instances"][0]:
                        instance["public_ip"] = response["Reservations"][0][
                            "Instances"
                        ][0]["PublicIpAddress"]

                    if (
                        "PrivateIpAddress"
                        in response["Reservations"][0]["Instances"][0]
                    ):
                        instance["private_ip"] = response["Reservations"][0][
                            "Instances"
                        ][0]["PrivateIpAddress"]

                return status

            return "unknown"

        except ClientError as e:
            if e.response["Error"]["Code"] == "InvalidInstanceID.NotFound":
                return "terminated"
            logger.error(f"Error getting instance status {instance_id}: {e}")
            raise

    def create_bastion(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Create a bastion/coordinator host.

        Parameters
        ----------
        config : Dict[str, Any]
            Configuration for the bastion host

        Returns
        -------
        Dict[str, Any]
            Bastion host information
        """
        try:
            # Ensure network resources exist
            network = self._setup_network_resources()

            # Generate a unique ID for this bastion
            bastion_id = f"bastion-{str(uuid.uuid4())[:8]}"

            # Prepare EC2 instance parameters
            instance_params = {
                "ImageId": self.provider.image_id,
                "InstanceType": config.get("instance_type", "t3.micro"),
                "MinCount": 1,
                "MaxCount": 1,
                "SecurityGroupIds": [network["security_group_id"]],
                "SubnetId": network["subnet_id"],
                "TagSpecifications": [
                    {
                        "ResourceType": "instance",
                        "Tags": [
                            {
                                "Key": "Name",
                                "Value": f"{TAG_PREFIX}-bastion-{self.provider.workflow_id}",
                            },
                            {"Key": TAG_NAME, "Value": "true"},
                            {
                                "Key": TAG_WORKFLOW_ID,
                                "Value": self.provider.workflow_id,
                            },
                            {"Key": "IsBastion", "Value": "true"},
                        ],
                    }
                ],
            }

            # Add provider tags
            for key, value in self.provider.tags.items():
                instance_params["TagSpecifications"][0]["Tags"].append(
                    {"Key": key, "Value": value}
                )

            # Launch instance
            response = self.ec2_client.run_instances(**instance_params)
            instance_id = response["Instances"][0]["InstanceId"]

            # Record instance information
            self.instances[instance_id] = {
                "id": instance_id,
                "node_id": bastion_id,
                "type": "bastion",
                "public_ip": None,
                "private_ip": None,
                "status": "pending",
                "is_bastion": True,
                "idle_timeout": config.get("idle_timeout", 30),
                "auto_shutdown": config.get("auto_shutdown", True),
            }

            logger.info(f"Created bastion host: {instance_id}")

            # Wait for instance to be running
            waiter = self.ec2_client.get_waiter("instance_running")
            waiter.wait(InstanceIds=[instance_id])

            # Get public and private IPs
            response = self.ec2_client.describe_instances(InstanceIds=[instance_id])
            public_ip = response["Reservations"][0]["Instances"][0].get(
                "PublicIpAddress"
            )
            private_ip = response["Reservations"][0]["Instances"][0].get(
                "PrivateIpAddress"
            )

            # Update instance information
            self.instances[instance_id]["public_ip"] = public_ip
            self.instances[instance_id]["private_ip"] = private_ip
            self.instances[instance_id]["status"] = "running"

            return {
                "id": instance_id,
                "public_ip": public_ip,
                "private_ip": private_ip,
                "status": "running",
            }

        except Exception as e:
            logger.error(f"Error creating bastion host: {e}")
            raise ResourceCreationError(f"Failed to create bastion host: {e}")

    def cleanup_all_resources(self) -> None:
        """Clean up all AWS resources created by this manager."""
        try:
            # Terminate all instances
            instance_ids = list(self.instances.keys())
            if instance_ids:
                try:
                    self.ec2_client.terminate_instances(InstanceIds=instance_ids)
                    logger.info(f"Terminated {len(instance_ids)} instances")
                except Exception as e:
                    logger.error(f"Error terminating instances: {e}")

            # Cancel spot requests
            spot_request_ids = list(self.spot_requests.keys())
            if spot_request_ids:
                try:
                    self.ec2_client.cancel_spot_instance_requests(
                        SpotInstanceRequestIds=spot_request_ids
                    )
                    logger.info(f"Cancelled {len(spot_request_ids)} spot requests")
                except Exception as e:
                    logger.error(f"Error cancelling spot requests: {e}")

            # Clean up network resources (this will handle VPC, subnets, security groups, etc.)
            self._cleanup_network_resources()

        except Exception as e:
            logger.error(f"Error cleaning up resources: {e}")
            raise ResourceCleanupError(f"Failed to clean up resources: {e}")

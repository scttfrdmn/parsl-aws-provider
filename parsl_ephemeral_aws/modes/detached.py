"""
Detached operating mode for the EphemeralAWSProvider.

The detached mode uses a persistent bastion host for coordinating long-running
workflows, allowing the client to disconnect and reconnect to the same
infrastructure.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import base64
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

from parsl_ephemeral_aws.constants import (
    DEFAULT_INBOUND_RULES,
    DEFAULT_OUTBOUND_RULES,
    DEFAULT_PUBLIC_SUBNET_CIDR,
    DEFAULT_SECURITY_GROUP_DESCRIPTION,
    DEFAULT_SECURITY_GROUP_NAME,
    DEFAULT_VPC_CIDR,
    RESOURCE_TYPE_EC2,
    RESOURCE_TYPE_SECURITY_GROUP,
    RESOURCE_TYPE_SUBNET,
    RESOURCE_TYPE_VPC,
    RESOURCE_TYPE_BASTION,
    RESOURCE_TYPE_CLOUDFORMATION,
    RESOURCE_TYPE_SPOT_FLEET,
    STATUS_CANCELED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_UNKNOWN,
)
from parsl_ephemeral_aws.exceptions import (
    NetworkCreationError,
    OperatingModeError,
    ResourceCreationError,
    ResourceNotFoundError,
)
from parsl_ephemeral_aws.modes.base import OperatingMode
from parsl_ephemeral_aws.state.base import StateStore
from parsl_ephemeral_aws.utils.aws import (
    create_tags,
    delete_resource,
    get_default_ami,
    wait_for_resource,
    get_cf_template,
)
from parsl_ephemeral_aws.compute.spot_fleet_cleanup import (
    cleanup_all_spot_fleet_resources,
)
from parsl_ephemeral_aws.compute.spot_interruption import (
    SpotInterruptionMonitor,
    ParslSpotInterruptionHandler,
)


logger = logging.getLogger(__name__)


class DetachedMode(OperatingMode):
    """Detached operating mode implementation.

    In detached mode, a persistent bastion host is created to coordinate long-running
    workflows, allowing the client to disconnect and reconnect to the same infrastructure.
    The bastion host manages EC2 worker instances as needed.

    Attributes
    ----------
    workflow_id : str
        Unique identifier for the workflow
    bastion_id : Optional[str]
        ID of the bastion host instance or CloudFormation stack
    bastion_host_type : str
        Type of bastion host deployment (direct or cloudformation)
    bastion_instance_type : str
        EC2 instance type for the bastion host
    idle_timeout : int
        Minutes to wait before shutting down idle resources
    preserve_bastion : bool
        Whether to preserve the bastion host during cleanup
    stack_name : Optional[str]
        Name of the CloudFormation stack for the bastion host
    """

    def __init__(
        self,
        provider_id: str,
        session: boto3.Session,
        state_store: StateStore,
        workflow_id: Optional[str] = None,
        bastion_instance_type: str = "t3.micro",
        idle_timeout: int = 30,
        preserve_bastion: bool = True,
        bastion_host_type: str = "cloudformation",
        use_spot_fleet: bool = False,
        instance_types: Optional[List[str]] = None,
        nodes_per_block: int = 1,
        spot_max_price_percentage: Optional[int] = None,
        bastion_id: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the detached mode.

        Parameters
        ----------
        provider_id : str
            Unique identifier for the provider instance
        session : boto3.Session
            AWS session for API calls
        state_store : StateStore
            Store for persisting state
        workflow_id : Optional[str], optional
            Unique identifier for the workflow, by default None
        bastion_instance_type : str, optional
            EC2 instance type for the bastion host, by default "t3.micro"
        idle_timeout : int, optional
            Minutes to wait before shutting down idle resources, by default 30
        preserve_bastion : bool, optional
            Whether to preserve the bastion host during cleanup, by default True
        bastion_host_type : str, optional
            Type of bastion host deployment (direct or cloudformation), by default "cloudformation"
        use_spot_fleet : bool, optional
            Whether to use Spot Fleet for worker instances, by default False
        instance_types : Optional[List[str]], optional
            List of instance types to use with Spot Fleet, by default None
        nodes_per_block : int, optional
            Number of nodes per block, by default 1
        spot_max_price_percentage : Optional[int], optional
            Maximum spot price as a percentage of on-demand price, by default None
        **kwargs : Any
            Additional arguments passed to the parent class
        """
        super().__init__(provider_id, session, state_store, **kwargs)

        # Detached mode specific attributes
        self.workflow_id = workflow_id or str(uuid.uuid4())
        self.bastion_id = bastion_id
        self.bastion_host_type = bastion_host_type
        self.bastion_instance_type = bastion_instance_type
        self.idle_timeout = idle_timeout
        self.preserve_bastion = preserve_bastion
        self.stack_name = None

        # Spot Fleet specific attributes
        self.use_spot_fleet = use_spot_fleet
        self.instance_types = instance_types or []
        self.nodes_per_block = nodes_per_block
        self.spot_max_price_percentage = spot_max_price_percentage

        # Initialize spot interruption handling if enabled
        self.spot_interruption_monitor = None
        self.spot_interruption_handler = None

        if self.use_spot and self.spot_interruption_handling:
            if not self.checkpoint_bucket and self.spot_interruption_handling:
                logger.warning(
                    "Spot interruption handling is enabled but no checkpoint bucket specified"
                )
            else:
                logger.debug(
                    "Initializing SpotInterruptionMonitor and Handler for DetachedMode"
                )
                self.spot_interruption_monitor = SpotInterruptionMonitor(self.session)
                self.spot_interruption_handler = ParslSpotInterruptionHandler(
                    session=self.session,
                    checkpoint_bucket=self.checkpoint_bucket,
                    checkpoint_prefix=self.checkpoint_prefix,
                )
                self.spot_interruption_monitor.start_monitoring()

        # If predefined VPC resources are provided, don't create new VPC
        if self.vpc_id:
            self.create_vpc = False

        # Update resources dict to include bastion host
        self.resources = self.resources or {}

        logger.debug(f"Initialized detached mode with workflow_id={self.workflow_id}")

    def initialize(self) -> None:
        """Initialize detached mode infrastructure.

        Creates the necessary VPC, subnet, security group resources,
        and a persistent bastion host for coordinating the workflow.

        Raises
        ------
        ResourceCreationError
            If resource creation fails
        """
        # Try to load state first
        if self.load_state():
            logger.debug("Loaded state, checking resources")
            # Verify that the loaded resources exist
            self._verify_resources()
            return

        logger.debug("Initializing detached mode infrastructure")

        # Create AWS resources
        try:
            # Create VPC if needed
            if not self.vpc_id and self.create_vpc:
                self.vpc_id = self._create_vpc()

            # Create subnet if needed
            if not self.subnet_id and self.vpc_id:
                self.subnet_id = self._create_subnet()

            # Create security group if needed
            if not self.security_group_id and self.vpc_id:
                self.security_group_id = self._create_security_group()

            # Create bastion host
            if self.bastion_host_type == "cloudformation":
                self.bastion_id = self._create_bastion_cloudformation()
            else:
                self.bastion_id = self._create_bastion_direct()

            # Save state
            self.save_state()

            logger.info(
                f"Initialized detached mode infrastructure: "
                f"vpc_id={self.vpc_id}, subnet_id={self.subnet_id}, "
                f"security_group_id={self.security_group_id}, "
                f"bastion_id={self.bastion_id}"
            )
        except Exception as e:
            logger.error(f"Failed to initialize detached mode infrastructure: {e}")
            # Try to clean up any resources we created
            self.cleanup_infrastructure()
            raise ResourceCreationError(
                f"Failed to initialize detached mode infrastructure: {e}"
            ) from e

    def _verify_resources(self) -> None:
        """Verify that the required resources exist.

        Raises
        ------
        ResourceNotFoundError
            If a required resource does not exist
        """
        ec2 = self.session.client("ec2")

        # Verify VPC
        if self.vpc_id:
            try:
                ec2.describe_vpcs(VpcIds=[self.vpc_id])
                logger.debug(f"Verified VPC {self.vpc_id} exists")
            except ClientError as e:
                if "InvalidVpcID.NotFound" in str(e):
                    logger.warning(f"VPC {self.vpc_id} does not exist")
                    self.vpc_id = None
                else:
                    raise

        # Verify subnet
        if self.subnet_id:
            try:
                ec2.describe_subnets(SubnetIds=[self.subnet_id])
                logger.debug(f"Verified subnet {self.subnet_id} exists")
            except ClientError as e:
                if "InvalidSubnetID.NotFound" in str(e):
                    logger.warning(f"Subnet {self.subnet_id} does not exist")
                    self.subnet_id = None
                else:
                    raise

        # Verify security group
        if self.security_group_id:
            try:
                ec2.describe_security_groups(GroupIds=[self.security_group_id])
                logger.debug(f"Verified security group {self.security_group_id} exists")
            except ClientError as e:
                if "InvalidGroup.NotFound" in str(e):
                    logger.warning(
                        f"Security group {self.security_group_id} does not exist"
                    )
                    self.security_group_id = None
                else:
                    raise

        # Verify bastion host
        if self.bastion_id:
            if self.bastion_host_type == "cloudformation":
                cf = self.session.client("cloudformation")
                try:
                    stack_response = cf.describe_stacks(StackName=self.bastion_id)
                    stack_status = stack_response["Stacks"][0]["StackStatus"]
                    if "FAILED" in stack_status or "DELETE" in stack_status:
                        logger.warning(
                            f"Bastion stack {self.bastion_id} is in state {stack_status}"
                        )
                        self.bastion_id = None
                    else:
                        logger.debug(
                            f"Verified bastion stack {self.bastion_id} exists with status {stack_status}"
                        )
                except ClientError as e:
                    if "does not exist" in str(e):
                        logger.warning(
                            f"Bastion stack {self.bastion_id} does not exist"
                        )
                        self.bastion_id = None
                    else:
                        raise
            else:
                try:
                    response = ec2.describe_instances(InstanceIds=[self.bastion_id])
                    if (
                        not response["Reservations"]
                        or not response["Reservations"][0]["Instances"]
                    ):
                        logger.warning(f"Bastion instance {self.bastion_id} not found")
                        self.bastion_id = None
                    else:
                        instance_state = response["Reservations"][0]["Instances"][0][
                            "State"
                        ]["Name"]
                        if instance_state in ["terminated", "shutting-down"]:
                            logger.warning(
                                f"Bastion instance {self.bastion_id} is {instance_state}"
                            )
                            self.bastion_id = None
                        else:
                            logger.debug(
                                f"Verified bastion instance {self.bastion_id} exists with state {instance_state}"
                            )
                except ClientError as e:
                    if "InvalidInstanceID.NotFound" in str(e):
                        logger.warning(f"Bastion instance {self.bastion_id} not found")
                        self.bastion_id = None
                    else:
                        raise

    def _create_vpc(self) -> str:
        """Create a VPC for the provider.

        Returns
        -------
        str
            VPC ID

        Raises
        ------
        NetworkCreationError
            If VPC creation fails
        """
        logger.info("Creating VPC")
        ec2 = self.session.client("ec2")

        try:
            # Create VPC
            response = ec2.create_vpc(
                CidrBlock=DEFAULT_VPC_CIDR,
                AmazonProvidedIpv6CidrBlock=False,
                InstanceTenancy="default",
                TagSpecifications=[
                    {
                        "ResourceType": "vpc",
                        "Tags": [
                            {
                                "Key": "Name",
                                "Value": f"parsl-detached-{self.workflow_id[:8]}",
                            },
                            {"Key": "CreatedBy", "Value": "ParslEphemeralAWSProvider"},
                            {"Key": "ProviderId", "Value": self.provider_id},
                            {"Key": "WorkflowId", "Value": self.workflow_id},
                        ],
                    }
                ],
            )

            vpc_id = response["Vpc"]["VpcId"]
            logger.debug(f"Created VPC {vpc_id}")

            # Enable DNS support and hostnames
            ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
            ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})

            # Create internet gateway
            igw_response = ec2.create_internet_gateway(
                TagSpecifications=[
                    {
                        "ResourceType": "internet-gateway",
                        "Tags": [
                            {
                                "Key": "Name",
                                "Value": f"parsl-detached-igw-{self.workflow_id[:8]}",
                            },
                            {"Key": "CreatedBy", "Value": "ParslEphemeralAWSProvider"},
                            {"Key": "ProviderId", "Value": self.provider_id},
                            {"Key": "WorkflowId", "Value": self.workflow_id},
                        ],
                    }
                ]
            )

            igw_id = igw_response["InternetGateway"]["InternetGatewayId"]

            # Attach internet gateway to VPC
            ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)

            logger.debug(f"Attached internet gateway {igw_id} to VPC {vpc_id}")

            # Add tags
            if self.additional_tags:
                create_tags(vpc_id, self.additional_tags, self.session)
                create_tags(igw_id, self.additional_tags, self.session)

            # Wait for VPC to be available
            wait_for_resource(vpc_id, "vpc_available", ec2, resource_name="VPC")

            return vpc_id
        except Exception as e:
            logger.error(f"Failed to create VPC: {e}")
            raise NetworkCreationError(f"Failed to create VPC: {e}") from e

    def _create_subnet(self) -> str:
        """Create a subnet for the provider.

        Returns
        -------
        str
            Subnet ID

        Raises
        ------
        NetworkCreationError
            If subnet creation fails
        """
        if not self.vpc_id:
            raise NetworkCreationError("VPC ID is required to create a subnet")

        logger.info(f"Creating subnet in VPC {self.vpc_id}")
        ec2 = self.session.client("ec2")

        try:
            # Create subnet
            cidr_block = DEFAULT_PUBLIC_SUBNET_CIDR
            response = ec2.create_subnet(
                VpcId=self.vpc_id,
                CidrBlock=cidr_block,
                TagSpecifications=[
                    {
                        "ResourceType": "subnet",
                        "Tags": [
                            {
                                "Key": "Name",
                                "Value": f"parsl-detached-subnet-{self.workflow_id[:8]}",
                            },
                            {"Key": "CreatedBy", "Value": "ParslEphemeralAWSProvider"},
                            {"Key": "ProviderId", "Value": self.provider_id},
                            {"Key": "WorkflowId", "Value": self.workflow_id},
                        ],
                    }
                ],
            )

            subnet_id = response["Subnet"]["SubnetId"]
            logger.debug(f"Created subnet {subnet_id} in VPC {self.vpc_id}")

            # Enable auto-assign public IP if public IPs are requested
            if self.use_public_ips:
                ec2.modify_subnet_attribute(
                    SubnetId=subnet_id, MapPublicIpOnLaunch={"Value": True}
                )
                logger.debug(f"Enabled auto-assign public IP for subnet {subnet_id}")

            # Create route table
            route_table_response = ec2.create_route_table(
                VpcId=self.vpc_id,
                TagSpecifications=[
                    {
                        "ResourceType": "route-table",
                        "Tags": [
                            {
                                "Key": "Name",
                                "Value": f"parsl-detached-rt-{self.workflow_id[:8]}",
                            },
                            {"Key": "CreatedBy", "Value": "ParslEphemeralAWSProvider"},
                            {"Key": "ProviderId", "Value": self.provider_id},
                            {"Key": "WorkflowId", "Value": self.workflow_id},
                        ],
                    }
                ],
            )

            route_table_id = route_table_response["RouteTable"]["RouteTableId"]

            # Associate route table with subnet
            ec2.associate_route_table(RouteTableId=route_table_id, SubnetId=subnet_id)

            # Get internet gateway ID
            igw_response = ec2.describe_internet_gateways(
                Filters=[{"Name": "attachment.vpc-id", "Values": [self.vpc_id]}]
            )

            if igw_response["InternetGateways"]:
                igw_id = igw_response["InternetGateways"][0]["InternetGatewayId"]

                # Create route to internet
                ec2.create_route(
                    RouteTableId=route_table_id,
                    DestinationCidrBlock="0.0.0.0/0",
                    GatewayId=igw_id,
                )

                logger.debug(
                    f"Created route to internet via {igw_id} for subnet {subnet_id}"
                )
            else:
                logger.warning(f"No internet gateway found for VPC {self.vpc_id}")

            # Add tags
            if self.additional_tags:
                create_tags(subnet_id, self.additional_tags, self.session)
                create_tags(route_table_id, self.additional_tags, self.session)

            # Wait for subnet to be available
            wait_for_resource(
                subnet_id, "subnet_available", ec2, resource_name="subnet"
            )

            return subnet_id
        except Exception as e:
            logger.error(f"Failed to create subnet in VPC {self.vpc_id}: {e}")
            raise NetworkCreationError(
                f"Failed to create subnet in VPC {self.vpc_id}: {e}"
            ) from e

    def _create_security_group(self) -> str:
        """Create a security group for the provider.

        Returns
        -------
        str
            Security group ID

        Raises
        ------
        NetworkCreationError
            If security group creation fails
        """
        if not self.vpc_id:
            raise NetworkCreationError("VPC ID is required to create a security group")

        logger.info(f"Creating security group in VPC {self.vpc_id}")
        ec2 = self.session.client("ec2")

        try:
            # Create security group
            response = ec2.create_security_group(
                GroupName=f"{DEFAULT_SECURITY_GROUP_NAME}-{self.workflow_id[:8]}",
                Description=DEFAULT_SECURITY_GROUP_DESCRIPTION,
                VpcId=self.vpc_id,
                TagSpecifications=[
                    {
                        "ResourceType": "security-group",
                        "Tags": [
                            {
                                "Key": "Name",
                                "Value": f"parsl-detached-sg-{self.workflow_id[:8]}",
                            },
                            {"Key": "CreatedBy", "Value": "ParslEphemeralAWSProvider"},
                            {"Key": "ProviderId", "Value": self.provider_id},
                            {"Key": "WorkflowId", "Value": self.workflow_id},
                        ],
                    }
                ],
            )

            security_group_id = response["GroupId"]
            logger.debug(
                f"Created security group {security_group_id} in VPC {self.vpc_id}"
            )

            # Add inbound rules with SSH access
            inbound_rules = (
                DEFAULT_INBOUND_RULES.copy() if DEFAULT_INBOUND_RULES else []
            )
            # Add SSH rule if not already present
            ssh_rule_exists = any(
                rule.get("FromPort") == 22 and rule.get("ToPort") == 22
                for rule in inbound_rules
            )
            if not ssh_rule_exists:
                inbound_rules.append(
                    {
                        "IpProtocol": "tcp",
                        "FromPort": 22,
                        "ToPort": 22,
                        "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                    }
                )

            if inbound_rules:
                ec2.authorize_security_group_ingress(
                    GroupId=security_group_id, IpPermissions=inbound_rules
                )
                logger.debug(
                    f"Added inbound rules to security group {security_group_id}"
                )

            # Add outbound rules
            if DEFAULT_OUTBOUND_RULES:
                ec2.authorize_security_group_egress(
                    GroupId=security_group_id, IpPermissions=DEFAULT_OUTBOUND_RULES
                )
                logger.debug(
                    f"Added outbound rules to security group {security_group_id}"
                )

            # Add tags
            if self.additional_tags:
                create_tags(security_group_id, self.additional_tags, self.session)

            # Wait for security group to be available
            wait_for_resource(
                security_group_id,
                "security_group_exists",
                ec2,
                resource_name="security group",
            )

            return security_group_id
        except Exception as e:
            logger.error(f"Failed to create security group in VPC {self.vpc_id}: {e}")
            raise NetworkCreationError(
                f"Failed to create security group in VPC {self.vpc_id}: {e}"
            ) from e

    def _create_bastion_direct(self) -> str:
        """Create a bastion host instance directly using EC2.

        Returns
        -------
        str
            EC2 instance ID of the bastion host

        Raises
        ------
        ResourceCreationError
            If bastion host creation fails
        """
        if not self.vpc_id or not self.subnet_id or not self.security_group_id:
            raise ResourceCreationError(
                "VPC, subnet, and security group are required to create a bastion host"
            )

        logger.info("Creating bastion host instance")
        ec2 = self.session.client("ec2")

        # Validate image_id
        if not self.image_id:
            self.image_id = get_default_ami(self.session.region_name)
            logger.info(
                f"Using default AMI {self.image_id} for region {self.session.region_name}"
            )

        try:
            # Prepare bastion init script
            init_script = self._prepare_bastion_init_script()

            # Prepare instance tags
            tags = [
                {"Key": "Name", "Value": f"parsl-bastion-{self.workflow_id[:8]}"},
                {"Key": "CreatedBy", "Value": "ParslEphemeralAWSProvider"},
                {"Key": "ProviderId", "Value": self.provider_id},
                {"Key": "WorkflowId", "Value": self.workflow_id},
                {"Key": "ResourceType", "Value": "bastion"},
            ]

            # Add additional tags
            for key, value in self.additional_tags.items():
                tags.append({"Key": key, "Value": value})

            # Prepare network configuration
            network_interface = {
                "DeviceIndex": 0,
                "SubnetId": self.subnet_id,
                "AssociatePublicIpAddress": self.use_public_ips,
                "Groups": [self.security_group_id],
            }

            # Create the bastion host
            response = ec2.run_instances(
                ImageId=self.image_id,
                InstanceType=self.bastion_instance_type,
                MaxCount=1,
                MinCount=1,
                UserData=init_script,
                KeyName=self.key_name,
                TagSpecifications=[{"ResourceType": "instance", "Tags": tags}],
                NetworkInterfaces=[network_interface],
                InstanceInitiatedShutdownBehavior="terminate",
                Monitoring={"Enabled": True},
            )

            instance_id = response["Instances"][0]["InstanceId"]
            logger.debug(f"Created bastion host instance {instance_id}")

            # Wait for instance to be running
            wait_for_resource(
                instance_id,
                "instance_running",
                ec2,
                resource_name="EC2 bastion instance",
            )

            # Add to resources
            self.resources[instance_id] = {
                "type": RESOURCE_TYPE_BASTION,
                "created_at": time.time(),
                "workflow_id": self.workflow_id,
            }

            # Save state with updated resources
            self.save_state()

            return instance_id
        except Exception as e:
            logger.error(f"Failed to create bastion host: {e}")
            raise ResourceCreationError(f"Failed to create bastion host: {e}") from e

    def _create_bastion_cloudformation(self) -> str:
        """Create a bastion host using CloudFormation.

        Returns
        -------
        str
            CloudFormation stack ID

        Raises
        ------
        ResourceCreationError
            If bastion host creation fails
        """
        if not self.vpc_id or not self.subnet_id or not self.security_group_id:
            raise ResourceCreationError(
                "VPC, subnet, and security group are required to create a bastion host"
            )

        logger.info("Creating bastion host using CloudFormation")
        cf = self.session.client("cloudformation")

        # Validate image_id
        if not self.image_id:
            self.image_id = get_default_ami(self.session.region_name)
            logger.info(
                f"Using default AMI {self.image_id} for region {self.session.region_name}"
            )

        try:
            # Prepare stack name
            self.stack_name = f"parsl-bastion-{self.workflow_id[:8]}"

            # Prepare bastion init script
            init_script = self._prepare_bastion_init_script()
            init_script_b64 = base64.b64encode(init_script.encode()).decode()

            # Prepare tags
            tags = [
                {"Key": "CreatedBy", "Value": "ParslEphemeralAWSProvider"},
                {"Key": "ProviderId", "Value": self.provider_id},
                {"Key": "WorkflowId", "Value": self.workflow_id},
            ]

            # Add additional tags
            for key, value in self.additional_tags.items():
                tags.append({"Key": key, "Value": value})

            # Create CloudFormation stack
            template = get_cf_template("bastion.yml")

            response = cf.create_stack(
                StackName=self.stack_name,
                TemplateBody=template,
                Parameters=[
                    {"ParameterKey": "VpcId", "ParameterValue": self.vpc_id},
                    {"ParameterKey": "SubnetId", "ParameterValue": self.subnet_id},
                    {
                        "ParameterKey": "SecurityGroupId",
                        "ParameterValue": self.security_group_id,
                    },
                    {
                        "ParameterKey": "InstanceType",
                        "ParameterValue": self.bastion_instance_type,
                    },
                    {"ParameterKey": "ImageId", "ParameterValue": self.image_id},
                    {"ParameterKey": "KeyName", "ParameterValue": self.key_name or ""},
                    {"ParameterKey": "WorkflowId", "ParameterValue": self.workflow_id},
                    {"ParameterKey": "UserData", "ParameterValue": init_script_b64},
                    {
                        "ParameterKey": "UseSpotInstance",
                        "ParameterValue": "true" if self.use_spot else "false",
                    },
                    {
                        "ParameterKey": "SpotMaxPrice",
                        "ParameterValue": self.spot_max_price or "",
                    },
                    {
                        "ParameterKey": "IdleTimeout",
                        "ParameterValue": str(self.idle_timeout),
                    },
                    {
                        "ParameterKey": "Tags",
                        "ParameterValue": json.dumps(self.additional_tags),
                    },
                    {
                        "ParameterKey": "UseSpotFleet",
                        "ParameterValue": "true" if self.use_spot_fleet else "false",
                    },
                    {
                        "ParameterKey": "InstanceTypes",
                        "ParameterValue": json.dumps(self.instance_types)
                        if self.instance_types
                        else "[]",
                    },
                    {
                        "ParameterKey": "NodesPerBlock",
                        "ParameterValue": str(self.nodes_per_block),
                    },
                    {
                        "ParameterKey": "SpotMaxPricePercentage",
                        "ParameterValue": str(self.spot_max_price_percentage)
                        if self.spot_max_price_percentage
                        else "",
                    },
                ],
                Capabilities=["CAPABILITY_IAM"],
                OnFailure="DELETE",
                Tags=tags,
            )

            stack_id = response["StackId"]
            logger.debug(f"Created CloudFormation stack {stack_id} for bastion host")

            # Wait for stack creation to complete
            logger.info(
                f"Waiting for bastion host stack {self.stack_name} to be created"
            )
            waiter = cf.get_waiter("stack_create_complete")
            waiter.wait(
                StackName=self.stack_name,
                WaiterConfig={
                    "Delay": 10,
                    "MaxAttempts": 36,  # Up to 6 minutes
                },
            )

            # Get bastion host instance ID from stack outputs
            stack_response = cf.describe_stacks(StackName=self.stack_name)
            bastion_host_id = None
            for output in stack_response["Stacks"][0]["Outputs"]:
                if output["OutputKey"] == "BastionHostId":
                    bastion_host_id = output["OutputValue"]
                    break

            logger.info(f"Bastion host created with ID {bastion_host_id}")

            # Add to resources
            self.resources[stack_id] = {
                "type": RESOURCE_TYPE_CLOUDFORMATION,
                "created_at": time.time(),
                "workflow_id": self.workflow_id,
                "stack_name": self.stack_name,
                "bastion_host_id": bastion_host_id,
            }

            # Save state with updated resources
            self.save_state()

            return stack_id
        except Exception as e:
            logger.error(f"Failed to create bastion host with CloudFormation: {e}")
            # Try to clean up the stack if it was created
            if self.stack_name:
                try:
                    cf.delete_stack(StackName=self.stack_name)
                    logger.info(
                        f"Initiated deletion of stack {self.stack_name} due to error"
                    )
                except Exception as delete_error:
                    logger.error(
                        f"Failed to clean up stack {self.stack_name}: {delete_error}"
                    )

            raise ResourceCreationError(
                f"Failed to create bastion host with CloudFormation: {e}"
            ) from e

    def _prepare_bastion_init_script(self) -> str:
        """Prepare the bastion host initialization script.

        Returns
        -------
        str
            Initialization script for the bastion host
        """
        # Start with base init script
        init_script = "#!/bin/bash\n"
        init_script += "set -e\n\n"

        # Add custom initialization if provided
        if self.worker_init:
            init_script += f"# Custom initialization\n{self.worker_init}\n\n"

        # Install required packages
        init_script += "# Install required packages\n"
        init_script += "apt-get update -y || yum update -y\n"
        init_script += "apt-get install -y python3 python3-pip jq awscli || yum install -y python3 python3-pip jq awscli\n\n"

        # Set up environment variables
        init_script += "# Set up environment variables\n"
        init_script += (
            f"echo 'export PARSL_WORKFLOW_ID={self.workflow_id}' >> /etc/environment\n"
        )
        init_script += (
            f"echo 'export PARSL_PROVIDER_ID={self.provider_id}' >> /etc/environment\n"
        )
        init_script += f"echo 'export AWS_REGION={self.session.region_name}' >> /etc/environment\n\n"

        # Create bastion manager script
        init_script += "# Create bastion manager script\n"
        init_script += "cat > /usr/local/bin/parsl-bastion-manager.py << 'EOL'\n"
        init_script += self._get_bastion_manager_script()
        init_script += "EOL\n\n"

        # Make script executable
        init_script += "chmod +x /usr/local/bin/parsl-bastion-manager.py\n\n"

        # Set up systemd service for bastion manager
        init_script += "# Set up systemd service\n"
        init_script += (
            "cat > /etc/systemd/system/parsl-bastion-manager.service << 'EOL'\n"
        )
        init_script += "[Unit]\n"
        init_script += "Description=Parsl Bastion Manager\n"
        init_script += "After=network.target\n\n"
        init_script += "[Service]\n"
        init_script += "Type=simple\n"
        init_script += (
            "ExecStart=/usr/bin/python3 /usr/local/bin/parsl-bastion-manager.py\n"
        )
        init_script += "Restart=always\n"
        init_script += "RestartSec=10\n"
        init_script += "StandardOutput=journal\n"
        init_script += "StandardError=journal\n\n"
        init_script += "[Install]\n"
        init_script += "WantedBy=multi-user.target\n"
        init_script += "EOL\n\n"

        # Enable and start service
        init_script += "systemctl enable parsl-bastion-manager.service\n"
        init_script += "systemctl start parsl-bastion-manager.service\n\n"

        # Create idle shutdown script
        init_script += "# Create idle shutdown script\n"
        init_script += "cat > /usr/local/bin/parsl-idle-shutdown.sh << 'EOL'\n"
        init_script += "#!/bin/bash\n"
        init_script += f"IDLE_TIMEOUT={self.idle_timeout}\n"
        init_script += "LAST_ACTIVITY_FILE=/var/run/parsl-last-activity\n\n"
        init_script += "# Create activity file if it doesn't exist\n"
        init_script += "if [ ! -f $LAST_ACTIVITY_FILE ]; then\n"
        init_script += "    date +%s > $LAST_ACTIVITY_FILE\n"
        init_script += "fi\n\n"
        init_script += "# Check if there are running jobs\n"
        init_script += (
            "RUNNING_JOBS=$(ps aux | grep -v grep | grep -c 'parsl-worker')\n\n"
        )
        init_script += "if [ $RUNNING_JOBS -gt 0 ]; then\n"
        init_script += "    # Update activity timestamp\n"
        init_script += "    date +%s > $LAST_ACTIVITY_FILE\n"
        init_script += "else\n"
        init_script += "    # Check idle time\n"
        init_script += "    LAST_ACTIVITY=$(cat $LAST_ACTIVITY_FILE)\n"
        init_script += "    NOW=$(date +%s)\n"
        init_script += "    IDLE_TIME=$((NOW - LAST_ACTIVITY))\n"
        init_script += "    IDLE_MINUTES=$((IDLE_TIME / 60))\n\n"
        init_script += "    if [ $IDLE_MINUTES -gt $IDLE_TIMEOUT ]; then\n"
        init_script += (
            '        echo "No activity for $IDLE_MINUTES minutes, shutting down"\n'
        )
        init_script += "        shutdown -h now\n"
        init_script += "    fi\n"
        init_script += "fi\n"
        init_script += "EOL\n\n"

        # Make idle shutdown script executable
        init_script += "chmod +x /usr/local/bin/parsl-idle-shutdown.sh\n\n"

        # Create cron job for idle shutdown
        init_script += "# Create cron job for idle shutdown\n"
        init_script += "(crontab -l 2>/dev/null; echo '*/5 * * * * /usr/local/bin/parsl-idle-shutdown.sh') | crontab -\n\n"

        return init_script

    def _get_bastion_manager_script(self) -> str:
        """Get the Python script for the bastion manager.

        Returns
        -------
        str
            Python script for the bastion manager
        """
        return '''#!/usr/bin/env python3
import json
import logging
import os
import subprocess
import sys
import time
import traceback
import uuid
import base64
from datetime import datetime
import boto3
from botocore.exceptions import ClientError

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('parsl-bastion-manager')

# Constants
WORKFLOW_ID = os.environ.get('PARSL_WORKFLOW_ID')
PROVIDER_ID = os.environ.get('PARSL_PROVIDER_ID')
AWS_REGION = os.environ.get('AWS_REGION', 'us-east-1')
USE_SPOT_FLEET = os.environ.get('PARSL_USE_SPOT_FLEET', 'false').lower() == 'true'
SSM_PARAMETER_PREFIX = f'/parsl/workflows/{WORKFLOW_ID}'
JOB_COMMAND_PREFIX = f'{SSM_PARAMETER_PREFIX}/jobs'
JOB_STATUS_PREFIX = f'{SSM_PARAMETER_PREFIX}/status'
TAG_PREFIX = "parsl-ephemeral"
RESOURCE_TYPE_SPOT_FLEET = "spot_fleet"
EC2_STATUS_MAPPING = {
    'pending': 'PENDING',
    'running': 'RUNNING',
    'shutting-down': 'CANCELED',
    'terminated': 'COMPLETED',
    'stopping': 'CANCELED',
    'stopped': 'CANCELED',
}
SPOT_FLEET_IAM_ROLE_ARN = None  # Will be populated dynamically

def get_session():
    """Get AWS session."""
    return boto3.session.Session(region_name=AWS_REGION)

def update_last_activity():
    """Update the last activity timestamp."""
    with open('/var/run/parsl-last-activity', 'w') as f:
        f.write(str(int(time.time())))

def get_pending_jobs():
    """Get pending jobs from SSM Parameter Store."""
    session = get_session()
    ssm = session.client('ssm')

    try:
        # Get all parameters under job command prefix
        paginator = ssm.get_paginator('get_parameters_by_path')
        pending_jobs = []

        for page in paginator.paginate(Path=JOB_COMMAND_PREFIX, Recursive=True):
            for param in page['Parameters']:
                job_id = param['Name'].split('/')[-1]

                # Check if there's already a status for this job
                try:
                    status_param = ssm.get_parameter(Name=f'{JOB_STATUS_PREFIX}/{job_id}')
                    # If status exists and is not pending, skip this job
                    status_data = json.loads(status_param['Parameter']['Value'])
                    if status_data.get('status') not in ['PENDING', 'SUBMITTING']:
                        continue
                except ClientError as e:
                    if e.response['Error']['Code'] != 'ParameterNotFound':
                        raise

                # Parse job command
                job_data = json.loads(param['Value'])
                job_data['id'] = job_id
                pending_jobs.append(job_data)

        return pending_jobs
    except Exception as e:
        logger.error(f"Error getting pending jobs: {e}")
        traceback.print_exc()
        return []

def update_job_status(job_id, status, instance_id=None, error=None, fleet_request_id=None, all_instance_ids=None):
    """Update job status in SSM Parameter Store.

    Stores the current status of a job in the SSM Parameter Store, including
    additional Spot Fleet specific information when applicable. This allows
    the client to track both individual EC2 instances and entire Spot Fleets
    across provider restarts.

    Parameters
    ----------
    job_id : str
        ID of the job to update
    status : str
        New status of the job
    instance_id : str, optional
        Primary instance ID associated with the job, by default None
    error : str, optional
        Error message if the job failed, by default None
    fleet_request_id : str, optional
        Spot Fleet request ID if using Spot Fleet, by default None
    all_instance_ids : list, optional
        List of all instance IDs in the Spot Fleet, by default None
    """
    session = get_session()
    ssm = session.client('ssm')

    status_data = {
        'status': status,
        'updated_at': datetime.utcnow().isoformat(),
    }

    if instance_id:
        status_data['instance_id'] = instance_id

    if error:
        status_data['error'] = str(error)

    # Add Spot Fleet specific fields if applicable
    if fleet_request_id:
        status_data['fleet_request_id'] = fleet_request_id
        status_data['resource_type'] = RESOURCE_TYPE_SPOT_FLEET

    if all_instance_ids:
        status_data['all_instance_ids'] = all_instance_ids

    try:
        ssm.put_parameter(
            Name=f'{JOB_STATUS_PREFIX}/{job_id}',
            Value=json.dumps(status_data),
            Type='String',
            Overwrite=True
        )
        logger.info(f"Updated job {job_id} status to {status}")
    except Exception as e:
        logger.error(f"Error updating job status: {e}")
        traceback.print_exc()

def get_spot_fleet_role():
    """Get or create the IAM role for Spot Fleet requests.

    Returns
    -------
    str
        ARN of the IAM role for Spot Fleet
    """
    global SPOT_FLEET_IAM_ROLE_ARN

    # Return cached value if available
    if SPOT_FLEET_IAM_ROLE_ARN:
        return SPOT_FLEET_IAM_ROLE_ARN

    # Create a new role
    session = get_session()
    iam = session.client('iam')
    role_name = f"{TAG_PREFIX}-spot-fleet-role-{WORKFLOW_ID[:8]}"

    try:
        # Check if the role already exists
        try:
            response = iam.get_role(RoleName=role_name)
            SPOT_FLEET_IAM_ROLE_ARN = response['Role']['Arn']
            logger.info(f"Using existing IAM role for Spot Fleet: {role_name}")
            return SPOT_FLEET_IAM_ROLE_ARN
        except ClientError as e:
            if e.response['Error']['Code'] != 'NoSuchEntity':
                raise

            # Role doesn't exist, create it
            trust_policy = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "spotfleet.amazonaws.com"},
                        "Action": "sts:AssumeRole"
                    }
                ]
            }

            response = iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(trust_policy),
                Description=f"Role for Parsl Spot Fleet ({WORKFLOW_ID})"
            )

            # Attach the required policy
            iam.attach_role_policy(
                RoleName=role_name,
                PolicyArn="arn:aws:iam::aws:policy/service-role/AmazonEC2SpotFleetTaggingRole"
            )

            # Get the role ARN
            SPOT_FLEET_IAM_ROLE_ARN = response['Role']['Arn']
            logger.info(f"Created IAM role for Spot Fleet: {role_name}")

            # Wait for role to be ready (IAM changes can take time to propagate)
            time.sleep(10)

            return SPOT_FLEET_IAM_ROLE_ARN
    except Exception as e:
        logger.error(f"Error getting or creating IAM role for Spot Fleet: {e}")
        raise

def launch_spot_fleet(job_data):
    """Launch a Spot Fleet for the job.

    Parameters
    ----------
    job_data : dict
        Job data

    Returns
    -------
    str
        Instance ID or Spot Fleet request ID
    """
    session = get_session()
    ec2 = session.client('ec2')
    job_id = job_data['id']

    try:
        # Prepare user data script
        user_data = f"""#!/bin/bash
# Set up environment
export PARSL_JOB_ID={job_id}
export PARSL_WORKFLOW_ID={WORKFLOW_ID}
export PARSL_PROVIDER_ID={PROVIDER_ID}
export PARSL_WORKER_ID=$(hostname)

# Execute job command
{job_data['command']}

# Shutdown after completion if requested
{f"shutdown -h now" if job_data.get('auto_shutdown', True) else "# Auto-shutdown disabled"}
"""

        # Get IAM fleet role
        fleet_role_arn = get_spot_fleet_role()

        # Generate a unique client token
        client_token = f"{WORKFLOW_ID}-{job_id}"

        # Prepare launch specifications for multiple instance types
        launch_specifications = []

        # Use instance types from job data or default to a set of common types
        instance_types = job_data.get('instance_types', [])
        if not instance_types:
            # If no instance types specified, use the job's instance type
            instance_types = [job_data['instance_type']]

            # Add similar instance types for better availability
            instance_type = job_data['instance_type']
            if instance_type.startswith(('c', 'm', 'r', 't')) and len(instance_type) > 3:
                family = instance_type[0]
                size = instance_type.split('.')[1]

                # Add similar types
                if family == 'c':
                    instance_types.extend([f"m{instance_type[1:]}", f"r{instance_type[1:]}"])
                elif family == 'm':
                    instance_types.extend([f"c{instance_type[1:]}", f"r{instance_type[1:]}"])
                elif family == 'r':
                    instance_types.extend([f"m{instance_type[1:]}", f"c{instance_type[1:]}"])

                # Try to add a newer generation if possible
                gen = instance_type[1]
                if gen.isdigit() and int(gen) < 9:
                    next_gen = str(int(gen) + 1)
                    instance_types.append(f"{family}{next_gen}.{size}")

        # Common tags for all instances
        tags = [
            {'Key': 'Name', 'Value': f"parsl-worker-{job_id[:8]}"},
            {'Key': 'ParslResource', 'Value': 'true'},
            {'Key': 'ParslWorkflowId', 'Value': WORKFLOW_ID},
            {'Key': 'ParslProviderId', 'Value': PROVIDER_ID},
            {'Key': 'ParslJobId', 'Value': job_id},
        ]

        # Generate specs for each instance type
        for instance_type in instance_types:
            try:
                launch_spec = {
                    'ImageId': job_data['image_id'],
                    'InstanceType': instance_type,
                    'SubnetId': job_data['subnet_id'],
                    'SecurityGroups': [
                        {'GroupId': job_data['security_group_id']}
                    ],
                    'UserData': base64.b64encode(user_data.encode()).decode(),
                    'TagSpecifications': [
                        {
                            'ResourceType': 'instance',
                            'Tags': tags
                        }
                    ],
                    'Monitoring': {'Enabled': True},
                    'InstanceInitiatedShutdownBehavior': 'terminate',
                }

                # Add key name if provided
                if job_data.get('key_name'):
                    launch_spec['KeyName'] = job_data['key_name']

                launch_specifications.append(launch_spec)
            except Exception as e:
                logger.warning(f"Skipping instance type {instance_type} due to error: {e}")

        if not launch_specifications:
            raise Exception("No valid launch specifications could be created")

        # Prepare Spot Fleet request parameters
        nodes_per_block = job_data.get('nodes_per_block', 1)
        request_params = {
            'SpotFleetRequestConfig': {
                'ClientToken': client_token,
                'TargetCapacity': nodes_per_block,
                'OnDemandTargetCapacity': 0,  # Use only spot instances
                'IamFleetRole': fleet_role_arn,
                'LaunchSpecifications': launch_specifications,
                'TerminateInstancesWithExpiration': True,
                'Type': 'maintain',  # Maintain target capacity
                'AllocationStrategy': 'lowestPrice',  # Use the lowest price instance types
                'ReplaceUnhealthyInstances': True,
                'TagSpecifications': [
                    {
                        'ResourceType': 'spot-fleet-request',
                        'Tags': tags
                    }
                ]
            }
        }

        # Set a max price if specified
        if job_data.get('spot_max_price'):
            request_params['SpotFleetRequestConfig']['SpotPrice'] = job_data['spot_max_price']
        elif job_data.get('spot_max_price_percentage'):
            # Convert percentage to actual price (rough approximation)
            # In practice, you would query the price API for accurate pricing
            percent = float(job_data['spot_max_price_percentage']) / 100.0
            request_params['SpotFleetRequestConfig']['SpotPrice'] = str(percent)

        # Create the Spot Fleet request
        response = ec2.request_spot_fleet(**request_params)
        fleet_request_id = response['SpotFleetRequestId']
        logger.info(f"Created Spot Fleet request: {fleet_request_id} for job {job_id}")

        # Wait for Spot Fleet instances to be created
        logger.info(f"Waiting for Spot Fleet instances for fleet {fleet_request_id}")
        instance_ids = wait_for_fleet_instances(fleet_request_id)

        if not instance_ids:
            update_job_status(
                job_id,
                'FAILED',
                None,
                error=f"No instances were created in the Spot Fleet {fleet_request_id}",
                fleet_request_id=fleet_request_id
            )
            return None

        # Update job status with the first instance ID and the fleet request ID
        primary_instance_id = instance_ids[0]
        update_job_status(
            job_id,
            'RUNNING',
            primary_instance_id,
            fleet_request_id=fleet_request_id,
            all_instance_ids=instance_ids
        )
        update_last_activity()

        return primary_instance_id
    except Exception as e:
        logger.error(f"Error creating Spot Fleet for job {job_id}: {e}")
        traceback.print_exc()
        update_job_status(job_id, 'FAILED', None, error=str(e))
        return None

def wait_for_fleet_instances(fleet_request_id, max_wait=300):
    """Wait for Spot Fleet instances to be created.

    Parameters
    ----------
    fleet_request_id : str
        Spot Fleet request ID
    max_wait : int, optional
        Maximum wait time in seconds, by default 300

    Returns
    -------
    list
        List of instance IDs
    """
    start_time = time.time()
    instance_ids = []

    session = get_session()
    ec2 = session.client('ec2')

    logger.info(f"Waiting for Spot Fleet instances for fleet {fleet_request_id}")

    while time.time() - start_time < max_wait:
        try:
            # Check fleet status
            response = ec2.describe_spot_fleet_requests(
                SpotFleetRequestIds=[fleet_request_id]
            )

            if not response['SpotFleetRequestConfigs']:
                logger.warning(f"No Spot Fleet request found with ID {fleet_request_id}")
                break

            config = response['SpotFleetRequestConfigs'][0]
            status = config['SpotFleetRequestState']

            if status == 'active':
                # Get instances in the fleet
                instances_response = ec2.describe_spot_fleet_instances(
                    SpotFleetRequestId=fleet_request_id
                )

                # Extract instance IDs
                instance_ids = [instance['InstanceId'] for instance in instances_response.get('ActiveInstances', [])]

                if instance_ids:
                    logger.info(f"Spot Fleet {fleet_request_id} is active with {len(instance_ids)} instances")
                    break

            elif status in ['cancelled', 'cancelled_running', 'cancelled_terminating', 'error']:
                logger.error(f"Spot Fleet request {fleet_request_id} failed with status {status}")
                break

            # Wait before checking again
            time.sleep(10)

        except Exception as e:
            logger.error(f"Error checking Spot Fleet status: {e}")
            # Continue waiting, might be temporary
            time.sleep(10)

    # If we've waited too long without success
    if time.time() - start_time >= max_wait and not instance_ids:
        logger.error(f"Timeout waiting for Spot Fleet instances for fleet {fleet_request_id}")

    return instance_ids

def launch_instance(job_data):
    """Launch an EC2 instance or Spot Fleet to run the job."""
    job_id = job_data['id']

    try:
        # Check if we should use Spot Fleet for this job
        use_spot = job_data.get('use_spot', False)
        use_spot_fleet = job_data.get('use_spot_fleet', False) or USE_SPOT_FLEET

        if use_spot and use_spot_fleet:
            logger.info(f"Using Spot Fleet for job {job_id}")
            return launch_spot_fleet(job_data)
        else:
            # Use regular EC2 instance
            session = get_session()
            ec2 = session.client('ec2')

            # Prepare user data script
            user_data = f"""#!/bin/bash
# Set up environment
export PARSL_JOB_ID={job_id}
export PARSL_WORKFLOW_ID={WORKFLOW_ID}
export PARSL_PROVIDER_ID={PROVIDER_ID}
export PARSL_WORKER_ID=$(hostname)

# Execute job command
{job_data['command']}

# Shutdown after completion if requested
{f"shutdown -h now" if job_data.get('auto_shutdown', True) else "# Auto-shutdown disabled"}
"""

            # Launch instance
            instance_params = {
                'ImageId': job_data['image_id'],
                'InstanceType': job_data['instance_type'],
                'MinCount': 1,
                'MaxCount': 1,
                'UserData': user_data,
                'SecurityGroupIds': [job_data['security_group_id']],
                'SubnetId': job_data['subnet_id'],
                'TagSpecifications': [
                    {
                        'ResourceType': 'instance',
                        'Tags': [
                            {'Key': 'Name', 'Value': f"parsl-worker-{job_id[:8]}"},
                            {'Key': 'ParslResource', 'Value': 'true'},
                            {'Key': 'ParslWorkflowId', 'Value': WORKFLOW_ID},
                            {'Key': 'ParslProviderId', 'Value': PROVIDER_ID},
                            {'Key': 'ParslJobId', 'Value': job_id},
                        ]
                    }
                ],
                'Monitoring': {'Enabled': True},
                'InstanceInitiatedShutdownBehavior': 'terminate',
            }

            # Add key name if provided
            if job_data.get('key_name'):
                instance_params['KeyName'] = job_data['key_name']

            # Launch instance
            response = ec2.run_instances(**instance_params)
            instance_id = response['Instances'][0]['InstanceId']

            logger.info(f"Launched instance {instance_id} for job {job_id}")
            update_job_status(job_id, 'RUNNING', instance_id)
            update_last_activity()

            return instance_id
    except Exception as e:
        logger.error(f"Error launching instance for job {job_id}: {e}")
        traceback.print_exc()
        update_job_status(job_id, 'FAILED', None, str(e))
        return None

def update_running_job_status():
    """Update status of running jobs."""
    session = get_session()
    ec2 = session.client('ec2')
    ssm = session.client('ssm')

    try:
        # Get all running job statuses
        paginator = ssm.get_paginator('get_parameters_by_path')
        running_jobs_data = []  # Will store [job_id, status_data] pairs
        instance_ids = []
        spot_fleet_jobs = {}  # Map of job_id to fleet request id

        for page in paginator.paginate(Path=JOB_STATUS_PREFIX, Recursive=True):
            for param in page['Parameters']:
                job_id = param['Name'].split('/')[-1]
                status_data = json.loads(param['Value'])

                if status_data.get('status') == 'RUNNING':
                    # Keep track of both instance_id and the complete status data
                    running_jobs_data.append([job_id, status_data])

                    # Check if this is a Spot Fleet job
                    if 'resource_type' in status_data and status_data['resource_type'] == RESOURCE_TYPE_SPOT_FLEET:
                        if 'fleet_request_id' in status_data:
                            spot_fleet_jobs[job_id] = status_data['fleet_request_id']

                    # Still track primary instance ID for all job types
                    if 'instance_id' in status_data:
                        instance_ids.append(status_data['instance_id'])

        if not running_jobs_data:
            return

        # Get instance statuses
        instance_statuses = {}

        # Process in batches of 100 (AWS API limit)
        for i in range(0, len(instance_ids), 100):
            batch = instance_ids[i:i+100]
            try:
                response = ec2.describe_instances(InstanceIds=batch)

                for reservation in response['Reservations']:
                    for instance in reservation['Instances']:
                        instance_id = instance['InstanceId']
                        state = instance['State']['Name']
                        instance_statuses[instance_id] = EC2_STATUS_MAPPING.get(state, 'UNKNOWN')
            except ClientError as e:
                if 'InvalidInstanceID.NotFound' in str(e):
                    # Mark instances not found as completed
                    for instance_id in batch:
                        if instance_id not in instance_statuses:
                            instance_statuses[instance_id] = 'COMPLETED'
                else:
                    raise

        # Check Spot Fleet statuses if there are any
        fleet_statuses = {}
        if spot_fleet_jobs:
            # Get all fleet request IDs
            fleet_request_ids = list(spot_fleet_jobs.values())

            # Process in batches of 100 (AWS API limit)
            for i in range(0, len(fleet_request_ids), 100):
                batch = fleet_request_ids[i:i+100]
                try:
                    response = ec2.describe_spot_fleet_requests(SpotFleetRequestIds=batch)

                    for config in response.get('SpotFleetRequestConfigs', []):
                        fleet_id = config['SpotFleetRequestId']
                        state = config['SpotFleetRequestState']

                        # Map fleet state to job status
                        if state == 'active':
                            fleet_statuses[fleet_id] = 'RUNNING'
                        elif state in ['cancelled', 'cancelled_running', 'cancelled_terminating']:
                            fleet_statuses[fleet_id] = 'CANCELED'
                        elif state == 'error':
                            fleet_statuses[fleet_id] = 'FAILED'
                        else:
                            fleet_statuses[fleet_id] = 'UNKNOWN'
                except ClientError as e:
                    if 'InvalidSpotFleetRequestId.NotFound' in str(e):
                        # Mark fleets not found as completed
                        for fleet_id in batch:
                            if fleet_id not in fleet_statuses:
                                fleet_statuses[fleet_id] = 'COMPLETED'
                    else:
                        raise

        # Update job statuses
        for job_id, status_data in running_jobs_data:
            # Handle differently based on the resource type
            if 'resource_type' in status_data and status_data['resource_type'] == RESOURCE_TYPE_SPOT_FLEET:
                # This is a Spot Fleet job
                fleet_request_id = status_data.get('fleet_request_id')

                if fleet_request_id and fleet_request_id in fleet_statuses:
                    fleet_status = fleet_statuses[fleet_request_id]

                    # Only update if status has changed
                    if fleet_status != 'RUNNING':
                        update_job_status(
                            job_id,
                            fleet_status,
                            status_data.get('instance_id'),
                            fleet_request_id=fleet_request_id,
                            all_instance_ids=status_data.get('all_instance_ids')
                        )
                        logger.info(f"Spot Fleet job {job_id} (fleet: {fleet_request_id}) changed state to {fleet_status}")

                # Additionally check the primary instance status
                instance_id = status_data.get('instance_id')
                if instance_id and instance_id in instance_statuses:
                    instance_status = instance_statuses[instance_id]

                    # If the instance is no longer running but the fleet is still active,
                    # the fleet might have replaced the instance. Don't update job status in that case.
                    if instance_status != 'RUNNING' and (not fleet_request_id or fleet_statuses.get(fleet_request_id) != 'RUNNING'):
                        update_job_status(
                            job_id,
                            instance_status,
                            instance_id,
                            fleet_request_id=fleet_request_id,
                            all_instance_ids=status_data.get('all_instance_ids')
                        )
                        logger.info(f"Spot Fleet job {job_id} primary instance {instance_id} changed state to {instance_status}")
            else:
                # Regular EC2 instance job
                instance_id = status_data.get('instance_id')
                if instance_id and instance_id in instance_statuses:
                    instance_status = instance_statuses[instance_id]

                    if instance_status != 'RUNNING':
                        update_job_status(job_id, instance_status, instance_id)
                        logger.info(f"Job {job_id} on instance {instance_id} changed state to {instance_status}")
    except Exception as e:
        logger.error(f"Error updating running job status: {e}")
        traceback.print_exc()

def handle_cancel_requests():
    """Handle job cancellation requests."""
    session = get_session()
    ssm = session.client('ssm')
    ec2 = session.client('ec2')

    try:
        # Check for cancel parameter
        cancel_param_name = f'{SSM_PARAMETER_PREFIX}/cancel'

        try:
            cancel_param = ssm.get_parameter(Name=cancel_param_name)
            cancel_data = json.loads(cancel_param['Parameter']['Value'])
            job_ids = cancel_data.get('job_ids', [])
            spot_fleet_jobs = cancel_data.get('spot_fleet_jobs', {})

            if not job_ids:
                return

            # Get instance IDs for these jobs
            instance_ids = []
            # Track spot fleet cancellations
            fleet_request_ids = []
            all_fleet_instance_ids = []

            for job_id in job_ids:
                try:
                    status_param = ssm.get_parameter(Name=f'{JOB_STATUS_PREFIX}/{job_id}')
                    status_data = json.loads(status_param['Parameter']['Value'])

                    # Handle differently based on the resource type
                    if 'resource_type' in status_data and status_data['resource_type'] == RESOURCE_TYPE_SPOT_FLEET:
                        # This is a Spot Fleet job
                        if 'fleet_request_id' in status_data:
                            fleet_request_ids.append(status_data['fleet_request_id'])

                        # Add the primary instance ID and any other instance IDs
                        if 'instance_id' in status_data:
                            instance_ids.append(status_data['instance_id'])

                        if 'all_instance_ids' in status_data:
                            all_fleet_instance_ids.extend(status_data['all_instance_ids'])

                        # Update status to CANCELED
                        update_job_status(
                            job_id,
                            'CANCELED',
                            status_data.get('instance_id'),
                            fleet_request_id=status_data.get('fleet_request_id'),
                            all_instance_ids=status_data.get('all_instance_ids')
                        )
                    else:
                        # Regular EC2 instance job
                        if 'instance_id' in status_data:
                            instance_ids.append(status_data['instance_id'])
                            update_job_status(job_id, 'CANCELED', status_data['instance_id'])
                except ClientError as e:
                    if e.response['Error']['Code'] != 'ParameterNotFound':
                        raise

            # First cancel Spot Fleet requests
            if fleet_request_ids:
                try:
                    ec2.cancel_spot_fleet_requests(
                        SpotFleetRequestIds=fleet_request_ids,
                        TerminateInstances=True
                    )
                    logger.info(f"Canceled Spot Fleet requests: {fleet_request_ids}")
                except Exception as fleet_error:
                    logger.error(f"Error canceling Spot Fleet requests: {fleet_error}")
                    # Continue to terminate any instances we know about

                    # If we have the all_instance_ids, add them to our list
                    if all_fleet_instance_ids:
                        # Remove duplicates
                        unique_ids = set(all_fleet_instance_ids) - set(instance_ids)
                        instance_ids.extend(list(unique_ids))

            # Now terminate all remaining instances
            if instance_ids:
                # Filter out any duplicates
                unique_instance_ids = list(set(instance_ids))
                ec2.terminate_instances(InstanceIds=unique_instance_ids)
                logger.info(f"Terminated {len(unique_instance_ids)} instances for jobs: {job_ids}")

            # Delete the cancel parameter
            ssm.delete_parameter(Name=cancel_param_name)
        except ClientError as e:
            if e.response['Error']['Code'] != 'ParameterNotFound':
                raise
    except Exception as e:
        logger.error(f"Error handling cancel requests: {e}")
        traceback.print_exc()

def main():
    """Main function for the bastion manager."""
    logger.info(f"Starting Parsl bastion manager for workflow {WORKFLOW_ID}")

    while True:
        try:
            # Update status of running jobs
            update_running_job_status()

            # Handle cancel requests
            handle_cancel_requests()

            # Process pending jobs
            pending_jobs = get_pending_jobs()

            for job in pending_jobs:
                logger.info(f"Processing job {job['id']}")
                update_job_status(job['id'], 'SUBMITTING')
                instance_id = launch_instance(job)

                if instance_id:
                    # Successfully launched
                    update_last_activity()

            # Sleep before next iteration
            time.sleep(10)
        except Exception as e:
            logger.error(f"Error in main loop: {e}")
            traceback.print_exc()
            time.sleep(30)  # Longer sleep after error

if __name__ == '__main__':
    main()
'''

    def submit_job(
        self,
        job_id: str,
        command: str,
        tasks_per_node: int,
        job_name: Optional[str] = None,
    ) -> str:
        """Submit a job for execution.

        Parameters
        ----------
        job_id : str
            Unique identifier for the job
        command : str
            Command to execute
        tasks_per_node : int
            Number of tasks to run per node
        job_name : Optional[str], optional
            Human-readable name for the job, by default None

        Returns
        -------
        str
            Resource ID for tracking the job

        Raises
        ------
        OperatingModeError
            If job submission fails
        """
        # Check if the mode is initialized
        if not self.initialized:
            raise OperatingModeError(
                "DetachedMode must be initialized before submitting jobs"
            )

        # Validate image_id
        if not self.image_id:
            self.image_id = get_default_ami(self.session.region_name)
            logger.info(
                f"Using default AMI {self.image_id} for region {self.session.region_name}"
            )

        logger.info(f"Submitting job {job_id} ({job_name if job_name else 'unnamed'})")

        try:
            # Create a unique resource ID for the job
            resource_id = f"job-{job_id}-{str(uuid.uuid4())[:8]}"

            # Submit the job to the bastion host via SSM Parameter Store
            ssm = self.session.client("ssm")

            job_data = {
                "command": command,
                "image_id": self.image_id,
                "instance_type": self.instance_type,
                "subnet_id": self.subnet_id,
                "security_group_id": self.security_group_id,
                "key_name": self.key_name,
                "tasks_per_node": tasks_per_node,
                "auto_shutdown": self.auto_shutdown,
                "job_name": job_name or "unnamed",
                "submitted_at": time.time(),
                # Add Spot Fleet specific fields
                "use_spot": self.use_spot,
                "use_spot_fleet": self.use_spot_fleet,
                "instance_types": self.instance_types,
                "nodes_per_block": self.nodes_per_block,
                "spot_max_price": self.spot_max_price,
                "spot_max_price_percentage": self.spot_max_price_percentage,
            }

            # Store job in SSM Parameter Store
            ssm.put_parameter(
                Name=f"/parsl/workflows/{self.workflow_id}/jobs/{job_id}",
                Value=json.dumps(job_data),
                Type="String",
                Overwrite=True,
            )

            # Initialize job status
            status_data = {
                "status": STATUS_PENDING,
                "submitted_at": time.time(),
                "resource_id": resource_id,
            }

            ssm.put_parameter(
                Name=f"/parsl/workflows/{self.workflow_id}/status/{job_id}",
                Value=json.dumps(status_data),
                Type="String",
                Overwrite=True,
            )

            # Track the resource
            self.resources[resource_id] = {
                "type": RESOURCE_TYPE_EC2,
                "job_id": job_id,
                "job_name": job_name or "unnamed",
                "status": STATUS_PENDING,
                "created_at": time.time(),
                "command": command,
                "tasks_per_node": tasks_per_node,
            }

            # Save state
            self.save_state()

            logger.info(f"Submitted job {job_id} with resource ID {resource_id}")
            return resource_id
        except Exception as e:
            logger.error(f"Failed to submit job {job_id}: {e}")
            raise OperatingModeError(f"Failed to submit job {job_id}: {e}") from e

    def get_job_status(self, resource_ids: List[str]) -> Dict[str, str]:
        """Get the status of jobs.

        Parameters
        ----------
        resource_ids : List[str]
            List of resource IDs to check

        Returns
        -------
        Dict[str, str]
            Dictionary mapping resource IDs to status strings
        """
        if not resource_ids:
            return {}

        status_map = {}
        ssm = self.session.client("ssm")

        for resource_id in resource_ids:
            resource = self.resources.get(resource_id)
            if not resource:
                status_map[resource_id] = STATUS_UNKNOWN
                continue

            job_id = resource.get("job_id")
            if not job_id:
                status_map[resource_id] = STATUS_UNKNOWN
                continue

            try:
                # Get job status from SSM Parameter Store
                response = ssm.get_parameter(
                    Name=f"/parsl/workflows/{self.workflow_id}/status/{job_id}"
                )

                status_data = json.loads(response["Parameter"]["Value"])
                status = status_data.get("status", STATUS_UNKNOWN)

                # Update resource state
                if resource_id in self.resources:
                    self.resources[resource_id]["status"] = status

                    # Update additional Spot Fleet information if present
                    if "fleet_request_id" in status_data:
                        self.resources[resource_id]["fleet_request_id"] = status_data[
                            "fleet_request_id"
                        ]
                        self.resources[resource_id][
                            "resource_type"
                        ] = RESOURCE_TYPE_SPOT_FLEET

                    if "all_instance_ids" in status_data:
                        self.resources[resource_id]["all_instance_ids"] = status_data[
                            "all_instance_ids"
                        ]

                status_map[resource_id] = status
            except ClientError as e:
                if "ParameterNotFound" in str(e):
                    # If parameter doesn't exist, job is unknown
                    status_map[resource_id] = STATUS_UNKNOWN
                else:
                    logger.error(f"Failed to get job status for {job_id}: {e}")
                    status_map[resource_id] = STATUS_UNKNOWN
            except Exception as e:
                logger.error(f"Unexpected error getting job status for {job_id}: {e}")
                status_map[resource_id] = STATUS_UNKNOWN

        # Save state with updated status
        self.save_state()

        return status_map

    def cancel_jobs(self, resource_ids: List[str]) -> Dict[str, str]:
        """Cancel jobs.

        Parameters
        ----------
        resource_ids : List[str]
            List of resource IDs to cancel

        Returns
        -------
        Dict[str, str]
            Dictionary mapping resource IDs to status strings
        """
        if not resource_ids:
            return {}

        cancel_map = {}
        ssm = self.session.client("ssm")

        # Collect job IDs to cancel and separately track Spot Fleet resources
        job_ids = []
        fleet_jobs = {}  # Map of job_id to fleet_request_id

        for resource_id in resource_ids:
            resource = self.resources.get(resource_id)
            if not resource or not resource.get("job_id"):
                cancel_map[resource_id] = STATUS_UNKNOWN
                continue

            job_id = resource.get("job_id")
            job_ids.append(job_id)

            # Check if this is a Spot Fleet resource
            if resource.get(
                "resource_type"
            ) == RESOURCE_TYPE_SPOT_FLEET and resource.get("fleet_request_id"):
                fleet_jobs[job_id] = resource.get("fleet_request_id")

            # Mark as canceling in local state
            self.resources[resource_id]["status"] = STATUS_CANCELED
            cancel_map[resource_id] = STATUS_CANCELED

        if job_ids:
            try:
                # Submit cancel request to bastion host with Spot Fleet information
                cancel_data = {
                    "job_ids": job_ids,
                    "requested_at": time.time(),
                    "spot_fleet_jobs": fleet_jobs,
                }

                ssm.put_parameter(
                    Name=f"/parsl/workflows/{self.workflow_id}/cancel",
                    Value=json.dumps(cancel_data),
                    Type="String",
                    Overwrite=True,
                )

                # Log different message depending on whether we're canceling fleet jobs
                if fleet_jobs:
                    logger.info(
                        f"Requested cancellation of {len(job_ids)} jobs including {len(fleet_jobs)} Spot Fleet jobs"
                    )
                else:
                    logger.info(f"Requested cancellation of {len(job_ids)} jobs")
            except Exception as e:
                logger.error(f"Failed to submit cancel request: {e}")
                # Still return success since we can't easily check if the cancel worked

        # Save state with updated status
        self.save_state()

        return cancel_map

    def cleanup_resources(self, resource_ids: List[str]) -> None:
        """Clean up resources.

        Parameters
        ----------
        resource_ids : List[str]
            List of resource IDs to clean up
        """
        if not resource_ids:
            return

        ssm = self.session.client("ssm")
        ec2 = self.session.client("ec2")

        # First, cancel any active jobs
        active_resources = []
        spot_fleet_resources = []

        for resource_id in resource_ids:
            resource = self.resources.get(resource_id)
            if not resource:
                continue

            # Track Spot Fleet resources separately to ensure they are properly cleaned up
            if resource.get("resource_type") == RESOURCE_TYPE_SPOT_FLEET:
                spot_fleet_resources.append(resource_id)

            if (
                resource.get("type") == RESOURCE_TYPE_EC2
                or resource.get("resource_type") == RESOURCE_TYPE_SPOT_FLEET
            ):
                status = resource.get("status")
                if status in [STATUS_PENDING, STATUS_RUNNING]:
                    active_resources.append(resource_id)

        # Cancel all active jobs
        if active_resources:
            self.cancel_jobs(active_resources)

        # Ensure all Spot Fleet requests are cancelled explicitly
        for resource_id in spot_fleet_resources:
            resource = self.resources.get(resource_id)
            if resource and resource.get("fleet_request_id"):
                fleet_request_id = resource.get("fleet_request_id")
                try:
                    # Cancel the Spot Fleet request and terminate instances
                    ec2.cancel_spot_fleet_requests(
                        SpotFleetRequestIds=[fleet_request_id], TerminateInstances=True
                    )
                    logger.info(
                        f"Explicitly cancelled Spot Fleet request {fleet_request_id} during cleanup"
                    )
                except Exception as e:
                    logger.error(
                        f"Error cancelling Spot Fleet request {fleet_request_id} during cleanup: {e}"
                    )

        # Now clean up tracking in SSM
        for resource_id in resource_ids:
            resource = self.resources.get(resource_id)
            if not resource:
                continue

            job_id = resource.get("job_id")
            if job_id:
                try:
                    # Clean up SSM parameters
                    ssm.delete_parameter(
                        Name=f"/parsl/workflows/{self.workflow_id}/jobs/{job_id}"
                    )
                    ssm.delete_parameter(
                        Name=f"/parsl/workflows/{self.workflow_id}/status/{job_id}"
                    )
                except ClientError as e:
                    if "ParameterNotFound" not in str(e):
                        logger.error(
                            f"Failed to clean up parameters for job {job_id}: {e}"
                        )
                except Exception as e:
                    logger.error(
                        f"Unexpected error cleaning up parameters for job {job_id}: {e}"
                    )

            # Remove from local tracking
            if resource_id in self.resources:
                del self.resources[resource_id]

        # Save state with updated resources
        self.save_state()

    def cleanup_infrastructure(self) -> None:
        """Clean up infrastructure created by this mode.

        This cleans up the VPC, subnet, and security group if they were created by the provider.
        The bastion host is preserved if preserve_bastion is True.
        """
        logger.info("Cleaning up infrastructure")

        # Delete all resources first
        if self.resources:
            # Get resource IDs except bastion host
            resource_ids = []
            for resource_id, resource in self.resources.items():
                if (
                    resource.get("type") != RESOURCE_TYPE_BASTION
                    and resource.get("type") != RESOURCE_TYPE_CLOUDFORMATION
                ):
                    resource_ids.append(resource_id)

            if resource_ids:
                self.cleanup_resources(resource_ids)
                logger.info(f"Cleaned up {len(resource_ids)} resources")

        # Delete bastion host if not preserving
        if not self.preserve_bastion and self.bastion_id:
            logger.info(f"Cleaning up bastion host {self.bastion_id}")

            if self.bastion_host_type == "cloudformation":
                cf = self.session.client("cloudformation")
                try:
                    cf.delete_stack(StackName=self.bastion_id)
                    logger.info(
                        f"Initiated deletion of bastion stack {self.bastion_id}"
                    )

                    # Remove from resources
                    if self.bastion_id in self.resources:
                        del self.resources[self.bastion_id]
                except Exception as e:
                    logger.error(
                        f"Failed to delete bastion stack {self.bastion_id}: {e}"
                    )
            else:
                ec2 = self.session.client("ec2")
                try:
                    ec2.terminate_instances(InstanceIds=[self.bastion_id])
                    logger.info(f"Terminated bastion instance {self.bastion_id}")

                    # Remove from resources
                    if self.bastion_id in self.resources:
                        del self.resources[self.bastion_id]
                except ClientError as e:
                    if "InvalidInstanceID.NotFound" not in str(e):
                        logger.error(
                            f"Failed to terminate bastion instance {self.bastion_id}: {e}"
                        )
                except Exception as e:
                    logger.error(
                        f"Unexpected error terminating bastion instance {self.bastion_id}: {e}"
                    )

            self.bastion_id = None

        # Only delete networking if not preserving bastion
        if not self.preserve_bastion:
            try:
                # Stop spot interruption monitoring if enabled
                if self.spot_interruption_monitor:
                    try:
                        self.spot_interruption_monitor.stop_monitoring()
                        logger.info("Stopped spot interruption monitoring")
                    except Exception as e:
                        logger.error(
                            f"Failed to stop spot interruption monitoring: {e}"
                        )
                    self.spot_interruption_monitor = None
                    self.spot_interruption_handler = None

                # Delete security group
                if self.security_group_id:
                    try:
                        delete_resource(
                            self.security_group_id,
                            self.session,
                            RESOURCE_TYPE_SECURITY_GROUP,
                        )
                        logger.info(f"Deleted security group {self.security_group_id}")
                        self.security_group_id = None
                    except ResourceNotFoundError:
                        logger.debug(
                            f"Security group {self.security_group_id} not found or already deleted"
                        )
                        self.security_group_id = None
                    except Exception as e:
                        logger.error(
                            f"Failed to delete security group {self.security_group_id}: {e}"
                        )

                # Delete subnet
                if self.subnet_id:
                    try:
                        delete_resource(
                            self.subnet_id, self.session, RESOURCE_TYPE_SUBNET
                        )
                        logger.info(f"Deleted subnet {self.subnet_id}")
                        self.subnet_id = None
                    except ResourceNotFoundError:
                        logger.debug(
                            f"Subnet {self.subnet_id} not found or already deleted"
                        )
                        self.subnet_id = None
                    except Exception as e:
                        logger.error(f"Failed to delete subnet {self.subnet_id}: {e}")

                # Delete VPC
                if self.vpc_id:
                    # Detach and delete internet gateways first
                    ec2 = self.session.client("ec2")
                    try:
                        igw_response = ec2.describe_internet_gateways(
                            Filters=[
                                {"Name": "attachment.vpc-id", "Values": [self.vpc_id]}
                            ]
                        )

                        for igw in igw_response.get("InternetGateways", []):
                            igw_id = igw["InternetGatewayId"]
                            ec2.detach_internet_gateway(
                                InternetGatewayId=igw_id, VpcId=self.vpc_id
                            )
                            ec2.delete_internet_gateway(InternetGatewayId=igw_id)
                            logger.debug(f"Deleted internet gateway {igw_id}")
                    except Exception as e:
                        logger.error(
                            f"Failed to delete internet gateways for VPC {self.vpc_id}: {e}"
                        )

                    # Now delete the VPC
                    try:
                        delete_resource(
                            self.vpc_id, self.session, RESOURCE_TYPE_VPC, force=True
                        )
                        logger.info(f"Deleted VPC {self.vpc_id}")
                        self.vpc_id = None
                    except ResourceNotFoundError:
                        logger.debug(f"VPC {self.vpc_id} not found or already deleted")
                        self.vpc_id = None
                    except Exception as e:
                        logger.error(f"Failed to delete VPC {self.vpc_id}: {e}")

                # Clear initialization flag only if we're cleaning up everything
                self.initialized = False
            except Exception as e:
                logger.error(f"Failed to clean up infrastructure: {e}")

        # Save state
        self.save_state()

        logger.info("Infrastructure cleanup complete")

    def list_resources(self) -> Dict[str, List[Dict[str, Any]]]:
        """List all resources created by this mode.

        Returns
        -------
        Dict[str, List[Dict[str, Any]]]
            Dictionary of resource types and their details
        """
        result: Dict[str, List[Dict[str, Any]]] = {
            "ec2_instances": [],
            "bastion_host": [],
            "vpc": [],
            "subnet": [],
            "security_group": [],
        }

        # Add EC2 worker instances
        for resource_id, resource in self.resources.items():
            if resource.get("type") == RESOURCE_TYPE_EC2:
                result["ec2_instances"].append(
                    {
                        "id": resource_id,
                        "job_id": resource.get("job_id"),
                        "job_name": resource.get("job_name"),
                        "status": resource.get("status"),
                        "created_at": resource.get("created_at"),
                    }
                )
            elif (
                resource.get("type") == RESOURCE_TYPE_BASTION
                or resource.get("type") == RESOURCE_TYPE_CLOUDFORMATION
            ):
                result["bastion_host"].append(
                    {
                        "id": resource_id,
                        "type": resource.get("type"),
                        "workflow_id": resource.get("workflow_id"),
                        "created_at": resource.get("created_at"),
                    }
                )

        # Add VPC if available
        if self.vpc_id:
            result["vpc"].append(
                {
                    "id": self.vpc_id,
                }
            )

        # Add subnet if available
        if self.subnet_id:
            result["subnet"].append(
                {
                    "id": self.subnet_id,
                    "vpc_id": self.vpc_id,
                }
            )

        # Add security group if available
        if self.security_group_id:
            result["security_group"].append(
                {
                    "id": self.security_group_id,
                    "vpc_id": self.vpc_id,
                }
            )

        return result

    def cleanup_all(self) -> None:
        """Clean up all resources created by this mode."""
        logger.info("Cleaning up all resources")

        # First clean up Spot Fleet IAM roles if we're using Spot Fleet
        if self.use_spot_fleet:
            try:
                logger.info(
                    f"Cleaning up Spot Fleet resources for workflow {self.workflow_id}"
                )
                cleanup_results = cleanup_all_spot_fleet_resources(
                    session=self.session,
                    workflow_id=self.workflow_id,
                    cancel_active_requests=True,
                    cleanup_iam_roles=True,
                )

                # Log cleanup results
                if cleanup_results["cancelled_requests"]:
                    logger.info(
                        f"Cancelled {len(cleanup_results['cancelled_requests'])} Spot Fleet requests"
                    )

                if cleanup_results["cleaned_roles"]:
                    logger.info(
                        f"Cleaned up {len(cleanup_results['cleaned_roles'])} Spot Fleet IAM roles"
                    )

                if cleanup_results["errors"]:
                    logger.warning(
                        f"Encountered {len(cleanup_results['errors'])} errors during Spot Fleet cleanup"
                    )
                    for error in cleanup_results["errors"]:
                        logger.warning(f"Spot Fleet cleanup error: {error}")
            except Exception as e:
                logger.error(f"Error cleaning up Spot Fleet resources: {e}")
                # Continue with regular cleanup

        # Call cleanup_infrastructure with preserve_bastion=False
        old_value = self.preserve_bastion
        self.preserve_bastion = False
        self.cleanup_infrastructure()
        self.preserve_bastion = old_value

    def save_state(self) -> None:
        """Save the current state to the state store."""
        state = {
            "resources": self.resources,
            "provider_id": self.provider_id,
            "mode": self.__class__.__name__,
            "vpc_id": self.vpc_id,
            "subnet_id": self.subnet_id,
            "security_group_id": self.security_group_id,
            "initialized": self.initialized,
            "workflow_id": self.workflow_id,
            "bastion_id": self.bastion_id,
            "bastion_host_type": self.bastion_host_type,
            "stack_name": self.stack_name,
            "spot_interruption_handling": self.spot_interruption_handling,
        }

        try:
            self.state_store.save_state(state)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def load_state(self) -> bool:
        """Load state from the state store.

        Returns
        -------
        bool
            True if state was loaded successfully, False otherwise
        """
        try:
            state = self.state_store.load_state()
            if state and state.get("provider_id") == self.provider_id:
                self.resources = state.get("resources", {})
                self.vpc_id = state.get("vpc_id", self.vpc_id)
                self.subnet_id = state.get("subnet_id", self.subnet_id)
                self.security_group_id = state.get(
                    "security_group_id", self.security_group_id
                )
                self.initialized = state.get("initialized", False)
                self.workflow_id = state.get("workflow_id", self.workflow_id)
                self.bastion_id = state.get("bastion_id", self.bastion_id)
                self.bastion_host_type = state.get(
                    "bastion_host_type", self.bastion_host_type
                )
                self.stack_name = state.get("stack_name", self.stack_name)

                # Check if spot interruption handling was previously enabled
                previous_spot_handling = state.get("spot_interruption_handling", False)
                if previous_spot_handling != self.spot_interruption_handling:
                    logger.info(
                        f"Spot interruption handling changed from {previous_spot_handling} to {self.spot_interruption_handling}"
                    )

                    # Initialize or clean up spot interruption handling based on new setting
                    if self.spot_interruption_handling and self.use_spot:
                        if not self.spot_interruption_monitor:
                            logger.debug(
                                "Initializing SpotInterruptionMonitor and Handler after state load"
                            )
                            self.spot_interruption_monitor = SpotInterruptionMonitor(
                                self.session
                            )
                            self.spot_interruption_handler = (
                                ParslSpotInterruptionHandler(
                                    session=self.session,
                                    checkpoint_bucket=self.checkpoint_bucket,
                                    checkpoint_prefix=self.checkpoint_prefix,
                                )
                            )
                            self.spot_interruption_monitor.start_monitoring()
                    elif (
                        not self.spot_interruption_handling
                        and self.spot_interruption_monitor
                    ):
                        logger.debug(
                            "Stopping SpotInterruptionMonitor after state load"
                        )
                        self.spot_interruption_monitor.stop_monitoring()
                        self.spot_interruption_monitor = None
                        self.spot_interruption_handler = None

                # Re-register existing spot instances with interruption monitor if needed
                if (
                    self.spot_interruption_handling
                    and self.spot_interruption_monitor
                    and self.spot_interruption_handler
                ):
                    for resource_id, resource in self.resources.items():
                        if resource.get("type") == RESOURCE_TYPE_EC2 and resource.get(
                            "is_spot", False
                        ):
                            self.spot_interruption_monitor.register_instance(
                                resource_id,
                                self.spot_interruption_handler.handle_instance_interruption,
                            )
                            logger.info(
                                f"Re-registered spot instance {resource_id} for interruption handling"
                            )
                        elif resource.get(
                            "type"
                        ) == RESOURCE_TYPE_SPOT_FLEET and resource.get(
                            "fleet_request_id"
                        ):
                            fleet_request_id = resource.get("fleet_request_id")
                            self.spot_interruption_monitor.register_fleet(
                                fleet_request_id,
                                self.spot_interruption_handler.handle_fleet_interruption,
                            )
                            logger.info(
                                f"Re-registered spot fleet {fleet_request_id} for interruption handling"
                            )

                logger.debug(f"Loaded state with {len(self.resources)} resources")
                return True
        except Exception as e:
            logger.error(f"Failed to load state: {e}")

        return False

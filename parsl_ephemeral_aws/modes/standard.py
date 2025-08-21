"""
Standard operating mode for the EphemeralAWSProvider.

The standard mode uses EC2 instances for computation with direct communication
between the client and worker nodes.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

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
    EC2_STATUS_MAPPING,
    RESOURCE_TYPE_EC2,
    RESOURCE_TYPE_SECURITY_GROUP,
    RESOURCE_TYPE_SUBNET,
    RESOURCE_TYPE_VPC,
    RESOURCE_TYPE_SPOT_FLEET,
    STATUS_CANCELED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_UNKNOWN,
)
from parsl_ephemeral_aws.exceptions import (
    NetworkCreationError,
    OperatingModeError,
    ResourceCreationError,
    ResourceNotFoundError,
    SpotFleetError,
)
from parsl_ephemeral_aws.modes.base import OperatingMode
from parsl_ephemeral_aws.compute.spot_fleet import SpotFleetManager
from parsl_ephemeral_aws.compute.spot_interruption import (
    SpotInterruptionMonitor,
    ParslSpotInterruptionHandler,
)
from parsl_ephemeral_aws.utils.aws import (
    create_tags,
    delete_resource,
    get_default_ami,
    wait_for_resource,
)


logger = logging.getLogger(__name__)


class StandardMode(OperatingMode):
    """Standard operating mode implementation.

    In standard mode, EC2 instances are created for computation with direct
    communication between the client and worker nodes.

    This mode supports regular EC2 instances, spot instances, and spot fleet
    requests for more reliable and cost-effective computation.
    """

    def __init__(
        self,
        provider_id: str,
        session: boto3.Session,
        state_store: Any,
        image_id: Optional[str] = None,
        instance_type: str = "t3.micro",
        worker_init: str = "",
        vpc_id: Optional[str] = None,
        subnet_id: Optional[str] = None,
        security_group_id: Optional[str] = None,
        key_name: Optional[str] = None,
        use_spot: bool = False,
        spot_max_price: Optional[str] = None,
        spot_allocation_strategy: str = "capacity-optimized",
        additional_tags: Optional[Dict[str, str]] = None,
        auto_shutdown: bool = True,
        max_idle_time: int = 300,
        create_vpc: bool = True,
        use_public_ips: bool = True,
        custom_ami: bool = False,
        debug: bool = False,
        use_spot_fleet: bool = False,
        instance_types: Optional[List[str]] = None,
        nodes_per_block: int = 1,
        spot_max_price_percentage: Optional[int] = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the standard mode.

        Parameters
        ----------
        provider_id : str
            Unique identifier for the provider instance
        session : boto3.Session
            AWS session for API calls
        state_store : Any
            Store for persisting state
        image_id : Optional[str], optional
            EC2 AMI ID to use for instances, by default None
        instance_type : str, optional
            EC2 instance type for compute resources, by default "t3.micro"
        worker_init : str, optional
            Script to execute during worker initialization, by default ""
        vpc_id : Optional[str], optional
            Existing VPC ID to use, by default None
        subnet_id : Optional[str], optional
            Existing subnet ID to use, by default None
        security_group_id : Optional[str], optional
            Existing security group ID to use, by default None
        key_name : Optional[str], optional
            EC2 key pair name for SSH access, by default None
        use_spot : bool, optional
            Whether to use spot instances, by default False
        spot_max_price : Optional[str], optional
            Maximum price for spot instances, by default None
        spot_allocation_strategy : str, optional
            Allocation strategy for spot instances, by default "capacity-optimized"
        additional_tags : Optional[Dict[str, str]], optional
            Tags to apply to created resources, by default None
        auto_shutdown : bool, optional
            Whether to automatically shut down idle resources, by default True
        max_idle_time : int, optional
            Maximum idle time in seconds before shutdown, by default 300
        create_vpc : bool, optional
            Whether to create a new VPC if vpc_id is not provided, by default True
        use_public_ips : bool, optional
            Whether to assign public IPs to instances, by default True
        custom_ami : bool, optional
            Whether image_id refers to a custom AMI, by default False
        debug : bool, optional
            Whether to enable debug logging, by default False
        use_spot_fleet : bool, optional
            Whether to use Spot Fleet for spot instances, by default False
        instance_types : Optional[List[str]], optional
            List of instance types to use with Spot Fleet, by default None
        nodes_per_block : int, optional
            Number of nodes per block, by default 1
        spot_max_price_percentage : Optional[int], optional
            Maximum spot price as a percentage of on-demand price, by default None
        """
        # Call parent __init__ with standard params
        super().__init__(
            provider_id=provider_id,
            session=session,
            state_store=state_store,
            image_id=image_id,
            instance_type=instance_type,
            worker_init=worker_init,
            vpc_id=vpc_id,
            subnet_id=subnet_id,
            security_group_id=security_group_id,
            key_name=key_name,
            use_spot=use_spot,
            spot_max_price=spot_max_price,
            spot_allocation_strategy=spot_allocation_strategy,
            additional_tags=additional_tags,
            auto_shutdown=auto_shutdown,
            max_idle_time=max_idle_time,
            create_vpc=create_vpc,
            use_public_ips=use_public_ips,
            custom_ami=custom_ami,
            debug=debug,
            **kwargs,
        )

        # Standard mode specific attributes
        self.use_spot_fleet = use_spot_fleet
        self.instance_types = instance_types or []
        self.nodes_per_block = nodes_per_block
        self.spot_max_price_percentage = spot_max_price_percentage

        # Initialize SpotFleetManager if using spot fleet
        self.spot_fleet_manager = None
        self.spot_interruption_monitor = None
        self.spot_interruption_handler = None

        if self.use_spot and self.use_spot_fleet:
            # Create a simplified provider object for the SpotFleetManager
            # The SpotFleetManager expects a provider object with certain attributes
            provider = type(
                "SimpleProvider",
                (),
                {
                    "workflow_id": self.provider_id,
                    "region": self.session.region_name,
                    "aws_access_key_id": session._session.get_credentials().access_key,
                    "aws_secret_access_key": session._session.get_credentials().secret_key,
                    "aws_session_token": session._session.get_credentials().token,
                    "aws_profile": None,
                    "vpc_id": self.vpc_id,
                    "subnet_id": self.subnet_id,
                    "security_group_id": self.security_group_id,
                    "image_id": self.image_id,
                    "instance_type": self.instance_type,
                    "instance_types": self.instance_types,
                    "key_name": self.key_name,
                    "use_public_ips": self.use_public_ips,
                    "nodes_per_block": self.nodes_per_block,
                    "spot_max_price_percentage": self.spot_max_price_percentage,
                    "worker_init": self.worker_init,
                    "tags": self.additional_tags,
                },
            )

            logger.debug("Initializing SpotFleetManager for StandardMode")
            self.spot_fleet_manager = SpotFleetManager(provider)

        # Initialize spot interruption handling if enabled
        if self.use_spot and self.spot_interruption_handling:
            if not self.checkpoint_bucket and self.spot_interruption_handling:
                logger.warning(
                    "Spot interruption handling is enabled but no checkpoint bucket specified"
                )
            else:
                logger.debug("Initializing SpotInterruptionMonitor and Handler")
                self.spot_interruption_monitor = SpotInterruptionMonitor(self.session)
                self.spot_interruption_handler = ParslSpotInterruptionHandler(
                    session=self.session,
                    checkpoint_bucket=self.checkpoint_bucket,
                    checkpoint_prefix=self.checkpoint_prefix,
                )
                self.spot_interruption_monitor.start_monitoring()

    def save_state(self) -> None:
        """Save the current state to the state store."""
        # Default state
        state = {
            "resources": self.resources,
            "provider_id": self.provider_id,
            "mode": self.__class__.__name__,
            "vpc_id": self.vpc_id,
            "subnet_id": self.subnet_id,
            "security_group_id": self.security_group_id,
            "initialized": self.initialized,
            "use_spot_fleet": self.use_spot_fleet,
            "spot_interruption_handling": self.spot_interruption_handling,
        }

        # Include spot fleet state if applicable
        if self.use_spot_fleet and self.spot_fleet_manager:
            spot_fleet_state = {
                "blocks": self.spot_fleet_manager.blocks,
                "fleet_requests": self.spot_fleet_manager.fleet_requests,
                "instances": self.spot_fleet_manager.instances,
                "enabled": True,
            }
            state["spot_fleet_state"] = spot_fleet_state

            try:
                self.state_store.save_state(state)
                logger.debug(
                    f"Saved state including SpotFleetManager with {len(self.spot_fleet_manager.blocks)} blocks"
                )
            except Exception as e:
                logger.error(f"Failed to save state: {e}")
        else:
            # Call parent implementation if not using spot fleet
            super().save_state()

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

                # Load SpotFleetManager state if available
                if (
                    self.use_spot_fleet
                    and self.spot_fleet_manager
                    and state.get("use_spot_fleet", False)
                    and state.get("spot_fleet_state")
                ):
                    spot_fleet_state = state.get("spot_fleet_state", {})

                    if spot_fleet_state.get("blocks"):
                        self.spot_fleet_manager.blocks = spot_fleet_state.get(
                            "blocks", {}
                        )
                    if spot_fleet_state.get("fleet_requests"):
                        self.spot_fleet_manager.fleet_requests = spot_fleet_state.get(
                            "fleet_requests", {}
                        )
                    if spot_fleet_state.get("instances"):
                        self.spot_fleet_manager.instances = spot_fleet_state.get(
                            "instances", {}
                        )

                    logger.debug(
                        f"Loaded SpotFleetManager state with {len(self.spot_fleet_manager.blocks)} blocks"
                    )

                    # Re-register spot fleets with interruption monitor if needed
                    if (
                        self.spot_interruption_handling
                        and self.spot_interruption_monitor
                        and self.spot_interruption_handler
                    ):
                        for (
                            block_id,
                            block_data,
                        ) in self.spot_fleet_manager.blocks.items():
                            fleet_requests = block_data.get("fleet_requests", [])
                            for fleet_request_id in fleet_requests:
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

    def initialize(self) -> None:
        """Initialize standard mode infrastructure.

        Creates the necessary VPC, subnet, and security group resources
        if they don't already exist.

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

        logger.debug("Initializing standard mode infrastructure")

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

            # Save state
            self.save_state()

            logger.info(
                f"Initialized standard mode infrastructure: "
                f"vpc_id={self.vpc_id}, subnet_id={self.subnet_id}, "
                f"security_group_id={self.security_group_id}"
            )
        except Exception as e:
            logger.error(f"Failed to initialize standard mode infrastructure: {e}")
            # Try to clean up any resources we created
            self.cleanup_infrastructure()
            raise ResourceCreationError(
                f"Failed to initialize standard mode infrastructure: {e}"
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
                                "Value": f"parsl-ephemeral-{self.provider_id[:8]}",
                            },
                            {"Key": "CreatedBy", "Value": "ParslEphemeralAWSProvider"},
                            {"Key": "ProviderId", "Value": self.provider_id},
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
                                "Value": f"parsl-ephemeral-igw-{self.provider_id[:8]}",
                            },
                            {"Key": "CreatedBy", "Value": "ParslEphemeralAWSProvider"},
                            {"Key": "ProviderId", "Value": self.provider_id},
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
                                "Value": f"parsl-ephemeral-subnet-{self.provider_id[:8]}",
                            },
                            {"Key": "CreatedBy", "Value": "ParslEphemeralAWSProvider"},
                            {"Key": "ProviderId", "Value": self.provider_id},
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
                                "Value": f"parsl-ephemeral-rt-{self.provider_id[:8]}",
                            },
                            {"Key": "CreatedBy", "Value": "ParslEphemeralAWSProvider"},
                            {"Key": "ProviderId", "Value": self.provider_id},
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
                GroupName=f"{DEFAULT_SECURITY_GROUP_NAME}-{self.provider_id[:8]}",
                Description=DEFAULT_SECURITY_GROUP_DESCRIPTION,
                VpcId=self.vpc_id,
                TagSpecifications=[
                    {
                        "ResourceType": "security-group",
                        "Tags": [
                            {
                                "Key": "Name",
                                "Value": f"parsl-ephemeral-sg-{self.provider_id[:8]}",
                            },
                            {"Key": "CreatedBy", "Value": "ParslEphemeralAWSProvider"},
                            {"Key": "ProviderId", "Value": self.provider_id},
                        ],
                    }
                ],
            )

            security_group_id = response["GroupId"]
            logger.debug(
                f"Created security group {security_group_id} in VPC {self.vpc_id}"
            )

            # Add inbound rules
            if DEFAULT_INBOUND_RULES:
                ec2.authorize_security_group_ingress(
                    GroupId=security_group_id, IpPermissions=DEFAULT_INBOUND_RULES
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
            EC2 instance ID for tracking the job

        Raises
        ------
        OperatingModeError
            If job submission fails
        """
        # Ensure the mode is initialized
        self.ensure_initialized()

        # Validate image_id
        if not self.image_id:
            self.image_id = get_default_ami(self.session.region_name)
            logger.info(
                f"Using default AMI {self.image_id} for region {self.session.region_name}"
            )

        logger.info(f"Submitting job {job_id} ({job_name if job_name else 'unnamed'})")

        try:
            # Prepare worker initialization script
            init_script = self._prepare_init_script(command, job_id)

            # Create EC2 instance
            instance_id = self._create_instance(init_script, job_id, job_name)

            # Track the resource
            resource_type = RESOURCE_TYPE_EC2
            # Check if this is actually a Spot Fleet block ID
            if (
                self.use_spot_fleet
                and self.spot_fleet_manager
                and instance_id in self.spot_fleet_manager.blocks
            ):
                resource_type = RESOURCE_TYPE_SPOT_FLEET

            self.resources[instance_id] = {
                "type": resource_type,
                "job_id": job_id,
                "job_name": job_name or "unnamed",
                "status": STATUS_PENDING,
                "created_at": time.time(),
                "command": command,
                "tasks_per_node": tasks_per_node,
            }

            # Save state
            self.save_state()

            logger.info(f"Submitted job {job_id} as instance {instance_id}")
            return instance_id
        except Exception as e:
            logger.error(f"Failed to submit job {job_id}: {e}")
            raise OperatingModeError(f"Failed to submit job {job_id}: {e}") from e

    def _prepare_init_script(self, command: str, job_id: str) -> str:
        """Prepare the worker initialization script.

        Parameters
        ----------
        command : str
            Command to execute
        job_id : str
            Job ID

        Returns
        -------
        str
            Initialization script
        """
        # Start with base worker init script
        init_script = "#!/bin/bash\n"
        if self.worker_init:
            init_script += f"{self.worker_init}\n"

        # Set environment variables
        init_script += "\n# Set environment variables\n"
        init_script += f"export PARSL_JOB_ID={job_id}\n"
        init_script += f"export PARSL_PROVIDER_ID={self.provider_id}\n"
        init_script += "export PARSL_WORKER_ID=$(hostname)\n"

        # Add command
        init_script += "\n# Execute Parsl worker command\n"
        init_script += f"{command}\n"

        # Add cleanup if auto shutdown is enabled
        if self.auto_shutdown:
            init_script += "\n# Auto-shutdown\n"
            init_script += "shutdown -h now\n"

        return init_script

    def _create_instance(
        self, init_script: str, job_id: str, job_name: Optional[str] = None
    ) -> str:
        """Create an EC2 instance for the job.

        Parameters
        ----------
        init_script : str
            Initialization script
        job_id : str
            Job ID
        job_name : Optional[str], optional
            Job name, by default None

        Returns
        -------
        str
            EC2 instance ID

        Raises
        ------
        ResourceCreationError
            If instance creation fails
        """
        ec2 = self.session.client("ec2")

        # Prepare instance tags
        tags = [
            {"Key": "Name", "Value": f"parsl-worker-{job_id[:8]}"},
            {"Key": "CreatedBy", "Value": "ParslEphemeralAWSProvider"},
            {"Key": "ProviderId", "Value": self.provider_id},
            {"Key": "JobId", "Value": job_id},
        ]

        if job_name:
            tags.append({"Key": "JobName", "Value": job_name})

        # Add additional tags
        for key, value in self.additional_tags.items():
            tags.append({"Key": key, "Value": value})

        # Prepare network configuration
        network_interfaces = []
        if self.subnet_id:
            network_interface = {
                "DeviceIndex": 0,
                "SubnetId": self.subnet_id,
                "AssociatePublicIpAddress": self.use_public_ips,
            }

            if self.security_group_id:
                network_interface["Groups"] = [self.security_group_id]

            network_interfaces.append(network_interface)

        # Prepare run configuration
        run_args = {
            "ImageId": self.image_id,
            "InstanceType": self.instance_type,
            "MaxCount": 1,
            "MinCount": 1,
            "UserData": init_script,
            "TagSpecifications": [{"ResourceType": "instance", "Tags": tags}],
        }

        # Add network configuration if available
        if network_interfaces:
            run_args["NetworkInterfaces"] = network_interfaces
        elif self.security_group_id:
            run_args["SecurityGroupIds"] = [self.security_group_id]

        # Add key pair if specified
        if self.key_name:
            run_args["KeyName"] = self.key_name

        # Use spot instances if requested
        if self.use_spot:
            return self._create_spot_instance(run_args)
        else:
            # Create on-demand instance
            try:
                response = ec2.run_instances(**run_args)
                instance_id = response["Instances"][0]["InstanceId"]

                # Wait for instance to be running
                wait_for_resource(
                    instance_id, "instance_running", ec2, resource_name="EC2 instance"
                )

                return instance_id
            except Exception as e:
                logger.error(f"Failed to create EC2 instance: {e}")
                raise ResourceCreationError(
                    f"Failed to create EC2 instance: {e}"
                ) from e

    def _create_spot_instance(self, run_args: Dict[str, Any]) -> str:
        """Create a spot instance.

        Parameters
        ----------
        run_args : Dict[str, Any]
            Arguments for EC2 instance creation

        Returns
        -------
        str
            EC2 instance ID or block ID for spot fleet

        Raises
        ------
        ResourceCreationError
            If spot instance creation fails
        SpotFleetError
            If spot fleet creation fails
        """
        # Check if using spot fleet
        if self.use_spot_fleet and self.spot_fleet_manager:
            return self._create_spot_fleet_instance(run_args)

        # Traditional spot instance request
        ec2 = self.session.client("ec2")

        # Extract tags
        tags = run_args.pop("TagSpecifications", [{}])[0].get("Tags", [])

        # Prepare spot request
        spot_args = {
            "InstanceCount": 1,
            "Type": "one-time",
            "LaunchSpecification": run_args,
        }

        # Add max price if specified
        if self.spot_max_price:
            spot_args["SpotPrice"] = self.spot_max_price

        try:
            # Request spot instance
            response = ec2.request_spot_instances(**spot_args)
            request_id = response["SpotInstanceRequests"][0]["SpotInstanceRequestId"]

            # Add tags to spot request
            if tags:
                tag_spec = {"Resources": [request_id], "Tags": tags}
                ec2.create_tags(**tag_spec)

            # Wait for spot request to be fulfilled
            logger.debug(f"Waiting for spot request {request_id} to be fulfilled")
            waiter = ec2.get_waiter("spot_instance_request_fulfilled")
            waiter.wait(
                SpotInstanceRequestIds=[request_id],
                WaiterConfig={"Delay": 5, "MaxAttempts": 60},
            )

            # Get instance ID
            response = ec2.describe_spot_instance_requests(
                SpotInstanceRequestIds=[request_id]
            )
            instance_id = response["SpotInstanceRequests"][0]["InstanceId"]

            # Wait for instance to be running
            wait_for_resource(
                instance_id, "instance_running", ec2, resource_name="EC2 spot instance"
            )

            # Register with spot interruption monitor if enabled
            if (
                self.spot_interruption_handling
                and self.spot_interruption_monitor
                and self.spot_interruption_handler
            ):
                self.spot_interruption_monitor.register_instance(
                    instance_id,
                    self.spot_interruption_handler.handle_instance_interruption,
                )
                logger.info(
                    f"Registered spot instance {instance_id} for interruption handling"
                )

            return instance_id
        except Exception as e:
            logger.error(f"Failed to create spot instance: {e}")
            raise ResourceCreationError(f"Failed to create spot instance: {e}") from e

    def _create_spot_fleet_instance(self, run_args: Dict[str, Any]) -> str:
        """Create a spot fleet instance.

        Parameters
        ----------
        run_args : Dict[str, Any]
            Arguments for EC2 instance creation

        Returns
        -------
        str
            Block ID for the spot fleet

        Raises
        ------
        SpotFleetError
            If spot fleet creation fails
        """
        if not self.spot_fleet_manager:
            raise ResourceCreationError("SpotFleetManager not initialized")

        # Extract job ID from tags
        job_id = None
        if "TagSpecifications" in run_args and run_args["TagSpecifications"]:
            for tag in run_args["TagSpecifications"][0].get("Tags", []):
                if tag["Key"] == "JobId":
                    job_id = tag["Value"]
                    break

        if not job_id:
            job_id = str(uuid.uuid4())

        # Extract user data
        user_data = None
        if "UserData" in run_args:
            user_data = run_args["UserData"]
            self.spot_fleet_manager.provider.worker_init = user_data

        try:
            # Create the spot fleet
            blocks = self.spot_fleet_manager.create_blocks(1)

            if not blocks:
                logger.error(
                    "SpotFleetManager.create_blocks returned empty blocks dictionary"
                )
                raise ResourceCreationError("Failed to create Spot Fleet blocks")

            # Get the block ID (should be only one)
            block_id = next(iter(blocks.keys()))

            logger.info(f"Created Spot Fleet block {block_id} for job {job_id}")

            # Wait for block to be running
            max_wait = 300  # 5 minutes
            start_time = time.time()

            while time.time() - start_time < max_wait:
                status = self.spot_fleet_manager.get_block_status(block_id)
                logger.debug(f"Spot Fleet block {block_id} status: {status}")

                if status == STATUS_RUNNING:
                    break
                elif status in [STATUS_FAILED, STATUS_CANCELED, STATUS_COMPLETED]:
                    raise ResourceCreationError(
                        f"Spot Fleet block failed with status {status}"
                    )

                time.sleep(10)

            if time.time() - start_time >= max_wait:
                logger.warning(
                    f"Timeout waiting for Spot Fleet block {block_id} to be running"
                )

            # Register spot fleet with spot interruption monitor if enabled
            if (
                self.spot_interruption_handling
                and self.spot_interruption_monitor
                and self.spot_interruption_handler
            ):
                # Get fleet instances
                fleet_requests = self.spot_fleet_manager.blocks.get(block_id, {}).get(
                    "fleet_requests", []
                )
                for fleet_request_id in fleet_requests:
                    self.spot_interruption_monitor.register_fleet(
                        fleet_request_id,
                        self.spot_interruption_handler.handle_fleet_interruption,
                    )
                    logger.info(
                        f"Registered spot fleet {fleet_request_id} for interruption handling"
                    )

            return block_id

        except SpotFleetError as e:
            logger.error(f"Failed to create Spot Fleet: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error creating Spot Fleet: {e}")
            raise ResourceCreationError(f"Failed to create Spot Fleet: {e}") from e

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

        ec2 = self.session.client("ec2")
        status_map = {}

        # Group IDs by resource type
        ec2_instances = []
        spot_fleet_blocks = []

        for resource_id in resource_ids:
            resource = self.resources.get(resource_id)
            if not resource:
                status_map[resource_id] = STATUS_UNKNOWN
                continue

            if resource.get("type") == RESOURCE_TYPE_EC2:
                ec2_instances.append(resource_id)
            elif resource.get("type") == RESOURCE_TYPE_SPOT_FLEET:
                spot_fleet_blocks.append(resource_id)
            else:
                status_map[resource_id] = STATUS_UNKNOWN

        # Check EC2 instance status
        if ec2_instances:
            try:
                response = ec2.describe_instances(InstanceIds=ec2_instances)

                # Process response
                for reservation in response.get("Reservations", []):
                    for instance in reservation.get("Instances", []):
                        instance_id = instance["InstanceId"]
                        instance_state = instance["State"]["Name"]

                        # Map EC2 state to our status
                        status = EC2_STATUS_MAPPING.get(instance_state, STATUS_UNKNOWN)
                        status_map[instance_id] = status

                        # Update resource state
                        if instance_id in self.resources:
                            self.resources[instance_id]["status"] = status
            except ClientError as e:
                logger.error(f"Failed to get EC2 instance status: {e}")
                # Handle case where instances don't exist anymore
                if "InvalidInstanceID.NotFound" in str(e):
                    for instance_id in ec2_instances:
                        status_map[instance_id] = STATUS_COMPLETED
                        if instance_id in self.resources:
                            self.resources[instance_id]["status"] = STATUS_COMPLETED
            except Exception as e:
                logger.error(f"Unexpected error getting EC2 instance status: {e}")
                for instance_id in ec2_instances:
                    status_map[instance_id] = STATUS_UNKNOWN

        # Check Spot Fleet status
        if spot_fleet_blocks and self.use_spot_fleet and self.spot_fleet_manager:
            for block_id in spot_fleet_blocks:
                try:
                    status = self.spot_fleet_manager.get_block_status(block_id)
                    status_map[block_id] = status

                    # Update resource state
                    if block_id in self.resources:
                        self.resources[block_id]["status"] = status
                except Exception as e:
                    logger.error(
                        f"Failed to get Spot Fleet block status for {block_id}: {e}"
                    )
                    status_map[block_id] = STATUS_UNKNOWN

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

        ec2 = self.session.client("ec2")
        cancel_map = {}

        # Group IDs by resource type
        ec2_instances = []
        spot_fleet_blocks = []

        for resource_id in resource_ids:
            resource = self.resources.get(resource_id)
            if not resource:
                cancel_map[resource_id] = STATUS_UNKNOWN
                continue

            if resource.get("type") == RESOURCE_TYPE_EC2:
                ec2_instances.append(resource_id)
            elif resource.get("type") == RESOURCE_TYPE_SPOT_FLEET:
                spot_fleet_blocks.append(resource_id)
            else:
                cancel_map[resource_id] = STATUS_UNKNOWN

        # Cancel EC2 instances
        if ec2_instances:
            try:
                ec2.terminate_instances(InstanceIds=ec2_instances)

                for instance_id in ec2_instances:
                    cancel_map[instance_id] = STATUS_CANCELED
                    if instance_id in self.resources:
                        self.resources[instance_id]["status"] = STATUS_CANCELED

                logger.info(f"Canceled {len(ec2_instances)} EC2 instances")
            except ClientError as e:
                logger.error(f"Failed to cancel EC2 instances: {e}")
                # Handle case where instances don't exist anymore
                if "InvalidInstanceID.NotFound" in str(e):
                    for instance_id in ec2_instances:
                        cancel_map[instance_id] = STATUS_COMPLETED
                        if instance_id in self.resources:
                            self.resources[instance_id]["status"] = STATUS_COMPLETED
                else:
                    for instance_id in ec2_instances:
                        cancel_map[instance_id] = STATUS_FAILED
            except Exception as e:
                logger.error(f"Unexpected error canceling EC2 instances: {e}")
                for instance_id in ec2_instances:
                    cancel_map[instance_id] = STATUS_FAILED

        # Cancel Spot Fleet blocks
        if spot_fleet_blocks and self.use_spot_fleet and self.spot_fleet_manager:
            for block_id in spot_fleet_blocks:
                try:
                    self.spot_fleet_manager.terminate_block(block_id)
                    cancel_map[block_id] = STATUS_CANCELED

                    # Update resource state
                    if block_id in self.resources:
                        self.resources[block_id]["status"] = STATUS_CANCELED

                    logger.info(f"Canceled Spot Fleet block {block_id}")
                except Exception as e:
                    logger.error(f"Failed to cancel Spot Fleet block {block_id}: {e}")
                    cancel_map[block_id] = STATUS_FAILED
                    if block_id in self.resources:
                        self.resources[block_id]["status"] = STATUS_FAILED

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

        ec2 = self.session.client("ec2")

        # Group IDs by resource type
        ec2_instances = []
        spot_fleet_blocks = []

        for resource_id in resource_ids:
            resource = self.resources.get(resource_id)
            if not resource:
                continue

            if resource.get("type") == RESOURCE_TYPE_EC2:
                ec2_instances.append(resource_id)
            elif resource.get("type") == RESOURCE_TYPE_SPOT_FLEET:
                spot_fleet_blocks.append(resource_id)

        # Terminate EC2 instances
        if ec2_instances:
            try:
                ec2.terminate_instances(InstanceIds=ec2_instances)
                logger.info(f"Terminated {len(ec2_instances)} EC2 instances")

                # Remove resources from tracking
                for instance_id in ec2_instances:
                    if instance_id in self.resources:
                        del self.resources[instance_id]
            except ClientError as e:
                # If the instances are already terminated or don't exist, that's fine
                if "InvalidInstanceID.NotFound" not in str(e):
                    logger.error(f"Failed to terminate EC2 instances: {e}")

                # Still remove resources from tracking
                for instance_id in ec2_instances:
                    if instance_id in self.resources:
                        del self.resources[instance_id]
            except Exception as e:
                logger.error(f"Unexpected error terminating EC2 instances: {e}")

        # Terminate Spot Fleet blocks
        if spot_fleet_blocks and self.use_spot_fleet and self.spot_fleet_manager:
            for block_id in spot_fleet_blocks:
                try:
                    self.spot_fleet_manager.terminate_block(block_id)
                    logger.info(f"Terminated Spot Fleet block {block_id}")

                    # Remove resource from tracking
                    if block_id in self.resources:
                        del self.resources[block_id]
                except Exception as e:
                    logger.error(
                        f"Failed to terminate Spot Fleet block {block_id}: {e}"
                    )

                    # Still remove resource from tracking
                    if block_id in self.resources:
                        del self.resources[block_id]

        # Save state with updated resources
        self.save_state()

    def cleanup_infrastructure(self) -> None:
        """Clean up infrastructure created by this mode.

        This cleans up the VPC, subnet, and security group if they were created by the provider.
        """
        logger.info("Cleaning up infrastructure")

        # Delete all instances first
        if self.resources:
            self.cleanup_all()

        try:
            # Stop spot interruption monitoring if enabled
            if self.spot_interruption_monitor:
                try:
                    self.spot_interruption_monitor.stop_monitoring()
                    logger.info("Stopped spot interruption monitoring")
                except Exception as e:
                    logger.error(f"Failed to stop spot interruption monitoring: {e}")
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
                    delete_resource(self.subnet_id, self.session, RESOURCE_TYPE_SUBNET)
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
                        Filters=[{"Name": "attachment.vpc-id", "Values": [self.vpc_id]}]
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

            # Clean up Spot Fleet resources if using spot fleet
            if self.use_spot_fleet and self.spot_fleet_manager:
                try:
                    self.spot_fleet_manager.cleanup_all_resources()
                    logger.info("Cleaned up all Spot Fleet resources")
                except Exception as e:
                    logger.error(f"Failed to clean up Spot Fleet resources: {e}")

            # Clear initialization flag
            self.initialized = False

            # Save state
            self.save_state()

            logger.info("Infrastructure cleanup complete")
        except Exception as e:
            logger.error(f"Failed to clean up infrastructure: {e}")
            # Still mark as not initialized
            self.initialized = False
            self.save_state()

    def list_resources(self) -> Dict[str, List[Dict[str, Any]]]:
        """List all resources created by this mode.

        Returns
        -------
        Dict[str, List[Dict[str, Any]]]
            Dictionary of resource types and their details
        """
        result: Dict[str, List[Dict[str, Any]]] = {
            "ec2_instances": [],
            "vpc": [],
            "subnet": [],
            "security_group": [],
            "spot_fleet": [],
        }

        # Add EC2 instances and Spot Fleet blocks
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
            elif resource.get("type") == RESOURCE_TYPE_SPOT_FLEET:
                result["spot_fleet"].append(
                    {
                        "id": resource_id,
                        "job_id": resource.get("job_id"),
                        "job_name": resource.get("job_name"),
                        "status": resource.get("status"),
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

        # Add detailed Spot Fleet information if available
        if self.use_spot_fleet and self.spot_fleet_manager:
            # Add fleet requests
            for fleet_id, fleet in self.spot_fleet_manager.fleet_requests.items():
                block_id = fleet.get("block_id")
                if block_id:
                    fleet_details = {
                        "id": fleet_id,
                        "block_id": block_id,
                        "status": fleet.get("status"),
                        "created_at": fleet.get("created_at"),
                        "target_capacity": fleet.get("target_capacity"),
                    }

                    # Check if this fleet is already in the result
                    if not any(f["id"] == fleet_id for f in result["spot_fleet"]):
                        result["spot_fleet"].append(fleet_details)

        return result

    def cleanup_all(self) -> None:
        """Clean up all resources created by this mode."""
        logger.info("Cleaning up all resources")

        # Get all resource IDs
        resource_ids = list(self.resources.keys())

        if resource_ids:
            self.cleanup_resources(resource_ids)
            logger.info(f"Cleaned up {len(resource_ids)} resources")
        else:
            logger.debug("No resources to clean up")

        # Clean up Spot Fleet resources if using spot fleet
        if self.use_spot_fleet and self.spot_fleet_manager:
            try:
                self.spot_fleet_manager.cleanup_all_resources()
                logger.info("Cleaned up all Spot Fleet resources")
            except Exception as e:
                logger.error(f"Failed to clean up Spot Fleet resources: {e}")

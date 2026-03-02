"""
Standard operating mode for the EphemeralAWSProvider.

The standard mode uses EC2 instances for computation with direct communication
between the client and worker nodes.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import ipaddress
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
    STATUS_WARM,
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
        warm_pool_size: int = 0,
        warm_pool_ttl: int = 600,
        iam_instance_profile_arn: Optional[str] = None,
        bake_ami: bool = False,
        baked_ami_id: Optional[str] = None,
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

        # Warm pool attributes
        self.warm_pool_size = warm_pool_size
        self.warm_pool_ttl = warm_pool_ttl
        self.iam_instance_profile_arn = iam_instance_profile_arn
        # List of instance IDs currently in the warm pool (ready for reuse, FIFO)
        self._warm_instances: List[str] = []

        # AMI baking attributes
        self.bake_ami = bake_ami
        self.baked_ami_id = baked_ami_id  # user-supplied pre-baked AMI
        self._baked_ami_id: Optional[str] = None  # resolved AMI ID (baked or supplied)
        self._owns_baked_ami: bool = False  # True if this provider created the AMI

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

        # Initialize spot interruption handling if enabled.
        # NOTE: start_monitoring() is called in initialize(), not here — so the
        # monitoring thread is only started after infrastructure is fully set up.
        # This prevents the monitor thread from leaking if __init__ succeeds but
        # a later call (e.g. initialize()) raises before cleanup can run.
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

        # If predefined VPC resources are provided, don't create new VPC
        if self.vpc_id:
            self.create_vpc = False

    # ------------------------------------------------------------------
    # AMI baking helpers
    # ------------------------------------------------------------------

    def _bake_ami(self) -> str:
        """Bake worker_init into a custom AMI.

        Launches a builder instance with worker_init as UserData, waits for it
        to stop (via ``shutdown -h now`` at the end of UserData), creates an
        image snapshot, waits for the image to become available, terminates the
        builder, and returns the new AMI ID.

        Returns
        -------
        str
            ID of the newly created AMI.

        Raises
        ------
        ResourceCreationError
            If any step of the baking process fails.
        """
        ec2 = self.session.client("ec2")
        builder_id = self._launch_builder_instance()
        logger.info(f"Waiting for builder instance {builder_id} to stop...")
        try:
            wait_for_resource(
                builder_id,
                "instance_stopped",
                ec2,
                resource_name="AMI builder instance",
                delay=15,
                max_attempts=80,  # up to 20 minutes for slow UserData
            )
        except Exception as e:
            # Terminate the builder before re-raising so it doesn't linger
            try:
                ec2.terminate_instances(InstanceIds=[builder_id])
            except Exception:  # nosec B110
                pass
            raise ResourceCreationError(
                f"Builder instance {builder_id} did not stop: {e}"
            ) from e

        ami_name = f"parsl-baked-{self.provider_id[:8]}-{int(time.time())}"
        try:
            response = ec2.create_image(
                InstanceId=builder_id,
                Name=ami_name,
                NoReboot=True,
                TagSpecifications=[
                    {
                        "ResourceType": "image",
                        "Tags": [
                            {"Key": "ParslBakedAMI", "Value": "true"},
                            {"Key": "ProviderId", "Value": self.provider_id},
                            {
                                "Key": "CreatedBy",
                                "Value": "ParslEphemeralAWSProvider",
                            },
                        ],
                    }
                ],
            )
        except Exception as e:
            try:
                ec2.terminate_instances(InstanceIds=[builder_id])
            except Exception:  # nosec B110
                pass
            raise ResourceCreationError(f"create_image failed: {e}") from e

        ami_id = response["ImageId"]
        logger.info(f"Created AMI {ami_id}, waiting for it to become available...")
        try:
            wait_for_resource(
                ami_id,
                "image_available",
                ec2,
                resource_name="baked AMI",
                delay=15,
                max_attempts=80,
            )
        except Exception as e:
            raise ResourceCreationError(
                f"AMI {ami_id} did not become available: {e}"
            ) from e
        finally:
            # Always terminate the builder, even on failure
            try:
                ec2.terminate_instances(InstanceIds=[builder_id])
                logger.debug(f"Terminated builder instance {builder_id}")
            except Exception as te:
                logger.warning(f"Failed to terminate builder {builder_id}: {te}")

        return ami_id

    def _launch_builder_instance(self) -> str:
        """Launch a builder EC2 instance that runs worker_init then shuts down.

        Returns
        -------
        str
            Instance ID of the launched builder.
        """
        ec2 = self.session.client("ec2")
        user_data = f"#!/bin/bash\n{self.worker_init}\nshutdown -h now\n"
        kwargs: Dict[str, Any] = {
            "ImageId": self.image_id,
            "InstanceType": self.instance_type,
            "MinCount": 1,
            "MaxCount": 1,
            "UserData": user_data,
            "TagSpecifications": [
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {
                            "Key": "Name",
                            "Value": f"parsl-ami-builder-{self.provider_id[:8]}",
                        },
                        {"Key": "ParslAMIBuilder", "Value": "true"},
                        {"Key": "ProviderId", "Value": self.provider_id},
                        {
                            "Key": "CreatedBy",
                            "Value": "ParslEphemeralAWSProvider",
                        },
                    ],
                }
            ],
        }
        if self.subnet_id:
            kwargs["SubnetId"] = self.subnet_id
        if self.security_group_id:
            kwargs["SecurityGroupIds"] = [self.security_group_id]
        if self.key_name:
            kwargs["KeyName"] = self.key_name
        if self.iam_instance_profile_arn:
            kwargs["IamInstanceProfile"] = {"Arn": self.iam_instance_profile_arn}

        response = ec2.run_instances(**kwargs)
        instance_id = response["Instances"][0]["InstanceId"]
        logger.info(f"Launched AMI builder instance {instance_id}")
        return instance_id

    def _deregister_baked_ami(self, ami_id: str) -> None:
        """Deregister a baked AMI and delete its backing EBS snapshots.

        Parameters
        ----------
        ami_id : str
            AMI ID to deregister.
        """
        ec2 = self.session.client("ec2")
        # Collect snapshot IDs before deregistering the image
        snapshot_ids: List[str] = []
        try:
            response = ec2.describe_images(ImageIds=[ami_id])
            images = response.get("Images", [])
            if images:
                for block_device in images[0].get("BlockDeviceMappings", []):
                    ebs = block_device.get("Ebs", {})
                    if "SnapshotId" in ebs:
                        snapshot_ids.append(ebs["SnapshotId"])
        except Exception as e:
            logger.warning(f"Could not describe AMI {ami_id} before deregistering: {e}")

        ec2.deregister_image(ImageId=ami_id)
        logger.debug(f"Deregistered AMI {ami_id}")

        for snapshot_id in snapshot_ids:
            try:
                ec2.delete_snapshot(SnapshotId=snapshot_id)
                logger.debug(f"Deleted snapshot {snapshot_id}")
            except Exception as e:
                logger.warning(f"Failed to delete snapshot {snapshot_id}: {e}")

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
            "warm_instances": list(self._warm_instances),
            "baked_ami_id": self._baked_ami_id,
            "owns_baked_ami": self._owns_baked_ami,
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
            # Save directly (includes baked AMI fields not in the base class state)
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
                # Restore warm pool list; fall back to scanning resources for
                # STATUS_WARM entries in case the key is absent (older state files)
                if "warm_instances" in state:
                    self._warm_instances = state["warm_instances"]
                else:
                    self._warm_instances = [
                        rid
                        for rid, r in self.resources.items()
                        if r.get("warm_pool") and r.get("status") == STATUS_WARM
                    ]

                # Restore baked AMI state
                saved_baked_ami = state.get("baked_ami_id")
                if saved_baked_ami:
                    self._baked_ami_id = saved_baked_ami
                    self._owns_baked_ami = state.get("owns_baked_ami", False)
                    self.image_id = saved_baked_ami
                    logger.info(f"Restored baked AMI {saved_baked_ami} from state")

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
        # Idempotent: if already initialized, do nothing.
        if self.initialized:
            return

        # Try to load state first
        if self.load_state():
            logger.debug("Loaded state, checking resources")
            # Verify that the loaded resources exist
            self._verify_resources()
            return

        logger.debug("Initializing standard mode infrastructure")

        # Track which resources this provider created so cleanup only removes
        # its own resources, not pre-existing VPCs/subnets passed by the caller.
        self._owns_vpc = False
        self._owns_subnet = False
        self._owns_security_group = False

        # Create AWS resources
        try:
            # Create VPC if needed
            if not self.vpc_id and self.create_vpc:
                self.vpc_id = self._create_vpc()
                self._owns_vpc = True

            # Create subnet if needed
            if not self.subnet_id and self.vpc_id:
                self.subnet_id = self._create_subnet()
                self._owns_subnet = True

            # Create security group if needed
            if not self.security_group_id and self.vpc_id:
                self.security_group_id = self._create_security_group()
                self._owns_security_group = True

            # AMI baking: snapshot worker_init into a custom AMI
            if self.bake_ami and not self._baked_ami_id:
                ami_id = self._bake_ami()
                self._baked_ami_id = ami_id
                self._owns_baked_ami = True
                self.image_id = ami_id
                logger.info(f"Baked AMI {ami_id} from worker_init")
            elif self.baked_ami_id and not self._baked_ami_id:
                self._baked_ami_id = self.baked_ami_id
                self.image_id = self.baked_ami_id
                logger.info(f"Using pre-supplied baked AMI {self.baked_ami_id}")

            # Save state
            self.save_state()

            logger.info(
                f"Initialized standard mode infrastructure: "
                f"vpc_id={self.vpc_id}, subnet_id={self.subnet_id}, "
                f"security_group_id={self.security_group_id}"
            )

            # Start spot interruption monitoring here (not in __init__) so
            # the thread is only alive when infrastructure is fully ready.
            # The try/finally ensures we stop the thread if anything below
            # (or a subsequent call to initialize()) raises.
            if self.spot_interruption_monitor:
                try:
                    self.spot_interruption_monitor.start_monitoring()
                    logger.info("Started spot interruption monitoring")
                except Exception as monitor_err:
                    logger.error(
                        f"Failed to start spot interruption monitoring: {monitor_err}"
                    )
                    # Non-fatal — jobs can still run, just without interruption recovery
                    self.spot_interruption_monitor = None

            # Mark as initialized
            self.initialized = True
        except Exception as e:
            logger.error(f"Failed to initialize standard mode infrastructure: {e}")
            # Stop the monitor thread if it was started before the exception
            if self.spot_interruption_monitor:
                try:
                    self.spot_interruption_monitor.stop_monitoring()
                except Exception as stop_err:  # nosec B110
                    logger.debug(f"Error stopping monitor during cleanup: {stop_err}")
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

    @staticmethod
    def _find_available_vpc_cidr(ec2_client: Any) -> str:
        """Find a /16 CIDR block in the 10.x.0.0/16 range that does not overlap
        any existing VPC in the account.

        Parameters
        ----------
        ec2_client : Any
            Boto3 EC2 client

        Returns
        -------
        str
            Available CIDR block (e.g. '10.0.0.0/16')

        Raises
        ------
        NetworkCreationError
            If no non-overlapping /16 CIDR is available
        """
        existing = [
            ipaddress.ip_network(vpc["CidrBlock"])
            for vpc in ec2_client.describe_vpcs()["Vpcs"]
        ]
        for n in range(256):
            candidate = ipaddress.ip_network(f"10.{n}.0.0/16")
            if not any(candidate.overlaps(e) for e in existing):
                return str(candidate)
        raise NetworkCreationError("No /16 CIDR available in 10.x.0.0/16 range")

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
            # Select a CIDR block that does not conflict with existing VPCs
            vpc_cidr = self._find_available_vpc_cidr(ec2)

            # Create VPC
            response = ec2.create_vpc(
                CidrBlock=vpc_cidr,
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

            # Add outbound rules (ignore duplicate-rule errors — default SGs
            # already have an allow-all egress rule)
            if DEFAULT_OUTBOUND_RULES:
                try:
                    ec2.authorize_security_group_egress(
                        GroupId=security_group_id, IpPermissions=DEFAULT_OUTBOUND_RULES
                    )
                    logger.debug(
                        f"Added outbound rules to security group {security_group_id}"
                    )
                except ClientError as _dup_err:
                    if "InvalidPermission.Duplicate" in str(_dup_err):
                        logger.debug(
                            "Outbound rule already present on %s; skipping",
                            security_group_id,
                        )
                    else:
                        raise

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
        # Check if the mode is initialized
        if not self.initialized:
            raise OperatingModeError(
                "StandardMode must be initialized before submitting jobs"
            )

        # Validate image_id
        if not self.image_id:
            self.image_id = get_default_ami(self.session.region_name)
            logger.info(
                f"Using default AMI {self.image_id} for region {self.session.region_name}"
            )

        logger.info(f"Submitting job {job_id} ({job_name if job_name else 'unnamed'})")

        try:
            # --- Warm pool fast path: reuse an idle instance ---
            if self.warm_pool_size > 0:
                warm_instance_id = self._get_warm_instance()
                if warm_instance_id is not None:
                    logger.info(
                        f"Reusing warm instance {warm_instance_id} for job {job_id}"
                    )
                    ssm_command_id = self._dispatch_ssm_command(
                        warm_instance_id, command, job_id
                    )
                    # Update resource record in-place for the new job
                    self.resources[warm_instance_id].update(
                        {
                            "job_id": job_id,
                            "job_name": job_name or "unnamed",
                            "status": STATUS_RUNNING,
                            "command": command,
                            "tasks_per_node": tasks_per_node,
                            "ssm_command_id": ssm_command_id,
                            "warm_since": None,
                            "created_at": time.time(),
                        }
                    )
                    self.save_state()
                    return warm_instance_id

            # --- Cold path: create a new EC2 instance ---
            init_script = self._prepare_init_script(command, job_id)
            instance_id = self._create_instance(init_script, job_id, job_name)

            # Track the resource
            resource_type = RESOURCE_TYPE_EC2
            if (
                self.use_spot_fleet
                and self.spot_fleet_manager
                and instance_id in self.spot_fleet_manager.blocks
            ):
                resource_type = RESOURCE_TYPE_SPOT_FLEET

            resource_data: Dict[str, Any] = {
                "type": resource_type,
                "job_id": job_id,
                "job_name": job_name or "unnamed",
                "status": STATUS_PENDING,
                "created_at": time.time(),
                "command": command,
                "tasks_per_node": tasks_per_node,
            }

            if self.warm_pool_size > 0:
                # Warm pool cold start: wait for SSM then dispatch command
                resource_data["warm_pool"] = True
                self.resources[instance_id] = resource_data
                try:
                    self._wait_for_ssm_online(instance_id)
                    self._wait_for_worker_ready(instance_id)
                    ssm_command_id = self._dispatch_ssm_command(
                        instance_id, command, job_id
                    )
                    self.resources[instance_id]["ssm_command_id"] = ssm_command_id
                    self.resources[instance_id]["status"] = STATUS_RUNNING
                    self.resources[instance_id]["warm_since"] = None
                except Exception as ssm_err:
                    logger.error(
                        f"SSM setup failed for warm pool instance {instance_id}: "
                        f"{ssm_err}. Falling back to UserData execution."
                    )
                    # Remove warm_pool flag so status is tracked via EC2 state
                    self.resources[instance_id].pop("warm_pool", None)
            else:
                self.resources[instance_id] = resource_data

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
            Command to execute (ignored when warm_pool_size > 0; dispatched via SSM)
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

        if self.warm_pool_size > 0:
            # Warm pool: UserData only runs worker_init and drops a ready marker.
            # The actual command is dispatched later via SSM SendCommand so the
            # instance can be reused across multiple jobs without re-installing.
            init_script += "mkdir -p /var/run/parsl\n"
            init_script += "touch /var/run/parsl_worker_ready\n"
            return init_script

        # Non-warm-pool path: embed command and optional shutdown in UserData
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

    # ------------------------------------------------------------------
    # Warm pool helpers
    # ------------------------------------------------------------------

    def _get_warm_instance(self) -> Optional[str]:
        """Pop the oldest available warm instance from the pool (FIFO).

        Returns
        -------
        Optional[str]
            Instance ID if a warm instance is available, otherwise None.
            The returned instance's status is updated to STATUS_RUNNING.
        """
        if not self._warm_instances:
            return None
        instance_id = self._warm_instances.pop(0)
        if instance_id in self.resources:
            self.resources[instance_id]["status"] = STATUS_RUNNING
            self.resources[instance_id]["warm_since"] = None
        logger.debug(
            f"Popped warm instance {instance_id} from pool "
            f"({len(self._warm_instances)} remaining)"
        )
        return instance_id

    def _wait_for_ssm_online(self, instance_id: str, timeout: int = 300) -> None:
        """Wait for an EC2 instance to register with AWS Systems Manager.

        Parameters
        ----------
        instance_id : str
            EC2 instance ID to wait for.
        timeout : int, optional
            Maximum seconds to wait, by default 300.

        Raises
        ------
        OperatingModeError
            If the instance does not appear in SSM within *timeout* seconds.
        """
        ssm = self.session.client("ssm")
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                resp = ssm.describe_instance_information(
                    Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
                )
                if resp.get("InstanceInformationList"):
                    logger.debug(f"Instance {instance_id} is online in SSM")
                    return
            except ClientError as e:
                logger.debug(f"SSM describe_instance_information: {e}")
            time.sleep(10)
        raise OperatingModeError(
            f"Instance {instance_id} did not become available in SSM "
            f"within {timeout}s"
        )

    def _wait_for_worker_ready(self, instance_id: str, timeout: int = 600) -> None:
        """Wait for the worker ready marker on an instance via SSM RunCommand.

        The init script (UserData) touches ``/var/run/parsl_worker_ready`` once
        worker_init completes.  This method polls until that file exists.

        Parameters
        ----------
        instance_id : str
            EC2 instance ID to check.
        timeout : int, optional
            Maximum seconds to wait, by default 600.

        Raises
        ------
        OperatingModeError
            If the ready marker is not found within *timeout* seconds.
        """
        ssm = self.session.client("ssm")
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                resp = ssm.send_command(
                    InstanceIds=[instance_id],
                    DocumentName="AWS-RunShellScript",
                    Parameters={"commands": ["test -f /var/run/parsl_worker_ready"]},
                    Comment="Parsl worker ready check",
                )
                command_id = resp["Command"]["CommandId"]
                # Brief wait then check the result
                time.sleep(5)
                invocation = ssm.get_command_invocation(
                    CommandId=command_id,
                    InstanceId=instance_id,
                )
                if invocation.get("StatusDetails") == "Success":
                    logger.debug(f"Worker ready on {instance_id}")
                    return
            except ClientError as e:
                logger.debug(f"SSM ready check: {e}")
            time.sleep(15)
        raise OperatingModeError(
            f"Worker ready marker not found on {instance_id} within {timeout}s"
        )

    def _dispatch_ssm_command(self, instance_id: str, command: str, job_id: str) -> str:
        """Dispatch a shell command to an EC2 instance via SSM SendCommand.

        Parameters
        ----------
        instance_id : str
            Target EC2 instance ID.
        command : str
            Shell command to execute.
        job_id : str
            Parsl job ID (used for environment variable export and comment).

        Returns
        -------
        str
            SSM CommandId for later status polling via ``get_command_invocation``.
        """
        ssm = self.session.client("ssm")
        env_setup = (
            f"export PARSL_JOB_ID={job_id}\n"
            f"export PARSL_PROVIDER_ID={self.provider_id}\n"
            "export PARSL_WORKER_ID=$(hostname)\n"
        )
        response = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [env_setup + command]},
            Comment=f"Parsl job {job_id[:16]}",
        )
        command_id = response["Command"]["CommandId"]
        logger.debug(
            f"Dispatched SSM command {command_id} to {instance_id} for job {job_id}"
        )
        return command_id

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

        # Attach IAM instance profile when warm pool is enabled (SSM requires it)
        if self.warm_pool_size > 0 and self.iam_instance_profile_arn:
            run_args["IamInstanceProfile"] = {"Arn": self.iam_instance_profile_arn}

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
                logger.error(
                    f"Timeout waiting for Spot Fleet block {block_id} to reach RUNNING"
                )
                try:
                    self.spot_fleet_manager.terminate_block(block_id)
                    self.blocks.pop(block_id, None)
                except Exception as cleanup_err:
                    logger.error(
                        f"Failed to clean up timed-out fleet block {block_id}: {cleanup_err}"
                    )
                raise ResourceCreationError(
                    f"Spot Fleet block {block_id} did not reach RUNNING "
                    f"state within {max_wait}s"
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

        # Group IDs by resource type / tracking method
        ec2_instances = []
        spot_fleet_blocks = []
        warm_pool_instances = []  # tracked via SSM command invocation

        for resource_id in resource_ids:
            resource = self.resources.get(resource_id)
            if not resource:
                status_map[resource_id] = STATUS_UNKNOWN
                continue

            if resource.get("warm_pool") and resource.get("ssm_command_id"):
                warm_pool_instances.append(resource_id)
            elif resource.get("type") == RESOURCE_TYPE_EC2:
                ec2_instances.append(resource_id)
            elif resource.get("type") == RESOURCE_TYPE_SPOT_FLEET:
                spot_fleet_blocks.append(resource_id)
            else:
                status_map[resource_id] = STATUS_UNKNOWN

        # --- Warm pool: poll SSM command invocation status ---
        if warm_pool_instances:
            ssm = self.session.client("ssm")
            for instance_id in warm_pool_instances:
                resource = self.resources[instance_id]
                command_id = resource["ssm_command_id"]
                try:
                    response = ssm.get_command_invocation(
                        CommandId=command_id,
                        InstanceId=instance_id,
                    )
                    ssm_status = response.get("Status", "Unknown")
                    if ssm_status == "Success":
                        status = STATUS_COMPLETED
                    elif ssm_status in (
                        "Failed",
                        "TimedOut",
                        "Cancelled",
                        "Undeliverable",
                        "DeliveryTimedOut",
                        "ExecutionTimedOut",
                    ):
                        status = STATUS_FAILED
                    else:
                        # InProgress, Pending, Delayed, etc.
                        status = STATUS_RUNNING
                    status_map[instance_id] = status
                    self.resources[instance_id]["status"] = status
                except ClientError as e:
                    if "InvocationDoesNotExist" in str(e):
                        # Command not yet received by the instance
                        status_map[instance_id] = STATUS_RUNNING
                    else:
                        logger.error(
                            f"Failed to get SSM command status for {instance_id}: {e}"
                        )
                        status_map[instance_id] = STATUS_UNKNOWN

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

        # Terminate EC2 instances — only remove from tracking on confirmed success
        # or when the instance is already gone (InvalidInstanceID.NotFound).
        # On any other error, keep the entry so the next cleanup cycle can retry.
        if ec2_instances:
            try:
                ec2.terminate_instances(InstanceIds=ec2_instances)
                logger.info(f"Terminated {len(ec2_instances)} EC2 instances")
                for instance_id in ec2_instances:
                    self.resources.pop(instance_id, None)
            except ClientError as e:
                if "InvalidInstanceID.NotFound" in str(e):
                    # Instances already gone — safe to remove from tracking
                    for instance_id in ec2_instances:
                        self.resources.pop(instance_id, None)
                else:
                    logger.error(f"Failed to terminate EC2 instances: {e}")
                    # Do NOT remove — will be retried on next cleanup cycle
            except Exception as e:
                logger.error(f"Unexpected error terminating EC2 instances: {e}")
                # Do NOT remove — will be retried on next cleanup cycle

        # Terminate Spot Fleet blocks — same conservative removal policy
        if spot_fleet_blocks and self.use_spot_fleet and self.spot_fleet_manager:
            for block_id in spot_fleet_blocks:
                try:
                    self.spot_fleet_manager.terminate_block(block_id)
                    logger.info(f"Terminated Spot Fleet block {block_id}")
                    self.resources.pop(block_id, None)
                except Exception as e:
                    logger.error(
                        f"Failed to terminate Spot Fleet block {block_id}: {e}"
                    )
                    # Do NOT remove — will be retried on next cleanup cycle

        # Save state with updated resources
        self.save_state()

    def cleanup_infrastructure(self) -> None:
        """Clean up infrastructure created by this mode.

        This cleans up the VPC, subnet, and security group if they were created by the provider.
        """
        logger.info("Cleaning up infrastructure")

        # Collect EC2 instance IDs before cleanup so we can wait for termination.
        # cleanup_all() sends terminate requests but does not wait; instances remain
        # in "shutting-down" state and their ENIs keep references to the security
        # group, causing a DependencyViolation when we try to delete the SG.
        ec2_instance_ids = [
            rid
            for rid, r in self.resources.items()
            if r.get("type") == RESOURCE_TYPE_EC2
        ]

        # Delete all instances first
        if self.resources:
            self.cleanup_all()

        # Wait for all EC2 instances to reach terminated state so that their
        # network interfaces are released before we attempt SG/subnet deletion.
        if ec2_instance_ids:
            try:
                ec2 = self.session.client("ec2")
                waiter = ec2.get_waiter("instance_terminated")
                waiter.wait(
                    InstanceIds=ec2_instance_ids,
                    WaiterConfig={"Delay": 5, "MaxAttempts": 36},  # up to 3 min
                )
                logger.debug(
                    "All EC2 instances confirmed terminated: %s", ec2_instance_ids
                )
            except Exception as e:
                logger.warning(
                    "Timed out or error waiting for instance termination: %s — "
                    "proceeding with infrastructure cleanup (SG deletion may fail)",
                    e,
                )

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

            # Deregister baked AMI if this provider created it
            if self._baked_ami_id and getattr(self, "_owns_baked_ami", False):
                try:
                    self._deregister_baked_ami(self._baked_ami_id)
                    logger.info(f"Deregistered baked AMI {self._baked_ami_id}")
                    self._baked_ami_id = None
                except Exception as e:
                    logger.error(
                        f"Failed to deregister baked AMI {self._baked_ami_id}: {e}"
                    )

            # Delete security group (only if this provider created it)
            if self.security_group_id and getattr(self, "_owns_security_group", True):
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
            elif self.security_group_id:
                logger.debug(
                    "Skipping deletion of pre-existing security group %s",
                    self.security_group_id,
                )

            # Delete subnet (only if this provider created it)
            if self.subnet_id and getattr(self, "_owns_subnet", True):
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
            elif self.subnet_id:
                logger.debug(
                    "Skipping deletion of pre-existing subnet %s", self.subnet_id
                )

            # Delete VPC (only if this provider created it)
            if self.vpc_id and getattr(self, "_owns_vpc", True):
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
            elif self.vpc_id:
                logger.debug("Skipping deletion of pre-existing VPC %s", self.vpc_id)

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
            seen_fleet_ids = {f["id"] for f in result["spot_fleet"]}
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

                    if fleet_id not in seen_fleet_ids:
                        result["spot_fleet"].append(fleet_details)
                        seen_fleet_ids.add(fleet_id)

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

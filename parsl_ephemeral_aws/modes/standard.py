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
from typing import Any, Dict, List, Optional, Tuple, Union

import boto3
from botocore.exceptions import ClientError

from parsl_ephemeral_aws.constants import (
    DEFAULT_INBOUND_RULES,
    DEFAULT_OUTBOUND_RULES,
    DEFAULT_PRIVATE_SUBNET_CIDR,
    DEFAULT_PUBLIC_SUBNET_CIDR,
    DEFAULT_SECURITY_GROUP_DESCRIPTION,
    DEFAULT_SECURITY_GROUP_NAME,
    DEFAULT_VPC_CIDR,
    EC2_STATUS_MAPPING,
    RESOURCE_TYPE_EC2,
    RESOURCE_TYPE_SECURITY_GROUP,
    RESOURCE_TYPE_SUBNET,
    RESOURCE_TYPE_VPC,
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
    ResourceDeletionError,
    ResourceNotFoundError,
)
from parsl_ephemeral_aws.modes.base import OperatingMode
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
    """

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
                    logger.warning(f"Security group {self.security_group_id} does not exist")
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
                            {"Key": "Name", "Value": f"parsl-ephemeral-{self.provider_id[:8]}"},
                            {"Key": "CreatedBy", "Value": "ParslEphemeralAWSProvider"},
                            {"Key": "ProviderId", "Value": self.provider_id},
                        ]
                    }
                ]
            )
            
            vpc_id = response["Vpc"]["VpcId"]
            logger.debug(f"Created VPC {vpc_id}")
            
            # Enable DNS support and hostnames
            ec2.modify_vpc_attribute(
                VpcId=vpc_id,
                EnableDnsSupport={"Value": True}
            )
            ec2.modify_vpc_attribute(
                VpcId=vpc_id,
                EnableDnsHostnames={"Value": True}
            )
            
            # Create internet gateway
            igw_response = ec2.create_internet_gateway(
                TagSpecifications=[
                    {
                        "ResourceType": "internet-gateway",
                        "Tags": [
                            {"Key": "Name", "Value": f"parsl-ephemeral-igw-{self.provider_id[:8]}"},
                            {"Key": "CreatedBy", "Value": "ParslEphemeralAWSProvider"},
                            {"Key": "ProviderId", "Value": self.provider_id},
                        ]
                    }
                ]
            )
            
            igw_id = igw_response["InternetGateway"]["InternetGatewayId"]
            
            # Attach internet gateway to VPC
            ec2.attach_internet_gateway(
                InternetGatewayId=igw_id,
                VpcId=vpc_id
            )
            
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
                            {"Key": "Name", "Value": f"parsl-ephemeral-subnet-{self.provider_id[:8]}"},
                            {"Key": "CreatedBy", "Value": "ParslEphemeralAWSProvider"},
                            {"Key": "ProviderId", "Value": self.provider_id},
                        ]
                    }
                ]
            )
            
            subnet_id = response["Subnet"]["SubnetId"]
            logger.debug(f"Created subnet {subnet_id} in VPC {self.vpc_id}")
            
            # Enable auto-assign public IP if public IPs are requested
            if self.use_public_ips:
                ec2.modify_subnet_attribute(
                    SubnetId=subnet_id,
                    MapPublicIpOnLaunch={"Value": True}
                )
                logger.debug(f"Enabled auto-assign public IP for subnet {subnet_id}")
            
            # Create route table
            route_table_response = ec2.create_route_table(
                VpcId=self.vpc_id,
                TagSpecifications=[
                    {
                        "ResourceType": "route-table",
                        "Tags": [
                            {"Key": "Name", "Value": f"parsl-ephemeral-rt-{self.provider_id[:8]}"},
                            {"Key": "CreatedBy", "Value": "ParslEphemeralAWSProvider"},
                            {"Key": "ProviderId", "Value": self.provider_id},
                        ]
                    }
                ]
            )
            
            route_table_id = route_table_response["RouteTable"]["RouteTableId"]
            
            # Associate route table with subnet
            ec2.associate_route_table(
                RouteTableId=route_table_id,
                SubnetId=subnet_id
            )
            
            # Get internet gateway ID
            igw_response = ec2.describe_internet_gateways(
                Filters=[
                    {"Name": "attachment.vpc-id", "Values": [self.vpc_id]}
                ]
            )
            
            if igw_response["InternetGateways"]:
                igw_id = igw_response["InternetGateways"][0]["InternetGatewayId"]
                
                # Create route to internet
                ec2.create_route(
                    RouteTableId=route_table_id,
                    DestinationCidrBlock="0.0.0.0/0",
                    GatewayId=igw_id
                )
                
                logger.debug(f"Created route to internet via {igw_id} for subnet {subnet_id}")
            else:
                logger.warning(f"No internet gateway found for VPC {self.vpc_id}")
            
            # Add tags
            if self.additional_tags:
                create_tags(subnet_id, self.additional_tags, self.session)
                create_tags(route_table_id, self.additional_tags, self.session)
            
            # Wait for subnet to be available
            wait_for_resource(subnet_id, "subnet_available", ec2, resource_name="subnet")
            
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
                            {"Key": "Name", "Value": f"parsl-ephemeral-sg-{self.provider_id[:8]}"},
                            {"Key": "CreatedBy", "Value": "ParslEphemeralAWSProvider"},
                            {"Key": "ProviderId", "Value": self.provider_id},
                        ]
                    }
                ]
            )
            
            security_group_id = response["GroupId"]
            logger.debug(f"Created security group {security_group_id} in VPC {self.vpc_id}")
            
            # Add inbound rules
            if DEFAULT_INBOUND_RULES:
                ec2.authorize_security_group_ingress(
                    GroupId=security_group_id,
                    IpPermissions=DEFAULT_INBOUND_RULES
                )
                logger.debug(f"Added inbound rules to security group {security_group_id}")
            
            # Add outbound rules
            if DEFAULT_OUTBOUND_RULES:
                ec2.authorize_security_group_egress(
                    GroupId=security_group_id,
                    IpPermissions=DEFAULT_OUTBOUND_RULES
                )
                logger.debug(f"Added outbound rules to security group {security_group_id}")
            
            # Add tags
            if self.additional_tags:
                create_tags(security_group_id, self.additional_tags, self.session)
            
            # Wait for security group to be available
            wait_for_resource(security_group_id, "security_group_exists", ec2, resource_name="security group")
            
            return security_group_id
        except Exception as e:
            logger.error(f"Failed to create security group in VPC {self.vpc_id}: {e}")
            raise NetworkCreationError(
                f"Failed to create security group in VPC {self.vpc_id}: {e}"
            ) from e

    def submit_job(
        self, job_id: str, command: str, tasks_per_node: int, job_name: Optional[str] = None
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
            logger.info(f"Using default AMI {self.image_id} for region {self.session.region_name}")
        
        logger.info(f"Submitting job {job_id} ({job_name if job_name else 'unnamed'})")
        
        try:
            # Prepare worker initialization script
            init_script = self._prepare_init_script(command, job_id)
            
            # Create EC2 instance
            instance_id = self._create_instance(init_script, job_id, job_name)
            
            # Track the resource
            self.resources[instance_id] = {
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
        init_script += f"\n# Set environment variables\n"
        init_script += f"export PARSL_JOB_ID={job_id}\n"
        init_script += f"export PARSL_PROVIDER_ID={self.provider_id}\n"
        init_script += f"export PARSL_WORKER_ID=$(hostname)\n"
        
        # Add command
        init_script += f"\n# Execute Parsl worker command\n"
        init_script += f"{command}\n"
        
        # Add cleanup if auto shutdown is enabled
        if self.auto_shutdown:
            init_script += f"\n# Auto-shutdown\n"
            init_script += f"shutdown -h now\n"
        
        return init_script

    def _create_instance(self, init_script: str, job_id: str, job_name: Optional[str] = None) -> str:
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
            "TagSpecifications": [
                {
                    "ResourceType": "instance",
                    "Tags": tags
                }
            ],
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
                wait_for_resource(instance_id, "instance_running", ec2, resource_name="EC2 instance")
                
                return instance_id
            except Exception as e:
                logger.error(f"Failed to create EC2 instance: {e}")
                raise ResourceCreationError(f"Failed to create EC2 instance: {e}") from e

    def _create_spot_instance(self, run_args: Dict[str, Any]) -> str:
        """Create a spot instance.
        
        Parameters
        ----------
        run_args : Dict[str, Any]
            Arguments for EC2 instance creation
        
        Returns
        -------
        str
            EC2 instance ID
        
        Raises
        ------
        ResourceCreationError
            If spot instance creation fails
        """
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
                tag_spec = {
                    "Resources": [request_id],
                    "Tags": tags
                }
                ec2.create_tags(**tag_spec)
            
            # Wait for spot request to be fulfilled
            logger.debug(f"Waiting for spot request {request_id} to be fulfilled")
            waiter = ec2.get_waiter("spot_instance_request_fulfilled")
            waiter.wait(
                SpotInstanceRequestIds=[request_id],
                WaiterConfig={
                    "Delay": 5,
                    "MaxAttempts": 60
                }
            )
            
            # Get instance ID
            response = ec2.describe_spot_instance_requests(SpotInstanceRequestIds=[request_id])
            instance_id = response["SpotInstanceRequests"][0]["InstanceId"]
            
            # Wait for instance to be running
            wait_for_resource(instance_id, "instance_running", ec2, resource_name="EC2 spot instance")
            
            return instance_id
        except Exception as e:
            logger.error(f"Failed to create spot instance: {e}")
            raise ResourceCreationError(f"Failed to create spot instance: {e}") from e

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
        
        for resource_id in resource_ids:
            resource = self.resources.get(resource_id)
            if resource and resource.get("type") == RESOURCE_TYPE_EC2:
                ec2_instances.append(resource_id)
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
        
        for resource_id in resource_ids:
            resource = self.resources.get(resource_id)
            if resource and resource.get("type") == RESOURCE_TYPE_EC2:
                ec2_instances.append(resource_id)
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
        
        for resource_id in resource_ids:
            resource = self.resources.get(resource_id)
            if resource and resource.get("type") == RESOURCE_TYPE_EC2:
                ec2_instances.append(resource_id)
        
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
            # Delete security group
            if self.security_group_id:
                try:
                    delete_resource(self.security_group_id, self.session, RESOURCE_TYPE_SECURITY_GROUP)
                    logger.info(f"Deleted security group {self.security_group_id}")
                    self.security_group_id = None
                except ResourceNotFoundError:
                    logger.debug(f"Security group {self.security_group_id} not found or already deleted")
                    self.security_group_id = None
                except Exception as e:
                    logger.error(f"Failed to delete security group {self.security_group_id}: {e}")
            
            # Delete subnet
            if self.subnet_id:
                try:
                    delete_resource(self.subnet_id, self.session, RESOURCE_TYPE_SUBNET)
                    logger.info(f"Deleted subnet {self.subnet_id}")
                    self.subnet_id = None
                except ResourceNotFoundError:
                    logger.debug(f"Subnet {self.subnet_id} not found or already deleted")
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
                            InternetGatewayId=igw_id,
                            VpcId=self.vpc_id
                        )
                        ec2.delete_internet_gateway(InternetGatewayId=igw_id)
                        logger.debug(f"Deleted internet gateway {igw_id}")
                except Exception as e:
                    logger.error(f"Failed to delete internet gateways for VPC {self.vpc_id}: {e}")
                
                # Now delete the VPC
                try:
                    delete_resource(self.vpc_id, self.session, RESOURCE_TYPE_VPC, force=True)
                    logger.info(f"Deleted VPC {self.vpc_id}")
                    self.vpc_id = None
                except ResourceNotFoundError:
                    logger.debug(f"VPC {self.vpc_id} not found or already deleted")
                    self.vpc_id = None
                except Exception as e:
                    logger.error(f"Failed to delete VPC {self.vpc_id}: {e}")
            
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
        }
        
        # Add EC2 instances
        for resource_id, resource in self.resources.items():
            if resource.get("type") == RESOURCE_TYPE_EC2:
                result["ec2_instances"].append({
                    "id": resource_id,
                    "job_id": resource.get("job_id"),
                    "job_name": resource.get("job_name"),
                    "status": resource.get("status"),
                    "created_at": resource.get("created_at"),
                })
        
        # Add VPC if available
        if self.vpc_id:
            result["vpc"].append({
                "id": self.vpc_id,
            })
        
        # Add subnet if available
        if self.subnet_id:
            result["subnet"].append({
                "id": self.subnet_id,
                "vpc_id": self.vpc_id,
            })
        
        # Add security group if available
        if self.security_group_id:
            result["security_group"].append({
                "id": self.security_group_id,
                "vpc_id": self.vpc_id,
            })
        
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
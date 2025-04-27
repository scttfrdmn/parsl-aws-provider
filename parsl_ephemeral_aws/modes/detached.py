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
import tempfile
import time
import uuid
from pathlib import Path
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
    RESOURCE_TYPE_BASTION,
    RESOURCE_TYPE_CLOUDFORMATION,
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
    get_cf_template,
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
        state_store: "StateStore",
        workflow_id: Optional[str] = None,
        bastion_instance_type: str = "t3.micro",
        idle_timeout: int = 30,
        preserve_bastion: bool = True,
        bastion_host_type: str = "cloudformation",
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
        **kwargs : Any
            Additional arguments passed to the parent class
        """
        super().__init__(provider_id, session, state_store, **kwargs)
        
        # Detached mode specific attributes
        self.workflow_id = workflow_id or str(uuid.uuid4())
        self.bastion_id = None
        self.bastion_host_type = bastion_host_type
        self.bastion_instance_type = bastion_instance_type
        self.idle_timeout = idle_timeout
        self.preserve_bastion = preserve_bastion
        self.stack_name = None
        
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
                    logger.warning(f"Security group {self.security_group_id} does not exist")
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
                        logger.warning(f"Bastion stack {self.bastion_id} is in state {stack_status}")
                        self.bastion_id = None
                    else:
                        logger.debug(f"Verified bastion stack {self.bastion_id} exists with status {stack_status}")
                except ClientError as e:
                    if "does not exist" in str(e):
                        logger.warning(f"Bastion stack {self.bastion_id} does not exist")
                        self.bastion_id = None
                    else:
                        raise
            else:
                try:
                    response = ec2.describe_instances(InstanceIds=[self.bastion_id])
                    if not response["Reservations"] or not response["Reservations"][0]["Instances"]:
                        logger.warning(f"Bastion instance {self.bastion_id} not found")
                        self.bastion_id = None
                    else:
                        instance_state = response["Reservations"][0]["Instances"][0]["State"]["Name"]
                        if instance_state in ["terminated", "shutting-down"]:
                            logger.warning(f"Bastion instance {self.bastion_id} is {instance_state}")
                            self.bastion_id = None
                        else:
                            logger.debug(f"Verified bastion instance {self.bastion_id} exists with state {instance_state}")
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
                            {"Key": "Name", "Value": f"parsl-detached-{self.workflow_id[:8]}"},
                            {"Key": "CreatedBy", "Value": "ParslEphemeralAWSProvider"},
                            {"Key": "ProviderId", "Value": self.provider_id},
                            {"Key": "WorkflowId", "Value": self.workflow_id},
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
                            {"Key": "Name", "Value": f"parsl-detached-igw-{self.workflow_id[:8]}"},
                            {"Key": "CreatedBy", "Value": "ParslEphemeralAWSProvider"},
                            {"Key": "ProviderId", "Value": self.provider_id},
                            {"Key": "WorkflowId", "Value": self.workflow_id},
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
                            {"Key": "Name", "Value": f"parsl-detached-subnet-{self.workflow_id[:8]}"},
                            {"Key": "CreatedBy", "Value": "ParslEphemeralAWSProvider"},
                            {"Key": "ProviderId", "Value": self.provider_id},
                            {"Key": "WorkflowId", "Value": self.workflow_id},
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
                            {"Key": "Name", "Value": f"parsl-detached-rt-{self.workflow_id[:8]}"},
                            {"Key": "CreatedBy", "Value": "ParslEphemeralAWSProvider"},
                            {"Key": "ProviderId", "Value": self.provider_id},
                            {"Key": "WorkflowId", "Value": self.workflow_id},
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
                GroupName=f"{DEFAULT_SECURITY_GROUP_NAME}-{self.workflow_id[:8]}",
                Description=DEFAULT_SECURITY_GROUP_DESCRIPTION,
                VpcId=self.vpc_id,
                TagSpecifications=[
                    {
                        "ResourceType": "security-group",
                        "Tags": [
                            {"Key": "Name", "Value": f"parsl-detached-sg-{self.workflow_id[:8]}"},
                            {"Key": "CreatedBy", "Value": "ParslEphemeralAWSProvider"},
                            {"Key": "ProviderId", "Value": self.provider_id},
                            {"Key": "WorkflowId", "Value": self.workflow_id},
                        ]
                    }
                ]
            )
            
            security_group_id = response["GroupId"]
            logger.debug(f"Created security group {security_group_id} in VPC {self.vpc_id}")
            
            # Add inbound rules with SSH access
            inbound_rules = DEFAULT_INBOUND_RULES.copy() if DEFAULT_INBOUND_RULES else []
            # Add SSH rule if not already present
            ssh_rule_exists = any(rule.get("FromPort") == 22 and rule.get("ToPort") == 22 
                              for rule in inbound_rules)
            if not ssh_rule_exists:
                inbound_rules.append({
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}]
                })
            
            if inbound_rules:
                ec2.authorize_security_group_ingress(
                    GroupId=security_group_id,
                    IpPermissions=inbound_rules
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
            logger.info(f"Using default AMI {self.image_id} for region {self.session.region_name}")
        
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
                "Groups": [self.security_group_id]
            }
            
            # Create the bastion host
            response = ec2.run_instances(
                ImageId=self.image_id,
                InstanceType=self.bastion_instance_type,
                MaxCount=1,
                MinCount=1,
                UserData=init_script,
                KeyName=self.key_name,
                TagSpecifications=[
                    {
                        "ResourceType": "instance",
                        "Tags": tags
                    }
                ],
                NetworkInterfaces=[network_interface],
                InstanceInitiatedShutdownBehavior="terminate",
                Monitoring={"Enabled": True}
            )
            
            instance_id = response["Instances"][0]["InstanceId"]
            logger.debug(f"Created bastion host instance {instance_id}")
            
            # Wait for instance to be running
            wait_for_resource(instance_id, "instance_running", ec2, resource_name="EC2 bastion instance")
            
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
            logger.info(f"Using default AMI {self.image_id} for region {self.session.region_name}")
        
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
                    {"ParameterKey": "SecurityGroupId", "ParameterValue": self.security_group_id},
                    {"ParameterKey": "InstanceType", "ParameterValue": self.bastion_instance_type},
                    {"ParameterKey": "ImageId", "ParameterValue": self.image_id},
                    {"ParameterKey": "KeyName", "ParameterValue": self.key_name or ""},
                    {"ParameterKey": "WorkflowId", "ParameterValue": self.workflow_id},
                    {"ParameterKey": "UserData", "ParameterValue": init_script_b64},
                    {"ParameterKey": "UseSpotInstance", "ParameterValue": "true" if self.use_spot else "false"},
                    {"ParameterKey": "SpotMaxPrice", "ParameterValue": self.spot_max_price or ""},
                    {"ParameterKey": "IdleTimeout", "ParameterValue": str(self.idle_timeout)},
                    {"ParameterKey": "Tags", "ParameterValue": json.dumps(self.additional_tags)},
                ],
                Capabilities=["CAPABILITY_IAM"],
                OnFailure="DELETE",
                Tags=tags
            )
            
            stack_id = response["StackId"]
            logger.debug(f"Created CloudFormation stack {stack_id} for bastion host")
            
            # Wait for stack creation to complete
            logger.info(f"Waiting for bastion host stack {self.stack_name} to be created")
            waiter = cf.get_waiter("stack_create_complete")
            waiter.wait(
                StackName=self.stack_name,
                WaiterConfig={
                    "Delay": 10,
                    "MaxAttempts": 36  # Up to 6 minutes
                }
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
                "bastion_host_id": bastion_host_id
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
                    logger.info(f"Initiated deletion of stack {self.stack_name} due to error")
                except Exception as delete_error:
                    logger.error(f"Failed to clean up stack {self.stack_name}: {delete_error}")
            
            raise ResourceCreationError(f"Failed to create bastion host with CloudFormation: {e}") from e

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
        init_script += f"echo 'export PARSL_WORKFLOW_ID={self.workflow_id}' >> /etc/environment\n"
        init_script += f"echo 'export PARSL_PROVIDER_ID={self.provider_id}' >> /etc/environment\n"
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
        init_script += "cat > /etc/systemd/system/parsl-bastion-manager.service << 'EOL'\n"
        init_script += "[Unit]\n"
        init_script += "Description=Parsl Bastion Manager\n"
        init_script += "After=network.target\n\n"
        init_script += "[Service]\n"
        init_script += "Type=simple\n"
        init_script += "ExecStart=/usr/bin/python3 /usr/local/bin/parsl-bastion-manager.py\n"
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
        init_script += "RUNNING_JOBS=$(ps aux | grep -v grep | grep -c 'parsl-worker')\n\n"
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
        init_script += "        echo \"No activity for $IDLE_MINUTES minutes, shutting down\"\n"
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
SSM_PARAMETER_PREFIX = f'/parsl/workflows/{WORKFLOW_ID}'
JOB_COMMAND_PREFIX = f'{SSM_PARAMETER_PREFIX}/jobs'
JOB_STATUS_PREFIX = f'{SSM_PARAMETER_PREFIX}/status'
EC2_STATUS_MAPPING = {
    'pending': 'PENDING',
    'running': 'RUNNING',
    'shutting-down': 'CANCELED',
    'terminated': 'COMPLETED',
    'stopping': 'CANCELED',
    'stopped': 'CANCELED',
}

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

def update_job_status(job_id, status, instance_id=None, error=None):
    """Update job status in SSM Parameter Store."""
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

def launch_instance(job_data):
    """Launch an EC2 instance to run the job."""
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
        running_job_ids = []
        instance_ids = []
        
        for page in paginator.paginate(Path=JOB_STATUS_PREFIX, Recursive=True):
            for param in page['Parameters']:
                job_id = param['Name'].split('/')[-1]
                status_data = json.loads(param['Value'])
                
                if status_data.get('status') == 'RUNNING' and 'instance_id' in status_data:
                    running_job_ids.append(job_id)
                    instance_ids.append(status_data['instance_id'])
        
        if not instance_ids:
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
        
        # Update job statuses
        for i, job_id in enumerate(running_job_ids):
            instance_id = instance_ids[i]
            status = instance_statuses.get(instance_id)
            
            if status and status != 'RUNNING':
                update_job_status(job_id, status, instance_id)
                logger.info(f"Job {job_id} on instance {instance_id} changed state to {status}")
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
            
            if not job_ids:
                return
            
            # Get instance IDs for these jobs
            instance_ids = []
            for job_id in job_ids:
                try:
                    status_param = ssm.get_parameter(Name=f'{JOB_STATUS_PREFIX}/{job_id}')
                    status_data = json.loads(status_param['Parameter']['Value'])
                    
                    if 'instance_id' in status_data:
                        instance_ids.append(status_data['instance_id'])
                        update_job_status(job_id, 'CANCELED', status_data['instance_id'])
                except ClientError as e:
                    if e.response['Error']['Code'] != 'ParameterNotFound':
                        raise
            
            # Terminate instances
            if instance_ids:
                ec2.terminate_instances(InstanceIds=instance_ids)
                logger.info(f"Terminated instances for jobs: {job_ids}")
            
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
            Resource ID for tracking the job
        
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
            }
            
            # Store job in SSM Parameter Store
            ssm.put_parameter(
                Name=f"/parsl/workflows/{self.workflow_id}/jobs/{job_id}",
                Value=json.dumps(job_data),
                Type="String",
                Overwrite=True
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
                Overwrite=True
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
        
        # Collect job IDs to cancel
        job_ids = []
        for resource_id in resource_ids:
            resource = self.resources.get(resource_id)
            if resource and resource.get("job_id"):
                job_ids.append(resource.get("job_id"))
                # Mark as canceling in local state
                self.resources[resource_id]["status"] = STATUS_CANCELED
                cancel_map[resource_id] = STATUS_CANCELED
            else:
                cancel_map[resource_id] = STATUS_UNKNOWN
        
        if job_ids:
            try:
                # Submit cancel request to bastion host
                cancel_data = {
                    "job_ids": job_ids,
                    "requested_at": time.time()
                }
                
                ssm.put_parameter(
                    Name=f"/parsl/workflows/{self.workflow_id}/cancel",
                    Value=json.dumps(cancel_data),
                    Type="String",
                    Overwrite=True
                )
                
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
        
        # First, cancel any active jobs
        active_resources = []
        for resource_id in resource_ids:
            resource = self.resources.get(resource_id)
            if resource and resource.get("type") == RESOURCE_TYPE_EC2:
                status = resource.get("status")
                if status in [STATUS_PENDING, STATUS_RUNNING]:
                    active_resources.append(resource_id)
        
        if active_resources:
            self.cancel_jobs(active_resources)
        
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
                        logger.error(f"Failed to clean up parameters for job {job_id}: {e}")
                except Exception as e:
                    logger.error(f"Unexpected error cleaning up parameters for job {job_id}: {e}")
            
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
                if resource.get("type") != RESOURCE_TYPE_BASTION and resource.get("type") != RESOURCE_TYPE_CLOUDFORMATION:
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
                    logger.info(f"Initiated deletion of bastion stack {self.bastion_id}")
                    
                    # Remove from resources
                    if self.bastion_id in self.resources:
                        del self.resources[self.bastion_id]
                except Exception as e:
                    logger.error(f"Failed to delete bastion stack {self.bastion_id}: {e}")
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
                        logger.error(f"Failed to terminate bastion instance {self.bastion_id}: {e}")
                except Exception as e:
                    logger.error(f"Unexpected error terminating bastion instance {self.bastion_id}: {e}")
            
            self.bastion_id = None
        
        # Only delete networking if not preserving bastion
        if not self.preserve_bastion:
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
                result["ec2_instances"].append({
                    "id": resource_id,
                    "job_id": resource.get("job_id"),
                    "job_name": resource.get("job_name"),
                    "status": resource.get("status"),
                    "created_at": resource.get("created_at"),
                })
            elif resource.get("type") == RESOURCE_TYPE_BASTION or resource.get("type") == RESOURCE_TYPE_CLOUDFORMATION:
                result["bastion_host"].append({
                    "id": resource_id,
                    "type": resource.get("type"),
                    "workflow_id": resource.get("workflow_id"),
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
        
        # Just call cleanup_infrastructure with preserve_bastion=False
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
                self.security_group_id = state.get("security_group_id", self.security_group_id)
                self.initialized = state.get("initialized", False)
                self.workflow_id = state.get("workflow_id", self.workflow_id)
                self.bastion_id = state.get("bastion_id", self.bastion_id)
                self.bastion_host_type = state.get("bastion_host_type", self.bastion_host_type)
                self.stack_name = state.get("stack_name", self.stack_name)
                logger.debug(f"Loaded state with {len(self.resources)} resources")
                return True
        except Exception as e:
            logger.error(f"Failed to load state: {e}")
        
        return False
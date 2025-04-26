"""VPC and subnet management for Parsl Ephemeral AWS Provider.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import logging
import time
from typing import Dict, List, Optional, Any, Tuple

import boto3
from botocore.exceptions import ClientError

from ..exceptions import ResourceCreationError, ResourceCleanupError
from ..constants import TAG_PREFIX, TAG_NAME, TAG_WORKFLOW_ID, DEFAULT_VPC_CIDR, DEFAULT_SUBNET_CIDR


logger = logging.getLogger(__name__)


class VPCManager:
    """Manager for AWS VPC and networking resources."""
    
    def __init__(self, provider: Any) -> None:
        """Initialize the VPC manager.
        
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
            session_kwargs["aws_secret_access_key"] = self.provider.aws_secret_access_key
            
        if self.provider.aws_session_token:
            session_kwargs["aws_session_token"] = self.provider.aws_session_token
            
        if self.provider.aws_profile:
            session_kwargs["profile_name"] = self.provider.aws_profile
            
        self.aws_session = boto3.Session(
            region_name=self.provider.region,
            **session_kwargs
        )
        
        # Initialize clients
        self.ec2_client = self.aws_session.client('ec2')
        self.ec2_resource = self.aws_session.resource('ec2')
        
        # Track resources for cleanup
        self.vpc_id = None
        self.subnet_ids = []
        self.igw_id = None
        self.route_table_ids = []
        self.network_acl_ids = []
    
    def create_vpc(self, cidr_block: str = DEFAULT_VPC_CIDR) -> str:
        """Create a VPC.
        
        Parameters
        ----------
        cidr_block : str, optional
            CIDR block for the VPC, by default DEFAULT_VPC_CIDR
            
        Returns
        -------
        str
            ID of the created VPC
        """
        try:
            # Check if VPC already exists
            if self.vpc_id:
                logger.info(f"Using existing VPC: {self.vpc_id}")
                return self.vpc_id
            
            # Create VPC
            vpc_response = self.ec2_client.create_vpc(
                CidrBlock=cidr_block,
                TagSpecifications=[
                    {
                        'ResourceType': 'vpc',
                        'Tags': [
                            {'Key': 'Name', 'Value': f"{TAG_PREFIX}-vpc-{self.provider.workflow_id}"},
                            {'Key': TAG_NAME, 'Value': 'true'},
                            {'Key': TAG_WORKFLOW_ID, 'Value': self.provider.workflow_id}
                        ]
                    }
                ]
            )
            self.vpc_id = vpc_response['Vpc']['VpcId']
            logger.info(f"Created VPC: {self.vpc_id}")
            
            # Wait for VPC to be available
            self.ec2_client.get_waiter('vpc_available').wait(VpcIds=[self.vpc_id])
            
            # Enable DNS hostnames and support
            self.ec2_client.modify_vpc_attribute(
                VpcId=self.vpc_id,
                EnableDnsHostnames={'Value': True}
            )
            
            self.ec2_client.modify_vpc_attribute(
                VpcId=self.vpc_id,
                EnableDnsSupport={'Value': True}
            )
            
            # Add provider tags
            if self.provider.tags:
                self.ec2_client.create_tags(
                    Resources=[self.vpc_id],
                    Tags=[{'Key': k, 'Value': v} for k, v in self.provider.tags.items()]
                )
            
            return self.vpc_id
            
        except ClientError as e:
            logger.error(f"Error creating VPC: {e}")
            raise ResourceCreationError(f"Failed to create VPC: {e}")
    
    def create_internet_gateway(self) -> str:
        """Create an internet gateway and attach it to the VPC.
        
        Returns
        -------
        str
            ID of the created internet gateway
        """
        try:
            # Check if internet gateway already exists
            if self.igw_id:
                logger.info(f"Using existing internet gateway: {self.igw_id}")
                return self.igw_id
            
            # Ensure VPC exists
            if not self.vpc_id:
                self.create_vpc()
            
            # Create internet gateway
            igw_response = self.ec2_client.create_internet_gateway(
                TagSpecifications=[
                    {
                        'ResourceType': 'internet-gateway',
                        'Tags': [
                            {'Key': 'Name', 'Value': f"{TAG_PREFIX}-igw-{self.provider.workflow_id}"},
                            {'Key': TAG_NAME, 'Value': 'true'},
                            {'Key': TAG_WORKFLOW_ID, 'Value': self.provider.workflow_id}
                        ]
                    }
                ]
            )
            self.igw_id = igw_response['InternetGateway']['InternetGatewayId']
            
            # Attach to VPC
            self.ec2_client.attach_internet_gateway(
                InternetGatewayId=self.igw_id,
                VpcId=self.vpc_id
            )
            logger.info(f"Created and attached internet gateway: {self.igw_id}")
            
            # Add provider tags
            if self.provider.tags:
                self.ec2_client.create_tags(
                    Resources=[self.igw_id],
                    Tags=[{'Key': k, 'Value': v} for k, v in self.provider.tags.items()]
                )
            
            return self.igw_id
            
        except ClientError as e:
            logger.error(f"Error creating internet gateway: {e}")
            raise ResourceCreationError(f"Failed to create internet gateway: {e}")
    
    def create_subnet(self, cidr_block: str = DEFAULT_SUBNET_CIDR, 
                     availability_zone: Optional[str] = None,
                     is_public: bool = True) -> str:
        """Create a subnet in the VPC.
        
        Parameters
        ----------
        cidr_block : str, optional
            CIDR block for the subnet, by default DEFAULT_SUBNET_CIDR
        availability_zone : Optional[str], optional
            Availability zone for the subnet, by default None (AWS chooses)
        is_public : bool, optional
            Whether to make the subnet public, by default True
            
        Returns
        -------
        str
            ID of the created subnet
        """
        try:
            # Ensure VPC exists
            if not self.vpc_id:
                self.create_vpc()
            
            # Create subnet
            subnet_params = {
                'VpcId': self.vpc_id,
                'CidrBlock': cidr_block,
                'TagSpecifications': [
                    {
                        'ResourceType': 'subnet',
                        'Tags': [
                            {'Key': 'Name', 'Value': f"{TAG_PREFIX}-subnet-{self.provider.workflow_id}"},
                            {'Key': TAG_NAME, 'Value': 'true'},
                            {'Key': TAG_WORKFLOW_ID, 'Value': self.provider.workflow_id},
                            {'Key': 'IsPublic', 'Value': str(is_public).lower()}
                        ]
                    }
                ]
            }
            
            if availability_zone:
                subnet_params['AvailabilityZone'] = availability_zone
            
            subnet_response = self.ec2_client.create_subnet(**subnet_params)
            subnet_id = subnet_response['Subnet']['SubnetId']
            self.subnet_ids.append(subnet_id)
            logger.info(f"Created subnet: {subnet_id}")
            
            # Enable auto-assign public IP if requested
            if is_public:
                self.ec2_client.modify_subnet_attribute(
                    SubnetId=subnet_id,
                    MapPublicIpOnLaunch={'Value': True}
                )
            
            # Add provider tags
            if self.provider.tags:
                self.ec2_client.create_tags(
                    Resources=[subnet_id],
                    Tags=[{'Key': k, 'Value': v} for k, v in self.provider.tags.items()]
                )
            
            return subnet_id
            
        except ClientError as e:
            logger.error(f"Error creating subnet: {e}")
            raise ResourceCreationError(f"Failed to create subnet: {e}")
    
    def create_route_table(self, subnet_id: str, is_public: bool = True) -> str:
        """Create a route table and associate it with a subnet.
        
        Parameters
        ----------
        subnet_id : str
            ID of the subnet to associate with the route table
        is_public : bool, optional
            Whether this is a public route table (with internet access), by default True
            
        Returns
        -------
        str
            ID of the created route table
        """
        try:
            # Ensure VPC exists
            if not self.vpc_id:
                self.create_vpc()
            
            # Create route table
            route_table_response = self.ec2_client.create_route_table(
                VpcId=self.vpc_id,
                TagSpecifications=[
                    {
                        'ResourceType': 'route-table',
                        'Tags': [
                            {'Key': 'Name', 'Value': f"{TAG_PREFIX}-rt-{self.provider.workflow_id}"},
                            {'Key': TAG_NAME, 'Value': 'true'},
                            {'Key': TAG_WORKFLOW_ID, 'Value': self.provider.workflow_id},
                            {'Key': 'IsPublic', 'Value': str(is_public).lower()}
                        ]
                    }
                ]
            )
            route_table_id = route_table_response['RouteTable']['RouteTableId']
            self.route_table_ids.append(route_table_id)
            
            # If this is a public route table, add internet route
            if is_public:
                # Ensure internet gateway exists
                if not self.igw_id:
                    self.create_internet_gateway()
                
                # Add route to internet
                self.ec2_client.create_route(
                    RouteTableId=route_table_id,
                    DestinationCidrBlock='0.0.0.0/0',
                    GatewayId=self.igw_id
                )
            
            # Associate with subnet
            self.ec2_client.associate_route_table(
                RouteTableId=route_table_id,
                SubnetId=subnet_id
            )
            logger.info(f"Created route table {route_table_id} and associated with subnet {subnet_id}")
            
            # Add provider tags
            if self.provider.tags:
                self.ec2_client.create_tags(
                    Resources=[route_table_id],
                    Tags=[{'Key': k, 'Value': v} for k, v in self.provider.tags.items()]
                )
            
            return route_table_id
            
        except ClientError as e:
            logger.error(f"Error creating route table: {e}")
            raise ResourceCreationError(f"Failed to create route table: {e}")
    
    def create_network_configuration(self, num_subnets: int = 1, 
                                    subnet_cidrs: Optional[List[str]] = None,
                                    availability_zones: Optional[List[str]] = None,
                                    is_public: bool = True) -> Dict[str, Any]:
        """Create a complete network configuration.
        
        This includes VPC, subnets, internet gateway, and route tables.
        
        Parameters
        ----------
        num_subnets : int, optional
            Number of subnets to create, by default 1
        subnet_cidrs : Optional[List[str]], optional
            CIDR blocks for the subnets, by default None (auto-generated)
        availability_zones : Optional[List[str]], optional
            Availability zones for the subnets, by default None (AWS chooses)
        is_public : bool, optional
            Whether to make the subnets public, by default True
            
        Returns
        -------
        Dict[str, Any]
            Dictionary containing network configuration details
        """
        try:
            # Create VPC
            vpc_id = self.create_vpc()
            
            # Create internet gateway if public
            igw_id = None
            if is_public:
                igw_id = self.create_internet_gateway()
            
            # Generate subnet CIDRs if not provided
            if not subnet_cidrs:
                # Split the VPC CIDR into subnet CIDRs
                subnet_cidrs = self._generate_subnet_cidrs(DEFAULT_VPC_CIDR, num_subnets)
            
            # Ensure we have enough subnet CIDRs
            if len(subnet_cidrs) < num_subnets:
                raise ResourceCreationError(
                    f"Not enough subnet CIDRs provided. Need {num_subnets}, got {len(subnet_cidrs)}"
                )
            
            # Create subnets
            subnet_ids = []
            for i in range(num_subnets):
                az = None
                if availability_zones and i < len(availability_zones):
                    az = availability_zones[i]
                
                subnet_id = self.create_subnet(
                    cidr_block=subnet_cidrs[i],
                    availability_zone=az,
                    is_public=is_public
                )
                subnet_ids.append(subnet_id)
            
            # Create route tables
            route_table_ids = []
            for subnet_id in subnet_ids:
                rt_id = self.create_route_table(subnet_id, is_public)
                route_table_ids.append(rt_id)
            
            # Return network configuration
            return {
                'vpc_id': vpc_id,
                'subnet_ids': subnet_ids,
                'internet_gateway_id': igw_id,
                'route_table_ids': route_table_ids,
                'is_public': is_public
            }
            
        except Exception as e:
            logger.error(f"Error creating network configuration: {e}")
            
            # Attempt to clean up any created resources
            self.cleanup_network_resources()
            
            raise ResourceCreationError(f"Failed to create network configuration: {e}")
    
    def _generate_subnet_cidrs(self, vpc_cidr: str, num_subnets: int) -> List[str]:
        """Generate subnet CIDR blocks from a VPC CIDR.
        
        Parameters
        ----------
        vpc_cidr : str
            CIDR block of the VPC
        num_subnets : int
            Number of subnet CIDR blocks to generate
            
        Returns
        -------
        List[str]
            List of subnet CIDR blocks
        """
        import ipaddress
        
        # Parse VPC CIDR
        vpc_network = ipaddress.IPv4Network(vpc_cidr)
        
        # Calculate subnet prefix length (one more than VPC prefix length per bit of num_subnets)
        # This ensures we have enough address space for the requested number of subnets
        subnet_prefix_length = vpc_network.prefixlen + max(1, (num_subnets - 1).bit_length())
        
        # Generate subnet CIDRs
        subnet_cidrs = [str(subnet) for subnet in vpc_network.subnets(new_prefix=subnet_prefix_length)]
        
        # Return only the requested number of subnets
        return subnet_cidrs[:num_subnets]
    
    def cleanup_subnet(self, subnet_id: str) -> None:
        """Delete a subnet.
        
        Parameters
        ----------
        subnet_id : str
            ID of the subnet to delete
        """
        try:
            # Check if subnet exists
            try:
                self.ec2_client.describe_subnets(SubnetIds=[subnet_id])
            except ClientError as e:
                if e.response['Error']['Code'] == 'InvalidSubnetID.NotFound':
                    logger.warning(f"Subnet {subnet_id} not found, skipping cleanup")
                    return
                raise
            
            # Delete subnet
            self.ec2_client.delete_subnet(SubnetId=subnet_id)
            if subnet_id in self.subnet_ids:
                self.subnet_ids.remove(subnet_id)
            logger.info(f"Deleted subnet: {subnet_id}")
            
        except ClientError as e:
            logger.error(f"Error deleting subnet {subnet_id}: {e}")
            raise ResourceCleanupError(f"Failed to delete subnet {subnet_id}: {e}")
    
    def cleanup_route_table(self, route_table_id: str) -> None:
        """Delete a route table.
        
        Parameters
        ----------
        route_table_id : str
            ID of the route table to delete
        """
        try:
            # Check if route table exists
            try:
                response = self.ec2_client.describe_route_tables(RouteTableIds=[route_table_id])
                if not response['RouteTables']:
                    logger.warning(f"Route table {route_table_id} not found, skipping cleanup")
                    return
            except ClientError as e:
                if e.response['Error']['Code'] == 'InvalidRouteTableID.NotFound':
                    logger.warning(f"Route table {route_table_id} not found, skipping cleanup")
                    return
                raise
            
            # Disassociate all subnets
            route_table = response['RouteTables'][0]
            for association in route_table.get('Associations', []):
                if association.get('Main', False):
                    # Skip main route table association
                    continue
                
                self.ec2_client.disassociate_route_table(
                    AssociationId=association['RouteTableAssociationId']
                )
            
            # Delete routes
            for route in route_table.get('Routes', []):
                # Skip the local route (cannot be deleted)
                if route.get('GatewayId') == 'local':
                    continue
                
                try:
                    self.ec2_client.delete_route(
                        RouteTableId=route_table_id,
                        DestinationCidrBlock=route['DestinationCidrBlock']
                    )
                except ClientError as e:
                    logger.warning(f"Error deleting route in route table {route_table_id}: {e}")
            
            # Delete route table
            self.ec2_client.delete_route_table(RouteTableId=route_table_id)
            if route_table_id in self.route_table_ids:
                self.route_table_ids.remove(route_table_id)
            logger.info(f"Deleted route table: {route_table_id}")
            
        except ClientError as e:
            logger.error(f"Error deleting route table {route_table_id}: {e}")
            raise ResourceCleanupError(f"Failed to delete route table {route_table_id}: {e}")
    
    def detach_internet_gateway(self) -> None:
        """Detach the internet gateway from the VPC."""
        try:
            if not self.igw_id or not self.vpc_id:
                logger.warning("No internet gateway or VPC to detach")
                return
            
            # Check if internet gateway exists
            try:
                self.ec2_client.describe_internet_gateways(InternetGatewayIds=[self.igw_id])
            except ClientError as e:
                if e.response['Error']['Code'] == 'InvalidInternetGatewayID.NotFound':
                    logger.warning(f"Internet gateway {self.igw_id} not found, skipping detach")
                    self.igw_id = None
                    return
                raise
            
            # Detach from VPC
            self.ec2_client.detach_internet_gateway(
                InternetGatewayId=self.igw_id,
                VpcId=self.vpc_id
            )
            logger.info(f"Detached internet gateway {self.igw_id} from VPC {self.vpc_id}")
            
        except ClientError as e:
            logger.error(f"Error detaching internet gateway {self.igw_id}: {e}")
            raise ResourceCleanupError(f"Failed to detach internet gateway {self.igw_id}: {e}")
    
    def delete_internet_gateway(self) -> None:
        """Delete the internet gateway."""
        try:
            if not self.igw_id:
                logger.warning("No internet gateway to delete")
                return
            
            # Check if internet gateway exists
            try:
                self.ec2_client.describe_internet_gateways(InternetGatewayIds=[self.igw_id])
            except ClientError as e:
                if e.response['Error']['Code'] == 'InvalidInternetGatewayID.NotFound':
                    logger.warning(f"Internet gateway {self.igw_id} not found, skipping delete")
                    self.igw_id = None
                    return
                raise
            
            # Detach from VPC if needed
            if self.vpc_id:
                try:
                    self.detach_internet_gateway()
                except Exception as e:
                    logger.warning(f"Error detaching internet gateway: {e}")
            
            # Delete internet gateway
            self.ec2_client.delete_internet_gateway(InternetGatewayId=self.igw_id)
            logger.info(f"Deleted internet gateway: {self.igw_id}")
            self.igw_id = None
            
        except ClientError as e:
            logger.error(f"Error deleting internet gateway {self.igw_id}: {e}")
            raise ResourceCleanupError(f"Failed to delete internet gateway {self.igw_id}: {e}")
    
    def delete_vpc(self) -> None:
        """Delete the VPC."""
        try:
            if not self.vpc_id:
                logger.warning("No VPC to delete")
                return
            
            # Check if VPC exists
            try:
                self.ec2_client.describe_vpcs(VpcIds=[self.vpc_id])
            except ClientError as e:
                if e.response['Error']['Code'] == 'InvalidVpcID.NotFound':
                    logger.warning(f"VPC {self.vpc_id} not found, skipping delete")
                    self.vpc_id = None
                    return
                raise
            
            # Delete VPC
            self.ec2_client.delete_vpc(VpcId=self.vpc_id)
            logger.info(f"Deleted VPC: {self.vpc_id}")
            self.vpc_id = None
            
        except ClientError as e:
            logger.error(f"Error deleting VPC {self.vpc_id}: {e}")
            raise ResourceCleanupError(f"Failed to delete VPC {self.vpc_id}: {e}")
    
    def cleanup_network_resources(self) -> None:
        """Clean up all network resources."""
        try:
            # Delete subnets
            for subnet_id in list(self.subnet_ids):
                try:
                    self.cleanup_subnet(subnet_id)
                except Exception as e:
                    logger.error(f"Error cleaning up subnet {subnet_id}: {e}")
            
            # Delete route tables
            for route_table_id in list(self.route_table_ids):
                try:
                    self.cleanup_route_table(route_table_id)
                except Exception as e:
                    logger.error(f"Error cleaning up route table {route_table_id}: {e}")
            
            # Delete internet gateway
            if self.igw_id:
                try:
                    self.delete_internet_gateway()
                except Exception as e:
                    logger.error(f"Error cleaning up internet gateway: {e}")
            
            # Delete VPC
            if self.vpc_id:
                try:
                    self.delete_vpc()
                except Exception as e:
                    logger.error(f"Error cleaning up VPC: {e}")
            
        except Exception as e:
            logger.error(f"Error cleaning up network resources: {e}")
            raise ResourceCleanupError(f"Failed to clean up network resources: {e}")
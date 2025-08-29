#!/usr/bin/env python3
"""
Private Subnet Support for Phase 1.5 Enhanced AWS Provider.

Enables deployment of Parsl workers in private subnets without internet access,
using VPC endpoints for secure AWS API communication.
"""

import logging
import uuid
from typing import Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class PrivateSubnetError(Exception):
    """Base exception for private subnet issues."""

    pass


class VPCEndpointError(PrivateSubnetError):
    """Failed to create or configure VPC endpoints."""

    pass


class SubnetConfigurationError(PrivateSubnetError):
    """Subnet configuration issues."""

    pass


class PrivateSubnetManager:
    """Manages deployment in private subnets with VPC endpoints."""

    def __init__(self, session: boto3.Session, vpc_id: Optional[str] = None):
        """Initialize private subnet manager."""
        self.session = session
        self.ec2 = session.client("ec2")
        self.region = session.region_name
        self.vpc_id = vpc_id or self._get_default_vpc()

        logger.info(
            f"PrivateSubnetManager initialized for VPC {self.vpc_id} in {self.region}"
        )

    def ensure_private_subnet_ready(
        self, subnet_id: Optional[str] = None
    ) -> Dict[str, str]:
        """Ensure private subnet has required infrastructure."""
        logger.info("Ensuring private subnet infrastructure is ready...")

        if not subnet_id:
            subnet_id = self._select_private_subnet()

        # Verify subnet is actually private
        self._verify_private_subnet(subnet_id)

        # Ensure VPC endpoints exist
        vpc_endpoints = self._ensure_ssm_endpoints()

        # Create security group for private workers
        security_group_id = self._create_private_worker_security_group()

        result = {
            "subnet_id": subnet_id,
            "security_group_id": security_group_id,
            "vpc_id": self.vpc_id,
            "vpc_endpoints": vpc_endpoints,
        }

        logger.info(f"Private subnet infrastructure ready: {result}")
        return result

    def _get_default_vpc(self) -> str:
        """Get the default VPC ID."""
        try:
            response = self.ec2.describe_vpcs(
                Filters=[{"Name": "is-default", "Values": ["true"]}]
            )

            if not response["Vpcs"]:
                raise PrivateSubnetError("No default VPC found")

            vpc_id = response["Vpcs"][0]["VpcId"]
            logger.info(f"Found default VPC: {vpc_id}")
            return vpc_id

        except ClientError as e:
            logger.error(f"Failed to get default VPC: {e}")
            raise PrivateSubnetError(f"Failed to get default VPC: {e}")

    def _select_private_subnet(self) -> str:
        """Select or create a private subnet."""
        try:
            # Look for existing private subnets (no route to internet gateway)
            response = self.ec2.describe_subnets(
                Filters=[
                    {"Name": "vpc-id", "Values": [self.vpc_id]},
                    {"Name": "state", "Values": ["available"]},
                ]
            )

            for subnet in response["Subnets"]:
                subnet_id = subnet["SubnetId"]
                if self._is_subnet_private(subnet_id):
                    logger.info(f"Using existing private subnet: {subnet_id}")
                    return subnet_id

            # If no private subnet exists, create one
            logger.info("No private subnet found, creating one...")
            return self._create_private_subnet()

        except ClientError as e:
            logger.error(f"Failed to select private subnet: {e}")
            raise SubnetConfigurationError(f"Failed to select private subnet: {e}")

    def _is_subnet_private(self, subnet_id: str) -> bool:
        """Check if subnet is private (no route to internet gateway)."""
        try:
            # Get route table for subnet
            response = self.ec2.describe_route_tables(
                Filters=[{"Name": "association.subnet-id", "Values": [subnet_id]}]
            )

            # If no explicit association, check main route table
            if not response["RouteTables"]:
                response = self.ec2.describe_route_tables(
                    Filters=[
                        {"Name": "vpc-id", "Values": [self.vpc_id]},
                        {"Name": "association.main", "Values": ["true"]},
                    ]
                )

            if not response["RouteTables"]:
                return False

            route_table = response["RouteTables"][0]

            # Check for routes to internet gateway
            for route in route_table["Routes"]:
                if (
                    route.get("GatewayId", "").startswith("igw-")
                    and route.get("DestinationCidrBlock") == "0.0.0.0/0"
                ):
                    return False

            return True

        except ClientError as e:
            logger.warning(f"Error checking if subnet {subnet_id} is private: {e}")
            return False

    def _create_private_subnet(self) -> str:
        """Create a new private subnet."""
        try:
            # Get VPC CIDR to determine subnet CIDR
            vpc_response = self.ec2.describe_vpcs(VpcIds=[self.vpc_id])
            vpc_cidr = vpc_response["Vpcs"][0]["CidrBlock"]

            # Find available subnet CIDR dynamically
            private_cidr = self._find_available_subnet_cidr(vpc_cidr)

            # Get availability zones
            az_response = self.ec2.describe_availability_zones()
            availability_zone = az_response["AvailabilityZones"][0]["ZoneName"]

            # Create subnet
            response = self.ec2.create_subnet(
                VpcId=self.vpc_id,
                CidrBlock=private_cidr,
                AvailabilityZone=availability_zone,
            )

            subnet_id = response["Subnet"]["SubnetId"]

            # Tag the subnet
            self.ec2.create_tags(
                Resources=[subnet_id],
                Tags=[
                    {
                        "Key": "Name",
                        "Value": f"parsl-private-subnet-{uuid.uuid4().hex[:8]}",
                    },
                    {"Key": "Type", "Value": "Private"},
                    {"Key": "CreatedBy", "Value": "parsl-aws-provider"},
                ],
            )

            logger.info(f"Created private subnet: {subnet_id} in {availability_zone}")
            return subnet_id

        except ClientError as e:
            logger.error(f"Failed to create private subnet: {e}")
            raise SubnetConfigurationError(f"Failed to create private subnet: {e}")

    def _find_available_subnet_cidr(self, vpc_cidr: str) -> str:
        """Find an available subnet CIDR within the VPC."""
        import ipaddress

        try:
            # Get existing subnet CIDRs
            existing_response = self.ec2.describe_subnets(
                Filters=[{"Name": "vpc-id", "Values": [self.vpc_id]}]
            )
            existing_cidrs = [
                subnet["CidrBlock"] for subnet in existing_response["Subnets"]
            ]

            # Parse VPC network
            vpc_network = ipaddress.IPv4Network(vpc_cidr)

            # Try to find available /24 subnet
            for subnet in vpc_network.subnets(new_prefix=24):
                subnet_cidr = str(subnet)
                if subnet_cidr not in existing_cidrs:
                    logger.info(f"Found available subnet CIDR: {subnet_cidr}")
                    return subnet_cidr

            # Fallback: try /28 subnets (16 IPs)
            for subnet in vpc_network.subnets(new_prefix=28):
                subnet_cidr = str(subnet)
                if subnet_cidr not in existing_cidrs:
                    logger.info(f"Found available small subnet CIDR: {subnet_cidr}")
                    return subnet_cidr

            raise SubnetConfigurationError("No available subnet CIDR found in VPC")

        except Exception as e:
            # Fallback to a random high subnet
            import random

            third_octet = random.randint(100, 254)
            fallback_cidr = f"10.0.{third_octet}.0/24"
            logger.warning(f"Using fallback CIDR {fallback_cidr}: {e}")
            return fallback_cidr

    def _verify_private_subnet(self, subnet_id: str) -> None:
        """Verify subnet is actually private."""
        if not self._is_subnet_private(subnet_id):
            logger.warning(f"Subnet {subnet_id} appears to have internet access")
            logger.warning("Private subnet deployment may not be fully isolated")

    def _ensure_ssm_endpoints(self) -> Dict[str, str]:
        """Create SSM VPC endpoints if they don't exist."""
        logger.info("Ensuring SSM VPC endpoints exist...")

        required_endpoints = [
            f"com.amazonaws.{self.region}.ssm",  # SSM service
            f"com.amazonaws.{self.region}.ssmmessages",  # SSM messages
            f"com.amazonaws.{self.region}.ec2messages",  # EC2 messages
        ]

        existing_endpoints = self._list_vpc_endpoints()
        created_endpoints = {}

        for service_name in required_endpoints:
            if service_name not in existing_endpoints:
                endpoint_id = self._create_vpc_endpoint(service_name)
                created_endpoints[service_name] = endpoint_id
                logger.info(f"Created VPC endpoint: {service_name} -> {endpoint_id}")
            else:
                created_endpoints[service_name] = existing_endpoints[service_name]
                logger.info(f"Using existing VPC endpoint: {service_name}")

        return created_endpoints

    def _list_vpc_endpoints(self) -> Dict[str, str]:
        """List existing VPC endpoints."""
        try:
            response = self.ec2.describe_vpc_endpoints(
                Filters=[{"Name": "vpc-id", "Values": [self.vpc_id]}]
            )

            endpoints = {}
            for endpoint in response["VpcEndpoints"]:
                service_name = endpoint["ServiceName"]
                endpoint_id = endpoint["VpcEndpointId"]
                endpoints[service_name] = endpoint_id

            logger.debug(f"Found {len(endpoints)} existing VPC endpoints")
            return endpoints

        except ClientError as e:
            logger.error(f"Failed to list VPC endpoints: {e}")
            return {}

    def _create_vpc_endpoint(self, service_name: str) -> str:
        """Create a VPC endpoint for the specified service."""
        try:
            # Get all private subnets for the endpoint
            private_subnets = self._get_all_private_subnets()

            response = self.ec2.create_vpc_endpoint(
                VpcId=self.vpc_id,
                ServiceName=service_name,
                VpcEndpointType="Interface",
                SubnetIds=private_subnets,
                PolicyDocument=json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Principal": "*",
                                "Action": "*",
                                "Resource": "*",
                            }
                        ],
                    }
                ),
            )

            endpoint_id = response["VpcEndpoint"]["VpcEndpointId"]

            # Tag the endpoint
            self.ec2.create_tags(
                Resources=[endpoint_id],
                Tags=[
                    {"Key": "Name", "Value": f'parsl-{service_name.split(".")[-1]}'},
                    {"Key": "CreatedBy", "Value": "parsl-aws-provider"},
                ],
            )

            return endpoint_id

        except ClientError as e:
            logger.error(f"Failed to create VPC endpoint for {service_name}: {e}")
            raise VPCEndpointError(f"Failed to create VPC endpoint: {e}")

    def _get_all_private_subnets(self) -> List[str]:
        """Get all private subnets in the VPC."""
        try:
            response = self.ec2.describe_subnets(
                Filters=[
                    {"Name": "vpc-id", "Values": [self.vpc_id]},
                    {"Name": "state", "Values": ["available"]},
                ]
            )

            private_subnets = []
            for subnet in response["Subnets"]:
                subnet_id = subnet["SubnetId"]
                if self._is_subnet_private(subnet_id):
                    private_subnets.append(subnet_id)

            if not private_subnets:
                # If no private subnets, use all subnets (endpoints need at least one)
                private_subnets = [subnet["SubnetId"] for subnet in response["Subnets"]]

            logger.debug(
                f"Found {len(private_subnets)} private subnets for VPC endpoints"
            )
            return private_subnets

        except ClientError as e:
            logger.error(f"Failed to get private subnets: {e}")
            return []

    def _create_private_worker_security_group(self) -> str:
        """Create security group for workers in private subnets."""
        try:
            group_name = f"parsl-private-workers-{uuid.uuid4().hex[:8]}"

            response = self.ec2.create_security_group(
                GroupName=group_name,
                Description="Parsl workers in private subnet - VPC endpoints only",
                VpcId=self.vpc_id,
            )

            sg_id = response["GroupId"]

            # Remove default egress rule (allow all)
            try:
                self.ec2.revoke_security_group_egress(
                    GroupId=sg_id,
                    IpPermissions=[
                        {"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}
                    ],
                )
            except ClientError:
                pass  # May not exist

            # Allow outbound to VPC endpoints only (HTTPS on 443)
            # Get VPC CIDR block
            vpc_response = self.ec2.describe_vpcs(VpcIds=[self.vpc_id])
            vpc_cidr = vpc_response["Vpcs"][0]["CidrBlock"]

            self.ec2.authorize_security_group_egress(
                GroupId=sg_id,
                IpPermissions=[
                    {
                        "IpProtocol": "tcp",
                        "FromPort": 443,
                        "ToPort": 443,
                        "IpRanges": [
                            {"CidrIp": vpc_cidr, "Description": "VPC endpoints"}
                        ],
                    }
                ],
            )

            # Tag the security group
            self.ec2.create_tags(
                Resources=[sg_id],
                Tags=[
                    {"Key": "Name", "Value": group_name},
                    {"Key": "Type", "Value": "PrivateWorker"},
                    {"Key": "CreatedBy", "Value": "parsl-aws-provider"},
                ],
            )

            logger.info(f"Created private worker security group: {sg_id}")
            return sg_id

        except ClientError as e:
            logger.error(f"Failed to create private security group: {e}")
            raise SubnetConfigurationError(f"Failed to create security group: {e}")

    def get_private_subnet_user_data(self) -> str:
        """Generate user data script for private subnet instances."""
        return """#!/bin/bash
# Minimal setup for private subnet workers
# SSM agent already installed in optimized AMI
# No internet access - all communication via VPC endpoints

# Verify SSM agent is running
systemctl status amazon-ssm-agent || systemctl start amazon-ssm-agent

# Set up logging for troubleshooting
exec > >(tee /var/log/user-data.log) 2>&1
echo "$(date): Private subnet worker initialization complete"

# Verify VPC endpoint connectivity
echo "$(date): Testing VPC endpoint connectivity..."
aws ssm describe-instance-information --region {region} --max-items 1 || echo "VPC endpoint connectivity test failed"
echo "$(date): Private subnet worker ready"
""".format(region=self.region)


import json  # Need to import json for VPC endpoint policy

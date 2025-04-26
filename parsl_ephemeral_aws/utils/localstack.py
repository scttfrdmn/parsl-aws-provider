"""LocalStack utilities for Parsl Ephemeral AWS Provider.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import logging
import os
import boto3
from typing import Dict, Optional, Any

logger = logging.getLogger(__name__)


def get_localstack_endpoint() -> str:
    """Get the LocalStack endpoint URL.
    
    Returns
    -------
    str
        The LocalStack endpoint URL
    """
    # Check environment variable first
    if 'LOCALSTACK_ENDPOINT' in os.environ:
        return os.environ['LOCALSTACK_ENDPOINT']
    
    # Default endpoint
    return "http://localhost:4566"


def is_localstack_running(endpoint: Optional[str] = None) -> bool:
    """Check if LocalStack is running.
    
    Parameters
    ----------
    endpoint : Optional[str]
        The LocalStack endpoint URL, by default None (uses get_localstack_endpoint)
        
    Returns
    -------
    bool
        True if LocalStack is running, False otherwise
    """
    if not endpoint:
        endpoint = get_localstack_endpoint()
    
    try:
        import requests
        response = requests.get(f"{endpoint}/health", timeout=1)
        return response.status_code == 200
    except Exception as e:
        logger.debug(f"Error checking LocalStack status: {e}")
        return False


def create_localstack_session(
    region: str = 'us-east-1', 
    endpoint: Optional[str] = None
) -> boto3.Session:
    """Create a boto3 session that connects to LocalStack.
    
    Parameters
    ----------
    region : str, optional
        The AWS region to use, by default 'us-east-1'
    endpoint : Optional[str], optional
        The LocalStack endpoint URL, by default None (uses get_localstack_endpoint)
        
    Returns
    -------
    boto3.Session
        A boto3 session configured to use LocalStack
    """
    if not endpoint:
        endpoint = get_localstack_endpoint()
    
    if not is_localstack_running(endpoint):
        raise RuntimeError("LocalStack is not running")
    
    # Create a session with dummy credentials
    return boto3.Session(
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name=region
    )


def get_localstack_client(
    service_name: str, 
    session: Optional[boto3.Session] = None,
    region: str = 'us-east-1', 
    endpoint: Optional[str] = None
) -> Any:
    """Get a boto3 client for a specific service that connects to LocalStack.
    
    Parameters
    ----------
    service_name : str
        The name of the AWS service
    session : Optional[boto3.Session], optional
        An existing boto3 session to use, by default None (creates a new one)
    region : str, optional
        The AWS region to use, by default 'us-east-1'
    endpoint : Optional[str], optional
        The LocalStack endpoint URL, by default None (uses get_localstack_endpoint)
        
    Returns
    -------
    Any
        A boto3 client for the specified service
    """
    if not endpoint:
        endpoint = get_localstack_endpoint()
    
    if not session:
        session = create_localstack_session(region, endpoint)
    
    # Create client with the LocalStack endpoint
    return session.client(
        service_name,
        endpoint_url=endpoint
    )


def get_localstack_resource(
    service_name: str, 
    session: Optional[boto3.Session] = None,
    region: str = 'us-east-1', 
    endpoint: Optional[str] = None
) -> Any:
    """Get a boto3 resource for a specific service that connects to LocalStack.
    
    Parameters
    ----------
    service_name : str
        The name of the AWS service
    session : Optional[boto3.Session], optional
        An existing boto3 session to use, by default None (creates a new one)
    region : str, optional
        The AWS region to use, by default 'us-east-1'
    endpoint : Optional[str], optional
        The LocalStack endpoint URL, by default None (uses get_localstack_endpoint)
        
    Returns
    -------
    Any
        A boto3 resource for the specified service
    """
    if not endpoint:
        endpoint = get_localstack_endpoint()
    
    if not session:
        session = create_localstack_session(region, endpoint)
    
    # Create resource with the LocalStack endpoint
    return session.resource(
        service_name,
        endpoint_url=endpoint
    )


def setup_localstack_vpc() -> Dict[str, str]:
    """Set up a VPC in LocalStack for testing.
    
    Returns
    -------
    Dict[str, str]
        Dictionary containing VPC resource IDs
    """
    ec2_client = get_localstack_client('ec2')
    
    # Create a VPC
    vpc_response = ec2_client.create_vpc(CidrBlock='10.0.0.0/16')
    vpc_id = vpc_response['Vpc']['VpcId']
    
    # Enable DNS support and hostnames
    ec2_client.modify_vpc_attribute(
        VpcId=vpc_id,
        EnableDnsSupport={'Value': True}
    )
    ec2_client.modify_vpc_attribute(
        VpcId=vpc_id,
        EnableDnsHostnames={'Value': True}
    )
    
    # Tag the VPC
    ec2_client.create_tags(
        Resources=[vpc_id],
        Tags=[
            {'Key': 'Name', 'Value': 'parsl-test-vpc'},
            {'Key': 'ParslResource', 'Value': 'true'}
        ]
    )
    
    # Create a subnet
    subnet_response = ec2_client.create_subnet(
        VpcId=vpc_id,
        CidrBlock='10.0.0.0/24',
        AvailabilityZone=f"{ec2_client.meta.region_name}a"
    )
    subnet_id = subnet_response['Subnet']['SubnetId']
    
    # Tag the subnet
    ec2_client.create_tags(
        Resources=[subnet_id],
        Tags=[
            {'Key': 'Name', 'Value': 'parsl-test-subnet'},
            {'Key': 'ParslResource', 'Value': 'true'}
        ]
    )
    
    # Create an internet gateway
    igw_response = ec2_client.create_internet_gateway()
    igw_id = igw_response['InternetGateway']['InternetGatewayId']
    
    # Tag the internet gateway
    ec2_client.create_tags(
        Resources=[igw_id],
        Tags=[
            {'Key': 'Name', 'Value': 'parsl-test-igw'},
            {'Key': 'ParslResource', 'Value': 'true'}
        ]
    )
    
    # Attach the internet gateway to the VPC
    ec2_client.attach_internet_gateway(
        InternetGatewayId=igw_id,
        VpcId=vpc_id
    )
    
    # Create a route table
    route_table_response = ec2_client.create_route_table(VpcId=vpc_id)
    route_table_id = route_table_response['RouteTable']['RouteTableId']
    
    # Tag the route table
    ec2_client.create_tags(
        Resources=[route_table_id],
        Tags=[
            {'Key': 'Name', 'Value': 'parsl-test-rt'},
            {'Key': 'ParslResource', 'Value': 'true'}
        ]
    )
    
    # Create a route to the internet
    ec2_client.create_route(
        RouteTableId=route_table_id,
        DestinationCidrBlock='0.0.0.0/0',
        GatewayId=igw_id
    )
    
    # Associate the route table with the subnet
    ec2_client.associate_route_table(
        RouteTableId=route_table_id,
        SubnetId=subnet_id
    )
    
    # Create a security group
    sg_response = ec2_client.create_security_group(
        GroupName='parsl-test-sg',
        Description='Parsl test security group',
        VpcId=vpc_id
    )
    sg_id = sg_response['GroupId']
    
    # Tag the security group
    ec2_client.create_tags(
        Resources=[sg_id],
        Tags=[
            {'Key': 'Name', 'Value': 'parsl-test-sg'},
            {'Key': 'ParslResource', 'Value': 'true'}
        ]
    )
    
    # Add inbound rules
    ec2_client.authorize_security_group_ingress(
        GroupId=sg_id,
        IpPermissions=[
            {
                'IpProtocol': 'tcp',
                'FromPort': 22,
                'ToPort': 22,
                'IpRanges': [{'CidrIp': '0.0.0.0/0'}]
            },
            {
                'IpProtocol': 'tcp',
                'FromPort': 54000,
                'ToPort': 55000,
                'IpRanges': [{'CidrIp': '0.0.0.0/0'}]
            }
        ]
    )
    
    # Add self-referencing rule
    ec2_client.authorize_security_group_ingress(
        GroupId=sg_id,
        IpPermissions=[
            {
                'IpProtocol': '-1',
                'FromPort': -1,
                'ToPort': -1,
                'UserIdGroupPairs': [{'GroupId': sg_id}]
            }
        ]
    )
    
    return {
        'vpc_id': vpc_id,
        'subnet_id': subnet_id,
        'security_group_id': sg_id,
        'route_table_id': route_table_id,
        'internet_gateway_id': igw_id
    }


def cleanup_localstack_vpc(vpc_id: str) -> None:
    """Clean up a VPC and all associated resources in LocalStack.
    
    Parameters
    ----------
    vpc_id : str
        The ID of the VPC to clean up
    """
    ec2_client = get_localstack_client('ec2')
    
    try:
        # Describe the VPC to get all associated resources
        vpc_response = ec2_client.describe_vpcs(VpcIds=[vpc_id])
        
        if not vpc_response or 'Vpcs' not in vpc_response or not vpc_response['Vpcs']:
            logger.warning(f"VPC {vpc_id} not found")
            return
        
        # Get security groups
        sg_response = ec2_client.describe_security_groups(
            Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}]
        )
        
        # Delete security groups (except default)
        for sg in sg_response.get('SecurityGroups', []):
            if sg['GroupName'] != 'default':
                try:
                    ec2_client.delete_security_group(GroupId=sg['GroupId'])
                    logger.debug(f"Deleted security group {sg['GroupId']}")
                except Exception as e:
                    logger.error(f"Error deleting security group {sg['GroupId']}: {e}")
        
        # Get subnets
        subnet_response = ec2_client.describe_subnets(
            Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}]
        )
        
        # Delete subnets
        for subnet in subnet_response.get('Subnets', []):
            try:
                ec2_client.delete_subnet(SubnetId=subnet['SubnetId'])
                logger.debug(f"Deleted subnet {subnet['SubnetId']}")
            except Exception as e:
                logger.error(f"Error deleting subnet {subnet['SubnetId']}: {e}")
        
        # Get route tables
        rt_response = ec2_client.describe_route_tables(
            Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}]
        )
        
        # Delete route tables (except main)
        for rt in rt_response.get('RouteTables', []):
            # Skip the main route table
            is_main = False
            for assoc in rt.get('Associations', []):
                if assoc.get('Main', False):
                    is_main = True
                    break
            
            if not is_main:
                try:
                    ec2_client.delete_route_table(RouteTableId=rt['RouteTableId'])
                    logger.debug(f"Deleted route table {rt['RouteTableId']}")
                except Exception as e:
                    logger.error(f"Error deleting route table {rt['RouteTableId']}: {e}")
        
        # Get internet gateways
        igw_response = ec2_client.describe_internet_gateways(
            Filters=[{'Name': 'attachment.vpc-id', 'Values': [vpc_id]}]
        )
        
        # Detach and delete internet gateways
        for igw in igw_response.get('InternetGateways', []):
            try:
                ec2_client.detach_internet_gateway(
                    InternetGatewayId=igw['InternetGatewayId'],
                    VpcId=vpc_id
                )
                ec2_client.delete_internet_gateway(
                    InternetGatewayId=igw['InternetGatewayId']
                )
                logger.debug(f"Deleted internet gateway {igw['InternetGatewayId']}")
            except Exception as e:
                logger.error(f"Error deleting internet gateway {igw['InternetGatewayId']}: {e}")
        
        # Delete the VPC
        try:
            ec2_client.delete_vpc(VpcId=vpc_id)
            logger.debug(f"Deleted VPC {vpc_id}")
        except Exception as e:
            logger.error(f"Error deleting VPC {vpc_id}: {e}")
    
    except Exception as e:
        logger.error(f"Error cleaning up VPC {vpc_id}: {e}")
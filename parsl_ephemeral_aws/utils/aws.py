"""
AWS utility functions for the EphemeralAWSProvider.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional, Union

import boto3
from botocore.exceptions import ClientError

from parsl_ephemeral_aws.constants import DEFAULT_AMI_MAPPING, DEFAULT_REGION
from parsl_ephemeral_aws.exceptions import (
    AMINotFoundError,
    AWSAuthenticationError,
    AWSConnectionError,
    ResourceCreationError,
    ResourceDeletionError,
    ResourceNotFoundError,
)


logger = logging.getLogger(__name__)


def create_session(
    region: Optional[str] = None,
    profile_name: Optional[str] = None,
    aws_access_key_id: Optional[str] = None,
    aws_secret_access_key: Optional[str] = None,
    aws_session_token: Optional[str] = None,
    endpoint_url: Optional[str] = None,
) -> boto3.Session:
    """Create a boto3 session with the given parameters.

    Parameters
    ----------
    region : Optional[str], optional
        AWS region to use, by default None
    profile_name : Optional[str], optional
        AWS profile name to use, by default None
    aws_access_key_id : Optional[str], optional
        AWS access key ID, by default None
    aws_secret_access_key : Optional[str], optional
        AWS secret access key, by default None
    aws_session_token : Optional[str], optional
        AWS session token, by default None
    endpoint_url : Optional[str], optional
        Custom endpoint URL for AWS services (e.g., for LocalStack), by default None

    Returns
    -------
    boto3.Session
        The created boto3 session

    Raises
    ------
    AWSAuthenticationError
        If authentication fails
    AWSConnectionError
        If connection to AWS services fails
    """
    try:
        session = boto3.Session(
            region_name=region or DEFAULT_REGION,
            profile_name=profile_name,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            aws_session_token=aws_session_token,
        )

        # Verify that the session is valid by calling a simple operation
        sts = session.client("sts", endpoint_url=endpoint_url)
        sts.get_caller_identity()

        logger.debug(f"Created AWS session for region {session.region_name}")
        return session

    except ClientError as e:
        if "InvalidClientTokenId" in str(e) or "AccessDenied" in str(e):
            logger.error(f"AWS authentication failed: {e}")
            raise AWSAuthenticationError(f"AWS authentication failed: {e}") from e
        else:
            logger.error(f"AWS connection failed: {e}")
            raise AWSConnectionError(f"AWS connection failed: {e}") from e
    except Exception as e:
        logger.error(f"Failed to create AWS session: {e}")
        raise AWSConnectionError(f"Failed to create AWS session: {e}") from e


def get_default_ami(region: str) -> str:
    """Get the default AMI ID for the given region.

    Parameters
    ----------
    region : str
        AWS region

    Returns
    -------
    str
        Default AMI ID for the region

    Raises
    ------
    AMINotFoundError
        If no default AMI is found for the region
    """
    if region in DEFAULT_AMI_MAPPING:
        return DEFAULT_AMI_MAPPING[region]
    else:
        message = f"No default AMI found for region {region}"
        logger.error(message)
        raise AMINotFoundError(message)


def wait_for_resource(
    resource_id: str,
    waiter_name: str,
    service_client: Any,
    waiter_config: Optional[Dict[str, Any]] = None,
    resource_name: str = "resource",
    delay: int = 5,
    max_attempts: int = 60,
) -> None:
    """Wait for a resource to reach the desired state.

    Parameters
    ----------
    resource_id : str
        Resource ID to wait for
    waiter_name : str
        Name of the waiter to use
    service_client : Any
        Boto3 service client
    waiter_config : Optional[Dict[str, Any]], optional
        Waiter configuration, by default None
    resource_name : str, optional
        Name of the resource for logging purposes, by default "resource"
    delay : int, optional
        Seconds between waiter attempts, by default 5
    max_attempts : int, optional
        Maximum number of waiter attempts, by default 60

    Raises
    ------
    ResourceCreationError
        If the resource fails to reach the desired state
    """
    try:
        logger.debug(f"Waiting for {resource_name} {resource_id} ({waiter_name})")
        waiter = service_client.get_waiter(waiter_name)

        config = {
            "WaiterConfig": {
                "Delay": delay,
                "MaxAttempts": max_attempts,
            }
        }

        if waiter_config:
            config["WaiterConfig"].update(waiter_config)

        if waiter_name in ["instance_running", "instance_status_ok"]:
            waiter.wait(InstanceIds=[resource_id], **config)
        elif waiter_name in ["vpc_available", "vpc_exists"]:
            waiter.wait(VpcIds=[resource_id], **config)
        elif waiter_name in ["subnet_available"]:
            waiter.wait(SubnetIds=[resource_id], **config)
        elif waiter_name in ["security_group_exists"]:
            waiter.wait(GroupIds=[resource_id], **config)
        elif waiter_name in ["function_active", "function_exists"]:
            waiter.wait(FunctionName=resource_id, **config)
        elif waiter_name in ["task_running", "task_stopped"]:
            waiter.wait(Tasks=[resource_id], **config)
        elif "stack" in waiter_name:
            waiter.wait(StackName=resource_id, **config)
        else:
            # Generic wait for resources without specific waiter support
            logger.debug(f"Using generic wait for {resource_name} {resource_id}")
            waiter.wait(Id=resource_id, **config)

        logger.debug(
            f"{resource_name.capitalize()} {resource_id} reached desired state"
        )

    except Exception as e:
        logger.error(f"Error waiting for {resource_name} {resource_id}: {e}")
        raise ResourceCreationError(
            f"Error waiting for {resource_name} {resource_id}: {e}"
        ) from e


def create_tags(
    resource_ids: Union[str, List[str]],
    tags: Dict[str, str],
    session: boto3.Session,
    region: Optional[str] = None,
) -> None:
    """Create tags for AWS resources.

    Parameters
    ----------
    resource_ids : Union[str, List[str]]
        Resource ID or list of resource IDs to tag
    tags : Dict[str, str]
        Tags to apply to the resources
    session : boto3.Session
        Boto3 session to use
    region : Optional[str], optional
        AWS region, by default None

    Raises
    ------
    ResourceCreationError
        If tagging fails
    """
    if not isinstance(resource_ids, list):
        resource_ids = [resource_ids]

    if not resource_ids:
        logger.debug("No resources to tag")
        return

    if not tags:
        logger.debug("No tags to apply")
        return

    # Convert tags dictionary to AWS Tags format
    aws_tags = [{"Key": key, "Value": value} for key, value in tags.items()]

    try:
        ec2 = session.client("ec2", region_name=region)
        ec2.create_tags(Resources=resource_ids, Tags=aws_tags)
        logger.debug(f"Created tags for resources {resource_ids}: {tags}")
    except Exception as e:
        logger.error(f"Failed to create tags for resources {resource_ids}: {e}")
        # Don't raise an exception here, as tagging failure should not abort the operation
        logger.warning("Continuing despite tag creation failure")


def get_resources_by_tags(
    tags: Dict[str, str],
    session: boto3.Session,
    region: Optional[str] = None,
    resource_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Get AWS resources by tags.

    Parameters
    ----------
    tags : Dict[str, str]
        Tags to filter resources by
    session : boto3.Session
        Boto3 session to use
    region : Optional[str], optional
        AWS region, by default None
    resource_type : Optional[str], optional
        Resource type to filter by, by default None

    Returns
    -------
    List[Dict[str, Any]]
        List of resources matching the tags

    Raises
    ------
    AWSConnectionError
        If connection to AWS services fails
    """
    # Convert tags dictionary to AWS filter format
    filters = [{"Name": f"tag:{key}", "Values": [value]} for key, value in tags.items()]

    if resource_type:
        filters.append({"Name": "resource-type", "Values": [resource_type]})

    try:
        ec2 = session.client("ec2", region_name=region)
        response = ec2.describe_tags(Filters=filters)

        # Get unique resource IDs
        resource_ids = list(set(tag["ResourceId"] for tag in response["Tags"]))

        # Get resource details
        resources = []

        if resource_ids:
            if not resource_type or resource_type == "instance":
                try:
                    instance_response = ec2.describe_instances(
                        InstanceIds=[
                            rid for rid in resource_ids if rid.startswith("i-")
                        ]
                    )
                    for reservation in instance_response.get("Reservations", []):
                        resources.extend(reservation.get("Instances", []))
                except ClientError:
                    # Some resource IDs might not be instances
                    pass

            if not resource_type or resource_type == "vpc":
                try:
                    vpc_response = ec2.describe_vpcs(
                        VpcIds=[rid for rid in resource_ids if rid.startswith("vpc-")]
                    )
                    resources.extend(vpc_response.get("Vpcs", []))
                except ClientError:
                    pass

            if not resource_type or resource_type == "subnet":
                try:
                    subnet_response = ec2.describe_subnets(
                        SubnetIds=[
                            rid for rid in resource_ids if rid.startswith("subnet-")
                        ]
                    )
                    resources.extend(subnet_response.get("Subnets", []))
                except ClientError:
                    pass

            if not resource_type or resource_type == "security-group":
                try:
                    sg_response = ec2.describe_security_groups(
                        GroupIds=[rid for rid in resource_ids if rid.startswith("sg-")]
                    )
                    resources.extend(sg_response.get("SecurityGroups", []))
                except ClientError:
                    pass

        return resources

    except Exception as e:
        logger.error(f"Failed to get resources by tags: {e}")
        raise AWSConnectionError(f"Failed to get resources by tags: {e}") from e


def delete_resource(
    resource_id: str,
    session: boto3.Session,
    resource_type: str,
    region: Optional[str] = None,
    force: bool = False,
) -> bool:
    """Delete an AWS resource.

    Parameters
    ----------
    resource_id : str
        Resource ID to delete
    session : boto3.Session
        Boto3 session to use
    resource_type : str
        Type of resource to delete
    region : Optional[str], optional
        AWS region, by default None
    force : bool, optional
        Whether to force deletion even if resource is in use, by default False

    Returns
    -------
    bool
        True if the resource was deleted, False otherwise

    Raises
    ------
    ResourceDeletionError
        If deletion fails
    ResourceNotFoundError
        If the resource is not found
    """
    try:
        if resource_type == "instance":
            ec2 = session.client("ec2", region_name=region)
            ec2.terminate_instances(InstanceIds=[resource_id])
            logger.debug(f"Terminated EC2 instance {resource_id}")
            return True

        elif resource_type == "vpc":
            ec2 = session.client("ec2", region_name=region)

            # Delete all resources within the VPC
            if force:
                # 1. NAT Gateways must be deleted before subnets can be removed
                nat_gws = ec2.describe_nat_gateways(
                    Filters=[
                        {"Name": "vpc-id", "Values": [resource_id]},
                        {
                            "Name": "state",
                            "Values": ["available", "pending", "deleting"],
                        },
                    ]
                ).get("NatGateways", [])
                allocation_ids = [
                    a["AllocationId"]
                    for ngw in nat_gws
                    for a in ngw.get("NatGatewayAddresses", [])
                    if a.get("AllocationId")
                ]
                deleted_nat_gw_ids = []
                for ngw in nat_gws:
                    try:
                        ec2.delete_nat_gateway(NatGatewayId=ngw["NatGatewayId"])
                        deleted_nat_gw_ids.append(ngw["NatGatewayId"])
                        logger.debug(f"Deleting NAT gateway {ngw['NatGatewayId']}")
                    except ClientError as e:
                        logger.warning(
                            f"Could not delete NAT gateway "
                            f"{ngw['NatGatewayId']}: {e}"
                        )
                # Poll until all NAT gateways have finished deleting (max ~2 min).
                # Only enter the loop if we actually submitted deletion requests.
                if deleted_nat_gw_ids:
                    for _ in range(24):
                        still_deleting = ec2.describe_nat_gateways(
                            Filters=[
                                {"Name": "vpc-id", "Values": [resource_id]},
                                {"Name": "state", "Values": ["deleting"]},
                            ]
                        ).get("NatGateways", [])
                        if not still_deleting:
                            break
                        time.sleep(5)

                # 2. Release EIPs that backed the deleted NAT gateways
                for alloc_id in allocation_ids:
                    try:
                        ec2.release_address(AllocationId=alloc_id)
                        logger.debug(f"Released EIP {alloc_id}")
                    except ClientError as e:
                        logger.warning(f"Could not release EIP {alloc_id}: {e}")

                # 3. Delete detached ENIs (e.g. leftover Lambda/ECS interfaces)
                for eni in ec2.describe_network_interfaces(
                    Filters=[{"Name": "vpc-id", "Values": [resource_id]}]
                ).get("NetworkInterfaces", []):
                    if eni.get("Status") == "available":
                        try:
                            ec2.delete_network_interface(
                                NetworkInterfaceId=eni["NetworkInterfaceId"]
                            )
                            logger.debug(f"Deleted ENI {eni['NetworkInterfaceId']}")
                        except ClientError as e:
                            logger.warning(
                                f"Could not delete ENI "
                                f"{eni['NetworkInterfaceId']}: {e}"
                            )

                # Get all subnets in the VPC
                subnets = ec2.describe_subnets(
                    Filters=[{"Name": "vpc-id", "Values": [resource_id]}]
                )
                for subnet in subnets.get("Subnets", []):
                    delete_resource(
                        subnet["SubnetId"], session, "subnet", region, force
                    )

                # Get all security groups in the VPC
                security_groups = ec2.describe_security_groups(
                    Filters=[{"Name": "vpc-id", "Values": [resource_id]}]
                )
                for sg in security_groups.get("SecurityGroups", []):
                    if sg["GroupName"] != "default":  # Can't delete default SG
                        delete_resource(
                            sg["GroupId"], session, "security-group", region, force
                        )

                # Get internet gateways attached to the VPC
                igws = ec2.describe_internet_gateways(
                    Filters=[{"Name": "attachment.vpc-id", "Values": [resource_id]}]
                )
                for igw in igws.get("InternetGateways", []):
                    ec2.detach_internet_gateway(
                        InternetGatewayId=igw["InternetGatewayId"], VpcId=resource_id
                    )
                    delete_resource(
                        igw["InternetGatewayId"],
                        session,
                        "internet-gateway",
                        region,
                        force,
                    )

                # Delete non-main route tables (main RT is deleted with the VPC)
                route_tables = ec2.describe_route_tables(
                    Filters=[{"Name": "vpc-id", "Values": [resource_id]}]
                )
                for rt in route_tables.get("RouteTables", []):
                    is_main = any(
                        assoc.get("Main") for assoc in rt.get("Associations", [])
                    )
                    if is_main:
                        continue
                    # Disassociate all explicit associations first
                    for assoc in rt.get("Associations", []):
                        assoc_id = assoc.get("RouteTableAssociationId")
                        if assoc_id:
                            try:
                                ec2.disassociate_route_table(AssociationId=assoc_id)
                            except ClientError:
                                pass
                    try:
                        ec2.delete_route_table(RouteTableId=rt["RouteTableId"])
                        logger.debug(f"Deleted route table {rt['RouteTableId']}")
                    except ClientError as e:
                        logger.warning(
                            f"Could not delete route table "
                            f"{rt['RouteTableId']}: {e}"
                        )

            # Delete the VPC
            ec2.delete_vpc(VpcId=resource_id)
            logger.debug(f"Deleted VPC {resource_id}")
            return True

        elif resource_type == "subnet":
            ec2 = session.client("ec2", region_name=region)
            ec2.delete_subnet(SubnetId=resource_id)
            logger.debug(f"Deleted subnet {resource_id}")
            return True

        elif resource_type == "security-group":
            ec2 = session.client("ec2", region_name=region)
            ec2.delete_security_group(GroupId=resource_id)
            logger.debug(f"Deleted security group {resource_id}")
            return True

        elif resource_type == "internet-gateway":
            ec2 = session.client("ec2", region_name=region)
            ec2.delete_internet_gateway(InternetGatewayId=resource_id)
            logger.debug(f"Deleted internet gateway {resource_id}")
            return True

        elif resource_type == "function":
            lambda_client = session.client("lambda", region_name=region)
            lambda_client.delete_function(FunctionName=resource_id)
            logger.debug(f"Deleted Lambda function {resource_id}")
            return True

        elif resource_type == "task":
            ecs = session.client("ecs", region_name=region)
            ecs.stop_task(task=resource_id, cluster="default")
            logger.debug(f"Stopped ECS task {resource_id}")
            return True

        elif resource_type == "cloudformation-stack":
            cfn = session.client("cloudformation", region_name=region)
            cfn.delete_stack(StackName=resource_id)
            # Wait for stack deletion
            logger.debug(f"Initiated deletion of CloudFormation stack {resource_id}")
            logger.debug(f"Waiting for stack {resource_id} to be deleted...")
            waiter = cfn.get_waiter("stack_delete_complete")
            waiter.wait(
                StackName=resource_id, WaiterConfig={"Delay": 10, "MaxAttempts": 30}
            )
            logger.debug(f"Deleted CloudFormation stack {resource_id}")
            return True

        else:
            logger.warning(f"Unsupported resource type: {resource_type}")
            return False

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")

        if any(
            code in error_code
            for code in [
                "NotFound",
                "InvalidSubnetID.NotFound",
                "InvalidVpcID.NotFound",
                "InvalidGroup.NotFound",
                "InvalidInternetGatewayID.NotFound",
                "ResourceNotFoundException",
            ]
        ):
            logger.debug(f"Resource {resource_id} not found or already deleted")
            raise ResourceNotFoundError(
                f"Resource {resource_id} not found or already deleted"
            ) from e
        else:
            logger.error(f"Failed to delete {resource_type} {resource_id}: {e}")
            raise ResourceDeletionError(
                f"Failed to delete {resource_type} {resource_id}: {e}"
            ) from e

    except Exception as e:
        logger.error(f"Failed to delete {resource_type} {resource_id}: {e}")
        raise ResourceDeletionError(
            f"Failed to delete {resource_type} {resource_id}: {e}"
        ) from e


def get_cf_template(template_name: str) -> str:
    """
    Load a CloudFormation template from the templates directory.

    Parameters
    ----------
    template_name : str
        Name of the template file (e.g., 'bastion.yml')

    Returns
    -------
    str
        CloudFormation template content

    Raises
    ------
    FileNotFoundError
        If template file is not found
    """
    import os
    import pkg_resources

    try:
        # Try to load from package resources
        template_path = f"templates/cloudformation/{template_name}"
        template_content = pkg_resources.resource_string(
            "parsl_ephemeral_aws", template_path
        ).decode("utf-8")
        return template_content
    except (FileNotFoundError, ModuleNotFoundError):
        # Fallback to file system
        current_dir = os.path.dirname(os.path.abspath(__file__))
        template_path = os.path.join(
            current_dir, "..", "templates", "cloudformation", template_name
        )

        if os.path.exists(template_path):
            with open(template_path, "r") as f:
                return f.read()
        else:
            # Return a basic template as fallback
            logger.warning(f"Template {template_name} not found, using basic template")
            return """
AWSTemplateFormatVersion: '2010-09-09'
Description: 'Basic template placeholder'
Resources:
  PlaceholderResource:
    Type: AWS::CloudFormation::WaitConditionHandle
"""


def get_or_create_iam_role(
    iam_client: Any,
    role_name: str,
    assume_role_policy: Dict[str, Any],
    policy_arns: List[str],
    tags: Optional[List[Dict[str, str]]] = None,
    description: str = "",
) -> str:
    """Get or create an IAM role idempotently.

    Checks whether the role already exists and returns its ARN without
    modifying it.  If the role does not exist, creates it, attaches the
    supplied managed-policy ARNs, and returns the new ARN.

    Parameters
    ----------
    iam_client : Any
        A boto3 IAM client.
    role_name : str
        Name of the IAM role.
    assume_role_policy : Dict[str, Any]
        Trust-relationship policy document (Python dict, not JSON string).
    policy_arns : List[str]
        Managed policy ARNs to attach.
    tags : Optional[List[Dict[str, str]]], optional
        Tags to apply when creating the role.
    description : str, optional
        Role description.

    Returns
    -------
    str
        ARN of the existing or newly created role.

    Raises
    ------
    ResourceCreationError
        If role creation or retrieval fails.
    """
    try:
        response = iam_client.get_role(RoleName=role_name)
        logger.debug(f"Reusing existing IAM role: {role_name}")
        return response["Role"]["Arn"]
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("NoSuchEntity", "NoSuchEntityException"):
            raise ResourceCreationError(
                f"Failed to check IAM role {role_name}: {e}"
            ) from e

    # Role does not exist — create it
    try:
        create_kwargs: Dict[str, Any] = {
            "RoleName": role_name,
            "AssumeRolePolicyDocument": json.dumps(assume_role_policy),
            "Description": description,
        }
        if tags:
            create_kwargs["Tags"] = tags

        response = iam_client.create_role(**create_kwargs)
        role_arn: str = response["Role"]["Arn"]

        for policy_arn in policy_arns:
            iam_client.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)

        logger.info(f"Created IAM role: {role_name}")
        return role_arn

    except ClientError as e:
        if e.response["Error"]["Code"] in ("EntityAlreadyExists",):
            # Race condition — another process created it; fetch ARN
            try:
                response = iam_client.get_role(RoleName=role_name)
                logger.debug(f"IAM role created by concurrent caller: {role_name}")
                return response["Role"]["Arn"]
            except Exception as inner_e:
                raise ResourceCreationError(
                    f"Failed to retrieve IAM role {role_name} after concurrent creation: {inner_e}"
                ) from inner_e
        raise ResourceCreationError(
            f"Failed to create IAM role {role_name}: {e}"
        ) from e

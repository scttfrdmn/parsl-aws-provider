"""Utilities for cleaning up Spot Fleet resources.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import logging
import time
from typing import Optional, List, Dict, Any

import boto3
from botocore.exceptions import ClientError

from parsl_ephemeral_aws.exceptions import ResourceDeletionError

logger = logging.getLogger(__name__)


def cleanup_spot_fleet_role(
    session: boto3.Session,
    role_name: str,
    wait_for_detachment: bool = True,
    max_attempts: int = 3,
    delay_seconds: int = 5
) -> bool:
    """Clean up IAM role created for Spot Fleet requests.

    This function handles detaching policies and deleting the IAM role created
    for Spot Fleet requests. It includes retry logic to handle potential
    eventual consistency issues in the IAM service.

    Parameters
    ----------
    session : boto3.Session
        AWS session to use for API calls
    role_name : str
        Name of the IAM role to delete
    wait_for_detachment : bool, optional
        Whether to wait for policy detachment to complete, by default True
    max_attempts : int, optional
        Maximum number of attempts for cleanup, by default 3
    delay_seconds : int, optional
        Delay between cleanup attempts in seconds, by default 5

    Returns
    -------
    bool
        True if role was successfully deleted, False otherwise
    """
    iam = session.client('iam')
    
    for attempt in range(max_attempts):
        try:
            # Get the role to ensure it exists
            try:
                role = iam.get_role(RoleName=role_name)
            except ClientError as e:
                if e.response['Error']['Code'] == 'NoSuchEntity':
                    logger.debug(f"Role {role_name} not found, nothing to clean up")
                    return True
                raise
            
            # Get attached policies
            attached_policies = iam.list_attached_role_policies(RoleName=role_name)
            
            # Detach all policies
            for policy in attached_policies.get('AttachedPolicies', []):
                policy_arn = policy['PolicyArn']
                logger.debug(f"Detaching policy {policy_arn} from role {role_name}")
                iam.detach_role_policy(
                    RoleName=role_name,
                    PolicyArn=policy_arn
                )
            
            # Wait for policies to detach (if requested)
            if wait_for_detachment and attached_policies.get('AttachedPolicies'):
                logger.debug(f"Waiting for policies to detach from role {role_name}")
                time.sleep(delay_seconds)
            
            # Delete the role
            logger.debug(f"Deleting IAM role {role_name}")
            iam.delete_role(RoleName=role_name)
            
            logger.info(f"Successfully deleted Spot Fleet IAM role {role_name}")
            return True
            
        except ClientError as e:
            if e.response['Error']['Code'] in [
                'DeleteConflict',  # Role is still being used
                'ResourceInUseException',  # Role has resources attached
            ]:
                if attempt < max_attempts - 1:
                    logger.warning(
                        f"Could not delete role {role_name} on attempt {attempt+1}, "
                        f"retrying in {delay_seconds} seconds..."
                    )
                    time.sleep(delay_seconds)
                    continue
                else:
                    logger.warning(
                        f"Could not delete role {role_name} after {max_attempts} attempts. "
                        "The role might still be in use by active Spot Fleet requests."
                    )
                    return False
            else:
                logger.error(f"Error deleting IAM role {role_name}: {e}")
                return False
        except Exception as e:
            logger.error(f"Unexpected error deleting IAM role {role_name}: {e}")
            return False
    
    return False


def cleanup_all_spot_fleet_resources(
    session: boto3.Session,
    workflow_id: str,
    cancel_active_requests: bool = True,
    cleanup_iam_roles: bool = True
) -> Dict[str, Any]:
    """Clean up all Spot Fleet resources associated with a workflow.

    This function handles cleaning up:
    1. Active Spot Fleet requests
    2. Running instances from these requests
    3. IAM roles created for the Spot Fleet requests

    Parameters
    ----------
    session : boto3.Session
        AWS session to use for API calls
    workflow_id : str
        Workflow ID used to identify resources to clean up
    cancel_active_requests : bool, optional
        Whether to cancel active Spot Fleet requests, by default True
    cleanup_iam_roles : bool, optional
        Whether to clean up IAM roles, by default True

    Returns
    -------
    Dict[str, Any]
        Dictionary with cleanup results:
        - cancelled_requests: List of cancelled requests
        - cleaned_roles: List of cleaned IAM roles
        - errors: List of errors encountered
    """
    result = {
        "cancelled_requests": [],
        "cleaned_roles": [],
        "errors": []
    }
    
    ec2 = session.client('ec2')
    iam = session.client('iam')
    
    # Find Spot Fleet requests for this workflow
    try:
        # Get all Spot Fleet requests
        fleet_requests = []
        paginator = ec2.get_paginator('describe_spot_fleet_requests')
        
        for page in paginator.paginate():
            for config in page.get('SpotFleetRequestConfigs', []):
                # Check tags for workflow ID
                request_id = config['SpotFleetRequestId']
                
                # Get request tags
                try:
                    tags_response = ec2.describe_tags(
                        Filters=[
                            {
                                'Name': 'resource-id',
                                'Values': [request_id]
                            }
                        ]
                    )
                    
                    # Check if this request belongs to our workflow
                    for tag in tags_response.get('Tags', []):
                        if (tag['Key'] == 'WorkflowId' or tag['Key'] == 'ParslWorkflowId') and tag['Value'] == workflow_id:
                            fleet_requests.append(request_id)
                            break
                except ClientError as e:
                    logger.warning(f"Error checking tags for Spot Fleet request {request_id}: {e}")
        
        # Cancel active requests
        if cancel_active_requests and fleet_requests:
            logger.info(f"Cancelling {len(fleet_requests)} Spot Fleet requests")
            try:
                response = ec2.cancel_spot_fleet_requests(
                    SpotFleetRequestIds=fleet_requests,
                    TerminateInstances=True
                )
                
                for success in response.get('SuccessfulFleetRequests', []):
                    result["cancelled_requests"].append(success['SpotFleetRequestId'])
                
                for failure in response.get('UnsuccessfulFleetRequests', []):
                    error = {
                        "resource_id": failure['SpotFleetRequestId'],
                        "error": failure.get('Error', {}).get('Message', 'Unknown error')
                    }
                    result["errors"].append(error)
            
            except Exception as e:
                logger.error(f"Error cancelling Spot Fleet requests: {e}")
                result["errors"].append({
                    "operation": "cancel_spot_fleet_requests",
                    "error": str(e)
                })
    except Exception as e:
        logger.error(f"Error finding Spot Fleet requests: {e}")
        result["errors"].append({
            "operation": "find_spot_fleet_requests",
            "error": str(e)
        })
    
    # Clean up IAM roles
    if cleanup_iam_roles:
        try:
            # Get roles with name pattern for this workflow
            role_prefix = f"parsl-aws-spot-fleet-role-{workflow_id[:8]}"
            
            roles_to_clean = []
            paginator = iam.get_paginator('list_roles')
            
            for page in paginator.paginate():
                for role in page['Roles']:
                    if role['RoleName'].startswith(role_prefix):
                        roles_to_clean.append(role['RoleName'])
            
            # Clean up each role
            for role_name in roles_to_clean:
                logger.info(f"Cleaning up Spot Fleet IAM role {role_name}")
                if cleanup_spot_fleet_role(session, role_name):
                    result["cleaned_roles"].append(role_name)
                else:
                    result["errors"].append({
                        "resource_id": role_name,
                        "operation": "cleanup_spot_fleet_role",
                        "error": "Failed to clean up role"
                    })
        except Exception as e:
            logger.error(f"Error cleaning up Spot Fleet IAM roles: {e}")
            result["errors"].append({
                "operation": "cleanup_iam_roles",
                "error": str(e)
            })
    
    return result
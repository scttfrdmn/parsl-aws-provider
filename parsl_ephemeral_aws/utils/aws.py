"""AWS utility functions for Parsl Ephemeral AWS Provider.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import logging
import time
from typing import Dict, Any, Optional, List, Tuple

import boto3
import botocore.exceptions

from ..exceptions import ConfigurationError


logger = logging.getLogger(__name__)


def create_aws_session(
    region: Optional[str] = None,
    aws_access_key_id: Optional[str] = None,
    aws_secret_access_key: Optional[str] = None,
    aws_session_token: Optional[str] = None,
    profile_name: Optional[str] = None,
    retry_attempts: int = 5
) -> boto3.Session:
    """Create an AWS session with proper credentials and configuration.
    
    Parameters
    ----------
    region : Optional[str], optional
        AWS region to use, by default None (uses default from config/profile)
    aws_access_key_id : Optional[str], optional
        AWS access key ID, by default None (uses environment or profile)
    aws_secret_access_key : Optional[str], optional
        AWS secret access key, by default None (uses environment or profile)
    aws_session_token : Optional[str], optional
        AWS session token, by default None (uses environment or profile)
    profile_name : Optional[str], optional
        AWS profile name, by default None (uses default profile)
    retry_attempts : int, optional
        Number of retry attempts for AWS API calls, by default 5
        
    Returns
    -------
    boto3.Session
        Configured AWS session
    
    Raises
    ------
    ConfigurationError
        If AWS credentials could not be found or configured
    """
    try:
        # Create session with provided credentials
        session_kwargs = {}
        
        if region is not None:
            session_kwargs['region_name'] = region
            
        if aws_access_key_id is not None and aws_secret_access_key is not None:
            session_kwargs['aws_access_key_id'] = aws_access_key_id
            session_kwargs['aws_secret_access_key'] = aws_secret_access_key
            
            if aws_session_token is not None:
                session_kwargs['aws_session_token'] = aws_session_token
                
        if profile_name is not None:
            session_kwargs['profile_name'] = profile_name
            
        session = boto3.Session(**session_kwargs)
        
        # Configure retry behavior
        config = botocore.config.Config(
            retries={
                'max_attempts': retry_attempts,
                'mode': 'standard'
            }
        )
        
        # Test the session by creating an EC2 client and making a simple API call
        ec2_client = session.client('ec2', config=config)
        ec2_client.describe_regions(DryRun=False)
        
        logger.debug(f"Successfully created AWS session with region: {session.region_name}")
        return session
        
    except botocore.exceptions.NoCredentialsError:
        error_msg = (
            "AWS credentials not found. Please provide credentials via environment variables, "
            "AWS config file, IAM instance profile, or session parameters."
        )
        logger.error(error_msg)
        raise ConfigurationError(error_msg)
        
    except botocore.exceptions.ClientError as e:
        error_code = e.response['Error']['Code']
        error_msg = e.response['Error']['Message']
        
        if error_code == 'AuthFailure':
            logger.error(f"AWS authentication failed: {error_msg}")
            raise ConfigurationError(f"AWS authentication failed: {error_msg}")
        elif error_code == 'UnauthorizedOperation':
            logger.error(f"Insufficient permissions for AWS operations: {error_msg}")
            raise ConfigurationError(f"Insufficient permissions for AWS operations: {error_msg}")
        else:
            logger.error(f"AWS session creation failed: {error_code} - {error_msg}")
            raise ConfigurationError(f"AWS session creation failed: {error_code} - {error_msg}")
        
    except Exception as e:
        logger.error(f"Unexpected error creating AWS session: {e}")
        raise ConfigurationError(f"Unexpected error creating AWS session: {e}")


def get_aws_account_id(session: boto3.Session) -> str:
    """Get the AWS account ID for the current session.
    
    Parameters
    ----------
    session : boto3.Session
        AWS session
        
    Returns
    -------
    str
        AWS account ID
    """
    try:
        client = session.client('sts')
        return client.get_caller_identity()['Account']
    except Exception as e:
        logger.error(f"Error getting AWS account ID: {e}")
        raise


def wait_for_aws_resource(
    check_func,
    resource_id: str,
    target_states: List[str],
    failure_states: List[str],
    description: str = "resource",
    timeout: int = 300,
    interval: int = 5
) -> Tuple[bool, str]:
    """Wait for an AWS resource to reach a desired state.
    
    Parameters
    ----------
    check_func : callable
        Function that takes a resource ID and returns its current state
    resource_id : str
        ID of the resource to check
    target_states : List[str]
        List of states that indicate success
    failure_states : List[str]
        List of states that indicate failure
    description : str, optional
        Description of the resource, by default "resource"
    timeout : int, optional
        Maximum time to wait in seconds, by default 300
    interval : int, optional
        Time between checks in seconds, by default 5
        
    Returns
    -------
    Tuple[bool, str]
        (True, current_state) if resource reached a target state,
        (False, current_state) if resource reached a failure state or timed out
    """
    start_time = time.time()
    end_time = start_time + timeout
    
    logger.debug(f"Waiting for {description} {resource_id} to reach one of {target_states}")
    
    while time.time() < end_time:
        try:
            current_state = check_func(resource_id)
            
            if current_state in target_states:
                logger.debug(f"{description.capitalize()} {resource_id} reached target state: {current_state}")
                return True, current_state
            
            if current_state in failure_states:
                logger.warning(f"{description.capitalize()} {resource_id} reached failure state: {current_state}")
                return False, current_state
            
            logger.debug(f"{description.capitalize()} {resource_id} current state: {current_state}, waiting...")
            time.sleep(interval)
            
        except Exception as e:
            logger.error(f"Error checking {description} {resource_id} state: {e}")
            time.sleep(interval)
    
    logger.warning(f"Timeout waiting for {description} {resource_id} to reach one of {target_states}")
    return False, "timeout"


def get_default_tags(provider: Any) -> List[Dict[str, str]]:
    """Get default tags for AWS resources.
    
    Parameters
    ----------
    provider : EphemeralAWSProvider
        Provider instance
        
    Returns
    -------
    List[Dict[str, str]]
        List of tag dictionaries
    """
    tags = [
        {'Key': 'ParslManagedResource', 'Value': 'true'},
        {'Key': 'ParslWorkflowId', 'Value': provider.workflow_id},
    ]
    
    # Add provider tags
    if provider.tags:
        for key, value in provider.tags.items():
            tags.append({'Key': key, 'Value': value})
            
    return tags


def get_available_regions() -> List[str]:
    """Get available AWS regions.
    
    Returns
    -------
    List[str]
        List of region names
    """
    try:
        ec2_client = boto3.client('ec2')
        regions = [region['RegionName'] for region in ec2_client.describe_regions()['Regions']]
        return regions
    except Exception as e:
        logger.error(f"Error getting available AWS regions: {e}")
        return []


def get_instance_type_offerings(session: boto3.Session, region: str) -> List[str]:
    """Get available instance types in a region.
    
    Parameters
    ----------
    session : boto3.Session
        AWS session
    region : str
        AWS region
        
    Returns
    -------
    List[str]
        List of available instance types
    """
    try:
        ec2_client = session.client('ec2', region_name=region)
        paginator = ec2_client.get_paginator('describe_instance_type_offerings')
        instance_types = []
        
        for page in paginator.paginate(LocationType='region', Filters=[{'Name': 'location', 'Values': [region]}]):
            for offering in page['InstanceTypeOfferings']:
                instance_types.append(offering['InstanceType'])
                
        return sorted(instance_types)
    except Exception as e:
        logger.error(f"Error getting instance type offerings for region {region}: {e}")
        return []
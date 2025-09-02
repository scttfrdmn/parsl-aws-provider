#!/usr/bin/env python3
"""Debug what's happening on the live AWS instance."""

import logging
import boto3
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def debug_live_instance():
    """Check what's running on our AWS instance."""
    
    # Get running instances
    ec2 = boto3.client('ec2', region_name='us-east-1')
    
    response = ec2.describe_instances(
        Filters=[
            {'Name': 'instance-state-name', 'Values': ['running']},
            {'Name': 'tag:parsl-provider', 'Values': ['*']}
        ]
    )
    
    instances = []
    for reservation in response['Reservations']:
        for instance in reservation['Instances']:
            instances.append(instance['InstanceId'])
    
    if not instances:
        logger.error("No running instances found")
        return
    
    instance_id = instances[0]
    logger.info(f"Found running instance: {instance_id}")
    
    # Check what processes are running
    ssm = boto3.client('ssm', region_name='us-east-1')
    
    commands = [
        "ps aux | grep python",
        "docker ps",
        "docker images | grep parsl",
        "ls -la /tmp/parsl*"
    ]
    
    for cmd in commands:
        logger.info(f"Running: {cmd}")
        try:
            response = ssm.send_command(
                InstanceIds=[instance_id],
                DocumentName='AWS-RunShellScript',
                Parameters={'commands': [cmd]}
            )
            
            command_id = response['Command']['CommandId']
            time.sleep(2)
            
            output = ssm.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id
            )
            
            print(f"STDOUT:\n{output['StandardOutputContent']}")
            if output['StandardErrorContent']:
                print(f"STDERR:\n{output['StandardErrorContent']}")
            print("-" * 50)
            
        except Exception as e:
            logger.error(f"Command failed: {e}")

if __name__ == "__main__":
    debug_live_instance()
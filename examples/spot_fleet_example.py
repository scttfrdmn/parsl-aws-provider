#!/usr/bin/env python3
"""Example showing how to use the EphemeralAWSProvider with Spot Fleet.

This example demonstrates how to configure Parsl to use the EphemeralAWSProvider
with EC2 Spot Fleet for more reliable and cost-effective spot instance usage.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import os
import parsl
from parsl.app.python import python_app
from parsl.config import Config
from parsl.providers import LocalProvider
from parsl.channels import SSHChannel
from parsl.launchers import SrunLauncher
from parsl.executors import HighThroughputExecutor

# Import the EphemeralAWSProvider (assuming it's installed)
from parsl_ephemeral_aws import EphemeralAWSProvider
from parsl_ephemeral_aws.constants import OPERATING_MODE_STANDARD


@python_app
def hello(name):
    """Simple app that returns a greeting message."""
    import platform
    return f"Hello {name} from {platform.node()} with hostname {os.uname().nodename}"


if __name__ == "__main__":
    # Set your AWS credentials as environment variables or use AWS profiles
    # os.environ['AWS_ACCESS_KEY_ID'] = 'your_access_key'
    # os.environ['AWS_SECRET_ACCESS_KEY'] = 'your_secret_key'
    # os.environ['AWS_REGION'] = 'us-east-1'
    
    # EphemeralAWSProvider configuration
    provider = EphemeralAWSProvider(
        # Basic AWS configuration
        image_id="ami-0123456789abcdef0",  # Replace with an appropriate AMI ID
        instance_type="c5.large",
        region="us-east-1",
        key_name="your-key-pair",  # Replace with your key pair name
        
        # Spot Fleet specific configuration
        use_spot=True,
        spot_max_price_percentage=100,  # Maximum 100% of on-demand price
        use_spot_fleet=True,  # Use Spot Fleet instead of individual spot requests
        
        # Instance types to consider in the fleet (alternatives with similar performance)
        instance_types=["c5.large", "c5d.large", "m5.large", "r5.large"],
        
        # Block configuration
        nodes_per_block=2,
        min_blocks=0,
        max_blocks=1,
        init_blocks=0,
        
        # Network configuration (optional - creates new VPC if not specified)
        # vpc_id="vpc-12345678",
        # subnet_id="subnet-12345678",
        # security_group_id="sg-12345678",
        use_public_ips=True,
        
        # Worker initialization
        worker_init="""
        # Set up worker environment
        sudo apt-get update -y
        sudo apt-get install -y python3-pip
        pip3 install --user parsl
        """,
        
        # Operational mode
        operating_mode=OPERATING_MODE_STANDARD,
        
        # Tagging
        tags={"Project": "ParslSpotFleetExample"},
        
        # Walltime for block (optional)
        walltime="01:00:00"
    )
    
    # Parsl configuration
    config = Config(
        executors=[
            HighThroughputExecutor(
                label="aws_executor",
                provider=provider,
                # Optional: Configure SSH channel for worker communication
                # channel=SSHChannel(
                #     hostname='<PUBLIC_IP>',  # Will be filled dynamically
                #     username='ubuntu',       # AMI-dependent
                #     key_filename='~/.ssh/your-key.pem'  # Path to your private key
                # ),
                # max_workers_per_node=2  # Number of workers per node
            )
        ]
    )
    
    # Load Parsl configuration
    parsl.load(config)
    
    try:
        # Submit applications
        future = hello("Parsl on Spot Fleet")
        
        # Wait for completion and print result
        print(f"Result: {future.result()}")
        
    finally:
        # Cleanup (important to avoid orphaned AWS resources)
        parsl.clear()
        
        # Optional: Force cleanup if needed
        # provider.cleanup_all_resources()
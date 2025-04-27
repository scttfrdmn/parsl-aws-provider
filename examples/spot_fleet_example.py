"""Example of using Spot Fleet with Parsl AWS Provider.

This example demonstrates how to configure and use Spot Fleet with the Parsl AWS Provider,
which provides improved reliability and cost-effectiveness for spot instance provisioning.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import parsl
from parsl.app.app import python_app
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
from parsl.providers import EphemeralAWSProvider
from parsl.addresses import address_by_hostname

# Import StateStore implementation from the provider package
from parsl_ephemeral_aws.state import S3StateStore


@python_app
def hello(name):
    """Simple app that returns a hello message."""
    import platform
    import socket
    return f"Hello, {name}! Running on {platform.node()} ({socket.gethostbyname(socket.gethostname())})"


if __name__ == "__main__":
    # Configure a SpotFleet-enabled provider
    provider = EphemeralAWSProvider(
        # Basic AWS configuration
        region="us-east-1",  # Specify your preferred region
        
        # Operating mode
        operating_mode="detached",  # Detached mode is required for spot fleet to work well
        preserve_bastion=True,  # Keep the bastion host alive across runs
        workflow_id="spot-fleet-demo",  # Unique identifier for this workflow
        
        # Spot Fleet configuration
        use_spot_fleet=True,  # Enable Spot Fleet (instead of individual spot instances)
        instance_types=[  # Multiple instance types for better availability
            "t3.small", 
            "t3.medium", 
            "m5.small", 
            "m5a.small"
        ],
        nodes_per_block=2,  # Launch 2 instances per block
        spot_max_price_percentage=80,  # Maximum 80% of on-demand price
        
        # Standard configuration
        instance_type="t3.small",  # Primary instance type (used as default if needed)
        min_blocks=0,
        max_blocks=4,
        init_blocks=1,
        use_spot=True,  # Must be True to use spot fleet
        
        # State tracking (required for detached mode)
        state_store=S3StateStore(
            bucket_name="your-state-bucket",  # Replace with your S3 bucket
            key_prefix="parsl-states",
            auto_create_bucket=True,
        ),
        
        # AWS tags
        tags={
            "Project": "ParslSpotFleetDemo",
            "Department": "Research"
        }
    )

    # Create a HighThroughputExecutor with the provider
    executor = HighThroughputExecutor(
        label="spot_fleet_executor",
        address=address_by_hostname(),
        provider=provider,
    )

    # Create Parsl configuration
    config = Config(
        executors=[executor],
        strategy=None,
    )

    # Load the configuration
    parsl.load(config)

    # Submit a bunch of hello apps
    futures = []
    for i in range(10):
        futures.append(hello(f"Task {i}"))

    # Wait for results and print them
    for future in futures:
        try:
            result = future.result()
            print(result)
        except Exception as e:
            print(f"Task failed: {e}")

    # Cleanup
    print("Waiting for AWS resources to be cleaned up...")
    parsl.dfk().cleanup()
    print("Done!")
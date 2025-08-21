#!/usr/bin/env python3
"""
Example of using the EphemeralAWSProvider with ServerlessMode and SpotFleet.

This example demonstrates how to configure and use the Parsl EphemeralAWSProvider
in serverless mode with SpotFleet for reliable, cost-effective EC2 compute resources.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import parsl
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
from parsl_ephemeral_aws import EphemeralAWSProvider


if __name__ == "__main__":
    # Configure the ephemeral AWS provider with serverless mode and SpotFleet
    provider = EphemeralAWSProvider(
        # Region is required
        region="us-west-2",
        # Set the mode to serverless and worker type to ECS
        # (required for SpotFleet in ServerlessMode)
        mode="serverless",
        worker_type="ecs",
        # Enable SpotFleet for more reliable spot instance management
        use_spot_fleet=True,
        # Configure multiple instance types for better availability and pricing
        instance_types=[
            "t3.small",  # Less expensive, general purpose
            "t3a.small",  # AMD-based alternative
            "t3.medium",  # More memory and CPU
            "t3a.medium",  # AMD alternative with more resources
            "m5.large",  # Even more resources for compute-intensive tasks
            "c5.large",  # Compute-optimized for CPU-bound tasks
        ],
        # Set the number of nodes per block
        nodes_per_block=2,
        # Set maximum spot price as percentage of on-demand price
        spot_max_price_percentage=80,  # Pay up to 80% of on-demand price
        # Network settings
        use_public_ips=True,  # Use public IPs for instances
        # State persistence
        state_store="parameter_store",
        state_prefix="/parsl/workflows",
        # Additional tags for resources
        additional_tags={
            "Project": "ParslDemo",
            "Environment": "Dev",
            "Example": "ServerlessSpotFleet",
        },
    )

    # Configure a Parsl executor with the provider
    executor = HighThroughputExecutor(
        label="aws_serverless_executor",
        max_workers=4,  # Maximum workers per node
        provider=provider,
    )

    # Create Parsl configuration
    config = Config(
        executors=[executor],
        run_dir="runinfo",
    )

    # Load Parsl configuration
    parsl.load(config)

    # Define a sample computation app
    @parsl.python_app
    def compute_intensive(duration=30):
        """A compute-intensive app that runs for the specified duration."""
        import time
        import numpy as np
        import os
        import platform

        # Get system information
        hostname = platform.node()
        cpu_info = platform.processor()
        system = platform.system()

        # Get environment info
        aws_region = os.environ.get("AWS_REGION", "Unknown")
        instance_id = os.environ.get("EC2_INSTANCE_ID", "Unknown")
        instance_type = os.environ.get("EC2_INSTANCE_TYPE", "Unknown")

        # Do some compute-intensive work
        start_time = time.time()
        result = 0

        # Matrix operations
        size = 1000
        for _ in range(duration):
            # Create random matrices
            a = np.random.rand(size, size)
            b = np.random.rand(size, size)

            # Matrix multiplication
            c = np.dot(a, b)

            # Sum the result
            result += np.sum(c)

            # Sleep a bit to avoid 100% CPU usage
            time.sleep(0.1)

        end_time = time.time()
        compute_time = end_time - start_time

        # Return job information along with result
        return {
            "hostname": hostname,
            "system": system,
            "cpu_info": cpu_info,
            "aws_region": aws_region,
            "instance_id": instance_id,
            "instance_type": instance_type,
            "compute_time": compute_time,
            "result_checksum": result,
        }

    # Submit multiple jobs
    print("Submitting jobs to run on AWS SpotFleet instances via ServerlessMode...")
    results = []

    num_jobs = 8
    for i in range(num_jobs):
        # Each job will run for a different duration
        duration = 20 + (i * 5)  # 20, 25, 30, 35, 40, 45, 50, 55 seconds
        print(f"  - Submitting job {i+1}/{num_jobs} with duration {duration} seconds")
        results.append(compute_intensive(duration=duration))

    # Wait for and process results
    print(f"\nWaiting for {num_jobs} jobs to complete...\n")

    for i, future in enumerate(results):
        print(f"Waiting for job {i+1}...")
        result = future.result()

        print(f"\nResults from job {i+1}:")
        print(f"  - Host: {result['hostname']}")
        print(f"  - System: {result['system']}")
        print(f"  - CPU: {result['cpu_info']}")
        print(f"  - AWS Region: {result['aws_region']}")
        print(f"  - Instance ID: {result['instance_id']}")
        print(f"  - Instance Type: {result['instance_type']}")
        print(f"  - Compute Time: {result['compute_time']:.2f} seconds")
        print(f"  - Result Checksum: {result['result_checksum']:.6e}")
        print()

    print("\nAll jobs completed successfully!")

    # Clean up resources
    print("\nCleaning up resources...")
    parsl.clear()

    print("\nDone. All AWS resources have been cleaned up.")

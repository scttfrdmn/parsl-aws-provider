#!/usr/bin/env python3
"""
Example of using the EphemeralAWSProvider with spot interruption handling.

This example demonstrates how to configure and use the Parsl EphemeralAWSProvider
with spot interruption handling for resilient task execution.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import parsl
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
from parsl_ephemeral_aws import EphemeralAWSProvider
from parsl_ephemeral_aws.compute.spot_interruption import checkpointable
import os
import logging
import boto3


# Setup logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)


# Example of a checkpointable task function
@checkpointable(checkpoint_bucket="parsl-spot-checkpoint-example")
def long_compute_task(iterations=100, checkpoint_data=None):
    """
    A long-running compute task that supports checkpointing.

    This function simulates a computation that can be interrupted
    and later resumed from a checkpoint.

    Parameters
    ----------
    iterations : int
        Number of iterations to perform
    checkpoint_data : dict, optional
        Previously saved checkpoint data

    Returns
    -------
    dict
        Result data containing computation results and metadata
    """
    import os
    import socket
    import numpy as np

    # Get environment information
    hostname = socket.gethostname()
    instance_id = os.environ.get("EC2_INSTANCE_ID", "unknown")

    # Initialize state from checkpoint or create new state
    if checkpoint_data:
        state = checkpoint_data
        print(f"Resuming from checkpoint at iteration {state['current_iteration']}")
    else:
        state = {
            "current_iteration": 0,
            "result": 0,
            "start_time": time.time(),
            "checkpoints": [],
            "hostname_history": [hostname],
            "instance_history": [instance_id],
        }
        print(f"Starting new computation on {hostname} ({instance_id})")

    # If we're resuming on a different host, record it
    if hostname not in state["hostname_history"]:
        state["hostname_history"].append(hostname)
        state["instance_history"].append(instance_id)

    # Perform computation with periodic checkpoints
    for i in range(state["current_iteration"], iterations):
        # Simulate compute-intensive work
        matrix_size = 1000
        a = np.random.rand(matrix_size, matrix_size)
        b = np.random.rand(matrix_size, matrix_size)
        c = np.dot(a, b)

        # Update state
        state["result"] += np.sum(c)
        state["current_iteration"] = i + 1

        # Checkpoint every 10 iterations
        if (i + 1) % 10 == 0:
            checkpoint_time = time.time()
            state["checkpoints"].append({"iteration": i + 1, "time": checkpoint_time})
            print(f"Checkpoint at iteration {i+1}/{iterations} on {hostname}")

            # Yield state for checkpointing
            yield state

        # Small delay for demo purposes
        time.sleep(0.1)

    # Compute final statistics
    end_time = time.time()
    total_time = end_time - state["start_time"]
    checkpoint_count = len(state["checkpoints"])

    # Final result
    result = {
        "result": state["result"],
        "iterations": iterations,
        "total_time": total_time,
        "checkpoint_count": checkpoint_count,
        "hostname_history": state["hostname_history"],
        "instance_history": state["instance_history"],
        "recovered": len(state["hostname_history"]) > 1,
    }

    print(f"Completed computation on {hostname} after {total_time:.2f} seconds")
    return result


if __name__ == "__main__":
    # Create S3 bucket for checkpoints if it doesn't exist
    bucket_name = "parsl-spot-checkpoint-example"
    region = os.environ.get("AWS_REGION", "us-east-1")

    s3_client = boto3.client("s3", region_name=region)
    try:
        s3_client.head_bucket(Bucket=bucket_name)
        print(f"Using existing checkpoint bucket: {bucket_name}")
    except:
        print(f"Creating checkpoint bucket: {bucket_name}")
        if region == "us-east-1":
            s3_client.create_bucket(Bucket=bucket_name)
        else:
            s3_client.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={"LocationConstraint": region},
            )

    # Configure the ephemeral AWS provider with spot instances
    provider = EphemeralAWSProvider(
        # Region
        region=region,
        # Use spot instances for cost savings
        use_spot_instances=True,
        spot_max_price_percentage=70,  # 70% of on-demand price
        # Spot Fleet for better reliability
        use_spot_fleet=True,
        instance_types=["t3.small", "t3a.small", "t3.medium", "m5.large"],
        # Enable spot interruption handling
        spot_interruption_handling=True,
        checkpoint_bucket=bucket_name,
        # Block configuration
        init_blocks=1,
        min_blocks=0,
        max_blocks=4,
        # Worker initialization
        worker_init="""
            # Install required packages
            pip install numpy scipy

            # Set environment variables for tracking
            export EC2_INSTANCE_ID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)
            export EC2_INSTANCE_TYPE=$(curl -s http://169.254.169.254/latest/meta-data/instance-type)
            export EC2_AVAILABILITY_ZONE=$(curl -s http://169.254.169.254/latest/meta-data/placement/availability-zone)
        """,
        # Tag resources for tracking
        additional_tags={"Project": "ParslDemo", "Example": "SpotInterruptionHandling"},
    )

    # Create a Parsl executor with the provider
    executor = HighThroughputExecutor(
        label="spot_executor",
        max_workers=2,
        provider=provider,
    )

    # Create Parsl configuration
    config = Config(
        executors=[executor],
        strategy=None,  # Allow retries for spot interruptions
    )

    # Load the Parsl configuration
    parsl.load(config)

    # Submit multiple jobs with different iteration counts
    print("Submitting tasks with spot interruption handling...")
    tasks = []

    task_count = 5
    for i in range(task_count):
        iterations = 100 * (i + 1)  # 100, 200, 300, 400, 500
        print(f"  - Submitting task {i+1} with {iterations} iterations")
        tasks.append(long_compute_task(iterations=iterations))

    # Wait for results and print information about any recovered tasks
    for i, future in enumerate(tasks):
        try:
            print(f"\nWaiting for task {i+1}...")
            result = future.result()

            print(f"Task {i+1} completed:")
            print(f"  - Total time: {result['total_time']:.2f} seconds")
            print(f"  - Checkpoints: {result['checkpoint_count']}")
            print(f"  - Host history: {result['hostname_history']}")

            if result["recovered"]:
                print("  - Task was RECOVERED after spot interruption(s)")
                print(f"  - Instance history: {result['instance_history']}")
            else:
                print("  - Task completed on original instance without interruption")

        except Exception as e:
            print(f"Task {i+1} failed: {e}")

    # Clean up Parsl
    print("\nCleaning up resources...")
    parsl.clear()

    print("\nDone!")

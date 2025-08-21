#!/usr/bin/env python3
"""
Basic usage example for Parsl Ephemeral AWS Provider.

This script demonstrates how to use the Parsl Ephemeral AWS Provider
in all three operating modes: standard, detached, and serverless.
"""

import time
import logging
import parsl
from parsl.app.python import python_app
from parsl.config import Config
from parsl.executors import HighThroughputExecutor

# Import the EphemeralAWSProvider and its modes
from parsl_ephemeral_aws.provider import EphemeralAWSProvider
from parsl_ephemeral_aws.modes.standard import StandardMode
from parsl_ephemeral_aws.modes.detached import DetachedMode
from parsl_ephemeral_aws.modes.serverless import ServerlessMode
from parsl_ephemeral_aws.state.s3 import S3StateStore
from parsl_ephemeral_aws.state.parameter_store import ParameterStoreStateStore
from parsl_ephemeral_aws.state.file import FileStateStore

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ParslAWSExample")


# Define a simple Python app that we will use for testing
@python_app
def simple_task(duration=10, fail_rate=0):
    """
    A simple Python app that sleeps for a specified duration and then returns.
    Optionally, can be set to fail at a given rate (0-1) for testing error handling.

    Parameters
    ----------
    duration : int
        Sleep duration in seconds
    fail_rate : float
        Probability of task failure (0-1)

    Returns
    -------
    dict
        Dictionary containing execution info
    """
    import random
    import socket
    import os

    # Get host information
    hostname = socket.gethostname()

    # Simulate some work
    time.sleep(duration)

    # Simulate failure with specified probability
    if random.random() < fail_rate:
        raise Exception(f"Task failed with {fail_rate} probability on {hostname}")

    # Return useful information
    return {
        "hostname": hostname,
        "ip_address": socket.gethostbyname(hostname),
        "pid": os.getpid(),
        "execution_time": duration,
        "timestamp": time.time(),
    }


def run_with_standard_mode():
    """Run a simple workflow using the standard mode of the provider."""
    logger.info("Initializing provider with Standard Mode...")

    # Standard mode configuration
    provider = EphemeralAWSProvider(
        mode=StandardMode(
            # AWS-specific configuration
            region="us-west-2",
            key_name="your-key-pair-name",  # Optional: SSH key for debugging
            use_public_ips=True,  # Use public IPs for direct access
            instance_type="t3.micro",
            min_blocks=0,
            max_blocks=4,
            # Useful for development/debugging - set to False in production
            skip_instance_profile_check=True,
        ),
        # State management (local file for standard mode example)
        state_store=FileStateStore(file_path="./aws_provider_state.json"),
        # Common provider parameters
        instance_profile="ParslWorkerInstanceProfile",  # IAM instance profile
        vpc_id="vpc-12345",  # Optional: specific VPC to use
        worker_init="pip install -U pip && pip install parsl boto3",
        walltime="01:00:00",  # Shutdown instances after this time
        tags={"Project": "ParslExample", "Environment": "Dev"},
    )

    # Create a configuration with our provider
    config = Config(
        executors=[
            HighThroughputExecutor(
                label="aws_standard_executor",
                provider=provider,
                max_workers=2,  # Workers per block
            )
        ]
    )

    # Initialize Parsl with this configuration
    parsl.load(config)

    try:
        # Submit 5 tasks
        tasks = [simple_task(duration=60, fail_rate=0.2) for _ in range(5)]

        # Wait for tasks to complete
        logger.info("Waiting for all tasks to complete...")
        for i, task in enumerate(tasks):
            try:
                result = task.result()
                logger.info(f"Task {i} completed on {result['hostname']}")
            except Exception as e:
                logger.error(f"Task {i} failed: {str(e)}")

    except KeyboardInterrupt:
        logger.info("Workflow interrupted. Cleaning up resources...")
    finally:
        # Clean up Parsl resources
        parsl.clear()
        logger.info("Workflow with Standard Mode complete")


def run_with_detached_mode():
    """Run a simple workflow using the detached mode of the provider."""
    logger.info("Initializing provider with Detached Mode...")

    # Detached mode configuration
    provider = EphemeralAWSProvider(
        mode=DetachedMode(
            # AWS-specific configuration
            region="us-west-2",
            instance_type="t3.micro",
            min_blocks=0,
            max_blocks=4,
            # Detached mode specific parameters
            bastion_instance_type="t3.nano",
            bastion_image_id="ami-12345abcdef",  # Amazon Linux 2 AMI
            key_name="your-key-pair-name",  # Required for Detached mode
            # Useful for development/debugging
            skip_instance_profile_check=True,
        ),
        # For detached mode, Parameter Store is a good option
        state_store=ParameterStoreStateStore(
            region="us-west-2", prefix="/parsl/myproject"
        ),
        # Common provider parameters
        instance_profile="ParslWorkerInstanceProfile",
        worker_init="pip install -U pip && pip install parsl boto3",
        walltime="01:00:00",
        tags={"Project": "ParslExample", "Environment": "Dev"},
    )

    # Create a configuration with our provider
    config = Config(
        executors=[
            HighThroughputExecutor(
                label="aws_detached_executor",
                provider=provider,
                max_workers=2,
            )
        ]
    )

    # Initialize Parsl with this configuration
    parsl.load(config)

    try:
        # Submit 5 tasks
        tasks = [simple_task(duration=60, fail_rate=0.2) for _ in range(5)]

        # Wait for tasks to complete
        logger.info("Waiting for all tasks to complete...")
        for i, task in enumerate(tasks):
            try:
                result = task.result()
                logger.info(f"Task {i} completed on {result['hostname']}")
            except Exception as e:
                logger.error(f"Task {i} failed: {str(e)}")

    except KeyboardInterrupt:
        logger.info("Workflow interrupted. Cleaning up resources...")
    finally:
        # Clean up Parsl resources
        parsl.clear()
        logger.info("Workflow with Detached Mode complete")


def run_with_serverless_mode():
    """Run a simple workflow using the serverless mode of the provider."""
    logger.info("Initializing provider with Serverless Mode...")

    # Serverless mode configuration
    provider = EphemeralAWSProvider(
        mode=ServerlessMode(
            # AWS-specific configuration
            region="us-west-2",
            # Serverless mode specific configuration
            compute_type="lambda",  # 'lambda' or 'fargate'
            memory_size=1024,  # MB for Lambda or Fargate
            timeout=300,  # seconds (5 minutes)
            min_blocks=0,
            max_blocks=10,
            # For Lambda, include any needed layers
            lambda_layers=[
                "arn:aws:lambda:us-west-2:123456789012:layer:ParslDependencies:1"
            ],
            # For Fargate, specify container image
            # container_image="123456789012.dkr.ecr.us-west-2.amazonaws.com/parsl-worker:latest",
        ),
        # S3 state storage works well for serverless mode
        state_store=S3StateStore(
            bucket="my-parsl-state-bucket", prefix="serverless-mode", region="us-west-2"
        ),
        # Common provider parameters
        execution_role="arn:aws:iam::123456789012:role/ParslWorkerExecutionRole",
        worker_init="",  # Not directly used in serverless mode - dependencies in layer/container
        tags={"Project": "ParslExample", "Environment": "Dev"},
    )

    # Create a configuration with our provider
    config = Config(
        executors=[
            HighThroughputExecutor(
                label="aws_serverless_executor",
                provider=provider,
                # Note: max_workers has a different meaning in serverless mode
                # It represents concurrent invocations per block
                max_workers=10,
            )
        ]
    )

    # Initialize Parsl with this configuration
    parsl.load(config)

    try:
        # Submit 10 tasks - serverless is good for highly parallel workloads
        tasks = [simple_task(duration=30, fail_rate=0.1) for _ in range(10)]

        # Wait for tasks to complete
        logger.info("Waiting for all tasks to complete...")
        for i, task in enumerate(tasks):
            try:
                result = task.result()
                logger.info(
                    f"Task {i} completed on {result.get('hostname', 'serverless')}"
                )
            except Exception as e:
                logger.error(f"Task {i} failed: {str(e)}")

    except KeyboardInterrupt:
        logger.info("Workflow interrupted. Cleaning up resources...")
    finally:
        # Clean up Parsl resources
        parsl.clear()
        logger.info("Workflow with Serverless Mode complete")


def main():
    """Main function to run examples of all three modes."""

    # Uncomment the mode you want to test
    # Note: Running multiple modes in a single session not recommended
    # for real workloads - shown here for demonstration

    run_with_standard_mode()
    # run_with_detached_mode()
    # run_with_serverless_mode()

    logger.info("All examples completed!")


if __name__ == "__main__":
    main()

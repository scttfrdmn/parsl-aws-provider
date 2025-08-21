#!/usr/bin/env python3
"""
Standard Mode Example for Parsl Ephemeral AWS Provider.

This script demonstrates how to use the Parsl Ephemeral AWS Provider in Standard Mode.
In Standard Mode, EC2 instances are provisioned directly, and your local machine
communicates with them directly. This mode is simplest to understand and works well
when your local environment has direct network access to AWS.

Key features of Standard Mode:
- Direct communication between your local machine and AWS resources
- Support for public or private IP addressing
- Simple architecture with no intermediary resources
- Quick startup and immediate resource release
"""

import time
import logging
import parsl
from parsl.app.python import python_app
from parsl.config import Config
from parsl.executors import HighThroughputExecutor

# Import the EphemeralAWSProvider and StandardMode
from parsl_ephemeral_aws.provider import EphemeralAWSProvider
from parsl_ephemeral_aws.modes.standard import StandardMode
from parsl_ephemeral_aws.state.file import FileStateStore

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("StandardModeExample")


# Define a simple Python app for testing
@python_app
def standard_task(duration=10, compute_intensive=False):
    """
    A Python app that runs on AWS resources provisioned in Standard Mode.

    Parameters
    ----------
    duration : int
        Sleep duration in seconds
    compute_intensive : bool
        If True, performs CPU-intensive calculation

    Returns
    -------
    dict
        Dictionary containing execution info
    """
    import time
    import socket
    import platform
    import psutil

    hostname = socket.gethostname()
    ip_address = socket.gethostbyname(hostname)

    start_time = time.time()

    # Simulate CPU-intensive work if requested
    if compute_intensive:
        # Calculate prime numbers as a CPU-intensive task
        def is_prime(n):
            if n <= 1:
                return False
            if n <= 3:
                return True
            if n % 2 == 0 or n % 3 == 0:
                return False
            i = 5
            while i * i <= n:
                if n % i == 0 or n % (i + 2) == 0:
                    return False
                i += 6
            return True

        # Find some prime numbers to create CPU load
        primes = [n for n in range(2, 10000) if is_prime(n)]
        num_primes = len(primes)
    else:
        # Just sleep if no compute needed
        time.sleep(duration)
        num_primes = 0

    # Collect system information
    cpu_percent = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()

    end_time = time.time()
    execution_time = end_time - start_time

    # Return detailed information about the execution environment
    return {
        "hostname": hostname,
        "ip_address": ip_address,
        "pid": os.getpid(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "cpu_percent": cpu_percent,
        "memory_total_gb": mem.total / (1024**3),
        "memory_available_gb": mem.available / (1024**3),
        "execution_time": execution_time,
        "compute_intensive": compute_intensive,
        "num_primes_calculated": num_primes if compute_intensive else 0,
        "timestamp": time.time(),
    }


def main():
    """
    Main function to demonstrate Standard Mode of the Parsl Ephemeral AWS Provider.
    """
    logger.info("Initializing EphemeralAWSProvider with Standard Mode...")

    # Configure the provider with Standard Mode
    provider = EphemeralAWSProvider(
        mode=StandardMode(
            # AWS Region - be sure to specify your desired region
            region="us-west-2",
            # EC2 Instance Configuration
            instance_type="t3.small",  # Choose instance type based on your workload
            image_id="ami-0c55b159cbfafe1f0",  # Amazon Linux 2 AMI (update with current AMI)
            # Network Configuration
            use_public_ips=True,  # Set to False if running in a VPC with VPN/Direct Connect
            security_group_ids=None,  # Optional: Specify existing security groups, if None, creates new
            # Scaling Configuration
            min_blocks=0,  # Minimum number of blocks to maintain
            max_blocks=4,  # Maximum number of blocks to scale to
            init_blocks=0,  # Initial blocks to start with
            # SSH Access (useful for debugging)
            key_name="your-key-pair-name",  # Optional: SSH key for direct instance access
            # Advanced Options
            spot_instances=False,  # Set to True to use spot instances (cheaper but can be terminated)
            spot_max_bid=None,  # Maximum bid price as % of on-demand price (e.g., "0.5")
            # Development/Debugging Options (disable in production)
            skip_instance_profile_check=True,  # Skip checking instance profile existence
        ),
        # State management - Standard mode typically uses local file storage
        # The state file stores information about provisioned resources
        state_store=FileStateStore(
            file_path="./standard_mode_state.json",
            backup_interval=300,  # Backup the state every 5 minutes
        ),
        # Common provider configuration parameters
        instance_profile="ParslWorkerInstanceProfile",  # IAM instance profile for EC2 instances
        # Script to run on worker initialization
        # This installs Parsl and any dependencies needed on the worker
        worker_init="""#!/bin/bash
            # Update system packages
            yum update -y

            # Install Python dependencies
            pip3 install --upgrade pip
            pip3 install parsl boto3 psutil

            # Set up any custom environment needed for your workflow
            echo "Worker initialization complete"
        """,
        # Maximum walltime for instances (helps prevent runaway costs)
        walltime="01:00:00",  # Format: HH:MM:SS
        # Tags to apply to all created resources for organization and tracking
        tags={
            "Project": "ParslExample",
            "Environment": "Development",
            "ManagedBy": "ParslEphemeralAWSProvider",
            "Mode": "Standard",
        },
    )

    # Create a Parsl configuration with our provider
    config = Config(
        executors=[
            HighThroughputExecutor(
                label="standard_mode_executor",
                provider=provider,
                max_workers=2,  # Workers per EC2 instance (block)
                address="0.0.0.0",  # Address for the worker to listen on
                worker_debug=True,  # Enable worker debug logging
            )
        ],
        strategy="simple",  # Execution strategy
        run_dir="runinfo",  # Directory to store Parsl runtime files
    )

    # Initialize Parsl with our configuration
    parsl.load(config)

    try:
        logger.info("Submitting tasks to AWS resources...")

        # Submit a mix of regular and compute-intensive tasks
        tasks = []

        # Regular tasks
        for i in range(3):
            tasks.append(standard_task(duration=60, compute_intensive=False))

        # Compute-intensive tasks
        for i in range(2):
            tasks.append(standard_task(duration=0, compute_intensive=True))

        # Wait for tasks to complete and process results
        logger.info("Waiting for all tasks to complete...")

        for i, task in enumerate(tasks):
            try:
                logger.info(f"Waiting for task {i} result...")
                result = task.result()

                # Log detailed information about the execution
                logger.info(f"Task {i} completed successfully:")
                logger.info(f"  Host: {result['hostname']} ({result['ip_address']})")
                logger.info(f"  Platform: {result['platform']}")
                logger.info(
                    f"  CPU: {result['cpu_count']} cores, {result['cpu_percent']}% utilized"
                )
                logger.info(
                    f"  Memory: {result['memory_available_gb']:.2f}GB available of {result['memory_total_gb']:.2f}GB total"
                )
                logger.info(f"  Execution time: {result['execution_time']:.2f} seconds")

                if result["compute_intensive"]:
                    logger.info(
                        f"  Computed {result['num_primes_calculated']} prime numbers"
                    )

            except Exception as e:
                logger.error(f"Task {i} failed: {str(e)}")

        # Allow some time for cleanup before exiting
        logger.info("All tasks completed. Waiting for resources to clean up...")
        time.sleep(5)

    except KeyboardInterrupt:
        logger.info("Workflow interrupted. Cleaning up resources...")

    finally:
        # Clean up all Parsl resources
        # This is critical to ensure AWS resources are properly terminated
        logger.info("Cleaning up Parsl resources...")
        parsl.clear()

        logger.info("Standard Mode example complete")


if __name__ == "__main__":
    main()

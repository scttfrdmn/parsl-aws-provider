#!/usr/bin/env python3
"""
Detached Mode Example for Parsl Ephemeral AWS Provider.

This script demonstrates how to use the Parsl Ephemeral AWS Provider in Detached Mode.
In Detached Mode, a bastion host is provisioned in AWS that manages the worker nodes.
Your local machine communicates only with the bastion host, which then handles all
worker provisioning and communication. This mode is useful when:

1. Your local environment has limited connectivity to AWS
2. You want to submit a job and disconnect your local machine
3. You need to run long-duration workflows that should continue even if your
   local machine disconnects or shuts down

Key features of Detached Mode:
- Persistent bastion host that maintains workflow state
- Continues execution even when local machine disconnects
- Works with restricted network environments
- Supports workflow reconnection after disconnection
"""

import time
import logging
import parsl
from parsl.app.python import python_app
from parsl.config import Config
from parsl.executors import HighThroughputExecutor

# Import the EphemeralAWSProvider and DetachedMode
from parsl_ephemeral_aws.provider import EphemeralAWSProvider
from parsl_ephemeral_aws.modes.detached import DetachedMode
from parsl_ephemeral_aws.state.parameter_store import ParameterStoreStateStore

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("DetachedModeExample")


# Define a simple Python app for testing
@python_app
def detached_task(duration=10, task_id=None):
    """
    A Python app that runs on AWS resources provisioned in Detached Mode.

    Parameters
    ----------
    duration : int
        Sleep duration in seconds
    task_id : str
        Optional identifier for the task

    Returns
    -------
    dict
        Dictionary containing execution info
    """
    import time
    import socket
    import uuid
    import datetime

    # Generate a task ID if none provided
    if task_id is None:
        task_id = str(uuid.uuid4())[:8]

    hostname = socket.gethostname()
    start_time = time.time()

    # Simulate work that continues even if the client disconnects
    for i in range(duration):
        # In a real application, this would be your long-running computation
        time.sleep(1)

        # Create a checkpoint every 10 seconds
        # In detached mode, this allows progress to be preserved
        if i > 0 and i % 10 == 0:
            checkpoint_file = f"/tmp/checkpoint_task_{task_id}.txt"
            with open(checkpoint_file, "w") as f:
                f.write(f"Task {task_id} checkpoint at {i}/{duration} steps\n")
                f.write(f"Timestamp: {datetime.datetime.now().isoformat()}\n")
                f.write(f"Progress: {(i/duration)*100:.1f}%\n")

    end_time = time.time()

    # Return detailed execution information
    return {
        "task_id": task_id,
        "hostname": hostname,
        "ip_address": socket.gethostbyname(hostname),
        "start_time": start_time,
        "end_time": end_time,
        "duration": duration,
        "actual_runtime": end_time - start_time,
        "pid": os.getpid(),
        "bastion_managed": True,
        "timestamp": time.time(),
    }


def main():
    """
    Main function to demonstrate Detached Mode of the Parsl Ephemeral AWS Provider.
    """
    logger.info("Initializing EphemeralAWSProvider with Detached Mode...")

    # Create a unique workflow ID for this run
    # This helps with reconnecting to the same workflow later
    workflow_id = f"detached-workflow-{int(time.time())}"
    logger.info(f"Workflow ID: {workflow_id}")

    # Configure the provider with Detached Mode
    provider = EphemeralAWSProvider(
        mode=DetachedMode(
            # AWS Region configuration
            region="us-west-2",
            # Bastion Host Configuration
            bastion_instance_type="t3.micro",  # Small instance for the bastion
            bastion_image_id="ami-0c55b159cbfafe1f0",  # Amazon Linux 2 AMI (update with current AMI)
            key_name="your-key-pair-name",  # REQUIRED for bastion access
            # Worker EC2 Configuration
            instance_type="t3.small",  # Worker instance type
            min_blocks=0,
            max_blocks=4,
            init_blocks=1,  # Start with one worker block
            # Detached mode specific options
            workflow_id=workflow_id,  # Identifier for this workflow
            reconnect=False,  # Set to True if reconnecting to an existing workflow
            # Connection settings
            bastion_public_ip=None,  # Will be auto-assigned, but can be specified for reconnection
            ssh_port=22,
            # Advanced bastion configuration
            bastion_setup_script="""#!/bin/bash
                # Set up the bastion host with any additional software needed
                yum update -y
                pip3 install --upgrade pip
                pip3 install pyzmq

                # Configure system for better performance
                sysctl -w net.core.somaxconn=4096

                # Set up monitoring
                echo "*/5 * * * * /usr/bin/aws cloudwatch put-metric-data --namespace ParslDetachedMode --metric-name Heartbeat --value 1 --unit Count --region us-west-2" | crontab -

                echo "Bastion setup complete"
            """,
            # Worker security
            worker_security_group_name="ParslWorkerSecurityGroup",
            bastion_security_group_name="ParslBastionSecurityGroup",
            # Development/Debugging options
            skip_instance_profile_check=True,
        ),
        # State management - Parameter Store works well for detached mode
        # as it's accessible from both your local machine and the bastion
        state_store=ParameterStoreStateStore(
            region="us-west-2",
            prefix=f"/parsl/{workflow_id}",  # Use workflow ID in the path
            ttl=86400,  # State expires after 24 hours (adjust as needed)
        ),
        # Common provider configuration
        instance_profile="ParslWorkerInstanceProfile",
        # Worker initialization script
        worker_init="""#!/bin/bash
            # Update and install dependencies
            yum update -y
            pip3 install --upgrade pip
            pip3 install parsl boto3 psutil

            # Create a directory for checkpoint files
            mkdir -p /tmp/checkpoints
            chmod 777 /tmp/checkpoints

            # Set up worker-specific configuration
            echo "Worker node initialized and ready for tasks"
        """,
        # Walltime for worker instances
        walltime="02:00:00",  # Workers run for up to 2 hours
        # Resource tagging for organization
        tags={
            "Project": "ParslExample",
            "Environment": "Development",
            "ManagedBy": "ParslEphemeralAWSProvider",
            "Mode": "Detached",
            "WorkflowID": workflow_id,
        },
    )

    # Create a Parsl configuration with our provider
    config = Config(
        executors=[
            HighThroughputExecutor(
                label="detached_mode_executor",
                provider=provider,
                max_workers=2,  # Workers per EC2 instance
                worker_debug=True,
            )
        ],
        run_dir="runinfo_detached",
    )

    # Initialize Parsl with our configuration
    parsl.load(config)

    try:
        logger.info("Submitting long-running tasks to detached AWS environment...")

        # Submit tasks with various durations to demonstrate detached execution
        tasks = []

        # Short tasks - to verify everything is working
        for i in range(2):
            tasks.append(detached_task(duration=30, task_id=f"short-{i}"))

        # Medium-duration tasks
        for i in range(2):
            tasks.append(detached_task(duration=120, task_id=f"medium-{i}"))

        # One long-duration task
        tasks.append(detached_task(duration=300, task_id="long-task"))

        logger.info(f"Submitted {len(tasks)} tasks to the detached environment")
        logger.info("Tasks will continue to run even if this script is terminated")
        logger.info(
            f"To reconnect to this workflow later, use workflow_id: {workflow_id}"
        )

        # Wait for initial tasks to start executing
        logger.info("Waiting for tasks to start executing...")
        time.sleep(60)

        # Check status of tasks without waiting for completion
        for i, task in enumerate(tasks):
            if task.done():
                try:
                    result = task.result()
                    logger.info(f"Task {i} already completed on {result['hostname']}")
                except Exception as e:
                    logger.error(f"Task {i} failed: {str(e)}")
            else:
                logger.info(f"Task {i} is still running")

        # To demonstrate detached operation, we can optionally wait for all tasks
        # Uncomment the following code to wait for all tasks to complete
        """
        logger.info("Waiting for all tasks to complete...")
        for i, task in enumerate(tasks):
            try:
                result = task.result()
                logger.info(f"Task {i} completed on {result['hostname']}")
                logger.info(f"  Runtime: {result['actual_runtime']:.2f} seconds")
            except Exception as e:
                logger.error(f"Task {i} failed: {str(e)}")
        """

        # Instead, we'll just wait a short time to demonstrate that tasks continue
        # running even after this script exits
        logger.info("Example is completing, but tasks will continue in AWS...")
        logger.info(
            f"You can reconnect to this workflow using workflow_id: {workflow_id}"
        )
        time.sleep(10)

    except KeyboardInterrupt:
        logger.info("Workflow monitoring interrupted.")
        logger.info("Tasks will continue running in the detached environment.")
        logger.info(f"To reconnect, use workflow_id: {workflow_id}")

    finally:
        # In detached mode, parsl.clear() doesn't terminate the resources
        # It just disconnects the client
        logger.info("Disconnecting from detached workflow...")
        parsl.clear()

        # To actually terminate all resources, you would need to use:
        # provider.cancel_all_blocks()
        # But in detached mode, we typically let the workflow continue

        logger.info("Detached Mode example complete - workflow continues in AWS")
        logger.info(
            f"To terminate all resources later, reconnect with workflow_id: {workflow_id}"
        )
        logger.info("Then call provider.cancel_all_blocks() to clean up")


def reconnect_to_workflow(workflow_id, bastion_ip=None):
    """
    Example function showing how to reconnect to an existing detached workflow.

    Parameters
    ----------
    workflow_id : str
        The workflow ID to reconnect to
    bastion_ip : str, optional
        The public IP of the bastion, if known
    """
    logger.info(f"Reconnecting to detached workflow: {workflow_id}")

    # Configure the provider with Detached Mode in reconnect mode
    provider = EphemeralAWSProvider(
        mode=DetachedMode(
            region="us-west-2",
            # Minimal configuration needed for reconnection
            workflow_id=workflow_id,
            reconnect=True,  # Critical flag to enable reconnection
            bastion_public_ip=bastion_ip,  # Can be None if stored in state
            key_name="your-key-pair-name",
        ),
        # Must use the same state store configuration as the original run
        state_store=ParameterStoreStateStore(
            region="us-west-2",
            prefix=f"/parsl/{workflow_id}",
        ),
        # Other parameters should match original configuration
        tags={"WorkflowID": workflow_id},
    )

    # Create a Parsl configuration with our provider
    config = Config(
        executors=[
            HighThroughputExecutor(
                label="reconnected_executor",
                provider=provider,
                max_workers=2,
            )
        ],
        run_dir="runinfo_reconnected",
    )

    # Initialize Parsl with our configuration
    parsl.load(config)

    try:
        logger.info("Reconnected to workflow. You can now:")
        logger.info("1. Submit new tasks to the existing environment")
        logger.info("2. Check status of previously submitted tasks")
        logger.info("3. Terminate all resources when done")

        # Example: Check status of the executor
        logger.info("Current executor status:")
        status = provider.status([])
        for block_id, block_status in status.items():
            logger.info(f"Block {block_id}: {block_status}")

        # Example: To terminate all resources
        # provider.cancel_all_blocks()

    except Exception as e:
        logger.error(f"Error during reconnection: {str(e)}")

    finally:
        logger.info("Disconnecting from reconnected workflow")
        parsl.clear()
        logger.info("Reconnection example complete")


if __name__ == "__main__":
    # Run the main example
    main()

    # Uncomment to demonstrate reconnection to a previous workflow
    # Replace with actual workflow ID and optional bastion IP
    # reconnect_to_workflow("detached-workflow-1234567890")

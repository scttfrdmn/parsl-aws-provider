#!/usr/bin/env python3
"""
Serverless Mode Example for Parsl Ephemeral AWS Provider.

This script demonstrates how to use the Parsl Ephemeral AWS Provider in Serverless Mode.
In Serverless Mode, the provider leverages AWS Lambda functions or Fargate containers
to execute tasks without provisioning or managing any EC2 instances. This mode is ideal for:

1. Highly scalable, short-duration tasks (Lambda)
2. Tasks requiring more memory or longer durations (Fargate)
3. Reducing infrastructure management overhead
4. Minimizing costs for intermittent workloads

Key features of Serverless Mode:
- Zero infrastructure management
- Rapid scaling to hundreds or thousands of concurrent tasks
- Pay only for actual compute time used
- Support for both Lambda and Fargate compute types
- Simplified security model
"""

import os
import time
import logging
import parsl
from parsl.app.python import python_app
from parsl.config import Config
from parsl.executors import HighThroughputExecutor

# Import the EphemeralAWSProvider and ServerlessMode
from parsl_ephemeral_aws.provider import EphemeralAWSProvider
from parsl_ephemeral_aws.modes.serverless import ServerlessMode
from parsl_ephemeral_aws.state.s3 import S3StateStore

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ServerlessModeExample")

# Define a simple Python app for testing
@python_app
def serverless_task(task_type="processing", data_size=10, cpu_intensive=False):
    """
    A Python app that runs on AWS serverless resources (Lambda or Fargate).
    
    Parameters
    ----------
    task_type : str
        Type of task to simulate ('processing', 'inference', etc.)
    data_size : int
        Simulated data size in MB to process
    cpu_intensive : bool
        Whether to perform CPU-intensive calculations
    
    Returns
    -------
    dict
        Dictionary containing execution info
    """
    import time
    import os
    import json
    import uuid
    import math
    import platform
    
    # In serverless mode, we may not have a traditional hostname
    # Lambda functions use an execution environment ID
    try:
        import socket
        hostname = socket.gethostname()
    except:
        hostname = f"serverless-{str(uuid.uuid4())[:8]}"
    
    # Get AWS Lambda specific environment variables if available
    aws_lambda_function_name = os.environ.get('AWS_LAMBDA_FUNCTION_NAME', 'not-lambda')
    aws_lambda_function_version = os.environ.get('AWS_LAMBDA_FUNCTION_VERSION', 'not-lambda')
    aws_region = os.environ.get('AWS_REGION', 'unknown')
    
    # Get container-specific info if in Fargate
    task_id = os.environ.get('ECS_TASK_ID', 'not-ecs')
    container_id = os.environ.get('ECS_CONTAINER_ID', 'not-ecs')
    
    # Record start time
    start_time = time.time()
    
    # Simulate workload based on task_type
    result_data = {}
    
    if task_type == "processing":
        # Simulate data processing workload
        data = [i for i in range(data_size * 100000)]  # Generate dummy data
        processed_items = len(data)
        result_data["processed_items"] = processed_items
        
    elif task_type == "inference":
        # Simulate ML inference workload
        if cpu_intensive:
            # Simulate matrix operations common in ML inference
            matrix_size = min(1000, data_size * 100)  # Limit matrix size based on data_size
            matrix = [[math.sin(i*j) for j in range(matrix_size)] for i in range(100)]
            result = sum(sum(row) for row in matrix)
            result_data["inference_result"] = result
        else:
            # Simulate lightweight inference
            time.sleep(data_size * 0.1)
            result_data["inference_result"] = 42
            
    elif task_type == "etl":
        # Simulate ETL workload
        # Process dummy data and transform it
        source_data = {"records": [{"id": i, "value": i*2} for i in range(data_size * 100)]}
        transformed_data = [record["value"] * 1.5 for record in source_data["records"]]
        result_data["transformed_count"] = len(transformed_data)
        result_data["avg_value"] = sum(transformed_data) / len(transformed_data) if transformed_data else 0
        
    else:
        # Default simple workload
        time.sleep(data_size * 0.2)
    
    # Perform CPU-intensive calculation if requested
    if cpu_intensive:
        # Calculate some prime numbers to use CPU
        primes = []
        for num in range(2, 10000):
            is_prime = True
            for i in range(2, int(math.sqrt(num)) + 1):
                if num % i == 0:
                    is_prime = False
                    break
            if is_prime:
                primes.append(num)
        result_data["prime_count"] = len(primes)
    
    # Calculate execution time
    end_time = time.time()
    execution_time = end_time - start_time
    
    # Get available memory limits - in Lambda this is available as an env var
    memory_limit_mb = os.environ.get('AWS_LAMBDA_FUNCTION_MEMORY_SIZE', 
                                     os.environ.get('FARGATE_MEMORY_LIMIT_MB', 'unknown'))
    
    # Return execution information
    return {
        "task_id": str(uuid.uuid4()),
        "task_type": task_type,
        "data_size_mb": data_size,
        "cpu_intensive": cpu_intensive,
        "execution_environment": "Lambda" if aws_lambda_function_name != 'not-lambda' else 
                                ("Fargate" if task_id != 'not-ecs' else "Unknown"),
        "hostname": hostname,
        "function_name": aws_lambda_function_name,
        "function_version": aws_lambda_function_version,
        "region": aws_region,
        "task_id": task_id if task_id != 'not-ecs' else None,
        "container_id": container_id if container_id != 'not-ecs' else None,
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "memory_limit_mb": memory_limit_mb,
        "execution_time_seconds": execution_time,
        "timestamp": time.time(),
        "result_data": result_data
    }


def lambda_mode_example():
    """Run a workflow using Lambda-based serverless mode."""
    logger.info("Initializing Serverless Mode with AWS Lambda...")
    
    # Configure the provider with Serverless Mode (Lambda)
    provider = EphemeralAWSProvider(
        mode=ServerlessMode(
            # AWS Region
            region="us-west-2",
            
            # Lambda-specific configuration
            compute_type="lambda",  # Use AWS Lambda
            memory_size=1024,       # Memory in MB
            timeout=300,            # Maximum function timeout (5 minutes)
            min_blocks=0,
            max_blocks=20,          # Can scale to many concurrent invocations
            
            # Lambda layers containing dependencies
            # Create layers with your dependencies including parsl
            lambda_layers=[
                "arn:aws:lambda:us-west-2:123456789012:layer:ParslDependencies:1",
                # Add additional layers as needed for your specific workload
            ],
            
            # Lambda function creation settings
            function_name_prefix="parsl-serverless-",  # Function name prefix
            lambda_runtime="python3.9",                # Python runtime version
            
            # Optional dead letter queue for failed executions
            dead_letter_queue_arn="arn:aws:sqs:us-west-2:123456789012:parsl-dlq",
            
            # Network settings (if Lambda needs VPC access)
            # vpc_config={
            #     "SubnetIds": ["subnet-12345", "subnet-67890"],
            #     "SecurityGroupIds": ["sg-12345"]
            # },
            
            # Advanced configuration
            concurrent_invocations_per_function=100,  # Concurrency limit per function
            environment_variables={
                "PARSL_WORKER_MODE": "lambda",
                "LOG_LEVEL": "INFO",
                # Add custom environment variables here
            },
        ),
        
        # S3 state storage works well for serverless mode
        state_store=S3StateStore(
            bucket="my-parsl-state-bucket",
            prefix="lambda-mode",
            region="us-west-2",
            create_bucket=True,  # Create the bucket if it doesn't exist
        ),
        
        # Lambda execution role
        execution_role="arn:aws:iam::123456789012:role/ParslLambdaExecutionRole",
        
        # No worker_init needed for Lambda - dependencies provided via layers
        worker_init="",
        
        # Resource tagging for organization
        tags={
            "Project": "ParslExample",
            "Environment": "Development",
            "ManagedBy": "ParslEphemeralAWSProvider",
            "Mode": "Serverless-Lambda"
        },
    )
    
    # Create a Parsl configuration with our provider
    config = Config(
        executors=[
            HighThroughputExecutor(
                label="lambda_executor",
                provider=provider,
                # For Lambda, max_workers controls concurrent invocations per block
                max_workers=50,
            )
        ],
        run_dir="runinfo_lambda",
    )
    
    # Initialize Parsl with our configuration
    parsl.load(config)
    
    try:
        logger.info("Submitting tasks to Lambda functions...")
        
        # Submit a variety of tasks to demonstrate Lambda's flexibility
        tasks = []
        
        # Processing tasks with different data sizes
        for size in [1, 5, 10]:
            tasks.append(serverless_task(task_type="processing", data_size=size, cpu_intensive=False))
        
        # Inference tasks
        for i in range(3):
            # Some with CPU-intensive workloads
            cpu_intensive = (i % 2 == 0)
            tasks.append(serverless_task(task_type="inference", data_size=5, cpu_intensive=cpu_intensive))
        
        # ETL tasks
        for i in range(2):
            tasks.append(serverless_task(task_type="etl", data_size=8, cpu_intensive=False))
        
        # Wait for tasks to complete and process results
        logger.info(f"Waiting for {len(tasks)} Lambda tasks to complete...")
        
        lambda_results = []
        for i, task in enumerate(tasks):
            try:
                result = task.result()
                lambda_results.append(result)
                
                # Log task completion and important details
                logger.info(f"Task {i} ({result['task_type']}) completed:")
                logger.info(f"  Environment: {result['execution_environment']}")
                logger.info(f"  Memory: {result['memory_limit_mb']} MB")
                logger.info(f"  Execution time: {result['execution_time_seconds']:.2f} seconds")
                
                # Log result-specific details
                if 'result_data' in result:
                    if 'processed_items' in result['result_data']:
                        logger.info(f"  Processed {result['result_data']['processed_items']} items")
                    if 'inference_result' in result['result_data']:
                        logger.info(f"  Inference completed")
                    if 'transformed_count' in result['result_data']:
                        logger.info(f"  Transformed {result['result_data']['transformed_count']} records")
                
            except Exception as e:
                logger.error(f"Task {i} failed: {str(e)}")
        
        # Calculate and display some aggregate statistics
        if lambda_results:
            avg_execution_time = sum(r['execution_time_seconds'] for r in lambda_results) / len(lambda_results)
            logger.info(f"Average execution time: {avg_execution_time:.2f} seconds")
            logger.info(f"Total tasks completed: {len(lambda_results)}")
        
    except KeyboardInterrupt:
        logger.info("Workflow interrupted. Cleaning up resources...")
    
    finally:
        # Clean up Parsl resources
        logger.info("Cleaning up Lambda resources...")
        parsl.clear()
        
        logger.info("Lambda Mode example complete")


def fargate_mode_example():
    """Run a workflow using Fargate-based serverless mode."""
    logger.info("Initializing Serverless Mode with AWS Fargate...")
    
    # Configure the provider with Serverless Mode (Fargate)
    provider = EphemeralAWSProvider(
        mode=ServerlessMode(
            # AWS Region
            region="us-west-2",
            
            # Fargate-specific configuration
            compute_type="fargate",      # Use AWS Fargate
            memory_size=2048,            # Memory in MB (minimum 1024)
            cpu=1024,                    # CPU units (1024 = 1 vCPU)
            timeout=3600,                # Maximum task timeout (1 hour)
            min_blocks=0,
            max_blocks=10,               # Maximum concurrent tasks
            
            # ECS/Fargate cluster configuration
            cluster_name="parsl-fargate-cluster",  # ECS cluster name
            
            # Container image with Parsl and dependencies
            # You must create a Docker image with your dependencies
            container_image="123456789012.dkr.ecr.us-west-2.amazonaws.com/parsl-worker:latest",
            
            # Network configuration (required for Fargate)
            # At minimum, you need a VPC with a subnet that has internet access
            vpc_config={
                "subnets": ["subnet-12345", "subnet-67890"],
                "security_groups": ["sg-12345"],
                "assign_public_ip": True,  # Assign public IP for internet access
            },
            
            # Advanced configuration
            launch_type="FARGATE",  # Use FARGATE (or FARGATE_SPOT for cost savings)
            platform_version="LATEST",  # Fargate platform version
            environment_variables={
                "PARSL_WORKER_MODE": "fargate",
                "LOG_LEVEL": "INFO",
                # Add custom environment variables here
            },
        ),
        
        # S3 state storage works well for serverless mode
        state_store=S3StateStore(
            bucket="my-parsl-state-bucket",
            prefix="fargate-mode",
            region="us-west-2",
            create_bucket=True,  # Create the bucket if it doesn't exist
        ),
        
        # Fargate task execution role
        execution_role="arn:aws:iam::123456789012:role/ParslFargateExecutionRole",
        
        # No worker_init needed for Fargate - dependencies included in container
        worker_init="",
        
        # Resource tagging for organization
        tags={
            "Project": "ParslExample",
            "Environment": "Development",
            "ManagedBy": "ParslEphemeralAWSProvider",
            "Mode": "Serverless-Fargate"
        },
    )
    
    # Create a Parsl configuration with our provider
    config = Config(
        executors=[
            HighThroughputExecutor(
                label="fargate_executor",
                provider=provider,
                # For Fargate, max_workers is typically equal to the container's
                # capacity for parallel processing
                max_workers=4,
            )
        ],
        run_dir="runinfo_fargate",
    )
    
    # Initialize Parsl with our configuration
    parsl.load(config)
    
    try:
        logger.info("Submitting tasks to Fargate containers...")
        
        # Submit a variety of tasks to demonstrate Fargate's capabilities
        # Fargate is well-suited for longer-running or more resource-intensive tasks
        tasks = []
        
        # CPU-intensive processing tasks
        for i in range(3):
            tasks.append(serverless_task(task_type="processing", data_size=20, cpu_intensive=True))
        
        # Complex inference tasks that benefit from more memory
        for i in range(3):
            tasks.append(serverless_task(task_type="inference", data_size=15, cpu_intensive=True))
        
        # Large ETL tasks
        for i in range(2):
            tasks.append(serverless_task(task_type="etl", data_size=25, cpu_intensive=True))
        
        # Wait for tasks to complete and process results
        logger.info(f"Waiting for {len(tasks)} Fargate tasks to complete...")
        
        fargate_results = []
        for i, task in enumerate(tasks):
            try:
                result = task.result()
                fargate_results.append(result)
                
                # Log task completion and important details
                logger.info(f"Task {i} ({result['task_type']}) completed:")
                logger.info(f"  Environment: {result['execution_environment']}")
                logger.info(f"  Container: {result['container_id'] or 'unknown'}")
                logger.info(f"  Execution time: {result['execution_time_seconds']:.2f} seconds")
                
                # Log result-specific details
                if 'result_data' in result:
                    if 'prime_count' in result['result_data']:
                        logger.info(f"  Calculated {result['result_data']['prime_count']} prime numbers")
                    if 'transformed_count' in result['result_data']:
                        logger.info(f"  Transformed {result['result_data']['transformed_count']} records")
                
            except Exception as e:
                logger.error(f"Task {i} failed: {str(e)}")
        
        # Calculate and display some aggregate statistics
        if fargate_results:
            avg_execution_time = sum(r['execution_time_seconds'] for r in fargate_results) / len(fargate_results)
            logger.info(f"Average execution time: {avg_execution_time:.2f} seconds")
            logger.info(f"Total tasks completed: {len(fargate_results)}")
        
    except KeyboardInterrupt:
        logger.info("Workflow interrupted. Cleaning up resources...")
    
    finally:
        # Clean up Parsl resources
        logger.info("Cleaning up Fargate resources...")
        parsl.clear()
        
        logger.info("Fargate Mode example complete")


def main():
    """
    Main function to demonstrate Serverless Mode of the Parsl Ephemeral AWS Provider.
    """
    logger.info("Starting Serverless Mode Examples...")
    
    # Uncomment the mode you want to test
    # Note: Testing both modes in a single run is possible but not recommended
    # for production workloads
    
    lambda_mode_example()
    # fargate_mode_example()
    
    logger.info("Serverless Mode examples complete")


if __name__ == "__main__":
    main()
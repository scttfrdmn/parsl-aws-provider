#!/usr/bin/env python3
"""Debug container execution directly."""

import logging
import subprocess
import boto3
from phase15_enhanced import AWSProvider

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_direct_container_execution():
    """Test direct Docker execution on AWS instance."""
    
    # Get the latest instance from the provider
    provider = AWSProvider(
        label="DEBUG-CONTAINER",
        region="us-east-1",
        container_runtime="docker",
        container_image="parsl-base:latest",
        init_blocks=1,
        max_blocks=1,
        instance_type="t3.medium"
    )
    
    try:
        # Submit a block to get an instance
        logger.info("Creating AWS instance...")
        job_ids = provider.submit('echo "test"', 1, 1)  # Simple test command
        
        import time
        time.sleep(30)  # Wait for instance startup
        
        # Get the instance ID
        status = provider.status(job_ids)
        logger.info(f"Job status: {status}")
        
        # Find the running instance
        instances = list(provider.resources.keys())
        if not instances:
            logger.error("No instances found")
            return False
            
        instance_id = instances[0]
        logger.info(f"Testing container execution on {instance_id}")
        
        # Test 1: Check if Docker is working
        logger.info("Test 1: Checking Docker availability...")
        result = provider.execute_remote_command_sync(
            instance_id,
            "docker --version"
        )
        logger.info(f"Docker version: {result.stdout}")
        
        # Test 2: Check if our container image exists
        logger.info("Test 2: Checking container image...")
        result = provider.execute_remote_command_sync(
            instance_id,
            "docker images | grep parsl-base"
        )
        logger.info(f"Container image: {result.stdout}")
        
        # Test 3: Test simple container execution
        logger.info("Test 3: Simple container test...")
        result = provider.execute_remote_command_sync(
            instance_id,
            'docker run --rm parsl-base:latest python3 -c "import os; print(f\\"In container: {os.path.exists(\\"/\\.dockerenv\\")}\\")"'
        )
        logger.info(f"Container test result: {result.stdout}")
        
        # Test 4: Test Parsl worker in container
        logger.info("Test 4: Parsl worker in container...")
        test_cmd = '''docker run --rm --network host -e PYTHONUNBUFFERED=1 parsl-base:latest python3 -c "
import os
import platform
import numpy as np

print('=== CONTAINER EXECUTION TEST ===')
print(f'In container: {os.path.exists(\\'/\\.dockerenv\\')}')
print(f'Platform: {platform.platform()}')
print(f'Working dir: {os.getcwd()}')
print(f'Python version: {platform.python_version()}')

# Test numpy
data = np.random.random(10)
result = np.mean(data)
print(f'NumPy test: {result}')
print('=== TEST COMPLETE ===')
"'''
        
        result = provider.execute_remote_command_sync(instance_id, test_cmd)
        logger.info(f"Parsl worker test: {result.stdout}")
        
        if "In container: True" in result.stdout:
            logger.info("✅ Container execution working!")
            return True
        else:
            logger.error("❌ Container execution failed")
            return False
        
    finally:
        try:
            provider.cancel(job_ids)
        except:
            pass

if __name__ == "__main__":
    test_direct_container_execution()
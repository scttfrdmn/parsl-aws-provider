#!/usr/bin/env python3
"""Test Docker command directly on AWS instance."""

import logging
import boto3
import time
from phase15_enhanced import AWSProvider

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_docker_command_directly():
    """Test our exact Docker command on AWS instance."""
    
    # Create a minimal provider just to get an instance
    provider = AWSProvider(
        label="DOCKER-TEST",
        region="us-east-1",
        init_blocks=1,
        max_blocks=1,
        instance_type="t3.small"
    )
    
    try:
        # Submit to get instance
        job_id = provider.submit('echo "test"', 1, 1)
        logger.info(f"Instance launching: {job_id}")
        
        # Wait for instance
        time.sleep(60)
        
        # Get instance ID
        instances = list(provider.resources.keys())
        if not instances:
            logger.error("No instances found")
            return
            
        instance_id = instances[0]
        logger.info(f"Testing on instance: {instance_id}")
        
        # Test 1: Basic Docker functionality
        logger.info("Test 1: Docker version check...")
        result = provider.execute_remote_command_sync(
            instance_id,
            "docker --version"
        )
        logger.info(f"Docker version: {result.stdout}")
        
        # Test 2: Container image availability
        logger.info("Test 2: Check container image...")
        result = provider.execute_remote_command_sync(
            instance_id,
            "docker images | grep parsl-base || echo 'IMAGE NOT FOUND'"
        )
        logger.info(f"Container images: {result.stdout}")
        
        # Test 3: Simple container execution test
        logger.info("Test 3: Simple container test...")
        simple_test = 'docker run --rm --network host parsl-base:latest python3 -c "import os; print(f\\"Container: {os.path.exists(\\"/\\.dockerenv\\")}\\")"'
        result = provider.execute_remote_command_sync(instance_id, simple_test)
        logger.info(f"Simple container test: {result.stdout}")
        
        # Test 4: Network connectivity from container
        logger.info("Test 4: Network test from container...")
        network_test = 'docker run --rm --network host parsl-base:latest python3 -c "import socket; s=socket.socket(); print(f\\"Can bind to localhost: {s.connect_ex((\\\"127.0.0.1\\\", 22)) == 0}\\")"'
        result = provider.execute_remote_command_sync(instance_id, network_test)
        logger.info(f"Network test: {result.stdout}")
        
        # Test 5: Parsl module availability in container
        logger.info("Test 5: Parsl module test...")
        parsl_test = 'docker run --rm --network host parsl-base:latest python3 -c "import parsl.executors.high_throughput.process_worker_pool; print(\\"Parsl worker module available\\")"'
        result = provider.execute_remote_command_sync(instance_id, parsl_test)
        logger.info(f"Parsl module test: {result.stdout}")
        
        return True
        
    except Exception as e:
        logger.error(f"Test failed: {e}")
        return False
    finally:
        try:
            provider.cancel([job_id])
        except:
            pass

if __name__ == "__main__":
    test_docker_command_directly()
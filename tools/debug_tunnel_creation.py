#!/usr/bin/env python3
"""Debug tunnel creation step by step."""

import sys, os
sys.path.insert(0, '.')
from phase15_enhanced import AWSProvider
import logging

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

def test_single_tunnel():
    """Test creating just one tunnel to see if it works."""
    
    provider = AWSProvider(
        region="us-east-1",
        instance_type="t3.micro", 
        init_blocks=0,
        max_blocks=1
    )
    
    # Create instance manually
    logger.info("Creating instance...")
    launch_config = provider._get_launch_config("debug-tunnel")
    response = provider.ec2.run_instances(MinCount=1, MaxCount=1, **launch_config)
    instance_id = response["Instances"][0]["InstanceId"]
    
    logger.info(f"Instance created: {instance_id}")
    provider.instances["debug-tunnel"] = instance_id
    
    # Wait for it to be ready
    provider._wait_for_instance_running(instance_id)
    
    # Install SSH key
    logger.info("Installing SSH key...")
    key_installed = provider.ssh_tunnel.install_ssh_key_on_instance(
        instance_id, provider.public_key_path
    )
    
    if not key_installed:
        logger.error("SSH key installation failed")
        return False
        
    # Try creating one tunnel
    logger.info("Creating single reverse tunnel...")
    tunnel = provider.ssh_tunnel.create_reverse_tunnel(
        instance_id, 54000, 54000, provider.private_key_path
    )
    
    if tunnel:
        logger.info("✅ Single tunnel created successfully")
        # Cleanup
        provider._cleanup_job_resources("debug-tunnel")
        return True
    else:
        logger.error("❌ Single tunnel creation failed")
        provider._cleanup_job_resources("debug-tunnel") 
        return False

if __name__ == "__main__":
    success = test_single_tunnel()
    print(f"Single tunnel test: {'SUCCESS' if success else 'FAILED'}")
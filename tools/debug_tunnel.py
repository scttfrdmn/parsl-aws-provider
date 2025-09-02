#!/usr/bin/env python3
"""Debug SSH tunnel establishment."""

import logging
import boto3
from ssh_reverse_tunnel import SSMSSHTunnel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_tunnel():
    """Test SSH tunnel creation directly."""
    
    instance_id = "i-0f344a2632ad83a8b"  # From the recent test
    
    logger.info(f"Testing SSH tunnel to {instance_id}")
    
    # Create tunnel manager
    tunnel_mgr = SSMSSHTunnel(
        session=boto3.Session(),
        region="us-east-1"
    )
    
    try:
        # Test tunnel creation
        logger.info("Creating reverse tunnel...")
        tunnel_mgr.create_reverse_tunnel(instance_id, 50000, 54177, "/Users/scttfrdmn/.ssh/parsl_ssm_rsa")
        logger.info("✅ Tunnel created successfully")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Tunnel failed: {e}")
        return False
    finally:
        try:
            tunnel_mgr.cleanup_tunnel(instance_id)
        except:
            pass

if __name__ == "__main__":
    test_tunnel()
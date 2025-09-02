#!/usr/bin/env python3
"""Debug containerized worker execution step by step."""

import sys, os
sys.path.insert(0, '.')
from phase15_enhanced import AWSProvider
import logging
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_worker_execution():
    """Test each step of worker execution."""
    
    provider = AWSProvider(
        region="us-east-1",
        instance_type="t3.medium",  # Use slightly larger instance
        init_blocks=0,
        max_blocks=1
    )
    
    # Step 1: Test basic connectivity
    test_command = "echo 'Basic test' && python3 --version && docker --version && whoami && date"
    
    try:
        logger.info("🔧 Step 1: Testing basic SSM connectivity...")
        job_id = provider.submit(test_command, "connectivity-test", "connectivity-test")
        logger.info(f"✅ Basic connectivity job submitted: {job_id}")
        
        # Wait longer for execution
        time.sleep(60)
        
        # Don't cleanup - leave instance running for manual inspection
        logger.info(f"✅ Test complete. Instance should be running for inspection.")
        return True
        
    except Exception as e:
        logger.error(f"💥 Test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = test_worker_execution()
    print(f"Worker execution test: {'SUCCESS' if success else 'FAILED'}")
    print("Check AWS console for running instance to inspect logs.")
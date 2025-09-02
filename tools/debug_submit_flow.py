#!/usr/bin/env python3
"""Debug the exact point where job submission fails."""

import sys, os
sys.path.insert(0, '.')
from phase15_enhanced import AWSProvider
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def debug_submit():
    """Debug step-by-step job submission."""
    
    provider = AWSProvider(
        region="us-east-1",
        instance_type="t3.micro", 
        init_blocks=0,
        max_blocks=1
    )
    
    # Test the submit method with a simple command
    test_command = "docker run --rm --network host python:3.10-slim python -m parsl.executors.high_throughput.process_worker_pool -a 127.0.0.1 --task_port=54000 --result_port=54001"
    
    try:
        logger.info("🔧 Testing job submission flow...")
        job_id = provider.submit(test_command, "debug-job", "debug-job")
        logger.info(f"✅ Job submitted successfully: {job_id}")
        
        # Check status
        import time
        time.sleep(30)
        status = provider.status([job_id])
        logger.info(f"📊 Job status: {status}")
        
        # Cleanup
        provider.cancel([job_id])
        
    except Exception as e:
        logger.error(f"💥 Job submission failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    debug_submit()
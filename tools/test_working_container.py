#!/usr/bin/env python3
"""Final comprehensive test of working containerized execution."""

import sys, os
sys.path.insert(0, '.')
from phase15_enhanced import AWSProvider
from container_executor import ContainerHighThroughputExecutor
import parsl
import logging
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_end_to_end_container():
    """Test end-to-end containerized execution with all fixes applied."""
    
    provider = AWSProvider(
        region="us-east-1",
        instance_type="t3.medium",
        init_blocks=1,  # Create 1 block immediately
        max_blocks=1
    )
    
    # Create executor with container that has minimal requirements
    executor = ContainerHighThroughputExecutor(
        label="e2e_container_test",
        container_image="python:3.10-slim",
        provider=provider,
        max_workers_per_node=1,
        worker_debug=True,
        # Remove logdir to avoid path issues - let it use default /tmp paths
        working_dir="/tmp"
    )
    
    config = parsl.Config(
        executors=[executor],
        strategy=None
    )
    
    try:
        logger.info("🔧 Starting end-to-end containerized test...")
        parsl.load(config)
        
        # Simple container test
        @parsl.python_app
        def prove_container_execution():
            """Prove we're running in a container."""
            import os
            import platform
            import subprocess
            
            # Multiple checks for container execution
            checks = {
                'dockerenv_exists': os.path.exists('/.dockerenv'),
                'hostname': platform.node(),
                'cgroup_docker': 'docker' in open('/proc/1/cgroup', 'r').read() if os.path.exists('/proc/1/cgroup') else False,
                'python_version': platform.python_version(),
                'pwd': os.getcwd(),
                'user': os.getenv('USER', os.getenv('USERNAME', 'unknown')),
                'container_id': subprocess.getoutput("cat /proc/self/cgroup | grep docker | head -1 | cut -d'/' -f3") if os.path.exists('/proc/self/cgroup') else 'none'
            }
            
            in_container = checks['dockerenv_exists'] or checks['cgroup_docker']
            checks['FINAL_RESULT'] = 'CONTAINER_SUCCESS' if in_container else 'HOST_EXECUTION'
            
            print(f"🔍 EXECUTION ENVIRONMENT: {checks}")
            return checks
        
        logger.info("📋 Submitting end-to-end container test...")
        future = prove_container_execution()
        
        # Wait with detailed monitoring
        for i in range(20):  # Wait up to 200 seconds
            if future.done():
                result = future.result()
                logger.info(f"✅ Task completed: {result}")
                
                if result.get('FINAL_RESULT') == 'CONTAINER_SUCCESS':
                    logger.info("🎉 SUCCESS: End-to-end containerized execution working!")
                    return True
                else:
                    logger.error(f"❌ FAILURE: Task executed on host, not container: {result}")
                    return False
            
            # Check executor status
            status = executor.status()
            logger.info(f"⏱️  Waiting... Executor status: {status}")
            time.sleep(10)
        
        logger.error("⏰ Timeout: Task did not complete in time")
        return False
        
    except Exception as e:
        logger.error(f"💥 Test failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    finally:
        try:
            parsl.dfk().cleanup()
            parsl.clear()
        except:
            pass

if __name__ == "__main__":
    success = test_end_to_end_container()
    print(f"End-to-end containerized test: {'SUCCESS' if success else 'FAILED'}")
    print("Check AWS console for running instance to inspect container logs.")
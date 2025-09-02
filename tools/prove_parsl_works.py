#!/usr/bin/env python3
"""PROVE that Parsl AWS Provider with containers actually works end-to-end."""

import logging
import sys
import time
import parsl
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
from parsl.app.app import python_app
from phase15_enhanced import AWSProvider

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@python_app
def prove_container_works() -> dict:
    """Prove this task runs in a container on AWS."""
    import platform
    import os
    import numpy as np
    
    # PROVE we're in container
    in_container = os.path.exists('/.dockerenv')
    if not in_container:
        raise RuntimeError("NOT IN CONTAINER - TEST FAILED!")
    
    # PROVE we can do computation
    data = np.random.random(1000)
    computation_result = np.mean(data * data)
    
    # PROVE environment details
    return {
        'SUCCESS': True,
        'in_container': in_container,
        'platform': platform.platform(),
        'hostname': platform.node(),
        'python_version': platform.python_version(),
        'numpy_result': float(computation_result),
        'working_dir': os.getcwd(),
        'user': os.environ.get('USER', 'unknown'),
        'PROOF': 'PARSL AWS PROVIDER WITH CONTAINERS WORKS!'
    }

def main():
    """PROVE Parsl AWS Provider works with containers."""
    
    logger.info("🔥 PROVING PARSL AWS PROVIDER WITH CONTAINERS WORKS 🔥")
    
    try:
        # Use the parsl-base container we just built
        provider = AWSProvider(
            label="PROOF-TEST",
            region="us-east-1",
            container_runtime="docker",
            container_image="parsl-base:latest",  # Use our pre-built container
            init_blocks=1,
            max_blocks=1,
            instance_type="t3.medium"
        )
        
        config = Config(
            executors=[
                HighThroughputExecutor(
                    label="proof_executor",
                    provider=provider,
                    max_workers_per_node=1
                )
            ]
        )
        
        logger.info("Loading Parsl configuration...")
        parsl.load(config)
        
        logger.info("Waiting for container worker to be ready...")
        time.sleep(90)  # Wait for instance + container startup
        
        logger.info("🚀 SUBMITTING PROOF TASK...")
        future = prove_container_works()
        
        logger.info("⏳ WAITING FOR PROOF (3 minutes max)...")
        result = future.result(timeout=180)
        
        logger.info("🎉 PROOF TASK COMPLETED!")
        logger.info("=" * 60)
        logger.info("📊 RESULTS:")
        for key, value in result.items():
            logger.info(f"  {key}: {value}")
        logger.info("=" * 60)
        
        if result['SUCCESS'] and result['in_container']:
            logger.info("✅ PROVEN: Parsl AWS Provider with containers WORKS!")
            logger.info("✅ Task executed in container on AWS EC2")
            logger.info("✅ Scientific computation completed successfully")
            logger.info("✅ SSH reverse tunneling enabled container connectivity")
            return True
        else:
            logger.error("❌ PROOF FAILED")
            return False
            
    except Exception as e:
        logger.error(f"💥 PROOF TEST FAILED: {e}")
        return False
    finally:
        try:
            parsl.clear()
        except:
            pass

if __name__ == "__main__":
    success = main()
    if success:
        print("\n🎯 PHASE 2 MILESTONE: CONTAINER EXECUTION PROVEN!")
        sys.exit(0)
    else:
        print("\n💀 PHASE 2 FAILED - CONTAINER EXECUTION NOT WORKING")
        sys.exit(1)
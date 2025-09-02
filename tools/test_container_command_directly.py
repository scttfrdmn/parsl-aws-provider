#!/usr/bin/env python3
"""Test container command execution directly via SSM."""

import sys, os
sys.path.insert(0, '.')
from phase15_enhanced import AWSProvider  
import time

def test_container_command_directly():
    """Test the exact container command on AWS instance."""
    
    provider = AWSProvider(
        region="us-east-1",
        instance_type="t3.medium", 
        init_blocks=0,  # Don't auto-scale
        max_blocks=1
    )
    
    # Manually create instance
    provider._ensure_aws_resources()
    
    try:
        print("🚀 Creating instance manually...")
        response = provider.ec2.run_instances(
            ImageId=provider.image_id,
            MinCount=1,
            MaxCount=1,
            InstanceType=provider.instance_type,
            SecurityGroupIds=[provider.security_group_id],
            IamInstanceProfile={'Name': provider.iam_instance_profile},
            TagSpecifications=[{
                'ResourceType': 'instance',
                'Tags': [
                    {'Key': 'Name', 'Value': f'debug-container-test'},
                    {'Key': 'parsl_provider', 'Value': 'aws-enhanced'}
                ]
            }]
        )
        
        instance_id = response['Instances'][0]['InstanceId']
        print(f"✅ Instance created: {instance_id}")
        
        # Wait for instance to be ready for SSM
        print("⏳ Waiting for instance to be ready...")
        waiter = provider.ec2.get_waiter('instance_running')
        waiter.wait(InstanceIds=[instance_id])
        
        # Additional wait for SSM agent
        time.sleep(60)
        
        # Test exact container command
        container_command = """
# Create directory structure
mkdir -p /Users/scttfrdmn/src/parsl-aws-provider/runinfo/030/fixed_container

# Test Docker directly
echo "=== TESTING DOCKER ==="
docker --version
docker ps

# Test container command with simple output
echo "=== TESTING CONTAINER EXECUTION ==="
docker run --rm python:3.10-slim python3 -c "import os; print('Container test:', os.path.exists('/.dockerenv'))"

# Test the actual Parsl command but with simple verification
echo "=== TESTING PARSL COMMAND PARTS ==="
docker run --rm python:3.10-slim bash -c "pip install --no-cache-dir parsl && python3 -c 'import parsl; print(\"Parsl available:\", parsl.__version__)'"

echo "=== TESTING FULL PARSL WORKER COMMAND (5 second timeout) ==="
timeout 5s docker run -v /tmp:/tmp -e PYTHONUNBUFFERED=1 --rm --network host -v /Users/scttfrdmn/src/parsl-aws-provider/runinfo/030:/Users/scttfrdmn/src/parsl-aws-provider/runinfo/030 -t python:3.10-slim bash -c 'pip install --no-cache-dir parsl && exec python3 -m parsl.executors.high_throughput.process_worker_pool --debug --max_workers_per_node=1 -a 127.0.0.1 -p 0 -c 1.0 -m None --poll 10 --port=54846 --cert_dir None --logdir=/Users/scttfrdmn/src/parsl-aws-provider/runinfo/030/fixed_container --block_id=0 --hb_period=30 --hb_threshold=120 --drain_period=None --cpu-affinity none --mpi-launcher=mpiexec --available-accelerators' || echo "Command completed or timed out"

echo "=== DONE ==="
"""
        
        print("📋 Sending container test command...")
        command_id = provider.ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={'commands': [container_command]}
        )['Command']['CommandId']
        
        print(f"Command ID: {command_id}")
        
        # Wait for command completion
        print("⏳ Waiting for command execution...")
        time.sleep(90)
        
        # Get results
        try:
            response = provider.ssm_client.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id
            )
            
            print("\n" + "=" * 60)
            print("COMMAND OUTPUT")
            print("=" * 60)
            print(response.get('StandardOutputContent', 'No output'))
            
            if response.get('StandardErrorContent'):
                print("\n" + "=" * 60)
                print("COMMAND ERRORS")
                print("=" * 60)
                print(response.get('StandardErrorContent'))
                
        except Exception as e:
            print(f"❌ Failed to get command output: {e}")
        
        print(f"\n🔧 Instance {instance_id} kept alive for manual inspection")
        print(f"Connect with: aws ssm start-session --target {instance_id}")
        
        return instance_id
        
    except Exception as e:
        print(f"❌ ERROR: {e}")
        return None

if __name__ == "__main__":
    instance_id = test_container_command_directly()
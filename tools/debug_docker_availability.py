#!/usr/bin/env python3
"""Debug Docker availability on AWS instance."""

import sys, os
sys.path.insert(0, '.')
from phase15_enhanced import AWSProvider  
import time

def debug_docker_on_aws():
    """Check if Docker is available and working on AWS instance."""
    
    provider = AWSProvider(
        region="us-east-1",
        instance_type="t3.medium", 
        init_blocks=1,
        max_blocks=1
    )
    
    try:
        # Use provider's infrastructure to launch instance
        job_id = provider.submit("echo 'Testing Docker availability'", 1, 1)[0]
        print(f"Job submitted: {job_id}")
        
        # Wait for instance to be ready
        time.sleep(60)
        
        # Get the instance ID from provider's job map
        instance_id = None
        for job, details in provider.job_map.items():
            if job == job_id:
                instance_id = details['instance_id']
                break
        
        print(f"Instance ID: {instance_id}")
        
        if instance_id:
            # Test Docker availability via SSM
            test_commands = [
                "docker --version",
                "docker ps",
                "docker pull hello-world",
                "docker run --rm hello-world",
                "python3 --version",
                "which python3"
            ]
            
            for cmd in test_commands:
                print(f"\n📋 Testing command: {cmd}")
                command_id = provider.ssm_client.send_command(
                    InstanceIds=[instance_id],
                    DocumentName="AWS-RunShellScript",
                    Parameters={'commands': [cmd]}
                )['Command']['CommandId']
                
                # Wait for command completion
                time.sleep(10)
                
                try:
                    response = provider.ssm_client.get_command_invocation(
                        CommandId=command_id,
                        InstanceId=instance_id
                    )
                    
                    print(f"✅ STDOUT: {response.get('StandardOutputContent', 'No output')}")
                    if response.get('StandardErrorContent'):
                        print(f"❌ STDERR: {response.get('StandardErrorContent')}")
                        
                except Exception as e:
                    print(f"❌ Command failed: {e}")
        
        return True
        
    except Exception as e:
        print(f"❌ ERROR: {e}")
        return False
        
    finally:
        # Keep instance alive for manual inspection
        print(f"\n🔧 Instance {instance_id} kept alive for manual inspection")
        print("Connect with: aws ssm start-session --target", instance_id)

if __name__ == "__main__":
    debug_docker_on_aws()
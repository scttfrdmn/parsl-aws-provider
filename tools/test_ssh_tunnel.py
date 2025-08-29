#!/usr/bin/env python3
"""Test SSH reverse tunneling functionality."""

import boto3
import logging
import time
from ssh_reverse_tunnel import SSMSSHTunnel

# Set up logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def test_ssh_tunnel():
    """Test SSH tunnel creation and reverse port forwarding."""
    try:
        # Create AWS session
        session = boto3.Session(region_name="us-east-1", profile_name="aws")
        ec2 = session.client("ec2")

        # Create SSH tunnel manager
        ssh_tunnel = SSMSSHTunnel(session, "us-east-1")

        # Set up SSH config
        ssh_tunnel.setup_ssh_config()

        # Generate SSH keys
        private_key_path, public_key_path = ssh_tunnel.generate_ssh_key()
        logger.info(f"SSH keys: {private_key_path}, {public_key_path}")

        # Launch test instance
        logger.info("Launching test instance...")
        config = get_config()
        ami_id = config.get_base_ami("us-east-1")
        instance_profile = (
            "AmazonSSMRoleForInstancesQuickSetup"  # Use known working profile
        )

        response = ec2.run_instances(
            ImageId=ami_id,
            InstanceType="t3.micro",
            MinCount=1,
            MaxCount=1,
            IamInstanceProfile={"Name": instance_profile},
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "Name", "Value": "ssh-tunnel-test"},
                        {"Key": "CreatedBy", "Value": "parsl-ssh-tunnel-test"},
                    ],
                }
            ],
        )

        instance_id = response["Instances"][0]["InstanceId"]
        logger.info(f"Instance launched: {instance_id}")

        # Wait for instance to be running
        logger.info("Waiting for instance to be running...")
        waiter = ec2.get_waiter("instance_running")
        waiter.wait(InstanceIds=[instance_id])
        logger.info("Instance is running")

        # Install SSH key
        logger.info("Installing SSH key...")
        key_installed = ssh_tunnel.install_ssh_key_on_instance(
            instance_id, public_key_path
        )

        if key_installed:
            logger.info("✅ SSH key installation successful")

            # Test SSH connection
            logger.info("Testing SSH connection...")
            import subprocess

            ssh_test_cmd = [
                "ssh",
                "-i",
                private_key_path,
                "-o",
                "ConnectTimeout=10",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                instance_id,
                "echo 'SSH connection successful'",
            ]

            result = subprocess.run(
                ssh_test_cmd, capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                logger.info("✅ SSH connection test successful")
                logger.info(f"SSH output: {result.stdout}")

                # Test reverse tunnel creation
                logger.info("Testing reverse tunnel creation...")
                tunnel_proc = ssh_tunnel.create_reverse_tunnel(
                    instance_id, 54321, 55321, private_key_path
                )

                if tunnel_proc:
                    logger.info("✅ Reverse tunnel creation successful")

                    # Test tunnel connectivity
                    time.sleep(5)
                    tunnel_test_cmd = [
                        "ssh",
                        "-i",
                        private_key_path,
                        "-o",
                        "ConnectTimeout=10",
                        instance_id,
                        "nc -z localhost 55321 && echo 'Tunnel port accessible' || echo 'Tunnel port not accessible'",
                    ]

                    tunnel_result = subprocess.run(
                        tunnel_test_cmd, capture_output=True, text=True, timeout=30
                    )
                    logger.info(f"Tunnel test result: {tunnel_result.stdout}")

                    # Clean up tunnel
                    ssh_tunnel.cleanup_tunnel(instance_id)
                else:
                    logger.error("❌ Reverse tunnel creation failed")

            else:
                logger.error(f"❌ SSH connection test failed: {result.stderr}")
        else:
            logger.error("❌ SSH key installation failed")

    except Exception as e:
        logger.error(f"Test failed: {e}")

    finally:
        # Cleanup instance
        try:
            logger.info(f"Terminating test instance {instance_id}")
            ec2.terminate_instances(InstanceIds=[instance_id])
        except:
            pass


if __name__ == "__main__":
    from config_loader import get_config

    test_ssh_tunnel()

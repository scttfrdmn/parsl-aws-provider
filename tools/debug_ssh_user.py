#!/usr/bin/env python3
"""Debug SSH user and key installation."""

import boto3
import logging
import time

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def debug_ssh_setup():
    """Debug SSH user and key setup."""
    try:
        session = boto3.Session(region_name="us-east-1", profile_name="aws")
        ec2 = session.client("ec2")
        ssm = session.client("ssm")

        # Launch instance
        from config_loader import get_config

        config = get_config()
        ami_id = config.get_base_ami("us-east-1")

        response = ec2.run_instances(
            ImageId=ami_id,
            InstanceType="t3.micro",
            MinCount=1,
            MaxCount=1,
            IamInstanceProfile={"Name": "AmazonSSMRoleForInstancesQuickSetup"},
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "Name", "Value": "ssh-debug-test"},
                        {"Key": "CreatedBy", "Value": "parsl-ssh-debug-test"},
                    ],
                }
            ],
        )

        instance_id = response["Instances"][0]["InstanceId"]
        logger.info(f"Instance: {instance_id}")

        # Wait for running
        waiter = ec2.get_waiter("instance_running")
        waiter.wait(InstanceIds=[instance_id])

        # Wait for SSM agent
        time.sleep(60)

        # Check what users exist
        user_check_cmd = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={
                "commands": [
                    "whoami",
                    "id",
                    "ls -la /home/",
                    'cat /etc/passwd | grep -E "ec2-user|ubuntu|admin"',
                ]
            },
        )

        command_id = user_check_cmd["Command"]["CommandId"]
        time.sleep(10)

        result = ssm.get_command_invocation(
            CommandId=command_id, InstanceId=instance_id
        )
        logger.info(f"User check output:\n{result['StandardOutputContent']}")
        if result.get("StandardErrorContent"):
            logger.info(f"User check errors:\n{result['StandardErrorContent']}")

    except Exception as e:
        logger.error(f"Debug failed: {e}")
    finally:
        try:
            ec2.terminate_instances(InstanceIds=[instance_id])
            logger.info(f"Terminated {instance_id}")
        except:
            pass


if __name__ == "__main__":
    debug_ssh_setup()

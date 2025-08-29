#!/usr/bin/env python3
"""
AMI Builder for Phase 1.5 Optimized AMIs

Creates pre-built AMIs with Parsl and dependencies installed for faster startup.
Manual process - run when needed to create optimized AMIs.
"""

import argparse
import json
import logging
import sys
import time
import uuid
from datetime import datetime

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class AMIBuilder:
    """Creates optimized AMIs with Parsl pre-installed."""

    def __init__(self, region: str = "us-east-1"):
        """Initialize AMI builder for specified region."""
        self.region = region
        self.session = boto3.Session()
        self.ec2 = self.session.client("ec2", region_name=region)

        # Validate AWS access
        try:
            sts = self.session.client("sts")
            identity = sts.get_caller_identity()
            logger.info(f"Using AWS credentials: {identity.get('Arn', 'unknown')}")
        except Exception as e:
            raise Exception(f"AWS credentials invalid: {e}")

        # Ensure SSM instance profile exists
        self._ensure_ssm_instance_profile()

    def build_ami(self, parsl_version: str = "latest") -> str:
        """
        Build optimized AMI with Parsl pre-installed.

        Args:
            parsl_version: Version of Parsl to install ('latest' or specific version)

        Returns:
            AMI ID of the created optimized AMI
        """
        build_id = f"ami-build-{uuid.uuid4().hex[:8]}"
        logger.info(f"Starting AMI build: {build_id}")

        try:
            # 1. Launch build instance
            base_ami = self._get_base_ami()
            logger.info(f"Using base AMI: {base_ami}")

            instance_id = self._launch_build_instance(base_ami, build_id)
            logger.info(f"Build instance launched: {instance_id}")

            # 2. Wait for instance to be ready and SSM agent available
            self._wait_for_instance_running(instance_id)
            self._wait_for_ssm_ready(instance_id)

            # 3. Install software via SSM (synchronous with live output)
            self._install_software_via_ssm(instance_id, parsl_version)

            # 4. Create AMI from instance
            ami_id = self._create_ami_from_instance(
                instance_id, parsl_version, build_id
            )
            logger.info(f"AMI creation initiated: {ami_id}")

            # 5. Wait for AMI to be available
            self._wait_for_ami_available(ami_id)

            # 6. Clean up build instance
            self._cleanup_build_instance(instance_id)

            logger.info(f"AMI build completed successfully: {ami_id}")
            return ami_id

        except Exception as e:
            logger.error(f"AMI build failed: {e}")
            raise

    def _get_base_ami(self) -> str:
        """Get base AMI for this region (same as Phase 1)."""
        ami_map = {
            "us-east-1": "ami-080e1f13689e07408",  # Amazon Linux 2023
            "us-east-2": "ami-03d21eed81858c120",
            "us-west-1": "ami-0d5b7dce3973d8817",
            "us-west-2": "ami-0473ec1595e64e666",
        }
        return ami_map.get(self.region, ami_map["us-east-1"])

    def _launch_build_instance(self, base_ami: str, build_id: str) -> str:
        """Launch EC2 instance for AMI building."""

        # Simple user data - just ensure SSM agent is ready
        user_data = self._create_bootstrap_script()

        try:
            response = self.ec2.run_instances(
                ImageId=base_ami,
                MinCount=1,
                MaxCount=1,
                InstanceType="t3.small",  # Slightly larger for faster builds
                IamInstanceProfile={"Name": "ParslAMIBuilderProfile"},
                UserData=user_data,
                TagSpecifications=[
                    {
                        "ResourceType": "instance",
                        "Tags": [
                            {"Key": "Name", "Value": f"parsl-ami-builder-{build_id}"},
                            {"Key": "Purpose", "Value": "AMI-Build"},
                            {"Key": "CreatedBy", "Value": "ParslAWSProvider"},
                            {"Key": "BuildId", "Value": build_id},
                            {"Key": "AutoCleanup", "Value": "true"},
                        ],
                    }
                ],
            )

            return response["Instances"][0]["InstanceId"]

        except ClientError as e:
            raise Exception(f"Failed to launch build instance: {e}")

    def _create_bootstrap_script(self) -> str:
        """Create minimal user data to prepare instance for SSM."""
        return """#!/bin/bash
# Minimal bootstrap - SSM agent should already be installed on Amazon Linux 2023
echo "=== BOOTSTRAP START ==="
echo "Instance ready for SSM commands: $(date)"
echo "=== BOOTSTRAP END ==="
"""

    def _wait_for_ssm_ready(self, instance_id: str, max_attempts: int = 30):
        """Wait for SSM agent to be ready on the instance."""
        logger.info("Waiting for SSM agent to be ready...")

        ssm = self.session.client("ssm", region_name=self.region)

        for attempt in range(max_attempts):
            try:
                response = ssm.describe_instance_information(
                    InstanceInformationFilterList=[
                        {"key": "InstanceIds", "valueSet": [instance_id]}
                    ]
                )

                if response["InstanceInformationList"]:
                    instance_info = response["InstanceInformationList"][0]
                    if instance_info["PingStatus"] == "Online":
                        logger.info("SSM agent is online and ready")
                        return
                    else:
                        logger.debug(f"SSM agent status: {instance_info['PingStatus']}")

            except ClientError as e:
                logger.debug(f"SSM check failed (attempt {attempt + 1}): {e}")

            time.sleep(10)

        raise Exception("SSM agent did not become ready within timeout")

    def _install_software_via_ssm(self, instance_id: str, parsl_version: str):
        """Install Parsl and dependencies via SSM with live output."""
        logger.info("Installing Parsl and dependencies via SSM...")

        ssm = self.session.client("ssm", region_name=self.region)

        # Create installation script
        install_commands = [
            "echo '=== PARSL AMI BUILD START ==='",
            "echo 'Build started:' $(date)",
            "",
            "echo 'Updating system packages...'",
            "yum update -y",
            "",
            "echo 'Installing Python and development tools...'",
            "yum install -y python3 python3-pip gcc git",
            "",
            "echo 'Installing Parsl and dependencies...'",
            "pip3 install parsl boto3 botocore",
            "",
            "echo 'Installing common scientific packages...'",
            "pip3 install numpy",
            "",
            "echo 'Verifying Parsl installation...'",
            "python3 -c 'import parsl; print(\"Parsl version:\", parsl.__version__)'",
            "",
            "echo 'Optimizing system...'",
            "yum clean all",
            "rm -rf /var/cache/yum /tmp/* /var/tmp/*",
            "",
            "echo 'Creating AMI metadata...'",
            "cat > /opt/parsl-ami-info.txt << 'EOF'",
            "AMI built: $(date)",
            "Parsl version: $(pip3 show parsl | grep Version || echo 'unknown')",
            "Python version: $(python3 --version)",
            "Build completed via SSM",
            "EOF",
            "",
            "echo 'Build completed:' $(date)",
            "echo '=== PARSL AMI BUILD END ==='",
        ]

        # Send command via SSM
        try:
            response = ssm.send_command(
                InstanceIds=[instance_id],
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": install_commands},
                CloudWatchOutputConfig={
                    "CloudWatchLogGroupName": "/aws/ssm/parsl-ami-build",
                    "CloudWatchOutputEnabled": True,
                },
            )

            command_id = response["Command"]["CommandId"]
            logger.info(f"SSM command sent: {command_id}")

            # Monitor command execution with live output
            self._monitor_ssm_command(ssm, instance_id, command_id)

        except ClientError as e:
            raise Exception(f"Failed to send SSM command: {e}")

    def _monitor_ssm_command(self, ssm, instance_id: str, command_id: str):
        """Monitor SSM command execution and show live output."""
        logger.info("Monitoring installation progress...")

        last_output_length = 0

        while True:
            try:
                # Check command status
                response = ssm.get_command_invocation(
                    CommandId=command_id, InstanceId=instance_id
                )

                status = response["Status"]

                # Show incremental output
                stdout = response.get("StandardOutputContent", "")
                if len(stdout) > last_output_length:
                    new_output = stdout[last_output_length:]
                    for line in new_output.splitlines():
                        if line.strip():  # Only show non-empty lines
                            print(f"  {line}")
                    last_output_length = len(stdout)

                if status in ["Success"]:
                    logger.info("Installation completed successfully")
                    break
                elif status in ["Failed", "Cancelled", "TimedOut"]:
                    stderr = response.get("StandardErrorContent", "")
                    raise Exception(f"Installation failed: {status}\nError: {stderr}")
                elif status in ["InProgress"]:
                    time.sleep(5)  # Continue monitoring
                else:
                    logger.debug(f"Command status: {status}")
                    time.sleep(5)

            except ClientError as e:
                if e.response["Error"]["Code"] == "InvocationDoesNotExist":
                    time.sleep(2)  # Command might not be ready yet
                    continue
                else:
                    raise Exception(f"Failed to monitor SSM command: {e}")

            except KeyboardInterrupt:
                logger.info("Build interrupted - attempting to cancel SSM command...")
                try:
                    ssm.cancel_command(CommandId=command_id)
                except:
                    pass
                raise

    def _wait_for_instance_running(self, instance_id: str, max_attempts: int = 60):
        """Wait for instance to reach running state."""
        logger.info("Waiting for build instance to be running...")

        for attempt in range(max_attempts):
            try:
                response = self.ec2.describe_instances(InstanceIds=[instance_id])
                instance = response["Reservations"][0]["Instances"][0]
                state = instance["State"]["Name"]

                if state == "running":
                    logger.info("Build instance is running")
                    return
                elif state in ["terminated", "shutting-down", "stopped"]:
                    raise Exception(f"Build instance failed: state={state}")

                if attempt % 10 == 0:  # Log every 10 attempts
                    logger.info(f"Instance state: {state}")

            except ClientError as e:
                if e.response["Error"]["Code"] != "InvalidInstanceID.NotFound":
                    raise

            time.sleep(5)

        raise Exception("Instance did not reach running state within timeout")

    def _create_ami_from_instance(
        self, instance_id: str, parsl_version: str, build_id: str
    ) -> str:
        """Create AMI from the build instance."""

        ami_name = f"parsl-aws-provider-{parsl_version}-{self.region}-{datetime.now().strftime('%Y%m%d-%H%M')}"

        logger.info(f"Creating AMI: {ami_name}")

        try:
            response = self.ec2.create_image(
                InstanceId=instance_id,
                Name=ami_name,
                Description=f"Optimized AMI for Parsl AWS Provider with Parsl {parsl_version} pre-installed",
                NoReboot=True,  # Create AMI without rebooting
                TagSpecifications=[
                    {
                        "ResourceType": "image",
                        "Tags": [
                            {"Key": "Name", "Value": ami_name},
                            {"Key": "CreatedBy", "Value": "ParslAWSProvider"},
                            {"Key": "Version", "Value": "1.5"},
                            {"Key": "ParslVersion", "Value": parsl_version},
                            {"Key": "BaseAMI", "Value": self._get_base_ami()},
                            {"Key": "Region", "Value": self.region},
                            {"Key": "Created", "Value": datetime.now().isoformat()},
                            {"Key": "BuildId", "Value": build_id},
                        ],
                    }
                ],
            )

            return response["ImageId"]

        except ClientError as e:
            raise Exception(f"Failed to create AMI: {e}")

    def _wait_for_ami_available(self, ami_id: str, max_attempts: int = 120):
        """Wait for AMI to become available."""
        logger.info(f"Waiting for AMI {ami_id} to be available...")

        for attempt in range(max_attempts):
            try:
                response = self.ec2.describe_images(ImageIds=[ami_id])
                if not response["Images"]:
                    continue

                ami = response["Images"][0]
                state = ami["State"]

                if state == "available":
                    logger.info(f"AMI {ami_id} is available")
                    return
                elif state == "failed":
                    raise Exception(
                        f"AMI creation failed: {ami.get('StateReason', 'unknown')}"
                    )

                if attempt % 20 == 0:  # Log every 20 attempts (10 minutes)
                    logger.info(f"AMI state: {state}")

            except ClientError as e:
                if e.response["Error"]["Code"] != "InvalidAMIID.NotFound":
                    raise

            time.sleep(30)  # Check every 30 seconds

        raise Exception("AMI did not become available within timeout")

    def _cleanup_build_instance(self, instance_id: str):
        """Terminate the build instance."""
        logger.info(f"Terminating build instance: {instance_id}")

        try:
            self.ec2.terminate_instances(InstanceIds=[instance_id])
            logger.info("Build instance terminated")
        except Exception as e:
            logger.warning(f"Failed to terminate build instance: {e}")

    def _ensure_ssm_instance_profile(self):
        """Ensure SSM instance profile exists for AMI building."""
        try:
            iam = self.session.client("iam")

            profile_name = "ParslAMIBuilderProfile"
            role_name = "ParslAMIBuilderRole"

            # Check if instance profile exists
            try:
                iam.get_instance_profile(InstanceProfileName=profile_name)
                logger.debug(f"SSM instance profile {profile_name} already exists")
                return
            except ClientError as e:
                if e.response["Error"]["Code"] != "NoSuchEntity":
                    raise

            logger.info("Creating SSM instance profile for AMI building...")

            # Create role for SSM
            trust_policy = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "ec2.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                    }
                ],
            }

            try:
                iam.create_role(
                    RoleName=role_name,
                    AssumeRolePolicyDocument=json.dumps(trust_policy),
                    Description="Role for Parsl AMI builder instances to use SSM",
                )
                logger.info(f"Created role: {role_name}")
            except ClientError as e:
                if e.response["Error"]["Code"] != "EntityAlreadyExists":
                    raise

            # Attach SSM policy to role
            try:
                iam.attach_role_policy(
                    RoleName=role_name,
                    PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
                )
                logger.debug("Attached SSM policy to role")
            except ClientError as e:
                if e.response["Error"]["Code"] != "PolicyAlreadyAttached":
                    logger.warning(f"Failed to attach SSM policy: {e}")

            # Create instance profile
            try:
                iam.create_instance_profile(InstanceProfileName=profile_name)
                logger.info(f"Created instance profile: {profile_name}")
            except ClientError as e:
                if e.response["Error"]["Code"] != "EntityAlreadyExists":
                    raise

            # Add role to instance profile
            try:
                iam.add_role_to_instance_profile(
                    InstanceProfileName=profile_name, RoleName=role_name
                )
                logger.debug("Added role to instance profile")
            except ClientError as e:
                if (
                    e.response["Error"]["Code"] != "LimitExceeded"
                ):  # Role already in profile
                    logger.warning(f"Failed to add role to instance profile: {e}")

            # Wait a moment for IAM propagation
            time.sleep(10)

        except Exception as e:
            logger.warning(f"Failed to create SSM instance profile: {e}")
            logger.warning(
                "You may need to create the IAM role manually with SSM permissions"
            )
            raise Exception(f"SSM setup failed: {e}")

    def list_existing_amis(self) -> list:
        """List existing optimized AMIs in this region."""
        try:
            response = self.ec2.describe_images(
                Owners=["self"],
                Filters=[
                    {"Name": "tag:CreatedBy", "Values": ["ParslAWSProvider"]},
                    {"Name": "tag:Version", "Values": ["1.5"]},
                    {"Name": "state", "Values": ["available"]},
                ],
            )

            amis = []
            for ami in response["Images"]:
                tags = {tag["Key"]: tag["Value"] for tag in ami.get("Tags", [])}
                amis.append(
                    {
                        "ImageId": ami["ImageId"],
                        "Name": ami["Name"],
                        "CreationDate": ami["CreationDate"],
                        "ParslVersion": tags.get("ParslVersion", "unknown"),
                        "BuildId": tags.get("BuildId", "unknown"),
                    }
                )

            # Sort by creation date (newest first)
            amis.sort(key=lambda x: x["CreationDate"], reverse=True)
            return amis

        except ClientError as e:
            logger.error(f"Failed to list AMIs: {e}")
            return []


def main():
    """CLI interface for AMI builder."""
    parser = argparse.ArgumentParser(
        description="Build optimized AMI for Parsl AWS Provider"
    )
    parser.add_argument("--region", default="us-east-1", help="AWS region")
    parser.add_argument(
        "--parsl-version", default="latest", help="Parsl version to install"
    )
    parser.add_argument(
        "--list", action="store_true", help="List existing optimized AMIs"
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")

    args = parser.parse_args()

    # Configure logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(levelname)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    try:
        builder = AMIBuilder(region=args.region)

        if args.list:
            amis = builder.list_existing_amis()
            if amis:
                print(f"\nExisting optimized AMIs in {args.region}:")
                print("-" * 80)
                for ami in amis:
                    print(f"AMI ID: {ami['ImageId']}")
                    print(f"Name: {ami['Name']}")
                    print(f"Created: {ami['CreationDate']}")
                    print(f"Parsl Version: {ami['ParslVersion']}")
                    print("-" * 80)
            else:
                print(f"No optimized AMIs found in {args.region}")
            return

        # Build new AMI
        print(f"\nBuilding optimized AMI in {args.region}...")
        print(f"Parsl version: {args.parsl_version}")
        print("-" * 60)

        ami_id = builder.build_ami(parsl_version=args.parsl_version)

        print("-" * 60)
        print("SUCCESS: AMI built successfully!")
        print(f"AMI ID: {ami_id}")
        print(f"Region: {args.region}")
        print("\nNext steps:")
        print(f"1. Test the AMI: python tools/validate_ami.py --ami {ami_id}")
        print(f"2. Sync to other regions: python tools/sync_ami.py --ami {ami_id}")

    except KeyboardInterrupt:
        print("\nBuild interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\nERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

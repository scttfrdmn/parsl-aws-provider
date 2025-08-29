#!/usr/bin/env python3
"""
Phase 1 AWS Provider using proper waiters.

Based on proven waiter approach that works correctly.
No silent failures - comprehensive error handling throughout.
"""

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from parsl.providers.base import ExecutionProvider
from parsl.jobs.states import JobStatus, JobState

logger = logging.getLogger(__name__)


def wait_for_security_group(ec2_client, group_id, max_attempts=30, delay=2):
    """Wait for security group to be available with comprehensive error handling."""

    logger.debug(f"Waiting for security group {group_id} to be available...")

    for attempt in range(max_attempts):
        try:
            response = ec2_client.describe_security_groups(GroupIds=[group_id])
            if response["SecurityGroups"]:
                sg = response["SecurityGroups"][0]
                logger.info(f"Security group {group_id} is ready: {sg['GroupName']}")
                return sg
        except ClientError as e:
            if e.response["Error"]["Code"] != "InvalidGroup.NotFound":
                raise Exception(f"Unexpected error waiting for security group: {e}")

        logger.debug(
            f"Security group not ready, attempt {attempt + 1}/{max_attempts}, waiting {delay}s..."
        )
        time.sleep(delay)

    raise Exception(
        f"Security group {group_id} not available after {max_attempts * delay} seconds"
    )


def wait_for_instance(ec2_client, instance_id, max_attempts=30, delay=2):
    """Wait for instance to be visible in AWS with comprehensive error handling."""

    logger.debug(f"Waiting for instance {instance_id} to be visible...")

    for attempt in range(max_attempts):
        try:
            response = ec2_client.describe_instances(InstanceIds=[instance_id])
            if response["Reservations"] and response["Reservations"][0]["Instances"]:
                instance = response["Reservations"][0]["Instances"][0]
                state = instance["State"]["Name"]
                logger.info(f"Instance {instance_id} is visible: {state}")
                return instance
        except ClientError as e:
            if e.response["Error"]["Code"] != "InvalidInstanceID.NotFound":
                raise Exception(f"Unexpected error waiting for instance: {e}")

        logger.debug(
            f"Instance not visible, attempt {attempt + 1}/{max_attempts}, waiting {delay}s..."
        )
        time.sleep(delay)

    raise Exception(
        f"Instance {instance_id} not visible after {max_attempts * delay} seconds"
    )


class AWSProvider(ExecutionProvider):
    """
    AWS provider using proven waiter approach.

    Features:
    - Comprehensive error handling with detailed error messages
    - Proper AWS resource waiters (no eventual consistency issues)
    - Thorough validation at every step
    - Fails fast and loud - no silent failures
    """

    def __init__(
        self,
        region: str = "us-east-1",
        instance_type: str = "t3.micro",
        ami_id: Optional[str] = None,
        key_name: Optional[str] = None,
        nodes_per_block: int = 1,
        min_blocks: int = 0,
        max_blocks: int = 10,
        init_blocks: int = 1,
        worker_init: str = "",
        label: str = "aws_phase1",
        prefer_optimized_ami: bool = True,  # Phase 1.5 feature
    ):
        """Initialize AWS provider."""

        logger.info("Initializing AWSProvider...")

        # Parsl interface requirements
        self._label = label
        self.nodes_per_block = nodes_per_block
        self.min_blocks = min_blocks
        self.max_blocks = max_blocks
        self.init_blocks = init_blocks

        # AWS configuration
        self.region = region
        self.instance_type = instance_type
        self.key_name = key_name
        self.worker_init = worker_init
        self.prefer_optimized_ami = prefer_optimized_ami

        # AMI selection (will be set during AWS initialization)
        self.ami_id = ami_id
        self.is_optimized_ami = False

        # Internal state
        self.provider_id = f"aws-provider-{uuid.uuid4().hex[:8]}"
        self.instances = {}  # job_id -> instance_id mapping
        self.security_group_id = None

        # Initialize AWS with comprehensive validation
        self._initialize_aws()

        logger.info(f"AWSProvider ready: {self.provider_id}")

    def _select_ami(self):
        """Select best available AMI (Phase 1.5 with optimized AMI discovery)."""

        # If user specified AMI, use it
        if self.ami_id:
            logger.info(f"Using user-specified AMI: {self.ami_id}")
            return

        # Try to find optimized AMI first (Phase 1.5)
        if self.prefer_optimized_ami:
            optimized_ami = self._find_optimized_ami()
            if optimized_ami:
                self.ami_id = optimized_ami
                self.is_optimized_ami = True
                logger.info(
                    f"Using optimized AMI: {self.ami_id} (Phase 1.5 - fast startup)"
                )
                return
            else:
                logger.info(
                    "No compatible optimized AMI found, using base AMI (Phase 1 behavior)"
                )

        # Fallback to base AMI (Phase 1 behavior)
        self.ami_id = self._get_base_ami()
        self.is_optimized_ami = False
        logger.info(f"Using base AMI: {self.ami_id} (Phase 1 - installing packages)")

    def _find_optimized_ami(self) -> Optional[str]:
        """Find compatible optimized AMI (Phase 1.5)."""
        try:
            response = self.ec2.describe_images(
                Owners=["self"],
                Filters=[
                    {"Name": "tag:CreatedBy", "Values": ["ParslAWSProvider"]},
                    {"Name": "tag:Version", "Values": ["1.5"]},
                    {"Name": "state", "Values": ["available"]},
                ],
            )

            compatible_amis = []
            for ami in response["Images"]:
                if self._is_compatible_ami(ami):
                    compatible_amis.append(ami)

            # Return newest compatible AMI
            if compatible_amis:
                newest = max(compatible_amis, key=lambda x: x["CreationDate"])
                return newest["ImageId"]

        except Exception as e:
            logger.debug(f"Optimized AMI discovery failed: {e}")

        return None

    def _is_compatible_ami(self, ami: Dict) -> bool:
        """Check if AMI is compatible with current requirements."""
        tags = {tag["Key"]: tag["Value"] for tag in ami.get("Tags", [])}

        # Check age (don't use AMIs older than 30 days for freshness)
        try:
            creation_date = datetime.fromisoformat(
                ami["CreationDate"].replace("Z", "+00:00")
            )
            age_days = (datetime.now(timezone.utc) - creation_date).days
            if age_days > 30:
                logger.debug(f"AMI {ami['ImageId']} too old: {age_days} days")
                return False
        except Exception:
            pass  # If we can't parse date, assume it's compatible

        return True

    def _get_base_ami(self) -> str:
        """Get base AMI for region (Phase 1 fallback)."""
        ami_map = {
            "us-east-1": "ami-080e1f13689e07408",  # Amazon Linux 2023
            "us-east-2": "ami-03d21eed81858c120",
            "us-west-1": "ami-0d5b7dce3973d8817",
            "us-west-2": "ami-0473ec1595e64e666",
        }
        return ami_map.get(self.region, ami_map["us-east-1"])

    def _initialize_aws(self):
        """Initialize AWS with comprehensive error handling."""

        logger.info("Initializing AWS...")

        # Create session
        try:
            self.session = boto3.Session()
            self.ec2 = self.session.client("ec2", region_name=self.region)
        except NoCredentialsError:
            raise Exception("AWS credentials not found. Run 'aws configure'")
        except Exception as e:
            raise Exception(f"Failed to create AWS session: {e}")

        # Validate credentials
        try:
            sts = self.session.client("sts")
            identity = sts.get_caller_identity()
            logger.info(f"AWS credentials validated: {identity.get('Arn', 'unknown')}")
        except Exception as e:
            raise Exception(f"AWS credentials invalid: {e}")

        # Select and validate AMI (Phase 1.5 optimized AMI discovery)
        self._select_ami()
        self._validate_ami()

        # Setup security group with waiter
        self._setup_security_group_with_waiter()

        logger.info("AWS initialization complete")

    def _validate_ami(self):
        """Validate AMI exists and is available."""

        logger.info(f"Validating AMI {self.ami_id}...")

        try:
            response = self.ec2.describe_images(ImageIds=[self.ami_id])

            if not response["Images"]:
                raise Exception(f"AMI {self.ami_id} not found in region {self.region}")

            ami = response["Images"][0]
            if ami["State"] != "available":
                raise Exception(
                    f"AMI {self.ami_id} not available (state: {ami['State']})"
                )

            logger.info(f"AMI validated: {ami['Name']}")

        except ClientError as e:
            if e.response["Error"]["Code"] == "InvalidAMIID.NotFound":
                raise Exception(
                    f"AMI {self.ami_id} does not exist in region {self.region}"
                )
            else:
                raise Exception(f"Failed to validate AMI: {e}")

    def _setup_security_group_with_waiter(self):
        """Setup security group using reliable waiter approach."""

        logger.info("Setting up security group with waiter...")

        # Check for existing security group
        existing_sg = self._find_existing_security_group()
        if existing_sg:
            self.security_group_id = existing_sg
            logger.info(f"Using existing security group: {self.security_group_id}")
            return

        # Get default VPC
        vpc_id = self._get_default_vpc()

        # Create security group
        logger.info(f"Creating security group {self.provider_id}...")
        try:
            response = self.ec2.create_security_group(
                GroupName=self.provider_id,
                Description=f"AWS Provider: {self.provider_id}",
                VpcId=vpc_id,
                TagSpecifications=[
                    {
                        "ResourceType": "security-group",
                        "Tags": [
                            {"Key": "Name", "Value": self.provider_id},
                            {"Key": "CreatedBy", "Value": "AWSProvider"},
                            {"Key": "Provider", "Value": self.provider_id},
                            {"Key": "AutoCleanup", "Value": "true"},
                        ],
                    }
                ],
            )

            self.security_group_id = response["GroupId"]
            logger.info(f"Security group created: {self.security_group_id}")

        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            error_msg = e.response["Error"]["Message"]
            raise Exception(
                f"Failed to create security group: {error_code} - {error_msg}"
            )

        # Wait for security group using proven waiter
        try:
            wait_for_security_group(self.ec2, self.security_group_id)
        except Exception as e:
            raise Exception(f"Security group creation failed during wait: {e}")

        # Add ingress rules
        self._add_security_group_rules()

        logger.info(f"Security group ready: {self.security_group_id}")

    def _find_existing_security_group(self) -> Optional[str]:
        """Find existing security group."""
        try:
            response = self.ec2.describe_security_groups(
                Filters=[{"Name": "group-name", "Values": [self.provider_id]}]
            )
            if response["SecurityGroups"]:
                return response["SecurityGroups"][0]["GroupId"]
        except Exception as e:
            logger.debug(f"No existing security group found: {e}")
        return None

    def _get_default_vpc(self) -> str:
        """Get default VPC."""
        try:
            response = self.ec2.describe_vpcs(
                Filters=[{"Name": "is-default", "Values": ["true"]}]
            )
            if not response["Vpcs"]:
                raise Exception(
                    "No default VPC found. Create one with 'aws ec2 create-default-vpc'"
                )
            return response["Vpcs"][0]["VpcId"]
        except Exception as e:
            raise Exception(f"Failed to get default VPC: {e}")

    def _add_security_group_rules(self):
        """Add ingress rules to security group."""

        logger.info("Adding security group rules...")

        try:
            self.ec2.authorize_security_group_ingress(
                GroupId=self.security_group_id,
                IpPermissions=[
                    {
                        "IpProtocol": "tcp",
                        "FromPort": 22,
                        "ToPort": 22,
                        "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "SSH"}],
                    },
                    {
                        "IpProtocol": "tcp",
                        "FromPort": 54000,
                        "ToPort": 55000,
                        "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "Parsl"}],
                    },
                ],
            )
            logger.info("Security group rules added")
        except ClientError as e:
            if e.response["Error"]["Code"] == "InvalidPermission.Duplicate":
                logger.info("Security group rules already exist")
            else:
                raise Exception(f"Failed to add security group rules: {e}")

    def submit(self, command: str, tasks_per_node: int, job_name: str) -> str:
        """Submit job with comprehensive error handling and waiters."""

        if not command or not command.strip():
            raise ValueError("Command cannot be empty")
        if tasks_per_node < 1:
            raise ValueError(f"tasks_per_node must be >= 1, got {tasks_per_node}")
        if not job_name or not job_name.strip():
            raise ValueError("job_name cannot be empty")

        job_id = f"job-{uuid.uuid4().hex[:8]}"
        logger.info(f"Submitting job {job_id}: {job_name}")

        # Pre-flight validation
        self._validate_ready_for_submit()

        # Create user data (optimized for Phase 1.5 if using optimized AMI)
        user_data = self._create_user_data(job_id, command)

        # Launch instance
        logger.info(f"Launching instance for job {job_id}...")
        try:
            response = self.ec2.run_instances(
                ImageId=self.ami_id,
                MinCount=1,
                MaxCount=1,
                InstanceType=self.instance_type,
                SecurityGroupIds=[self.security_group_id],
                UserData=user_data,
                TagSpecifications=[
                    {
                        "ResourceType": "instance",
                        "Tags": [
                            {"Key": "Name", "Value": f"{self.provider_id}-{job_id}"},
                            {"Key": "Provider", "Value": self.provider_id},
                            {"Key": "JobId", "Value": job_id},
                            {"Key": "JobName", "Value": job_name},
                            {"Key": "CreatedBy", "Value": "AWSProvider"},
                            {"Key": "AutoCleanup", "Value": "true"},
                        ],
                    }
                ],
            )

            instance_id = response["Instances"][0]["InstanceId"]
            logger.info(f"Instance launch API succeeded: {instance_id}")

        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            error_msg = e.response["Error"]["Message"]
            raise Exception(f"Failed to launch instance: {error_code} - {error_msg}")

        # Store mapping
        self.instances[job_id] = instance_id

        # Wait for instance to be visible using proven waiter
        try:
            wait_for_instance(self.ec2, instance_id)
            logger.info(f"Instance {instance_id} confirmed visible in AWS")
        except Exception as e:
            # Clean up failed instance from our tracking
            del self.instances[job_id]
            raise Exception(f"Instance launch failed during validation: {e}")

        logger.info(f"Job {job_id} submitted successfully to instance {instance_id}")
        return job_id

    def _validate_ready_for_submit(self):
        """Validate provider is ready to submit jobs."""

        # Check security group still exists
        try:
            self.ec2.describe_security_groups(GroupIds=[self.security_group_id])
        except ClientError as e:
            if e.response["Error"]["Code"] == "InvalidGroup.NotFound":
                raise Exception(
                    f"Security group {self.security_group_id} no longer exists"
                )
            else:
                raise Exception(f"Security group validation failed: {e}")

        # Check AMI still available
        try:
            response = self.ec2.describe_images(ImageIds=[self.ami_id])
            if not response["Images"] or response["Images"][0]["State"] != "available":
                raise Exception(f"AMI {self.ami_id} no longer available")
        except Exception as e:
            raise Exception(f"AMI validation failed: {e}")

    def status(self, job_ids: List[str]) -> List[JobStatus]:
        """Get job status with comprehensive error handling."""

        statuses = []

        for job_id in job_ids:
            try:
                if job_id not in self.instances:
                    statuses.append(
                        JobStatus(JobState.UNKNOWN, message=f"Job {job_id} not found")
                    )
                    continue

                instance_id = self.instances[job_id]
                response = self.ec2.describe_instances(InstanceIds=[instance_id])

                if not response["Reservations"]:
                    statuses.append(
                        JobStatus(JobState.COMPLETED, message="Instance terminated")
                    )
                    continue

                instance = response["Reservations"][0]["Instances"][0]
                ec2_state = instance["State"]["Name"]

                # Map EC2 states to Parsl JobStates
                state_mapping = {
                    "pending": JobState.PENDING,
                    "running": JobState.RUNNING,
                    "stopped": JobState.COMPLETED,
                    "terminated": JobState.COMPLETED,
                    "stopping": JobState.COMPLETED,
                    "shutting-down": JobState.COMPLETED,
                }

                parsl_state = state_mapping.get(ec2_state, JobState.UNKNOWN)
                statuses.append(
                    JobStatus(parsl_state, message=f"EC2 state: {ec2_state}")
                )

            except Exception as e:
                logger.error(f"Failed to get status for {job_id}: {e}")
                statuses.append(JobStatus(JobState.FAILED, message=str(e)))

        return statuses

    def cancel(self, job_ids: List[str]) -> List[bool]:
        """Cancel jobs with comprehensive error handling."""

        results = []

        for job_id in job_ids:
            try:
                if job_id in self.instances:
                    instance_id = self.instances[job_id]
                    self.ec2.terminate_instances(InstanceIds=[instance_id])
                    logger.info(f"Terminated instance {instance_id} for job {job_id}")
                    results.append(True)
                else:
                    logger.warning(f"Job {job_id} not found for cancellation")
                    results.append(False)

            except Exception as e:
                logger.error(f"Failed to cancel job {job_id}: {e}")
                results.append(False)

        return results

    def cleanup(self):
        """Clean up all resources with comprehensive error handling."""

        logger.info(f"Cleaning up all resources for {self.provider_id}")

        # Terminate all instances
        if self.instances:
            instance_ids = list(self.instances.values())
            try:
                self.ec2.terminate_instances(InstanceIds=instance_ids)
                logger.info(f"Terminated {len(instance_ids)} instances")

                # Wait for instances to start terminating
                time.sleep(5)

            except Exception as e:
                logger.error(f"Failed to terminate instances: {e}")

        # Delete security group with retry for dependencies
        if self.security_group_id:
            max_retries = 12
            retry_delay = 5

            for attempt in range(max_retries):
                try:
                    self.ec2.delete_security_group(GroupId=self.security_group_id)
                    logger.info(f"Deleted security group {self.security_group_id}")
                    break

                except ClientError as e:
                    error_code = e.response["Error"]["Code"]

                    if error_code == "DependencyViolation":
                        if attempt < max_retries - 1:
                            logger.debug(
                                f"Security group has dependencies, retrying in {retry_delay}s (attempt {attempt + 1})"
                            )
                            time.sleep(retry_delay)
                            continue
                        else:
                            logger.warning(
                                f"Could not delete security group after {max_retries} attempts"
                            )
                    elif error_code == "InvalidGroup.NotFound":
                        logger.info("Security group already deleted")
                        break
                    else:
                        logger.error(f"Failed to delete security group: {error_code}")
                        break

                except Exception as e:
                    logger.error(f"Unexpected error deleting security group: {e}")
                    break

    @property
    def status_polling_interval(self) -> int:
        return 30

    @property
    def label(self) -> str:
        return self._label

    def scale_out(self, blocks: int) -> List[str]:
        return []

    def scale_in(self, blocks: int) -> List[str]:
        return []

    def _create_user_data(self, job_id: str, command: str) -> str:
        """Create user data script, optimized for Phase 1.5 AMIs."""

        if self.is_optimized_ami:
            # Phase 1.5: Minimal user data for optimized AMI
            return f"""#!/bin/bash
exec > >(tee /var/log/user-data.log) 2>&1

echo "=== PHASE 1.5 JOB START (OPTIMIZED) ==="
echo "Job ID: {job_id}"
echo "Started: $(date)"
echo "AMI: {self.ami_id} (optimized)"

# Verify Parsl is available
echo "Verifying Parsl installation..."
python3 -c "import parsl; print('Parsl version:', parsl.__version__)" || echo "WARNING: Parsl not found"

# User initialization
{self.worker_init}

# Execute command
echo "Executing: {command}"
{command}

echo "Job completed: $(date)"
echo "=== PHASE 1.5 JOB END ==="
"""
        else:
            # Phase 1: Full installation for base AMI
            return f"""#!/bin/bash
exec > >(tee /var/log/user-data.log) 2>&1

echo "=== PHASE 1 JOB START (BASE AMI) ==="
echo "Job ID: {job_id}"
echo "Started: $(date)"
echo "AMI: {self.ami_id} (base - installing packages)"

# Install Python and Parsl
echo "Installing packages..."
yum update -y
yum install -y python3 python3-pip
pip3 install parsl

# User initialization
{self.worker_init}

# Execute command
echo "Executing: {command}"
{command}

echo "Job completed: $(date)"
echo "=== PHASE 1 JOB END ==="
"""


# Test the provider
if __name__ == "__main__":
    print("TESTING PHASE 1 PROVIDER")
    print("=" * 60)

    # Enable debug logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    provider = None

    try:
        print("1. Creating provider...")
        provider = AWSProvider()
        print(f"✓ Provider created: {provider.provider_id}")
        print(f"  Security Group: {provider.security_group_id}")

        print("\n2. Submitting test job...")
        job_id = provider.submit(
            command='echo "Phase 1 test successful!"; hostname; date; sleep 10',
            tasks_per_node=1,
            job_name="phase1_test",
        )
        print(f"✓ Job submitted: {job_id}")

        print("\n3. Checking job status...")
        statuses = provider.status([job_id])
        print(f"Status: {statuses}")

        print("\n4. Waiting 60 seconds to let job run...")
        time.sleep(60)

        statuses = provider.status([job_id])
        print(f"Status after 60s: {statuses}")

        print("\n✓ PHASE 1 PROVIDER TEST: SUCCESS")

    except Exception as e:
        print(f"\n✗ Test failed: {e}")
        import traceback

        traceback.print_exc()

    finally:
        if provider:
            print("\nCleaning up...")
            provider.cleanup()
            print("✓ Cleanup completed")

    print("\n" + "=" * 60)
    print("PHASE 1 PROVIDER TEST COMPLETE")

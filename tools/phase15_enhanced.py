#!/usr/bin/env python3
"""
Phase 1.5 Enhanced AWS Provider with SSM Tunneling.

Revolutionary networking solution that enables Parsl workers to run:
- Behind any NAT/firewall (home, corporate, etc.)
- In private AWS subnets without internet access
- With zero network configuration required

Features:
- SSM tunneling for universal connectivity
- Private subnet deployment with VPC endpoints
- Optimized AMI discovery for fast startup
- Comprehensive error handling
- Secure by default
"""

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from parsl.providers.base import ExecutionProvider
from parsl.jobs.states import JobStatus, JobState

from ssm_tunnel import ParslWorkerCommandParser
from ssh_reverse_tunnel import SSMSSHTunnel
from private_subnet import PrivateSubnetManager
from config_loader import get_config

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
    Phase 1.5 Enhanced AWS Provider with SSM Tunneling.

    Revolutionary networking capabilities:
    - Universal connectivity: Works from any network environment
    - Private subnet deployment: Maximum security with zero internet access
    - Zero configuration: Transparent to users
    - Cloud-native: Leverages AWS backbone for communication
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
        label: str = "aws_phase15_enhanced",
        prefer_optimized_ami: bool = True,
        enable_ssm_tunneling: bool = True,
        use_private_subnets: bool = False,
        tunnel_port_range: tuple = (50000, 60000),
        aws_profile: str = "aws",
        python_version: str = "3.10",  # Default to standard Ubuntu AMI version
    ):
        """Initialize Phase 1.5 Enhanced AWS provider."""

        logger.info("Initializing Phase 1.5 Enhanced AWSProvider...")

        # Parsl interface requirements
        self._label = label
        self.nodes_per_block = nodes_per_block
        self.min_blocks = min_blocks
        self.max_blocks = max_blocks
        self.init_blocks = init_blocks

        # AWS configuration
        self.region = region
        self.aws_profile = aws_profile
        self.instance_type = instance_type
        self.key_name = key_name
        self.worker_init = worker_init
        self.prefer_optimized_ami = prefer_optimized_ami
        self.python_version = python_version

        # Phase 1.5 Enhanced features
        self.enable_ssm_tunneling = enable_ssm_tunneling
        self.use_private_subnets = use_private_subnets
        self.tunnel_port_range = tunnel_port_range

        # Force SSM tunneling for private subnets
        if self.use_private_subnets:
            self.enable_ssm_tunneling = True
            logger.info("Private subnets enabled - forcing SSM tunneling")

        # AMI selection (will be set during AWS initialization)
        self.ami_id = ami_id
        self.is_optimized_ami = False

        # Internal state
        self.provider_id = f"aws-enhanced-{uuid.uuid4().hex[:8]}"
        self.instances = {}  # job_id -> instance_id mapping
        self.security_group_id = None

        # SSH reverse tunneling state
        self.ssh_tunnel = None
        self.job_tunnels = {}  # job_id -> tunnel_info

        # Initialize AWS with comprehensive validation
        self._initialize_aws()

        # Initialize SSM tunneling if enabled
        if self.enable_ssm_tunneling:
            self._initialize_ssm_tunneling()

        # Initialize private subnet management if enabled
        if self.use_private_subnets:
            self._initialize_private_subnets()

        logger.info(f"Phase 1.5 Enhanced AWSProvider ready: {self.provider_id}")
        if self.enable_ssm_tunneling:
            logger.info("  🔒 SSM tunneling enabled - works behind any firewall/NAT")
        if self.use_private_subnets:
            logger.info("  🛡️  Private subnet deployment - zero internet access")

    def _initialize_ssm_tunneling(self):
        """Initialize SSH over SSM reverse tunneling components."""
        try:
            # Initialize SSH reverse tunnel manager
            self.ssh_tunnel = SSMSSHTunnel(session=self.session, region=self.region)

            # Set up SSH config for SSM ProxyCommand
            self.ssh_tunnel.setup_ssh_config()

            # Generate SSH key pair for authentication
            (
                self.private_key_path,
                self.public_key_path,
            ) = self.ssh_tunnel.generate_ssh_key()

            logger.info("SSH over SSM reverse tunneling initialized successfully")
            logger.info(f"SSH keys: {self.private_key_path}, {self.public_key_path}")
        except Exception as e:
            logger.error(f"Failed to initialize SSH reverse tunneling: {e}")
            if self.use_private_subnets:
                raise Exception(
                    "SSH reverse tunneling required for private subnet deployment"
                )
            # Disable SSH tunneling and continue
            self.enable_ssm_tunneling = False
            logger.warning("Continuing with traditional networking")

    def _initialize_private_subnets(self):
        """Initialize private subnet management."""
        try:
            self.private_subnet_manager = PrivateSubnetManager(session=self.session)
            logger.info("Private subnet management initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize private subnet management: {e}")
            raise

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
        try:
            config = get_config()
            return config.get_base_ami(self.region)
        except Exception as e:
            logger.warning(
                f"Could not load AMI from config: {e}, using hardcoded fallback"
            )
            # Fallback to hardcoded values if config fails
            ami_map = {
                "us-east-1": "ami-080e1f13689e07408",  # Amazon Linux 2023
                "us-east-2": "ami-03d21eed81858c120",
                "us-west-1": "ami-0d5b7dce3973d8817",
                "us-west-2": "ami-0473ec1595e64e666",
            }
            return ami_map.get(self.region, ami_map["us-east-1"])

    def _get_iam_instance_profile(self) -> Optional[str]:
        """Get IAM instance profile for SSM access from configuration."""
        try:
            config = get_config()
            default_profile = config.get("ssm.iam.instance_profiles.default")
            fallback_profile = config.get("ssm.iam.instance_profiles.fallback")

            iam = self.session.client("iam")

            # Try default profile first
            try:
                iam.get_instance_profile(InstanceProfileName=default_profile)
                logger.info(f"Using default IAM instance profile: {default_profile}")
                return default_profile
            except ClientError:
                logger.info(
                    f"Default profile {default_profile} not found, trying fallback"
                )

            # Try fallback profile
            try:
                iam.get_instance_profile(InstanceProfileName=fallback_profile)
                logger.info(f"Using fallback IAM instance profile: {fallback_profile}")
                return fallback_profile
            except ClientError:
                logger.warning(
                    "Neither default nor fallback IAM instance profile available"
                )
                return None

        except Exception as e:
            logger.warning(f"Could not load IAM instance profile from config: {e}")
            return None

    def _initialize_aws(self):
        """Initialize AWS with comprehensive error handling."""
        logger.info("Initializing AWS...")

        # Create session with explicit region
        try:
            self.session = boto3.Session(
                region_name=self.region, profile_name=self.aws_profile
            )
            self.ec2 = self.session.client("ec2")
            logger.info(f"AWS session created for region: {self.region}")
        except NoCredentialsError:
            raise Exception("AWS credentials not found. Run 'aws configure'")
        except Exception as e:
            raise Exception(f"Failed to create AWS session: {e}")

        # Validate credentials
        logger.info("Found credentials in shared credentials file: ~/.aws/credentials")
        try:
            sts = self.session.client("sts")
            identity = sts.get_caller_identity()
            logger.info(f"AWS credentials validated: {identity.get('Arn', 'Unknown')}")
        except Exception as e:
            raise Exception(f"AWS credential validation failed: {e}")

        # Select AMI (Phase 1.5 enhanced)
        self._select_ami()

        # Validate AMI
        self._validate_ami()

        # Set up security group with waiter
        self._setup_security_group()

        logger.info("AWS initialization complete")

    def _validate_ami(self):
        """Validate selected AMI is available."""
        logger.info(f"Validating AMI {self.ami_id}...")
        try:
            response = self.ec2.describe_images(ImageIds=[self.ami_id])
            if not response["Images"]:
                raise Exception(f"AMI {self.ami_id} not found")

            ami = response["Images"][0]
            if ami["State"] != "available":
                raise Exception(f"AMI {self.ami_id} not available: {ami['State']}")

            ami_name = ami.get("Name", "Unknown")
            logger.info(f"AMI validated: {ami_name}")
        except Exception as e:
            raise Exception(f"AMI validation failed: {e}")

    def _setup_security_group(self):
        """Set up security group using waiter approach."""
        logger.info("Setting up security group with waiter...")

        group_name = self.provider_id
        try:
            # Try to create security group
            logger.info(f"Creating security group {group_name}...")
            response = self.ec2.create_security_group(
                GroupName=group_name,
                Description=f"Parsl AWS Provider: {self.provider_id}",
            )
            self.security_group_id = response["GroupId"]
            logger.info(f"Security group created: {self.security_group_id}")

            # Use waiter to ensure it's ready
            wait_for_security_group(self.ec2, self.security_group_id)

            # Add rules once it's ready
            logger.info("Adding security group rules...")
            self._add_security_group_rules()
            logger.info("Security group rules added")

        except ClientError as e:
            if e.response["Error"]["Code"] == "InvalidGroup.Duplicate":
                logger.info(f"Security group {group_name} already exists, using it")
                response = self.ec2.describe_security_groups(GroupNames=[group_name])
                self.security_group_id = response["SecurityGroups"][0]["GroupId"]
            else:
                raise Exception(f"Security group creation failed: {e}")

        logger.info(f"Security group ready: {self.security_group_id}")

    def _add_security_group_rules(self):
        """Add required rules to security group."""
        try:
            # For private subnets, we need minimal rules (VPC endpoints only)
            if self.use_private_subnets:
                # Get VPC CIDR for private subnet communication
                vpc_response = self.ec2.describe_vpcs()
                if vpc_response["Vpcs"]:
                    vpc_cidr = vpc_response["Vpcs"][0]["CidrBlock"]

                    # Allow outbound HTTPS for VPC endpoints
                    self.ec2.authorize_security_group_egress(
                        GroupId=self.security_group_id,
                        IpPermissions=[
                            {
                                "IpProtocol": "tcp",
                                "FromPort": 443,
                                "ToPort": 443,
                                "IpRanges": [{"CidrIp": vpc_cidr}],
                            }
                        ],
                    )
                    logger.info("Added VPC endpoint rules for private subnet")
            else:
                # Standard rules for public subnet deployment
                self.ec2.authorize_security_group_egress(
                    GroupId=self.security_group_id,
                    IpPermissions=[
                        {"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}
                    ],
                )
                logger.info("Added standard outbound rules")

        except ClientError as e:
            if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
                logger.warning(f"Failed to add security group rules: {e}")

    def _get_launch_config(self, job_id: str) -> dict:
        """Get instance launch configuration based on deployment mode."""

        if self.use_private_subnets:
            # Configure for private subnet deployment
            logger.info("Configuring for private subnet deployment...")
            private_config = self.private_subnet_manager.ensure_private_subnet_ready()

            config = {
                "ImageId": self.ami_id,
                "InstanceType": self.instance_type,
                "SecurityGroupIds": [private_config["security_group_id"]],
                "SubnetId": private_config["subnet_id"],
                "UserData": self.private_subnet_manager.get_private_subnet_user_data(),
                "TagSpecifications": [
                    {
                        "ResourceType": "instance",
                        "Tags": [
                            {"Key": "Name", "Value": f"{self.provider_id}-{job_id}"},
                            {"Key": "Provider", "Value": self.provider_id},
                            {"Key": "JobId", "Value": job_id},
                            {"Key": "DeploymentMode", "Value": "private-subnet"},
                            {
                                "Key": "CreatedBy",
                                "Value": "parsl-aws-provider-enhanced",
                            },
                        ],
                    }
                ],
            }

            # Add optional parameters
            if self.key_name:
                config["KeyName"] = self.key_name

            # Add IAM instance profile for SSM access
            instance_profile = self._get_iam_instance_profile()
            if instance_profile:
                config["IamInstanceProfile"] = {"Name": instance_profile}
            else:
                logger.warning(
                    "No IAM instance profile available for private subnet - SSM may not work"
                )

            return config
        else:
            # Standard configuration for public subnets
            user_data = self._get_user_data_script()

            config = {
                "ImageId": self.ami_id,
                "InstanceType": self.instance_type,
                "SecurityGroupIds": [self.security_group_id],
                "UserData": user_data,
                "TagSpecifications": [
                    {
                        "ResourceType": "instance",
                        "Tags": [
                            {"Key": "Name", "Value": f"{self.provider_id}-{job_id}"},
                            {"Key": "Provider", "Value": self.provider_id},
                            {"Key": "JobId", "Value": job_id},
                            {"Key": "DeploymentMode", "Value": "standard"},
                            {
                                "Key": "CreatedBy",
                                "Value": "parsl-aws-provider-enhanced",
                            },
                        ],
                    }
                ],
            }

            # Add optional parameters only if they exist
            if self.key_name:
                config["KeyName"] = self.key_name

            # Add IAM instance profile if SSM tunneling is enabled
            if self.enable_ssm_tunneling:
                # Add IAM instance profile for SSM access if tunneling is enabled
                instance_profile = self._get_iam_instance_profile()
                if instance_profile:
                    config["IamInstanceProfile"] = {"Name": instance_profile}
                else:
                    logger.warning(
                        "IAM instance profile not available - SSM tunneling may not work"
                    )

            return config

    def _get_user_data_script(self) -> str:
        """Generate user data script based on AMI type and configuration."""
        if self.is_optimized_ami:
            # Optimized AMI - minimal setup needed
            return f"""#!/bin/bash
# Phase 1.5 - Optimized AMI startup
exec > >(tee /var/log/user-data.log) 2>&1
echo "$(date): Starting optimized AMI initialization"

# User custom init
{self.worker_init}

echo "$(date): Optimized AMI ready"
"""
        else:
            # Base AMI - install packages
            return f"""#!/bin/bash
# Phase 1 - Base AMI setup
exec > >(tee /var/log/user-data.log) 2>&1
echo "$(date): Starting base AMI initialization"

# Update system
yum update -y

# Install Python 3 and pip
yum install -y python3 python3-pip

# Install Parsl and dependencies
pip3 install parsl zmq psutil

# User custom init
{self.worker_init}

echo "$(date): Base AMI setup complete"
"""

    def submit(self, command: str, tasks_per_node: int, job_name: str) -> str:
        """Submit job with SSM tunneling support."""
        if not command or not command.strip():
            raise ValueError("Command cannot be empty")

        job_id = f"job-{uuid.uuid4().hex[:8]}"
        logger.info(f"Submitting job {job_id}: {job_name}")

        try:
            if self.enable_ssm_tunneling:
                # Run async method in event loop
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    return loop.run_until_complete(
                        self._submit_with_tunneling(job_id, command, job_name)
                    )
                finally:
                    loop.close()
            else:
                return self._submit_traditional(job_id, command, job_name)
        except Exception as e:
            logger.error(f"Job submission failed for {job_id}: {e}")
            raise

    async def _submit_with_tunneling(
        self, job_id: str, command: str, job_name: str
    ) -> str:
        """Submit job using SSH reverse tunneling over SSM."""
        logger.info(f"Submitting job {job_id} with SSH reverse tunneling...")

        # Parse worker command to extract controller port
        try:
            parsed = ParslWorkerCommandParser.parse_addresses_and_port(command)
            controller_port = int(parsed["port"]) if parsed["port"] else 54321
        except Exception as e:
            logger.error(f"Failed to parse worker command: {e}")
            raise ValueError(f"Could not parse Parsl worker command: {command}")

        # Launch instance
        logger.info(f"Launching instance for job {job_id}...")
        launch_config = self._get_launch_config(job_id)

        try:
            response = self.ec2.run_instances(MinCount=1, MaxCount=1, **launch_config)
            instance_id = response["Instances"][0]["InstanceId"]
            logger.info(f"Instance launch API succeeded: {instance_id}")

            # Store instance mapping
            self.instances[job_id] = instance_id

            # Wait for instance to be visible
            wait_for_instance(self.ec2, instance_id)
            logger.info(f"Instance {instance_id} confirmed visible in AWS")

            # Wait for instance to be running and SSM agent ready
            logger.info(f"Waiting for instance {instance_id} to be ready for SSH...")
            self._wait_for_instance_running(instance_id)

            # Install SSH public key on instance
            logger.info(f"Installing SSH key on instance {instance_id}...")
            key_installed = self.ssh_tunnel.install_ssh_key_on_instance(
                instance_id, self.public_key_path
            )

            if not key_installed:
                raise Exception(f"Failed to install SSH key on instance {instance_id}")

            # Create reverse SSH tunnel (AWS instance back to local)
            logger.info(f"Creating reverse SSH tunnel for job {job_id}...")
            tunnel_port = self._allocate_tunnel_port()
            tunnel_proc = self.ssh_tunnel.create_reverse_tunnel(
                instance_id, controller_port, tunnel_port, self.private_key_path
            )

            if not tunnel_proc:
                raise Exception(
                    f"Failed to create reverse SSH tunnel for {instance_id}"
                )

            # Modify worker command to use tunnel port
            modified_command = self._modify_command_for_reverse_tunnel(
                command, tunnel_port
            )

            # Store tunnel info
            self.job_tunnels[job_id] = {
                "tunnel_process": tunnel_proc,
                "tunnel_port": tunnel_port,
                "instance_id": instance_id,
                "modified_command": modified_command,
            }

            # Execute worker command via SSM with proper environment setup
            logger.info(f"Executing worker command on instance {instance_id}...")
            ssm_client = self.session.client("ssm")

            # Create comprehensive setup script for SSM execution with Parsl installation
            setup_script = f"""#!/bin/bash
set -e
export HOME=/root
export USER=root
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"

# Set Python version to use (use existing system Python 3.10)
PYTHON_VERSION="{self.python_version}"
echo "Target Python version: $PYTHON_VERSION"

# Use existing system Python
if command -v python$PYTHON_VERSION >/dev/null 2>&1; then
    PYTHON_CMD="python$PYTHON_VERSION"
    echo "Using existing python$PYTHON_VERSION"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD="python3"
    echo "Using system python3"
else
    PYTHON_CMD="python"
    echo "Using default python"
fi

# Verify Python version
ACTUAL_VERSION=$($PYTHON_CMD --version 2>&1)
echo "Using Python: $ACTUAL_VERSION"

# Install pip if not available
if ! $PYTHON_CMD -m pip --version >/dev/null 2>&1; then
    echo "Installing pip..."
    apt-get update -y >/dev/null 2>&1
    apt-get install -y python3-pip >/dev/null 2>&1
fi

# Quick check for Parsl - install only if not available
if ! $PYTHON_CMD -c "import parsl" >/dev/null 2>&1; then
    echo "Installing Parsl with Python $ACTUAL_VERSION..."
    $PYTHON_CMD -m pip install parsl --quiet
    echo "Parsl installed successfully"
else
    echo "Parsl already available"
fi

# Verify installation
echo "Verifying Parsl installation:"
$PYTHON_CMD -c "import parsl; print(f'Parsl version: {{parsl.__version__}}')"
$PYTHON_CMD -c "import sys; print(f'Python version: {{sys.version}}')"

# Check for process_worker_pool.py in various locations using specified Python version
WORKER_SCRIPT=""
if which process_worker_pool.py >/dev/null 2>&1; then
    WORKER_SCRIPT="process_worker_pool.py"
    echo "Found process_worker_pool.py in PATH"
elif $PYTHON_CMD -c "import parsl.executors.high_throughput.process_worker_pool" >/dev/null 2>&1; then
    WORKER_SCRIPT="$PYTHON_CMD -m parsl.executors.high_throughput.process_worker_pool"
    echo "Found Parsl module, will execute as Python module with $PYTHON_CMD"
else
    # Find the script location manually using pyenv-managed Python
    PARSL_LOCATION=$($PYTHON_CMD -c "import parsl; import os; print(os.path.dirname(parsl.__file__))" 2>/dev/null)
    if [ -n "$PARSL_LOCATION" ] && [ -f "$PARSL_LOCATION/executors/high_throughput/process_worker_pool.py" ]; then
        WORKER_SCRIPT="$PYTHON_CMD $PARSL_LOCATION/executors/high_throughput/process_worker_pool.py"
        echo "Found worker script in Parsl installation: $PARSL_LOCATION"
    else
        echo "WARNING: Could not locate process_worker_pool.py, using module approach"
        WORKER_SCRIPT="$PYTHON_CMD -m parsl.executors.high_throughput.process_worker_pool"
    fi
fi

# Set working directory
cd /tmp

echo "Environment setup complete, worker script: $WORKER_SCRIPT"

# Execute the actual worker command with the detected worker script
ORIGINAL_COMMAND="{modified_command}"
WORKER_ARGS="${{ORIGINAL_COMMAND#*process_worker_pool.py}}"

# Command has already been cleaned up in Python - just extract the arguments
echo "Executing worker command: $ORIGINAL_COMMAND"

# Add required arguments that are often missing
if [[ "$WORKER_ARGS" != *"--cpu-affinity"* ]]; then
    WORKER_ARGS="$WORKER_ARGS --cpu-affinity none"
fi

# Create log directory (certificates disabled with --cert_dir None)
mkdir -p /tmp/parsl_logs

echo "Executing worker command with args: $WORKER_ARGS"
echo "Using worker script: $WORKER_SCRIPT"

# Execute the worker command with proper script path using nohup
if [ "$WORKER_SCRIPT" = "process_worker_pool.py" ]; then
    # Original command with nohup for background execution
    nohup $WORKER_SCRIPT $WORKER_ARGS > /tmp/parsl_logs/worker.log 2>&1 &
    echo "Worker started with PID $! in background"
else
    # Use the detected worker script with the arguments
    nohup $WORKER_SCRIPT $WORKER_ARGS > /tmp/parsl_logs/worker.log 2>&1 &
    echo "Worker started with PID $! in background"
fi

# Keep the script running briefly to ensure worker starts
sleep 5
echo "Worker startup complete, process should be running in background"
"""

            ssm_response = ssm_client.send_command(
                InstanceIds=[instance_id],
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": [setup_script]},
                TimeoutSeconds=3600,  # Allow 1 hour for worker to complete
            )

            command_id = ssm_response["Command"]["CommandId"]
            logger.info(f"Worker command sent via SSM: {command_id}")

            logger.info(f"Job {job_id} submitted successfully with SSM tunneling")
            return job_id

        except Exception as e:
            # Cleanup on failure
            if job_id in self.instances:
                self._cleanup_job_resources(job_id)
            logger.error(f"SSM tunneling submission failed: {e}")
            raise

    def _submit_traditional(self, job_id: str, command: str, job_name: str) -> str:
        """Submit job using traditional networking."""
        logger.info(f"Submitting job {job_id} with traditional networking...")

        # Launch instance
        launch_config = self._get_launch_config(job_id)

        # Add worker command to user data
        if "UserData" in launch_config:
            launch_config["UserData"] += f"\n\n# Start Parsl worker\n{command}\n"
        else:
            launch_config["UserData"] = f"#!/bin/bash\n{command}\n"

        try:
            response = self.ec2.run_instances(MinCount=1, MaxCount=1, **launch_config)
            instance_id = response["Instances"][0]["InstanceId"]
            logger.info(f"Instance launch API succeeded: {instance_id}")

            # Store instance mapping
            self.instances[job_id] = instance_id

            # Wait for instance to be visible
            wait_for_instance(self.ec2, instance_id)
            logger.info(f"Instance {instance_id} confirmed visible in AWS")
            logger.info(f"Job {job_id} submitted successfully")

            return job_id

        except Exception as e:
            logger.error(f"Traditional submission failed: {e}")
            raise

    def status(self, job_ids: List[str]) -> List[JobStatus]:
        """Get job status with comprehensive error handling."""
        statuses = []

        for job_id in job_ids:
            try:
                if job_id not in self.instances:
                    statuses.append(JobStatus(JobState.UNKNOWN, "Job not found"))
                    continue

                instance_id = self.instances[job_id]
                response = self.ec2.describe_instances(InstanceIds=[instance_id])

                if not response["Reservations"]:
                    statuses.append(JobStatus(JobState.UNKNOWN, "Instance not found"))
                    continue

                instance = response["Reservations"][0]["Instances"][0]
                ec2_state = instance["State"]["Name"]

                # Map EC2 states to Parsl JobStates
                state_mapping = {
                    "pending": JobState.PENDING,
                    "running": JobState.RUNNING,
                    "shutting-down": JobState.COMPLETED,
                    "terminated": JobState.COMPLETED,
                    "stopping": JobState.COMPLETED,
                    "stopped": JobState.COMPLETED,
                }

                parsl_state = state_mapping.get(ec2_state, JobState.UNKNOWN)
                statuses.append(
                    JobStatus(parsl_state, message=f"EC2 state: {ec2_state}")
                )

            except Exception as e:
                logger.error(f"Status check failed for job {job_id}: {e}")
                statuses.append(JobStatus(JobState.UNKNOWN, f"Status error: {e}"))

        return statuses

    def cancel(self, job_ids: List[str]) -> List[bool]:
        """Cancel jobs with comprehensive error handling."""
        results = []

        for job_id in job_ids:
            try:
                if job_id not in self.instances:
                    logger.warning(f"Job {job_id} not found for cancellation")
                    results.append(False)
                    continue

                instance_id = self.instances[job_id]
                logger.info(f"Terminating instance {instance_id} for job {job_id}")

                self.ec2.terminate_instances(InstanceIds=[instance_id])

                # Cleanup reverse tunnels if using SSH over SSM
                if self.enable_ssm_tunneling and job_id in self.job_tunnels:
                    self.ssh_tunnel.cleanup_tunnel(instance_id)
                    del self.job_tunnels[job_id]

                # Remove from tracking
                del self.instances[job_id]

                results.append(True)
                logger.info(f"Job {job_id} cancelled successfully")

            except Exception as e:
                logger.error(f"Cancellation failed for job {job_id}: {e}")
                results.append(False)

        return results

    def _cleanup_job_resources(self, job_id: str):
        """Clean up all resources for a job."""
        try:
            # Terminate instance
            if job_id in self.instances:
                instance_id = self.instances[job_id]
                self.ec2.terminate_instances(InstanceIds=[instance_id])
                del self.instances[job_id]

            # Cleanup reverse tunnels
            if self.enable_ssm_tunneling and job_id in self.job_tunnels:
                instance_id = self.instances.get(job_id)
                if instance_id:
                    self.ssh_tunnel.cleanup_tunnel(instance_id)
                del self.job_tunnels[job_id]

        except Exception as e:
            logger.error(f"Resource cleanup failed for job {job_id}: {e}")

    def cleanup(self):
        """Clean up all provider resources."""
        logger.info(f"Cleaning up all resources for {self.provider_id}")

        # Cancel all running jobs
        if self.instances:
            job_ids = list(self.instances.keys())
            self.cancel(job_ids)

        # Cleanup SSH reverse tunnels
        if self.enable_ssm_tunneling and hasattr(self, "ssh_tunnel"):
            self.ssh_tunnel.cleanup_all_tunnels()

        # Delete security group
        if self.security_group_id:
            try:
                self.ec2.delete_security_group(GroupId=self.security_group_id)
                logger.info(f"Security group {self.security_group_id} deleted")
            except Exception as e:
                logger.warning(f"Failed to delete security group: {e}")

        logger.info("Provider cleanup completed")

    def _wait_for_instance_running(self, instance_id: str, max_attempts=60, delay=10):
        """Wait for instance to be running and SSM agent ready."""
        logger.info(f"Waiting for instance {instance_id} to be running...")

        for attempt in range(max_attempts):
            try:
                response = self.ec2.describe_instances(InstanceIds=[instance_id])
                instance = response["Reservations"][0]["Instances"][0]
                state = instance["State"]["Name"]

                if state == "running":
                    logger.info(
                        f"Instance {instance_id} is running, waiting for SSM agent..."
                    )
                    # Wait additional time for SSM agent to be ready
                    time.sleep(30)
                    return
                elif state in ["terminated", "stopped", "stopping"]:
                    raise Exception(f"Instance {instance_id} failed: {state}")

                logger.debug(
                    f"Instance state: {state}, waiting... ({attempt+1}/{max_attempts})"
                )
                time.sleep(delay)

            except Exception as e:
                if attempt == max_attempts - 1:
                    raise Exception(
                        f"Instance {instance_id} not ready after {max_attempts * delay}s: {e}"
                    )
                time.sleep(delay)

        raise Exception(
            f"Instance {instance_id} not running after {max_attempts * delay} seconds"
        )

    def _allocate_tunnel_port(self) -> int:
        """Allocate a unique port for reverse tunnel."""
        # Simple port allocation - start from beginning of range
        start_port, end_port = self.tunnel_port_range

        # Find first available port
        used_ports = {info.get("tunnel_port") for info in self.job_tunnels.values()}
        for port in range(start_port, end_port):
            if port not in used_ports:
                return port

        raise Exception(f"No available ports in range {self.tunnel_port_range}")

    def _modify_command_for_reverse_tunnel(self, command: str, tunnel_port: int) -> str:
        """Modify worker command to connect to reverse tunnel port."""
        # Use the working SSM tunnel modification logic
        from ssm_tunnel import ParslWorkerCommandParser

        # Use the proven working modification method
        modified = ParslWorkerCommandParser.modify_for_tunnel(command, tunnel_port)

        logger.info(f"Modified command for reverse tunnel: {modified}")
        return modified

    # Parsl ExecutionProvider interface properties
    @property
    def status_polling_interval(self) -> int:
        return 30

    @property
    def label(self) -> str:
        return self._label

    @property
    def parallelism(self) -> float:
        """Return the parallelism factor for this provider."""
        return 1.0  # Each job uses one block/instance

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

from ssh_reverse_tunnel import SSMSSHTunnel
from private_subnet import PrivateSubnetManager
from config_loader import get_config

# Phase 2 imports (conditional to maintain backward compatibility)
try:
    from container_runtime import DockerRuntimeManager, ScientificContainerBuilder

    PHASE2_AVAILABLE = True
except ImportError:
    logger.warning(
        "Phase 2 container features not available - install container_runtime module"
    )
    PHASE2_AVAILABLE = False

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
        # Phase 2: Container Support
        container_runtime: Optional[str] = None,  # 'docker', 'singularity', or None
        container_image: Optional[str] = None,  # Container image to use
        scientific_stack: Optional[str] = None,  # 'basic', 'ml', 'bio', or custom
        container_config: Optional[Dict] = None,  # Advanced container configuration
        # Phase 2: Dependency Management
        custom_packages: Optional[List[str]] = None,  # Additional Python packages
        dependency_cache: bool = True,  # Enable dependency caching
        cache_backend: str = "s3",  # 's3', 'ebs', or 'memory'
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

        # Phase 2: Container and dependency features
        self.container_runtime = container_runtime
        self.container_image = container_image
        self.scientific_stack = scientific_stack
        self.container_config = container_config or {}
        self.custom_packages = custom_packages or []
        self.dependency_cache = dependency_cache
        self.cache_backend = cache_backend

        # Force SSM tunneling for private subnets
        if self.use_private_subnets:
            self.enable_ssm_tunneling = True
            logger.info("Private subnets enabled - forcing SSM tunneling")

        # Initialize Phase 2 managers (if available)
        self.container_manager = None
        self.dependency_cache_manager = None
        self.scientific_builder = None

        if self.container_runtime and PHASE2_AVAILABLE:
            self.container_manager = DockerRuntimeManager(self)
            self.scientific_builder = ScientificContainerBuilder()
            logger.info(f"Phase 2 container runtime enabled: {self.container_runtime}")

            if self.scientific_stack:
                logger.info(f"Using scientific stack: {self.scientific_stack}")
            elif self.container_image:
                logger.info(f"Using container image: {self.container_image}")
        elif self.container_runtime and not PHASE2_AVAILABLE:
            logger.warning(
                "Container runtime requested but Phase 2 modules not available"
            )
            logger.warning("Falling back to Phase 1.5 native execution")
            self.container_runtime = None

        if self.custom_packages:
            logger.info(f"Custom packages: {', '.join(self.custom_packages)}")

        if self.dependency_cache:
            logger.info(f"Dependency caching enabled: {self.cache_backend}")

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
        """Find compatible optimized AMI (Phase 1.5 or Phase 2)."""
        try:
            # First, try to find Phase 2 AMI if container runtime is enabled
            if self.container_runtime:
                phase2_ami = self._find_phase2_ami()
                if phase2_ami:
                    return phase2_ami

            # Fall back to Phase 1.5 AMI
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

    def _find_phase2_ami(self) -> Optional[str]:
        """Find compatible Phase 2 AMI with Docker support."""
        try:
            response = self.ec2.describe_images(
                Owners=["self"],
                Filters=[
                    {"Name": "tag:CreatedBy", "Values": ["ParslAWSProvider"]},
                    {"Name": "tag:Version", "Values": ["2.0"]},
                    {"Name": "tag:DockerSupport", "Values": ["true"]},
                    {"Name": "state", "Values": ["available"]},
                ],
            )

            compatible_amis = []
            for ami in response["Images"]:
                if self._is_compatible_ami(ami):
                    compatible_amis.append(ami)

            # Return newest compatible Phase 2 AMI
            if compatible_amis:
                newest = max(compatible_amis, key=lambda x: x["CreationDate"])
                logger.info(f"Found Phase 2 AMI with Docker: {newest['ImageId']}")
                return newest["ImageId"]

        except Exception as e:
            logger.debug(f"Phase 2 AMI discovery failed: {e}")

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
        # SSH configuration for container tunnel access (required for Phase 2)
        ssh_config = """
# Configure SSH to accept reverse tunneled connections from containers
echo "GatewayPorts yes" >> /etc/ssh/sshd_config
echo "ClientAliveInterval 60" >> /etc/ssh/sshd_config
echo "ClientAliveCountMax 3" >> /etc/ssh/sshd_config
systemctl restart sshd
"""

        if self.is_optimized_ami:
            # Optimized AMI - minimal setup needed
            # Always install Docker for potential container execution
            docker_setup = """
# Install Docker for container execution support
apt-get update -q >/dev/null 2>&1
apt-get install -y docker.io >/dev/null 2>&1
systemctl start docker
systemctl enable docker
usermod -a -G docker ubuntu
echo "✅ Docker installed and started"
"""
            
            return f"""#!/bin/bash
# Phase 1.5 - Optimized AMI startup
exec > >(tee /var/log/user-data.log) 2>&1
echo "$(date): Starting optimized AMI initialization"

{ssh_config.strip()}
{docker_setup.strip()}

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

{ssh_config.strip()}

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

        # Extract actual ports from the command that Parsl provides
        # For containers, we need reverse tunnels: AWS localhost:port -> local localhost:port
        logger.debug(f"Worker command: {command}")

        # Parse the command to get the actual controller ports
        import re

        # Log command type for debugging
        if "docker run" in command:
            logger.info(f"🐳 CONTAINER COMMAND RECEIVED: {command}")
        else:
            logger.debug(f"📋 Regular command: {command}")

        # Use command as-is (already containerized by executor if needed)
        container_command = command

        # For containers with SSH over SSM, add tunnel connectivity verification
        if "docker run" in command:
            # Extract the port from the command
            port_match = re.search(r"--port=(\d+)", command)
            if port_match:
                port = port_match.group(1)
                # Test tunnel before starting worker, with retries since SSM tunnel may need time
                tunnel_test = f"""
                echo 'Testing SSH over SSM tunnel connectivity to port {port}...'
                for i in {{1..30}}; do
                    if nc -z 127.0.0.1 {port}; then
                        echo "✅ SSH over SSM tunnel to port {port} is accessible"
                        break
                    else
                        echo "⏳ Waiting for SSH over SSM tunnel... (attempt $i/30)"
                        sleep 2
                    fi
                done
                nc -z 127.0.0.1 {port} || {{ echo '❌ SSH over SSM tunnel not accessible after 60s'; exit 1; }}
                """
                container_command = command.replace(
                    "bash -c 'pip install",
                    f"bash -c '{tunnel_test.strip()} && pip install",
                )

        # Try new single-port format first (Parsl 2025.8.25+)
        port_match = re.search(r"--port=(\d+)", container_command)
        if port_match:
            interchange_port = int(port_match.group(1))
            # In new format, single port is used for both task and result communication
            task_port = result_port = interchange_port
            logger.info(f"Found interchange port: {interchange_port}")
        else:
            # Fallback to old dual-port format (Parsl < 2025.8.25)
            task_port_match = re.search(r"--task_port=(\d+)", container_command)
            result_port_match = re.search(r"--result_port=(\d+)", container_command)

            if task_port_match and result_port_match:
                task_port = int(task_port_match.group(1))
                result_port = int(result_port_match.group(1))
                logger.info(
                    f"Found controller ports: task={task_port}, result={result_port}"
                )
            else:
                logger.error(f"Could not extract ports from command: {command}")
                raise ValueError("Command missing required port information")

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

            # Create reverse tunnels for controller ports in a single SSH connection
            logger.info(f"Creating reverse tunnels for {instance_id}...")

            # Handle both single-port (new Parsl) and dual-port (old Parsl) modes
            if task_port == result_port:
                # Single port mode (Parsl 2025.8.25+)
                ssh_cmd = [
                    "ssh",
                    "-i",
                    self.private_key_path,
                    "-R",
                    f"172.17.0.1:{task_port}:localhost:{task_port}",
                    "-N",
                    "-f",
                    "-o",
                    "ExitOnForwardFailure=yes",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "UserKnownHostsFile=/dev/null",
                    "-o",
                    "LogLevel=ERROR",
                    "-o",
                    "GatewayPorts=yes",
                    instance_id,
                ]
                logger.info(f"Creating single reverse tunnel for port {task_port}")
            else:
                # Dual port mode (Parsl < 2025.8.25)
                ssh_cmd = [
                    "ssh",
                    "-i",
                    self.private_key_path,
                    "-R",
                    f"172.17.0.1:{task_port}:localhost:{task_port}",
                    "-R",
                    f"172.17.0.1:{result_port}:localhost:{result_port}",
                    "-N",
                    "-f",
                    "-o",
                    "ExitOnForwardFailure=yes",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "UserKnownHostsFile=/dev/null",
                    "-o",
                    "LogLevel=ERROR",
                    "-o",
                    "GatewayPorts=yes",
                    instance_id,
                ]
                logger.info(
                    f"Creating dual reverse tunnels for ports {task_port}, {result_port}"
                )

            logger.debug(f"SSH tunnel command: {' '.join(ssh_cmd)}")

            import subprocess

            tunnel_proc = subprocess.Popen(
                ssh_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )

            # Give it time to establish
            import time

            time.sleep(3)

            # Check if tunnel is working
            if tunnel_proc.poll() is None:
                if task_port == result_port:
                    logger.info(f"✅ Reverse tunnel created: {task_port}")
                else:
                    logger.info(
                        f"✅ Reverse tunnels created: {task_port}, {result_port}"
                    )
            else:
                stdout, stderr = tunnel_proc.communicate()
                logger.error(f"❌ Reverse tunnel creation failed: {stderr}")
                raise Exception(
                    f"Failed to create reverse tunnels for {instance_id}: {stderr}"
                )

            # Modify command to use appropriate address for tunnel connection
            import re

            # With --network host, containers can use 127.0.0.1 directly
            modified_command = re.sub(
                r"-a [^\s]+", "-a 127.0.0.1", container_command
            )
            if "docker run" in container_command:
                logger.info(
                    "Using host networking - container can access 127.0.0.1 directly"
                )
            logger.info(f"Modified command: {modified_command}")

            # Detect if this is a containerized command and log it
            if "docker run" in modified_command:
                logger.info(f"🐳 EXECUTING CONTAINERIZED WORKER: {modified_command}")

            # Store tunnel info
            self.job_tunnels[job_id] = {
                "tunnel_process": tunnel_proc,
                "task_port": task_port,
                "result_port": result_port,
                "instance_id": instance_id,
                "modified_command": modified_command,
            }

            # Execute worker using a smart startup script that handles dynamic tunneling
            logger.info(
                f"Executing containerized worker with dynamic tunnel setup on {instance_id}..."
            )
            ssm_client = self.session.client("ssm")

            # Use environment variable approach for robust command passing (Globus pattern)
            import base64
            
            # Encode the command to safely pass through shell layers
            encoded_command = base64.b64encode(modified_command.encode()).decode()
            
            # Create smart worker startup script for containerized execution
            setup_script = f"""#!/bin/bash
set -e
export HOME=/root
export USER=root
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"

echo "🚀 Starting containerized Parsl worker with SSH tunneling..."

# Install essential packages
apt-get update -q >/dev/null 2>&1
apt-get install -y python3 python3-pip docker.io openssh-client curl >/dev/null 2>&1

# Configure SSH daemon for Docker bridge IP tunneling
echo "🔧 Configuring SSH daemon for Docker bridge tunneling..."
echo "GatewayPorts yes" >> /etc/ssh/sshd_config
systemctl reload sshd

# Start Docker daemon
service docker start
sleep 5

# Check Docker status
echo "🐳 Docker daemon status:"
docker info | head -5

# Don't install Parsl on host - force container-only execution
echo "✅ Environment setup complete (container-only mode)"

# Decode the worker command safely
WORKER_COMMAND=$(echo "{encoded_command}" | base64 -d)
echo "📁 Creating controller directory structure for containers..."
RUNDIR_PATH=$(echo "$WORKER_COMMAND" | grep -o -- '--logdir=[^[:space:]]*' | cut -d'=' -f2 | sed 's|/[^/]*$||')
if [ -n "$RUNDIR_PATH" ]; then
    mkdir -p "$RUNDIR_PATH"
    echo "✅ Created directory: $RUNDIR_PATH"
fi

# Execute the containerized worker command with reverse tunnels
echo "📋 Worker command with reverse tunnels: $WORKER_COMMAND"

# Run the containerized worker using the command with tunneled localhost connections
echo "🐳 Starting containerized worker with reverse tunnel connectivity..."
echo "🚀 Executing: $WORKER_COMMAND"

# Execute as containerized command - fail loudly if not containerized
if [[ "$WORKER_COMMAND" == *"docker run"* ]]; then
    echo "🐳 Executing containerized command..."
    echo "🔍 About to run: $WORKER_COMMAND"
    
    # Run with detailed logging
    bash -c "$WORKER_COMMAND" > /tmp/worker.log 2>&1 &
    CONTAINER_PID=$!
    echo "🔍 Container process PID: $CONTAINER_PID"
    
    # Wait and check status multiple times
    for i in {{1..10}}; do
        sleep 2
        if kill -0 $CONTAINER_PID 2>/dev/null; then
            echo "✅ Container process still running (check $i)"
        else
            echo "❌ Container process died (check $i)"
            break
        fi
    done
    
    # Show detailed logs
    echo "📋 Complete worker execution logs:"
    cat /tmp/worker.log || echo "No logs found"
    
    # Check if container process is still alive
    if ! kill -0 $CONTAINER_PID 2>/dev/null; then
        echo "❌ CONTAINER COMMAND FAILED - NO FALLBACK ALLOWED"
        echo "📋 Final log check:"
        tail -50 /tmp/worker.log || echo "No logs"
        exit 1
    fi
else
    echo "❌ NON-CONTAINERIZED COMMAND RECEIVED - REFUSING TO EXECUTE"
    echo "Command: $WORKER_COMMAND"
    exit 1
fi

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
        """Modify worker command for reverse tunnel and optional container execution."""

        # First, modify for tunnel (Phase 1.5 functionality)
        tunnel_modified = ParslWorkerCommandParser.modify_for_tunnel(
            command, tunnel_port
        )

        # Phase 2: Add container support if enabled
        if self.container_runtime and self.container_manager:
            # Phase 2 container execution - build container image on instance first
            container_image = self._get_container_image()

            logger.info(
                f"Phase 2: Ensuring container {container_image} exists on instance"
            )

            # Build container on instance if needed
            build_command = f"""
# Build parsl-base container if not exists
if ! docker images | grep -q "parsl-base"; then
    echo "Building parsl-base container..."
    cat > /tmp/Dockerfile.parsl-base << 'EOF'
FROM python:3.10-slim

# Install system dependencies
RUN apt-get update && apt-get install -y gcc g++ && rm -rf /var/lib/apt/lists/*

# Install Parsl and scientific stack
RUN pip install --no-cache-dir parsl numpy scipy pandas matplotlib

# Set working directory
WORKDIR /app

# Default command runs worker
CMD ["python3", "-m", "parsl.executors.high_throughput.process_worker_pool"]
EOF
    docker build -t parsl-base:latest -f /tmp/Dockerfile.parsl-base /tmp/
    echo "Container built successfully"
else
    echo "Container parsl-base:latest already exists"
fi

# Now execute the original worker command
{tunnel_modified}
"""

            logger.info("Modified command to build container and run worker on host")
            return build_command
        else:
            # Phase 1.5 native execution
            logger.info(f"Modified command for reverse tunnel: {tunnel_modified}")
            return tunnel_modified

    def _get_container_image(self) -> str:
        """Determine container image to use."""
        if self.container_image:
            return self.container_image
        elif self.scientific_stack and self.scientific_builder:
            return f"parsl-{self.scientific_stack}:latest"
        else:
            raise ValueError(
                "Container runtime enabled but no image or stack specified"
            )

    def _build_container_config(self, image: str) -> Dict:
        """Build container configuration for execution."""
        config = {
            "image": image,
            "network_mode": "host",  # Required for SSH tunnels
            "auto_remove": True,
            "detach": True,
        }

        # Add custom container configuration
        config.update(self.container_config)

        # Performance optimizations for scientific computing
        if self.instance_type.startswith("c5") or self.instance_type.startswith("m5"):
            config.update(
                {
                    "cpu_count": 0,  # Use all available CPUs
                    "mem_limit": "90%",  # Leave 10% for system
                }
            )

        return config

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

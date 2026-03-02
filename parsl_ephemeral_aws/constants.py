"""
Clean constants for the EphemeralAWSProvider.

No legacy garbage, just what's actually needed.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

# Operating modes
DEFAULT_MODE = "standard"

# AWS Regions
DEFAULT_REGION = "us-east-1"

# EC2 instance types
DEFAULT_INSTANCE_TYPE = "t3.micro"

# Block configuration
DEFAULT_MIN_BLOCKS = 0
DEFAULT_MAX_BLOCKS = 10

# Default worker initialization script for Amazon Linux 2023.
# AL2023 default python3 is 3.9; parsl>=2026.1.5 requires Python 3.10+.
# Install python3.11, symlink it as the default python3, then install parsl.
DEFAULT_WORKER_INIT = (
    "dnf install -y python3.11 python3.11-pip\n"
    "ln -sf /usr/bin/python3.11 /usr/bin/python3\n"
    "pip3.11 install --quiet --upgrade parsl\n"
)

# Resource management
DEFAULT_MAX_IDLE_TIME = 300  # 5 minutes in seconds

# Networking defaults
DEFAULT_VPC_CIDR = "10.0.0.0/16"
DEFAULT_SUBNET_CIDR = "10.0.1.0/24"  # Alias for compatibility
DEFAULT_PUBLIC_SUBNET_CIDR = "10.0.0.0/24"
DEFAULT_PRIVATE_SUBNET_CIDR = "10.0.1.0/24"

# Security group defaults
DEFAULT_SECURITY_GROUP_NAME = "parsl-ephemeral-sg"
DEFAULT_SG_NAME = DEFAULT_SECURITY_GROUP_NAME  # Alias for compatibility
DEFAULT_SECURITY_GROUP_DESCRIPTION = "Security group for Parsl ephemeral resources"

# Clean, simple security rules - no broken legacy stuff
DEFAULT_INBOUND_RULES = []  # Empty by default - will be set programmatically

# Security framework constants
DEFAULT_SECURITY_ENVIRONMENT = "dev"  # Options: "dev", "staging", "prod"
DEFAULT_STRICT_SECURITY_MODE = False  # Set to True for production environments
DEFAULT_ADMIN_CIDR_BLOCKS = ["10.0.0.0/8"]  # Administrative access networks
DEFAULT_ALLOW_VPC_INTERNAL = True  # Allow communication within VPC

# Default outbound rules (allow all - commonly acceptable)
DEFAULT_OUTBOUND_RULES = [
    {
        "IpProtocol": "-1",
        "FromPort": -1,
        "ToPort": -1,
        "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
    }
]

# AMI mappings for different regions (Amazon Linux 2023)
# Updated 2026-03-01 — al2023-ami-2023.10.20260216.1-kernel-6.12-x86_64
DEFAULT_AMI_MAPPING = {
    "us-east-1": "ami-0f3caa1cf4417e51b",  # N. Virginia
    "us-east-2": "ami-0d5503fb907719409",  # Ohio
    "us-west-1": "ami-0e2de80e7636c4837",  # N. California
    "us-west-2": "ami-075b5421f670d735c",  # Oregon
    "af-south-1": "ami-08d7290d17859bd2e",  # Cape Town
    "ap-east-1": "ami-0d96ec8a788679eb2",  # Hong Kong
    "ap-southeast-2": "ami-0a11f7293cd9a562e",  # Sydney
    "ap-southeast-1": "ami-0ac0e4288aa341886",  # Singapore
    "ap-northeast-1": "ami-088103e734f7e0529",  # Tokyo
    "ap-northeast-2": "ami-0ef0d6b9c5b7d9c81",  # Seoul
    "ap-northeast-3": "ami-088a969d6f085cca3",  # Osaka
    "ap-south-1": "ami-07e8927ba33de363c",  # Mumbai
    "ca-central-1": "ami-0b512d33ad3b7b983",  # Canada
    "eu-central-1": "ami-0c42fad2ea005202d",  # Frankfurt
    "eu-west-1": "ami-09c20105c9b62f893",  # Ireland
    "eu-west-2": "ami-06f89d8f36d17aa27",  # London
    "eu-west-3": "ami-0a13801de97493e85",  # Paris
    "eu-north-1": "ami-03df6dab118053bcb",  # Stockholm
    "eu-south-1": "ami-079fed56921cf99b9",  # Milan
    "me-south-1": "ami-03509ba459e8172c7",  # Bahrain
    "sa-east-1": "ami-0a4cf2f3770eb3f5e",  # São Paulo
}

# EC2 status mapping to Parsl job states
EC2_STATUS_MAPPING = {
    "pending": "PENDING",
    "running": "RUNNING",
    "shutting-down": "COMPLETED",
    "terminated": "COMPLETED",
    "stopping": "COMPLETED",
    "stopped": "COMPLETED",
}

# Resource type constants
RESOURCE_TYPE_VPC = "vpc"
RESOURCE_TYPE_SUBNET = "subnet"
RESOURCE_TYPE_SECURITY_GROUP = "security-group"
RESOURCE_TYPE_EC2 = "ec2-instance"
RESOURCE_TYPE_SPOT_FLEET = "spot-fleet"
RESOURCE_TYPE_BASTION = "bastion"
RESOURCE_TYPE_CLOUDFORMATION = "cloudformation"
RESOURCE_TYPE_LAMBDA_FUNCTION = "lambda_function"
RESOURCE_TYPE_ECS_TASK = "ecs_task"

# Spot fleet constants
SPOT_FLEET_TARGET_CAPACITY_TYPE = "TargetCapacity"
SPOT_FLEET_FULFILLED_CAPACITY_TYPE = "FulfilledCapacity"
SPOT_FLEET_DEFAULT_ALLOCATION_STRATEGY = "capacity-optimized"

# Cleanup constants
CLEANUP_BATCH_SIZE = 10
MAX_CLEANUP_RETRIES = 3
CLEANUP_RETRY_DELAY = 5  # seconds

# Spot instance defaults
DEFAULT_SPOT_ALLOCATION_STRATEGY = "capacity-optimized"
DEFAULT_SPOT_INSTANCE_INTERRUPTION_BEHAVIOR = "terminate"
DEFAULT_SPOT_INTERRUPTION_CHECK_INTERVAL = 30  # seconds
DEFAULT_SPOT_INTERRUPTION_LEAD_TIME = 120  # seconds
DEFAULT_SPOT_CHECKPOINT_INTERVAL = 60  # seconds
DEFAULT_SPOT_MAX_RECOVERY_ATTEMPTS = 3

# Tag defaults
DEFAULT_TAG_PREFIX = "parsl-ephemeral"
TAG_PREFIX = DEFAULT_TAG_PREFIX  # Alias for compatibility
TAG_NAME = "Name"
TAG_WORKFLOW_ID = "WorkflowId"
TAG_JOB_ID = "JobId"
TAG_BLOCK_ID = "BlockId"
DEFAULT_REQUIRED_TAGS = {
    "Name": "parsl-ephemeral",
    "CreatedBy": "ParslEphemeralAWSProvider",
    "AutoCleanup": "true",
}

# Security group aliases for compatibility
DEFAULT_SG_NAME = DEFAULT_SECURITY_GROUP_NAME

# Worker type constants (minimal for import compatibility)
WORKER_TYPE_LAMBDA = "lambda"
WORKER_TYPE_ECS = "ecs"
WORKER_TYPE_AUTO = "auto"

# Status constants
STATUS_PENDING = "PENDING"
STATUS_RUNNING = "RUNNING"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"
STATUS_CANCELED = "CANCELED"
STATUS_CANCELLED = "CANCELED"  # British spelling alias
STATUS_UNKNOWN = "UNKNOWN"
STATUS_SUCCEEDED = "COMPLETED"  # Alias for compatibility
STATUS_WARM = "WARM"  # instance running, job done, ready for reuse

# Warm pool defaults
DEFAULT_WARM_POOL_SIZE = 0  # 0 = disabled
DEFAULT_WARM_POOL_TTL = 600  # seconds a warm idle instance stays alive

# AMI baking defaults
DEFAULT_BAKE_AMI = False  # bake worker_init into a custom AMI during initialize()

# Lambda defaults (minimal for imports)
DEFAULT_LAMBDA_TIMEOUT = 300
DEFAULT_LAMBDA_RUNTIME = "python3.9"
DEFAULT_LAMBDA_HANDLER = "handler.lambda_handler"
DEFAULT_LAMBDA_MEMORY = 1024

# ECS defaults (minimal for imports)
DEFAULT_ECS_TASK_CPU = 1024
DEFAULT_ECS_TASK_MEMORY = 2048
DEFAULT_ECS_CPU = 1024  # Alias
DEFAULT_ECS_MEMORY = 2048  # Alias
DEFAULT_ECS_CONTAINER_IMAGE = "public.ecr.aws/lambda/python:3.9"
DEFAULT_ECS_CLUSTER_NAME = "parsl-ephemeral-cluster"

# Timeout constants (in seconds)
DEFAULT_RESOURCE_CREATION_TIMEOUT = 300  # 5 minutes
DEFAULT_RESOURCE_DELETION_TIMEOUT = 180  # 3 minutes
DEFAULT_INSTANCE_BOOT_TIMEOUT = 600  # 10 minutes

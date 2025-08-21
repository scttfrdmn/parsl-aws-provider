"""
Constants and default values for the EphemeralAWSProvider.

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

# Default worker initialization script
DEFAULT_WORKER_INIT = """#!/bin/bash
pip install parsl
"""

# Resource management
DEFAULT_MAX_IDLE_TIME = 300  # 5 minutes in seconds

# Networking defaults
DEFAULT_VPC_CIDR = "10.0.0.0/16"
DEFAULT_PUBLIC_SUBNET_CIDR = "10.0.0.0/24"
DEFAULT_PRIVATE_SUBNET_CIDR = "10.0.1.0/24"

# Security group defaults
DEFAULT_SECURITY_GROUP_NAME = "parsl-ephemeral-sg"
DEFAULT_SECURITY_GROUP_DESCRIPTION = "Security group for Parsl ephemeral resources"

# Default inbound rules for security groups
DEFAULT_INBOUND_RULES = [
    {
        "IpProtocol": "tcp",
        "FromPort": 22,
        "ToPort": 22,
        "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
    },
    {
        "IpProtocol": "tcp",
        "FromPort": 53,
        "ToPort": 53,
        "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
    },
    {
        "IpProtocol": "udp",
        "FromPort": 53,
        "ToPort": 53,
        "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
    },
    {
        "IpProtocol": "tcp",
        "FromPort": 80,
        "ToPort": 80,
        "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
    },
    {
        "IpProtocol": "tcp",
        "FromPort": 443,
        "ToPort": 443,
        "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
    },
    # Parsl communication
    {
        "IpProtocol": "tcp",
        "FromPort": 54000,
        "ToPort": 55000,
        "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
    },
]

# Default outbound rules (allow all)
DEFAULT_OUTBOUND_RULES = [
    {
        "IpProtocol": "-1",  # All protocols
        "FromPort": -1,  # All ports
        "ToPort": -1,  # All ports
        "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
    }
]

# Lambda defaults
DEFAULT_LAMBDA_MEMORY_SIZE = 1024  # MB
DEFAULT_LAMBDA_TIMEOUT = 300  # seconds
DEFAULT_LAMBDA_RUNTIME = "python3.12"
DEFAULT_LAMBDA_HANDLER = "handler.lambda_handler"

# ECS defaults
DEFAULT_ECS_TASK_CPU = 1024  # CPU units
DEFAULT_ECS_TASK_MEMORY = 2048  # MB
DEFAULT_ECS_CLUSTER_NAME = "parsl-ephemeral-cluster"

# Spot instance defaults
DEFAULT_SPOT_ALLOCATION_STRATEGY = "capacity-optimized"
DEFAULT_SPOT_INSTANCE_INTERRUPTION_BEHAVIOR = "terminate"
DEFAULT_SPOT_INTERRUPTION_CHECK_INTERVAL = (
    30  # seconds - how often to check for interruption notices
)
DEFAULT_SPOT_INTERRUPTION_LEAD_TIME = (
    120  # seconds - minimum lead time for recovery before termination
)
DEFAULT_SPOT_CHECKPOINT_INTERVAL = (
    60  # seconds - how often to checkpoint long-running tasks
)
DEFAULT_SPOT_MAX_RECOVERY_ATTEMPTS = 3  # maximum number of recovery attempts for a task

# Tag defaults
DEFAULT_TAG_PREFIX = "parsl-ephemeral"
DEFAULT_REQUIRED_TAGS = {
    "Name": "parsl-ephemeral",
    "CreatedBy": "ParslEphemeralAWSProvider",
    "AutoCleanup": "true",
}

# CloudFormation defaults
DEFAULT_CLOUDFORMATION_STACK_NAME_PREFIX = "parsl-ephemeral"

# Timeout defaults (in seconds)
DEFAULT_RESOURCE_CREATION_TIMEOUT = 300
DEFAULT_RESOURCE_DELETION_TIMEOUT = 300
DEFAULT_CONNECTION_TIMEOUT = 120
DEFAULT_COMMAND_TIMEOUT = 60

# Status constants
STATUS_PENDING = "PENDING"
STATUS_RUNNING = "RUNNING"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"
STATUS_CANCELED = "CANCELED"
STATUS_CANCELLED = "CANCELED"  # British spelling alias
STATUS_UNKNOWN = "UNKNOWN"
STATUS_SUCCEEDED = "COMPLETED"  # Alias for compatibility

# Parsl status mapping
# Maps provider-specific statuses to Parsl's standard statuses
EC2_STATUS_MAPPING = {
    "pending": STATUS_PENDING,
    "running": STATUS_RUNNING,
    "shutting-down": STATUS_CANCELED,
    "terminated": STATUS_COMPLETED,
    "stopping": STATUS_CANCELED,
    "stopped": STATUS_CANCELED,
}

LAMBDA_STATUS_MAPPING = {
    "Pending": STATUS_PENDING,
    "Active": STATUS_RUNNING,
    "Inactive": STATUS_COMPLETED,
    "Failed": STATUS_FAILED,
}

ECS_STATUS_MAPPING = {
    "PROVISIONING": STATUS_PENDING,
    "PENDING": STATUS_PENDING,
    "ACTIVATING": STATUS_PENDING,
    "RUNNING": STATUS_RUNNING,
    "DEACTIVATING": STATUS_CANCELED,
    "STOPPING": STATUS_CANCELED,
    "DEPROVISIONING": STATUS_CANCELED,
    "STOPPED": STATUS_COMPLETED,
}

# Resource type constants
RESOURCE_TYPE_EC2 = "ec2_instance"
RESOURCE_TYPE_LAMBDA = "lambda_function"
RESOURCE_TYPE_LAMBDA_FUNCTION = "lambda_function"  # Alias for compatibility
RESOURCE_TYPE_ECS_TASK = "ecs_task"
RESOURCE_TYPE_VPC = "vpc"
RESOURCE_TYPE_SUBNET = "subnet"
RESOURCE_TYPE_SECURITY_GROUP = "security_group"
RESOURCE_TYPE_INTERNET_GATEWAY = "internet_gateway"
RESOURCE_TYPE_ROUTE_TABLE = "route_table"
RESOURCE_TYPE_BASTION = "bastion_host"
RESOURCE_TYPE_CLUSTER = "ecs_cluster"
RESOURCE_TYPE_CLOUDFORMATION_STACK = "cloudformation_stack"
RESOURCE_TYPE_CLOUDFORMATION = "cloudformation_stack"  # Alias for compatibility
RESOURCE_TYPE_SPOT_FLEET = "spot_fleet"

# Worker type constants
WORKER_TYPE_LAMBDA = "lambda"
WORKER_TYPE_ECS = "ecs"
WORKER_TYPE_AUTO = "auto"

# Parsl worker environment variables
WORKER_ENV_VARS = {
    "PARSL_WORKER_ID": "%(worker_id)s",
    "PARSL_PROVIDER_ID": "%(provider_id)s",
    "PARSL_JOB_ID": "%(job_id)s",
    "PARSL_INSTANCE_ID": "%(instance_id)s",
}

# Default AMI lookup by region
# Latest Amazon Linux 2023 AMIs as of date of implementation
DEFAULT_AMI_MAPPING = {
    "us-east-1": "ami-080e1f13689e07408",  # N. Virginia
    "us-east-2": "ami-03d21eed81858c120",  # Ohio
    "us-west-1": "ami-0d5b7dce3973d8817",  # N. California
    "us-west-2": "ami-0473ec1595e64e666",  # Oregon
    "af-south-1": "ami-08d7290d17859bd2e",  # Cape Town
    "ap-east-1": "ami-0d96ec8a788679eb2",  # Hong Kong
    "ap-southeast-2": "ami-068d77de57cf72650",  # Sydney
    "ap-southeast-1": "ami-05400835b426ad39e",  # Singapore
    "ap-northeast-1": "ami-0df7d959e1ae99093",  # Tokyo
    "ap-northeast-2": "ami-0ef0d6b9c5b7d9c81",  # Seoul
    "ap-northeast-3": "ami-088a969d6f085cca3",  # Osaka
    "ap-south-1": "ami-07e8927ba33de363c",  # Mumbai
    "ca-central-1": "ami-0b512d33ad3b7b983",  # Canada
    "eu-central-1": "ami-06ca3d9ec5caa8d5c",  # Frankfurt
    "eu-west-1": "ami-09961115387019735",  # Ireland
    "eu-west-2": "ami-06f89d8f36d17aa27",  # London
    "eu-west-3": "ami-0a13801de97493e85",  # Paris
    "eu-north-1": "ami-03df6dab118053bcb",  # Stockholm
    "eu-south-1": "ami-079fed56921cf99b9",  # Milan
    "me-south-1": "ami-03509ba459e8172c7",  # Bahrain
    "sa-east-1": "ami-0f8fd9992b25a9090",  # São Paulo
}

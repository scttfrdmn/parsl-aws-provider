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

# Amazon Linux 2023 AMI resolution (#84).
#
# AMIs are resolved at runtime from AWS's public SSM Parameter Store aliases,
# which every account can read without credentials of its own for the parameter
# and which AWS repoints at each new AL2023 release. A hardcoded region->AMI
# table cannot work: the previous one was stamped 2026-03-01 and by 2026-07-30
# *all 21 entries* were unusable -- 9 carried a DeprecationTime of 2026-05-17,
# 6 returned InvalidAMIID.NotFound, 2 were InvalidAMIID.Malformed, and the rest
# were unreachable. Nothing in the package noticed, because a deprecated AMI
# still launches until AWS deletes it.
#
# ``kernel-default`` is AWS's version-independent alias. Naming a specific
# kernel here (6.1, 6.12, 6.18, ...) would only re-create the staleness this
# change removes.
AMI_SSM_PARAMETER_PREFIX = "/aws/service/ami-amazon-linux-latest"
AMI_SSM_PARAMETER_TEMPLATE = (
    AMI_SSM_PARAMETER_PREFIX + "/al2023-ami-kernel-default-{architecture}"
)

# EC2 architecture identifiers, as ``describe_instance_types`` reports them in
# ProcessorInfo.SupportedArchitectures.
ARCHITECTURE_X86_64 = "x86_64"
ARCHITECTURE_ARM64 = "arm64"
DEFAULT_ARCHITECTURE = ARCHITECTURE_X86_64

# Instance families that are Graviton (arm64) despite carrying no "g" in their
# generation suffix. a1 is the original Graviton family and predates the naming
# convention that every later arm64 family follows.
ARM64_INSTANCE_FAMILIES = frozenset({"a1"})

# Offline fallback for ``get_default_ami()``, used only when the SSM lookup
# above fails -- chiefly so moto- and substrate-backed tests need no network.
#
# Treat these as expiring the day they are written. Refreshed 2026-07-30 from
# the kernel-default SSM alias, but AWS deprecates each AL2023 AMI roughly two
# months after release, so this table is a last resort and not a source of
# truth. It is x86_64-only, and get_default_ami() deliberately refuses rather
# than hand one of these to an arm64 instance type.
#
# The four opt-in regions the previous table listed (af-south-1, ap-east-1,
# eu-south-1, me-south-1) are omitted: they cannot be read without enabling the
# region, so no value could be verified. SSM resolves them normally from an
# account that has them enabled.
DEFAULT_AMI_MAPPING = {
    "us-east-1": "ami-0006118602dfc1c09",  # N. Virginia
    "us-east-2": "ami-06dd88604c99ec11f",  # Ohio
    "us-west-1": "ami-0be92a0ad760d0371",  # N. California
    "us-west-2": "ami-0b76d82b547c3c077",  # Oregon
    "ap-northeast-1": "ami-03107d83a97af3820",  # Tokyo
    "ap-northeast-2": "ami-04bb4ddfc0e51bb5e",  # Seoul
    "ap-northeast-3": "ami-0cd939ceb3b70a09d",  # Osaka
    "ap-south-1": "ami-0884624fc54d115f3",  # Mumbai
    "ap-southeast-1": "ami-094819e1130d6d35b",  # Singapore
    "ap-southeast-2": "ami-0cb938ea8bf5b7973",  # Sydney
    "ca-central-1": "ami-06171593517b6bb1f",  # Canada
    "eu-central-1": "ami-0352a6b853b4367b3",  # Frankfurt
    "eu-north-1": "ami-0c783070b2e26d98c",  # Stockholm
    "eu-west-1": "ami-02c25106ee38f6087",  # Ireland
    "eu-west-2": "ami-0e3771f9c18926b8e",  # London
    "eu-west-3": "ami-033623a76c8038cf0",  # Paris
    "sa-east-1": "ami-0b44997419d0b0d38",  # Sao Paulo
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

# Allocation strategy (#84). ``price-capacity-optimized`` is AWS's current
# recommendation: it picks from the pools with the deepest spare capacity and
# then the lowest price among those, so it interrupts far less often than
# ``lowest-price`` at close to the same cost.
#
# The two fleet APIs spell the same enum differently, and each rejects the
# other's spelling. Verified against real EC2 in us-east-1:
#
#   RequestSpotFleet  SpotFleetRequestConfig.AllocationStrategy -> camelCase
#       "price-capacity-optimized" => InvalidParameterValue
#   CreateFleet       SpotOptions.AllocationStrategy            -> kebab-case
#       "priceCapacityOptimized"   => InvalidParameter
#
# So there cannot be one constant for both. SPOT_FLEET_* is the camelCase form
# for the legacy RequestSpotFleet path this package uses today; EC2_FLEET_* is
# the kebab-case form for the CreateFleet migration in #86.
SPOT_FLEET_DEFAULT_ALLOCATION_STRATEGY = "priceCapacityOptimized"
EC2_FLEET_DEFAULT_ALLOCATION_STRATEGY = "price-capacity-optimized"

# Accepted values for each API, so a caller-supplied strategy can be rejected
# with a useful message instead of an opaque InvalidParameterValue from EC2.
SPOT_FLEET_ALLOCATION_STRATEGIES = frozenset(
    {
        "lowestPrice",
        "diversified",
        "capacityOptimized",
        "capacityOptimizedPrioritized",
        "priceCapacityOptimized",
    }
)
EC2_FLEET_ALLOCATION_STRATEGIES = frozenset(
    {
        "lowest-price",
        "diversified",
        "capacity-optimized",
        "capacity-optimized-prioritized",
        "price-capacity-optimized",
    }
)

# Cleanup constants
CLEANUP_BATCH_SIZE = 10
MAX_CLEANUP_RETRIES = 3
CLEANUP_RETRY_DELAY = 5  # seconds

# Spot instance defaults.
#
# This is the *user-facing* default for the ``spot_allocation_strategy`` kwarg.
# It stays kebab-case -- that is what the provider and mode docstrings have
# always documented, and what CreateFleet takes -- and
# ``normalize_spot_fleet_allocation_strategy()`` translates it to camelCase at
# the RequestSpotFleet boundary. See SPOT_FLEET_DEFAULT_ALLOCATION_STRATEGY.
DEFAULT_SPOT_ALLOCATION_STRATEGY = "price-capacity-optimized"
DEFAULT_SPOT_INSTANCE_INTERRUPTION_BEHAVIOR = "terminate"
DEFAULT_SPOT_INTERRUPTION_CHECK_INTERVAL = 30  # seconds
DEFAULT_SPOT_INTERRUPTION_LEAD_TIME = 120  # seconds
DEFAULT_SPOT_CHECKPOINT_INTERVAL = 60  # seconds
DEFAULT_SPOT_MAX_RECOVERY_ATTEMPTS = 3

# Tag defaults
DEFAULT_TAG_PREFIX = "parsl-ephemeral"
TAG_PREFIX = DEFAULT_TAG_PREFIX  # Alias for compatibility
TAG_NAME = "Name"
# Marker tag identifying a resource as created by this provider (#109).
#
# This used to be TAG_NAME, which is the EC2-reserved "Name" key. Every tag list
# that carried a descriptive Name *and* the marker therefore sent "Name" twice,
# and EC2 rejects duplicate tag keys -- verified against real AWS:
#
#     request_spot_fleet  -> InvalidSpotFleetRequestConfig: Duplicate tag key 'Name'
#     run_instances       -> InvalidParameterValue: Duplicate tag key 'Name'
#
# moto accepts duplicates and keeps the last value, so the collision was invisible
# under test while silently overwriting the descriptive name with "true".
TAG_MANAGED = "ParslEphemeralManaged"
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
DEFAULT_ONE_SHOT = False  # each instance runs one command then terminates

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

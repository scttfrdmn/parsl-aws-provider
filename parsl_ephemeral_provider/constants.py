"""
Clean constants for the EphemeralProvider.

No legacy garbage, just what's actually needed.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
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
RESOURCE_TYPE_LAUNCH_TEMPLATE = "launch-template"
# EC2's tag resource type for an EC2 Fleet (#86). Distinct from
# RESOURCE_TYPE_SPOT_FLEET above, which is this package's own internal label for
# a fleet-backed resource record and is persisted in state documents.
RESOURCE_TYPE_FLEET = "fleet"

# Instance metadata service options applied to every launch (#85).
#
# IMDSv2 ("HttpTokens": "required") makes the metadata service reject the
# unauthenticated IMDSv1 GET, which is the request an SSRF-ed application can be
# tricked into making on the instance's behalf to read its role credentials.
# IMDSv2 requires a PUT to obtain a token first, and browsers and most proxies
# will not issue one.
#
# HttpEndpoint stays "enabled" because the SSM agent reads the instance identity
# document from it; disabling the endpoint outright would break the warm-pool
# and one-shot dispatch paths, which run over SSM.
#
# The hop limit stays at EC2's default of 2 rather than being lowered to 1:
# worker_init may run containers, and a container's request traverses the
# host's network namespace, which costs a hop. A limit of 1 would leave
# metadata unreachable from inside a container.
IMDSV2_METADATA_OPTIONS = {
    "HttpTokens": "required",
    "HttpEndpoint": "enabled",
}

# Spot fleet constants
SPOT_FLEET_TARGET_CAPACITY_TYPE = "TargetCapacity"
SPOT_FLEET_FULFILLED_CAPACITY_TYPE = "FulfilledCapacity"

# EC2 Fleet constants (#86).
#
# AWS's guidance on the API this package used to call: "Spot Fleet ... uses a
# legacy API with no planned investment." CreateFleet replaces it. The migration
# was only possible once #85 put a launch template in place, because CreateFleet
# has no LaunchSpecifications member at all -- a template is mandatory.
#
# Fleet type ``instant`` is what this package uses. It places a *synchronous*
# one-time request and returns the launched instance IDs in the CreateFleet
# response, which preserves the block -> instance-ID mapping the rest of the
# package is built on. The alternatives are asynchronous and would require
# polling before a block could report its instances.
#
# Verified against real EC2 in us-east-1, and these are not merely ignored --
# CreateFleet rejects them outright with InvalidParameter:
#
#   ReplaceUnhealthyInstances        -> "not supported for given fleet type"
#   TerminateInstancesWithExpiration -> "not supported for given fleet type"
#   SpotOptions.MaintenanceStrategies (Capacity Rebalance)
#                                    -> "only compatible with fleet type maintain"
#
# So an instant fleet gets no capacity rebalancing; the two-minute interruption
# warning is delivered instead through the EventBridge route below.
EC2_FLEET_TYPE_INSTANT = "instant"

# Deleting an instant fleet always terminates its instances -- AWS: "A deleted
# instant fleet with running instances is not supported." NoTerminateInstances is
# rejected for this fleet type, so the flag is always True.
EC2_FLEET_TERMINATE_INSTANCES = True

# Why no fleet is built by CloudFormation (#86).
#
# Every fleet in this package is created by calling CreateFleet directly. The
# override list is variable-length -- one entry per instance type -- and
# CloudFormation cannot build one. Three routes were tried against real AWS:
#
#   ``Fn::ForEach`` (Transform: AWS::LanguageExtensions) expands to a *map*, not
#   a list -- reading the processed template off a change set returned
#   ``{"InstanceTypeOverridet3.small": {...}, ...}`` where a list was needed.
#
#   A fixed number of ``!Select`` slots cannot be left partly filled: an
#   out-of-range ``!Select`` fails validation even inside the untaken branch of
#   an ``!If`` -- "Fn::Select cannot select nonexistent value at index 2".
#
#   Padding those slots by repeating a type is rejected by EC2 --
#   "InvalidFleetConfig: The fleet configuration contains duplicate instance
#   pools". Note a DryRun does *not* catch this: EC2 accepted the identical
#   duplicate-bearing request with DryRun=True and rejected it without.
#
# Tag EC2 applies to every instance a fleet launches, without being asked.
#
# This is the only way to find an instant fleet's instances after the fact.
# Verified against real EC2: ``describe_fleets()`` with no FleetIds returns an
# *empty* list for instant fleets (AWS: "If a fleet is of type instant, you must
# specify the fleet ID in the request, otherwise the fleet does not appear in
# the response"), a tag filter on describe_fleets does not find them either, and
# ``describe_fleet_instances`` refuses them with ``Unsupported``. Filtering
# describe_instances on this tag does work, so the orphan sweep goes through the
# instances rather than the fleets.
TAG_AWS_FLEET_ID = "aws:ec2:fleet-id"

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
# So there cannot be one constant for both. EC2_FLEET_* is the kebab-case form
# CreateFleet takes, which is what this package now uses (#86). SPOT_FLEET_* is
# the camelCase form for the legacy RequestSpotFleet API, still needed by the
# CloudFormation templates and the detached mode's bastion manager, which drive
# AWS::EC2::SpotFleet resources.
#
# Note that neither DryRun nor TotalTargetCapacity=0 validates these enums --
# both accepted the wrong spelling in probes, and describe_fleets showed EC2 had
# stored it verbatim. Only a real launch rejects it.
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

# Spot interruption warning delivery (#86).
#
# An ``instant`` fleet gets no Capacity Rebalance, so the two-minute warning is
# taken from EventBridge instead: a rule matching the interruption event, with an
# SQS queue as its target, which the driver polls. Verified end to end against
# real EC2 with a Fault Injection Simulator experiment
# (``aws:ec2:send-spot-instance-interruptions``) -- the warning reached the queue
# 15.2s after the experiment started, with the instance still ``running``. That
# is the whole point: the EC2-state poll this supplements cannot see anything
# until ``shutting-down``, by which time the executor has already dispatched work
# to a worker that is gone.
#
# ``EC2 Instance Rebalance Recommendation`` is deliberately *not* matched. It is
# a separate detail-type that signals elevated interruption risk, not an
# impending reclaim, and confirmed via ``test_event_pattern`` not to match this
# pattern. Treating it as an interruption would fail the blocks of healthy
# workers that were never going away.
SPOT_INTERRUPTION_EVENT_SOURCE = "aws.ec2"
SPOT_INTERRUPTION_EVENT_DETAIL_TYPE = "EC2 Spot Instance Interruption Warning"

# Retention is short on purpose: a warning is worthless once its two minutes are
# up, so an undelivered message should expire rather than be replayed against a
# long-dead instance on the next run.
SPOT_INTERRUPTION_QUEUE_RETENTION_SECONDS = 300

# SQS long-poll ceiling. 20s is the maximum ReceiveMessage accepts, and long
# polling is what keeps warning latency near-zero without spending an API call
# per second.
SPOT_INTERRUPTION_QUEUE_WAIT_SECONDS = 20

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

# Tag defaults
DEFAULT_TAG_PREFIX = "parsl-ephemeral"
TAG_PREFIX = DEFAULT_TAG_PREFIX  # Alias for compatibility
TAG_NAME = "Name"
# Launch template naming (#85). The provider ID is appended, keeping the whole
# name unique per provider instance and well inside EC2's 128-character limit.
LAUNCH_TEMPLATE_NAME_PREFIX = f"{DEFAULT_TAG_PREFIX}-lt"
# Names both the EventBridge rule and the SQS queue carrying spot interruption
# warnings (#86); the provider ID is appended, keeping them unique per provider.
#
# The warning cannot be filtered any more narrowly than by detail-type. The
# event's ``detail`` carries only ``instance-id`` and ``instance-action``, and
# instance IDs are not known until the fleet launches, so the rule necessarily
# matches every spot interruption in the account and region. Each monitor
# therefore discards warnings for instances it does not track, and two providers
# in one account each see the other's warnings and ignore them.
SPOT_INTERRUPTION_RULE_NAME_PREFIX = f"{DEFAULT_TAG_PREFIX}-spot-warning"
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
    "CreatedBy": "ParslEphemeralProvider",
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

# A spot instance AWS has issued a two-minute reclaim warning for (#137).
#
# Distinct from STATUS_FAILED so logs and state files say why the block died,
# but it maps to JobState.FAILED in the provider: from Parsl's side a reclaimed
# block did not finish its work, and FAILED is what makes the executor stop
# dispatching to it and re-run the lost tasks under its own `retries`.
#
# Without this the interruption is invisible. An interrupted instance goes to
# "shutting-down", which EC2_STATUS_MAPPING renders COMPLETED -- so a reclaimed
# block reported success and its tasks were silently dropped.
STATUS_INTERRUPTED = "INTERRUPTED"

# Warm pool defaults.
#
# A warm instance is *Running*, not Stopped, and so bills at the full on-demand
# or spot rate for every second it waits. AWS is blunt about this shape --
# keeping warm instances running is "highly discouraged to avoid incurring
# unnecessary charges" -- and the native ASG warm pool it recommends instead
# holds them Stopped or Hibernated.
#
# This package cannot use the native pool: dispatch is SSM SendCommand, and a
# Stopped instance runs no SSM agent, so it cannot receive one. Moving to a pull
# model would let the pool be Stopped and is tracked as #130 for v0.8.0.
#
# Until then the cost is bounded rather than eliminated: the TTL default is cut
# to 120s from the 600s it shipped with in v0.6.0, so an idle pool costs at most
# a fifth of what it did, and MAX_WARM_POOL_SIZE caps the blast radius of a
# mistyped size.
DEFAULT_WARM_POOL_SIZE = 0  # 0 = disabled
DEFAULT_WARM_POOL_TTL = 120  # seconds a warm *running* instance stays alive

# Hard ceiling on warm_pool_size.
#
# Every warm instance is a running instance, so an accidental warm_pool_size=200
# is a bill, not an error -- nothing in EC2 refuses it and no quota is
# necessarily hit. The limit is deliberately low: a warm pool is a latency
# optimisation for a handful of instances, and wanting dozens of idle machines
# means wanting a different mechanism.
MAX_WARM_POOL_SIZE = 20

# AMI baking defaults
DEFAULT_BAKE_AMI = False  # bake worker_init into a custom AMI during initialize()
DEFAULT_ONE_SHOT = False  # each instance runs one command then terminates

# CurveZMQ certificate distribution (#62). Off by default: it publishes the
# interchange's server secret key to Parameter Store for workers to fetch, which
# is the right trade for an encrypted channel between networks that share no
# boundary, but not something to do on a caller's behalf unasked.
DEFAULT_DISTRIBUTE_CERTIFICATES = False

# EICE reverse tunnels (#134). No default endpoint, and none is ever created:
# creating one takes several minutes, so it belongs to the pre-provisioned
# network the caller supplies (#69). Setting the ID is what turns tunnelling on.
DEFAULT_INSTANCE_CONNECT_ENDPOINT_ID = None
DEFAULT_TUNNEL_OS_USER = "ec2-user"  # matches the Amazon Linux AMIs above
DEFAULT_TUNNEL_PRIVATE_KEY_PATH = None  # generated per-provider when unset
DEFAULT_TUNNEL_PUBLIC_KEY_PATH = None

# How long to keep retrying the first connect to a new instance. "running" is not
# "sshd is up": the instance_running waiter clears well before the OS finishes
# booting, so the first few attempts are expected to fail. Bounded well inside
# HTEX's 120s heartbeat_threshold per attempt, but allowed several minutes in
# total because a cold boot legitimately takes that long.
TUNNEL_OPEN_TIMEOUT = 300
TUNNEL_OPEN_RETRY_DELAY = 10

# Detached-mode bastion defaults.
#
# Named here rather than left as literals in DetachedMode.__init__ so the
# provider's detached-only guard can compare against them: a guard that repeated
# the numbers would silently stop firing the moment a default changed (#136).
DEFAULT_BASTION_IDLE_TIMEOUT = 30  # minutes of inactivity before self-shutdown
DEFAULT_PRESERVE_BASTION = True  # bastion survives cleanup_infrastructure()
DEFAULT_BASTION_HOST_TYPE = "cloudformation"  # or "direct" (RunInstances)
# The bastion runs an orchestrator loop, not compute, so the smallest burstable
# type is the right default. Named here for the same reason as the three above:
# #155 extends the detached-only guard to cover it, and the guard compares
# against this constant rather than a copied-out "t3.micro".
DEFAULT_BASTION_INSTANCE_TYPE = "t3.micro"

# Delivery limits for the bastion's UserData, and the margin the shim must fit.
#
# The bastion init script is a whole program -- ~32 KB of shell wrapping an
# ~850-line embedded Python orchestrator -- and #227 found it exceeded every
# mechanism that was being used to deliver it: 10.4x the CloudFormation
# parameter limit and 2.0x EC2's raw UserData limit. It is now staged in S3 and
# fetched by a shim small enough that neither limit is a constraint again.
#
# Both limits are asserted at render time (`_prepare_bastion_user_data`) so a
# future edit to the script cannot silently reintroduce #227: the failure would
# otherwise appear only against live AWS, since substrate enforces neither.
MAX_CFN_PARAMETER_BYTES = 4096  # CloudFormation parameter value, probed exactly
MAX_EC2_USER_DATA_BYTES = 16384  # RunInstances UserData, before base64
MAX_EC2_USER_DATA_B64_BYTES = 25600  # RunInstances UserData, after base64
# How long the presigned GET on the staged script stays valid. The shim runs
# once, seconds into first boot, so this only has to outlast instance
# provisioning -- but a spot bastion can sit in `pending` for minutes, so the
# window is generous rather than tight.
BASTION_SCRIPT_URL_TTL = 3600  # seconds

# Lambda defaults (minimal for imports)
DEFAULT_LAMBDA_TIMEOUT = 300
# python3.9 reached end of support, and this package requires Python >= 3.10
# anyway, so the old default could not run the same code the driver does. Keep
# this in step with the Runtime AllowedValues in
# templates/cloudformation/lambda_worker.yml -- CloudFormation rejects the stack
# outright if the value is not listed there.
DEFAULT_LAMBDA_RUNTIME = "python3.12"
DEFAULT_LAMBDA_HANDLER = "handler.lambda_handler"
DEFAULT_LAMBDA_MEMORY = 1024

# ECS defaults (minimal for imports)
DEFAULT_ECS_TASK_CPU = 1024
DEFAULT_ECS_TASK_MEMORY = 2048
DEFAULT_ECS_CPU = 1024  # Alias
DEFAULT_ECS_MEMORY = 2048  # Alias
# A Fargate task image, not a Lambda one. This was
# "public.ecr.aws/lambda/python:3.9" -- a Lambda base image whose entrypoint is
# the Lambda runtime interface emulator, so it expects to be invoked with an
# event rather than to run the task's Command. The CloudFormation template's own
# default was already "python:3.12-slim"; the Python constant was overriding it
# with the wrong thing on every call.
DEFAULT_ECS_CONTAINER_IMAGE = "python:3.12-slim"
DEFAULT_ECS_CLUSTER_NAME = "parsl-ephemeral-cluster"

# Timeout constants (in seconds)
DEFAULT_RESOURCE_CREATION_TIMEOUT = 300  # 5 minutes
DEFAULT_RESOURCE_DELETION_TIMEOUT = 180  # 3 minutes
DEFAULT_INSTANCE_BOOT_TIMEOUT = 600  # 10 minutes

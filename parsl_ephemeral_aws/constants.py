"""Constants for Parsl Ephemeral AWS Provider.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

# Default AWS region
DEFAULT_REGION = 'us-east-1'

# Default instance type
DEFAULT_INSTANCE_TYPE = 't3.medium'

# Parsl-specific tags for AWS resources
TAG_PREFIX = 'parsl-ephemeral'
TAG_NAME = f"{TAG_PREFIX}-resource"
TAG_WORKFLOW_ID = f"{TAG_PREFIX}-workflow-id"
TAG_BLOCK_ID = f"{TAG_PREFIX}-block-id"
TAG_JOB_ID = f"{TAG_PREFIX}-job-id"

# AWS service endpoints
EC2_ENDPOINT = 'ec2.{region}.amazonaws.com'
SSM_ENDPOINT = 'ssm.{region}.amazonaws.com'
LAMBDA_ENDPOINT = 'lambda.{region}.amazonaws.com'
ECS_ENDPOINT = 'ecs.{region}.amazonaws.com'
S3_ENDPOINT = 's3.{region}.amazonaws.com'

# Default VPC configuration
DEFAULT_VPC_CIDR = '10.0.0.0/16'
DEFAULT_SUBNET_CIDR = '10.0.0.0/24'

# Security group defaults
DEFAULT_SG_NAME = f"{TAG_PREFIX}-security-group"

# Lambda function defaults
DEFAULT_LAMBDA_RUNTIME = 'python3.9'
DEFAULT_LAMBDA_HANDLER = 'handler.main'

# ECS task defaults
DEFAULT_ECS_CLUSTER_NAME = f"{TAG_PREFIX}-cluster"
DEFAULT_ECS_TASK_FAMILY = f"{TAG_PREFIX}-task"

# Provider modes
MODE_STANDARD = 'standard'
MODE_DETACHED = 'detached'
MODE_SERVERLESS = 'serverless'

# Worker types
WORKER_TYPE_EC2 = 'ec2'
WORKER_TYPE_LAMBDA = 'lambda'
WORKER_TYPE_ECS = 'ecs'
WORKER_TYPE_AUTO = 'auto'

# State store types
STATE_STORE_PARAMETER = 'parameter_store'
STATE_STORE_S3 = 's3'
STATE_STORE_FILE = 'file'
STATE_STORE_NONE = 'none'

# Task/job status
STATUS_PENDING = 'PENDING'
STATUS_RUNNING = 'RUNNING'
STATUS_SUCCEEDED = 'SUCCEEDED'
STATUS_FAILED = 'FAILED'
STATUS_CANCELLING = 'CANCELLING'
STATUS_CANCELLED = 'CANCELLED'
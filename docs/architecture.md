# Parsl Ephemeral AWS Provider Architecture

This document provides an overview of the architecture and design of the Parsl Ephemeral AWS Provider.

## Overview

The Parsl Ephemeral AWS Provider enables the execution of Parsl workflows on AWS resources that are created on-demand and cleaned up automatically when no longer needed. This approach maximizes cost-effectiveness while providing high scalability.

## Key Components

The provider is organized into the following key components:

```
parsl_ephemeral_aws/
├── __init__.py
├── provider.py                 # Main provider implementation
├── constants.py                # AWS-related constants and defaults
├── exceptions.py               # Custom exception classes
├── modes/                      # Different operating modes
│   ├── __init__.py
│   ├── base.py                 # Base mode interface
│   ├── standard.py             # Standard mode implementation
│   ├── detached.py             # Detached mode implementation
│   └── serverless.py           # Serverless mode implementation
├── compute/                    # Compute resource implementations
│   ├── __init__.py
│   ├── ec2.py                  # EC2 instance management
│   ├── lambda_func.py          # Lambda function management
│   └── ecs.py                  # ECS/Fargate management
├── network/                    # Network resource management
│   ├── __init__.py
│   ├── vpc.py                  # VPC and subnet management
│   └── security.py             # Security group management
├── state/                      # State persistence mechanisms
│   ├── __init__.py
│   ├── base.py                 # State interface
│   ├── parameter_store.py      # AWS Parameter Store integration
│   ├── s3.py                   # S3-based state management
│   └── file.py                 # File-based state management
├── utils/                      # Utility functions
│   ├── __init__.py
│   ├── aws.py                  # AWS helper functions
│   ├── logging.py              # Logging utilities
│   └── serialization.py        # State serialization/deserialization
└── templates/                  # Infrastructure as Code templates
    ├── __init__.py
    ├── cloudformation/         # CloudFormation templates
    │   ├── __init__.py
    │   ├── bastion.yml         # Bastion host template
    │   ├── vpc.yml             # VPC network template
    │   ├── ec2_worker.yml      # EC2 worker template
    │   ├── lambda_worker.yml   # Lambda worker template
    │   └── ecs_worker.yml      # ECS worker template
    └── terraform/              # Terraform/OpenTofu templates
        ├── __init__.py
        ├── vpc/                # VPC module
        ├── bastion/            # Bastion host module
        └── ec2_worker/         # EC2 worker module
```

## Operating Modes

The provider supports three distinct operating modes:

### Standard Mode

In Standard mode, the client (running on your local machine) directly communicates with worker nodes. This is suitable for development or smaller workflows where the client has a stable internet connection.

![Standard Mode](./img/standard_mode.png)

Workflow:
1. Client creates VPC and network infrastructure
2. Client launches EC2 instances or other compute resources
3. Client communicates directly with workers
4. When the workflow completes, client terminates all resources

### Detached Mode

In Detached mode, a small bastion/coordinator instance is launched in AWS that manages workers, allowing the client to disconnect while computation continues. This is great for long-running workflows or situations where the client is behind a NAT or has an unstable connection.

![Detached Mode](./img/detached_mode.png)

Workflow:
1. Client creates VPC and network infrastructure
2. Client launches a bastion host
3. Bastion takes over management of workers
4. Client can disconnect; workflow continues running
5. Bastion monitors for job completion and can automatically shut down

### Serverless Mode

In Serverless mode, AWS Lambda and/or ECS/Fargate are used to execute tasks without any EC2 instances. This is best for event-driven or sporadic workloads with short-running tasks.

![Serverless Mode](./img/serverless_mode.png)

Workflow:
1. Client creates network infrastructure if needed
2. Tasks are submitted directly to Lambda or ECS
3. Results are stored in S3 or other persistent storage
4. Resources scale to zero when not in use

## Resource Management

The provider creates and manages the following AWS resources:

- **VPC and Networking**: Dedicated VPC, subnets, internet gateway, route tables, and security groups
- **Compute Resources**: EC2 instances, Lambda functions, or ECS tasks
- **IAM Roles and Policies**: Minimal permissions following the principle of least privilege
- **State Storage**: Parameter Store parameters, S3 objects, or local files

All resources are tagged with:
- `ParslResource`: Indicates the resource is managed by Parsl
- `ParslWorkflowId`: Unique identifier for the workflow
- `ParslBlockId` (where applicable): Identifier for the block of resources

## State Management

State persistence is critical for:
- Tracking resources across Parsl sessions
- Supporting the detached mode of operation
- Ensuring proper cleanup of all resources

The provider supports three state storage mechanisms:

1. **Parameter Store**: AWS Systems Manager Parameter Store (default)
2. **S3**: Amazon S3 for larger state objects
3. **File**: Local file system for development and testing

## Infrastructure as Code

The provider includes both CloudFormation templates and Terraform/OpenTofu modules that can be rendered with specific parameters to deploy resources. This provides flexibility in how resources are provisioned and managed.

## Error Handling and Recovery

The provider implements robust error handling with:
- Custom exception hierarchy
- Detailed logging
- Graceful cleanup on failure
- Retry mechanisms for transient AWS API errors

## Security Considerations

The provider follows AWS security best practices:
- Least privilege IAM policies
- Secure VPC configuration
- Resource isolation between workflows
- No persistent credentials in EC2 instances

## Testing with LocalStack

For testing AWS interactions without real AWS resources, the provider supports LocalStack integration, which provides a local emulation of AWS services.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors

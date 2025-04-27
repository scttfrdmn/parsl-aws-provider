# Release Notes

This document tracks all notable changes to the Parsl Ephemeral AWS Provider.

## [0.1.0] - Unreleased

Initial release of the Parsl Ephemeral AWS Provider.

### Added
- Core provider implementation supporting the Parsl execution provider interface
- Three operating modes:
  - Standard mode with direct client-to-worker communication
  - Detached mode with bastion host for long-running workflows
  - Serverless mode utilizing AWS Lambda and ECS/Fargate
- Support for various AWS compute resources:
  - EC2 instances (on-demand and spot)
  - Lambda functions
  - ECS/Fargate tasks
- Networking management:
  - Automatic VPC and subnet creation
  - Security group management
  - Public and private subnet configurations
- State persistence mechanisms:
  - AWS Parameter Store implementation
  - S3-based state storage
  - File-based state storage
- Infrastructure-as-code templates:
  - CloudFormation templates for all resources
  - Terraform/OpenTofu modules
- Advanced features:
  - MPI support for HPC workloads
  - Spot instance support with interruption handling
  - Custom AMI configurations
  - Cost optimization strategies
  - Resource tagging and tracking
- Comprehensive test suite:
  - Unit tests with moto for AWS service mocking
  - Integration tests with LocalStack
  - Cross-platform and Python version compatibility testing
- Documentation:
  - Architecture overview
  - Getting started guide
  - Advanced usage documentation
  - LocalStack testing guide
  - Example workflows and configurations
  - Troubleshooting guide
  - CI/CD pipeline configuration guide

### Requirements
- Python 3.8+
- Parsl 1.2.0+
- boto3 1.20.0+
- Other dependencies as specified in requirements.txt

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
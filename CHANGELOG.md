# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial implementation of Parsl Ephemeral AWS Provider
- Support for three operating modes:
  - **Standard Mode**: Direct client-to-worker communication via EC2 instances
  - **Detached Mode**: Persistent infrastructure with bastion host for long-running workflows
  - **Serverless Mode**: AWS Lambda and ECS/Fargate execution without EC2 instances
- Comprehensive state persistence mechanisms:
  - File-based state storage
  - AWS Parameter Store integration
  - S3-based state management
- Advanced spot instance management:
  - EC2 Spot Fleet integration with automated failover
  - Spot interruption handling with task recovery
  - Hibernation support for cost optimization
- Network infrastructure management:
  - Automatic VPC, subnet, and security group creation
  - Configurable network isolation and connectivity
- Comprehensive testing framework:
  - Unit tests with moto AWS mocking
  - Integration tests with LocalStack support
  - Real AWS testing capabilities
- Development tooling:
  - Pre-commit hooks with comprehensive linting
  - Code coverage reporting and enforcement
  - Semantic versioning with changelog automation

### Changed
- Standardized on Python 3.9+ for broader compatibility
- Set pragmatic test coverage threshold at 70% focusing on functional testing
- Configured flexible testing approach supporting both LocalStack and real AWS

### Fixed
- Fixed pyproject.toml syntax errors in black configuration
- Resolved Python version inconsistencies between README and package configuration

## [0.1.0] - TBD

### Added
- Initial alpha release targeting core functionality completion
- Full Parsl ExecutionProvider interface implementation
- Production-ready architecture with proper error handling
- Comprehensive documentation and examples

---

---

**SPDX-License-Identifier: Apache-2.0**  
**SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors**

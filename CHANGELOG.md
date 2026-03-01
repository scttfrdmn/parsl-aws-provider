# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-02-28

### Added
- Initial implementation of `EphemeralAWSProvider` implementing Parsl `ExecutionProvider` interface
- Three operating modes: Standard (EC2), Detached (bastion host + SSH tunnel), Serverless (Lambda/ECS)
- Three state persistence backends: file-based, AWS Parameter Store, S3
- EC2 instance lifecycle management with on-demand and spot instance support
- Spot Fleet request management with capacity optimization
- Spot interruption monitoring and task recovery framework
- VPC, subnet, and security group provisioning
- Lambda function execution backend
- ECS/Fargate task execution backend
- Robust error handling framework with exponential backoff and jitter (`RetryConfig`, `RobustErrorHandler`)
- Security audit logging, credential management, and encryption modules
- Multi-region AMI support (Amazon Linux 2023, 23 regions)
- Resource tagging for cost tracking and cleanup
- Auto-shutdown with configurable idle time
- Unit tests with moto AWS mocking
- Integration tests with LocalStack support
- Pre-commit hooks, ruff/black/mypy linting
- Sphinx documentation and usage examples
- Unit tests for core Parsl provider interface methods (submit, status, cancel,
  scale_in, scale_out, shutdown, thread-safety, state persistence) (closes #41)
- Unit tests for SpotFleetManager instance-type list generation (closes #11)
- Integration tests for full job lifecycle and state recovery (closes #42)

### Fixed
- `SpotFleetManager` no longer synthesises invalid instance-type strings from
  the primary type; falls back to `[instance_type]` when `instance_types` is
  unset (closes #11)
- Bastion manager script now embeds `workflow_id` and `provider_id` at
  generation time via `self`, preventing `None` literals in shell exports
  (closes #12)
- Removed raw credential extraction via `session._session.get_credentials()`
  from `StandardMode`; `SpotFleetManager` now resolves credentials through its
  own `CredentialManager` (closes #13)
- Spot interruption detection replaced fake `"marked-for-termination"` state
  with real EC2 states (`shutting-down`, `stopping`); added `RLock` to protect
  `instance_handlers` and `fleet_handlers` dict mutations (closes #14)
- `EphemeralAWSProvider` now uses `threading.RLock` to guard all reads and
  writes of `resources` and `job_map` across `submit`, `status`, `cancel`,
  `scale_in`, `shutdown`, `_cleanup_resources`, `_save_state`, and `_load_state`
  (closes #15)
- `StandardMode.cleanup_resources` only removes entries from `self.resources`
  after confirmed termination; failed terminations are retried next cycle
  (closes #16)
- `SpotInterruptionMonitor.start_monitoring()` moved from `__init__` to
  `initialize()` with try/finally to prevent thread leaks on init failure
  (closes #17)
- Spot Fleet provisioning timeout now cancels the fleet request and raises
  `ResourceCreationError` instead of silently continuing (closes #18)
- Lambda async invocation now checks `StatusCode == 202` and `FunctionError`
  before tracking the submitted job (closes #21)
- ECS task definitions now create their CloudWatch log group before registration;
  log groups are tracked and deleted on cleanup (closes #22)

[Unreleased]: https://github.com/scttfrdmn/parsl-aws-provider/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/scttfrdmn/parsl-aws-provider/releases/tag/v0.1.0

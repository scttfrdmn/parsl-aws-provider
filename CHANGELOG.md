# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.6.0] - 2026-03-02

### Added
- **AMI baking** for `StandardMode`: set `bake_ami=True` to snapshot
  `worker_init` into a custom AMI during `initialize()`, so subsequent
  instance launches skip the ~30–60 s install entirely (closes #68).
  - `bake_ami` — when `True`, a builder instance runs `worker_init` then shuts
    itself down; once stopped, `create_image` snapshots it and all future
    `_create_instance()` calls use the resulting AMI.
  - `baked_ami_id` — supply a pre-existing baked AMI to skip the baking step
    and use the AMI directly.
  - Baked AMI ID and ownership flag are persisted in the state file; on
    provider restart, `image_id` is restored automatically without re-baking.
  - `cleanup_infrastructure()` deregisters the AMI and deletes its EBS
    snapshots when `_owns_baked_ami=True`.
  - `bake_ami=False` (default): zero code-path changes for existing users.
- **Warm pool** for `StandardMode`: set `warm_pool_size` (default `0`) to keep
  completed EC2 instances alive and reuse them for subsequent jobs via AWS SSM
  `SendCommand`, skipping the ~30–60 s `worker_init` cold-start cost (closes #63).
  - `warm_pool_size` — maximum number of idle instances to keep warm (0 = disabled).
  - `warm_pool_ttl` — seconds a warm idle instance stays alive before eviction
    (default 600).
  - Requires `auto_create_instance_profile=True` or `iam_instance_profile_arn`
    (SSM `SendCommand` needs an IAM role on the instance); a `ValueError` is raised
    at construction time if neither is provided.
  - Warm instances are tracked with `STATUS_WARM`; `_cleanup_resources()` handles
    pool-full oldest-instance eviction and TTL-based termination.
  - State (`_warm_instances` list) is persisted to the state file so warm instances
    survive a provider restart with the same `state_file_path`.

## [0.5.0] - 2026-03-01

### Added
- **Warm pool** for `StandardMode`: set `warm_pool_size` (default `0`) to keep
  completed EC2 instances alive and reuse them for subsequent jobs via AWS SSM
  `SendCommand`, skipping the ~30–60 s `worker_init` cold-start cost.
  - `warm_pool_size` — maximum number of idle instances to keep warm (0 = disabled).
  - `warm_pool_ttl` — seconds a warm idle instance stays alive before eviction
    (default 600).
  - Requires `auto_create_instance_profile=True` or `iam_instance_profile_arn`
    (SSM `SendCommand` needs an IAM role on the instance); a `ValueError` is raised
    at construction time if neither is provided.
  - Warm instances are tracked with `STATUS_WARM`; `_cleanup_resources()` handles
    pool-full oldest-instance eviction and TTL-based termination.
  - State (`_warm_instances` list) is persisted to the state file so warm instances
    survive a provider restart with the same `state_file_path`.
  - `status()` now short-circuits jobs already in a terminal state, preventing stale
    re-queries when an instance has been reused for a different job.

### Fixed
- `examples/parsl_aws_integration.py`: added `encrypted=False` to `HighThroughputExecutor`
  config. Parsl HTEX uses CurveZMQ encryption by default; the interchange generates TLS
  certificates in `run_dir` on the driver, but workers on fresh EC2 instances cannot
  access the driver's local filesystem. Workers were failing immediately with
  `FileNotFoundError: .../certificates`. Same-VPC deployments rely on VPC network
  isolation instead; cross-VPC or cross-internet deployments need certificate distribution
  (tracked in issue #62).
- `tools/launch_test_driver.py`: driver instances were launched with the default VPC
  security group, which only allows inbound from instances in the same SG. Workers created
  by the provider use a separate SG, so the interchange (bound on the driver) was
  unreachable. Now creates/reuses a dedicated `parsl-test-driver-sg` with TCP 54000-55000
  inbound from the VPC CIDR.
- `tools/launch_test_driver.py`: `DRIVER_USER_DATA` now installs `python3.11` before
  cloning and installing the package (AL2023 ships python3.9 by default;
  `parsl>=2026.1.5` requires Python 3.10+).
- `StandardMode.cleanup_infrastructure()` now waits for EC2 instances to reach
  `terminated` state (using `instance_terminated` boto3 waiter) before attempting to
  delete the security group. Previously the security group deletion raced against
  instance shutdown, causing `DependencyViolation` errors because the ENIs of
  shutting-down instances still held references to the SG.
- `examples/parsl_aws_integration.py`: the `finally` cleanup block now kills any
  lingering `parsl: HTEX interchange` subprocess after `parsl.clear()`. Without this,
  the interchange process remained alive after task completion, blocking SSM
  `send-command` invocations from returning.
- `EphemeralAWSProvider.__init__` now calls `operating_mode.initialize()` automatically
  so the provider is ready for `submit()` immediately after construction, matching
  Parsl's `ExecutionProvider` contract (no separate initialize step required).
  All three mode `initialize()` methods are now idempotent (`if self.initialized: return`).
- `EphemeralAWSProvider.status()` now returns `List[parsl.jobs.states.JobStatus]`
  instead of `List[Dict]`, matching the Parsl `ExecutionProvider` interface contract.
  Without this fix, Parsl's `HighThroughputExecutor` would raise `AttributeError`
  the first time it called `provider.status()`.
- `EphemeralAWSProvider.cancel()` now returns `List[bool]` instead of `List[Dict]`,
  matching the Parsl `ExecutionProvider` interface contract.
- Added missing required `ExecutionProvider` attributes: `init_blocks`,
  `nodes_per_block`, `parallelism`, `script_dir`. These are required by Parsl's
  executor strategy and scaling code.
- `DEFAULT_WORKER_INIT` changed from `pip install parsl` to
  `python3 -m pip install --quiet --upgrade parsl`, which works correctly on
  Amazon Linux 2023 (the default AMI). The old form used `pip` (not available
  by default on AL2023) and injected a duplicate `#!/bin/bash` shebang line.

### Added
- `init_blocks` and `nodes_per_block` constructor parameters on
  `EphemeralAWSProvider` (previously silently accepted via `**kwargs` but never
  stored on the instance).
- Integration example `examples/parsl_aws_integration.py`: a runnable script
  demonstrating end-to-end Parsl `@python_app` execution on EC2 via
  `HighThroughputExecutor` + `EphemeralAWSProvider`. Includes clear
  connectivity requirement documentation and Amazon Linux 2023 `worker_init`.

### Changed
- Parsl version pin updated from `>=1.2.0` to `>=2026.1.5` in `pyproject.toml`,
  reflecting the calendar-versioned release scheme and ensuring the correct
  `JobStatus` / `JobState` API is available.

- `GlobusComputeProvider` in `parsl_ephemeral_aws/globus_compute.py`: a subclass
  of `EphemeralAWSProvider` that accepts `endpoint_id`, `container_image`, and
  `display_name` parameters and exposes `generate_endpoint_config(path)` which
  writes a ready-to-use Globus Compute endpoint `config.yaml` (closes #56)
- `GlobusComputeProvider.minimum_iam_policy(include_ecr=False)` static helper
  returning the minimum IAM policy document required for EC2/SSM/IAM access
  (add `include_ecr=True` for private ECR repositories)
- `globus` optional extra in `pyproject.toml` (`globus-compute-sdk`,
  `globus-compute-endpoint`); declared as conflicting with the `test` extra
  to avoid `dill` version incompatibility with `localstack<4.10`
- `GlobusComputeProvider` exported from `parsl_ephemeral_aws.__init__`
- Unit tests for `GlobusComputeProvider` in
  `tests/unit/test_globus_compute_provider.py`: 34 tests covering import,
  construction, config generation for standard/spot/container variants,
  and the IAM policy helper (closes #56)

## [0.4.0] - 2026-02-28

### Added
- Real-AWS E2E test suite for StandardMode full lifecycle in `tests/aws/`
  (`tests/aws/conftest.py`, `tests/aws/test_standard_mode_e2e.py`): covers VPC/subnet/SG
  creation, CIDR conflict detection, instance tagging, PENDING→RUNNING→COMPLETED
  status transitions, cancellation, and full infrastructure teardown (closes #53)
- Real-AWS E2E test suite for spot instances and interruption recovery
  (`tests/aws/test_spot_e2e.py`): covers VPC/subnet/SG infrastructure with `use_spot=True`,
  `InstanceLifecycle='spot'` verification, RUNNING status after submit, command
  completion, cancellation, interruption monitor thread liveness, and
  force-termination detection (closes #55)
- Real-AWS E2E test suite for serverless mode Lambda/ECS
  (`tests/aws/test_serverless_mode_e2e.py`): covers VPC creation for auto worker_type,
  Lambda function existence after submit, COMPLETED status transition, Lambda
  function removal after cancel, and VPC/Lambda cleanup on shutdown (closes #61)
- Real-AWS E2E test suite for detached mode bastion host and SSM tunnel
  (`tests/aws/test_detached_mode_e2e.py`): covers VPC creation, bastion instance
  running state, bastion tagging, job submit/status/complete/cancel lifecycle,
  and full infrastructure teardown including bastion termination (closes #54)
- Real-AWS E2E test suite for Parameter Store and S3 state backends
  (`tests/aws/test_state_backends_e2e.py`): covers state written after initialize,
  job_id present in persisted state after submit, round-trip state restoration
  with a second provider instance, and state cleanup on shutdown (closes #57)
- New provider fixtures in `tests/aws/conftest.py`: `spot_provider`,
  `serverless_provider`, `detached_provider`, `parameter_store_provider`,
  `s3_state_bucket`, `s3_provider`; new autouse safety-net fixtures
  `cleanup_stray_lambda_resources` and `cleanup_stray_ssm_parameters`

## [0.3.0] - 2026-02-28

### Added
- `status_polling_interval` constructor parameter on `EphemeralAWSProvider`
  (default 60 s); the `status_polling_interval` property now returns the
  configured value instead of a hardcoded constant (closes #37)
- `waiter_delay` (default 5 s) and `waiter_max_attempts` (default 60) constructor
  parameters on `EphemeralAWSProvider`; stored as provider attributes and
  forwarded to `wait_for_resource()` via new `delay`/`max_attempts` keyword
  arguments (closes #39)
- `StandardMode._find_available_vpc_cidr()` static helper: scans existing VPCs
  and selects the first non-overlapping `/16` from the `10.x.0.0/16` range;
  `_create_vpc()` now calls this helper instead of using `DEFAULT_VPC_CIDR`
  unconditionally (closes #36)
- Unit tests for VPC manager, subnet CIDR generation, security group creation,
  and CIDR conflict detection in `tests/unit/test_vpc_manager.py` (closes #48)
- Unit tests for provider edge cases (zero-capacity submit, scale-in capping,
  empty status/cancel lists, configurable polling interval, waiter params) in
  `tests/unit/test_provider_edge_cases.py` (closes #49)

### Fixed
- VPC force-delete in `utils/aws.py` now deletes NAT Gateways (polling until
  fully deleted), releases their EIPs, and removes detached ENIs **before**
  attempting subnet deletion, preventing dependency errors on VPCs with NAT
  infrastructure (closes #38)
- `ParameterStoreState` now wires `provider.audit_logger` on construction and
  emits `SecurityEventType.STATE_ACCESS` events after successful `save_state`,
  `load_state`, and `delete_state` operations (closes #35)

### Performance
- Spot Fleet deduplication in `StandardMode.list_resources()` replaced O(n)
  `any()` list scan with an O(1) `seen_fleet_ids` set lookup (closes #40)

## [0.2.0] - 2026-03-01

### Added
- Optional `iam_instance_profile_arn` and `auto_create_instance_profile` parameters
  on `EphemeralAWSProvider`; EC2 instances and bastion host now receive an IAM
  instance profile for SSM access when configured (closes #19)
- `get_or_create_iam_role()` shared utility in `utils/aws.py` for idempotent
  IAM role creation with `EntityAlreadyExists` race handling
- ECS task execution role creation is now idempotent using the shared utility;
  IAM propagation waiter replaces 10-second sleep (closes #23)
- Lambda execution role creation is now idempotent using the shared utility;
  IAM propagation waiter replaces 10-second sleep (closes #20)
- `mock_iam_client` pytest fixture in `tests/conftest.py`; `mock_boto3_session`
  now routes `iam` service calls to the mock
- Unit tests for AWS quota, instance-type, and capacity errors in
  `TestEC2ManagerQuotaErrors` (closes #43, #44)
- Integration tests for full provider restart and state recovery in
  `tests/integration/test_provider_restart.py` (closes #45)
- Concurrent-submission stress tests (50 threads) and simultaneous submit+status
  tests in `test_provider_interface.py` (closes #46)
- Partial-infrastructure failure tests verifying VPC cleanup on subnet/SG
  creation failure in `test_standard_mode.py` (closes #47)

### Fixed
- VPC force-delete now removes non-main route table associations and tables
  before calling `delete_vpc`, preventing cleanup failures on custom route
  tables (closes #26)
- SpotFleet IAM role is now deleted by `cleanup_infrastructure()` via
  `cleanup_all_resources()` on normal provider shutdown (closes #24)
- SpotFleet instance-level interruption monitoring confirmed correct via
  fleet-level handler registration; no additional code change required (closes #25)
- Spot interruption handler lookups for both `instance_handlers` and
  `fleet_handlers` are now protected by `with self._lock:` to eliminate
  TOCTOU race (closes #28)
- S3 checkpoint `put_object` call now sets `ServerSideEncryption="AES256"`
  for at-rest encryption (closes #29)
- `FileStateStore` read and write operations are now protected by `fcntl.flock`
  (`LOCK_SH` read, `LOCK_EX` write) to prevent concurrent state corruption;
  no-op on platforms without `fcntl` (closes #30)
- `S3StateStore` bucket creation no longer passes the deprecated `ACL="private"`
  parameter; `put_public_access_block` is called instead to block all public
  access (closes #31)
- ECS `_get_or_create_network_resources` now prefers an explicit `provider.vpc_id`
  attribute; falls back to the default VPC with a clear error if neither exists
  (closes #32, #33)
- Spot Fleet max bid price now uses `describe_spot_price_history` (3× current
  spot as on-demand proxy) instead of hardcoded $0.10; falls back to $1.00
  on API failure (closes #34)
- Lambda `get_job_status` now returns deterministic `COMPLETED` status after
  the configured timeout instead of a random value (closes #27)

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

[Unreleased]: https://github.com/scttfrdmn/parsl-aws-provider/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/scttfrdmn/parsl-aws-provider/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/scttfrdmn/parsl-aws-provider/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/scttfrdmn/parsl-aws-provider/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/scttfrdmn/parsl-aws-provider/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/scttfrdmn/parsl-aws-provider/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/scttfrdmn/parsl-aws-provider/releases/tag/v0.1.0

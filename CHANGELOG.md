# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security
- `ServerlessMode.cleanup_infrastructure()` no longer deletes the caller's
  security group. The code was guarded only by a comment claiming "if we created
  it directly" — nothing verified ownership, and after #69 the ID is always
  user-supplied. It was reached from the `except` handler on every failed
  `initialize()`. The unconditional `parsl-vpc-<provider_id[:8]}` CloudFormation
  stack deletion (whose 8-character truncated IDs can collide across providers)
  was removed with it (closes #70).
- `SpotFleetManager._cleanup_network_resources()` removed. It unconditionally
  deleted the caller's security group, subnet, **every internet gateway attached
  to the VPC**, every non-main route table, and the VPC itself — with no
  ownership check, on IDs that `_setup_network_resources()` had adopted from the
  provider. It ran as the last step of `cleanup_all_resources()`, so with
  `use_spot_fleet=True` every normal provider shutdown attempted to destroy the
  caller's pre-provisioned network; a second path invoked it from the setup
  `except` handler. `delete_vpc`/`delete_subnet` usually failed with
  `DependencyViolation`, but the internet-gateway detach could succeed against a
  live VPC and blackhole egress for unrelated workloads (closes #94).

### Fixed
- The `docs` CI job could never have built the documentation. `docs/conf.py` has
  listed `myst_parser` in `extensions` all along without it being declared
  anywhere, so `make -C docs html` died on
  `ExtensionError: Could not import extension myst_parser`; it is now in the
  `docs` extra. All but three of the doc sources are Markdown, so the extension
  is load-bearing (refs #83).
- `docs/makefile` renamed to `docs/Makefile`, and `SOURCEDIR` corrected from
  `source` to `.`. The catch-all rule read `%: Makefile`, and on a
  case-insensitive filesystem that is the makefile itself — so `make html`
  matched the catch-all with `$@ = "makefile"` *before* the explicit `html` rule,
  because a pattern rule that can remake the makefile is tried first. The result
  was `sphinx -M makefile`, i.e.
  `SphinxError: Builder name makefile not registered`. `SOURCEDIR = source`
  pointed at `docs/source/`, an abandoned second doc tree, so every target built
  the wrong sources (refs #83, #124).
- `ECSManager._get_or_create_network_resources()` raised
  `UnboundLocalError: cannot access local variable 'subnet_response'` on every
  ECS/Fargate submission. A leftover line dereferenced `subnet_response`, which
  is bound only in the subnet-discovery branch; once `subnet_id` became required
  in #69 the explicit-subnet branch always ran, so the line always raised. It
  also overwrote the caller's explicit subnet with every subnet discovered in the
  VPC (closes #71).
- `ServerlessMode` could never be constructed. `LambdaManager` and `ECSManager`
  were written against `EphemeralAWSProvider` but are handed the mode as their
  `provider`, and the mode defined none of the attributes they read — so
  `_initialize_compute_managers()` raised
  `AttributeError: 'ServerlessMode' object has no attribute 'workflow_id'`.
  Since `EphemeralAWSProvider.__init__` calls `initialize()` unconditionally,
  `EphemeralAWSProvider(mode="serverless")` always raised. The mode now defines
  `workflow_id`, `subnet_ids`, `use_spot_instances`, `security_config`, and the
  four credential attributes (closes #72).
- `compute_type`, `memory_size`, and `timeout` were forwarded by the provider but
  accepted by no `ServerlessMode` parameter, so they vanished into `**kwargs`:
  `worker_type` was always `auto` regardless of `compute_type`, and Lambda memory
  and timeout were always the defaults (closes #73).
- `ServerlessMode.initialize()` never set `self.initialized`, so every
  `submit_job()` re-ran `ensure_initialized()` and rebuilt both compute managers
  (closes #73).
- `LambdaManager._generate_lambda_code()` raised
  `ValueError: Invalid format specifier` on every call. The generated handler was
  built as an f-string whose literal dict braces were read as replacement fields,
  so no Lambda job could ever be submitted. All six test call sites patch the
  method with a stub, so the real body had never run. It is now a plain template
  with a single JSON-encoded substitution for the command (closes #96).
- `LambdaManager._create_credential_config_from_provider()` raised
  `TypeError: CredentialConfiguration.__init__() got an unexpected keyword
  argument 'aws_access_key_id'` on every construction — none of the three
  `aws_*` kwargs it passed are fields on the dataclass. It now matches the
  `EC2Manager`/`ECSManager`/`SpotFleetManager` implementations, and no longer
  defaults `use_profile` to a profile literally named `aws` (closes #95).
- `auto_create_instance_profile` was accepted by `EphemeralAWSProvider` but never
  forwarded to `StandardMode`, so the documented warm-pool configuration could
  never work. Instances launched with no IAM instance profile, SSM never came
  online, `_wait_for_ssm_online()` timed out, and every submission silently fell
  back to UserData dispatch — losing both instance reuse and exit-code
  reporting. `StandardMode` now accepts the flag and resolves an ARN in
  `initialize()`, on both the fresh and resumed paths (the ARN is not persisted
  in state). A failure to create the profile is logged, not fatal: dispatch
  degrades to UserData rather than the provider failing to start (closes #75).
- One-shot instances stopped instead of terminating, leaving a billed EBS volume.
  `StandardMode._create_instance()` never set
  `InstanceInitiatedShutdownBehavior`, and EC2 defaults an instance-initiated
  shutdown to *stop* — so the `shutdown -h now` appended by
  `_prepare_init_script()` stopped the instance. Because `EC2_STATUS_MAPPING`
  maps `stopped` to `COMPLETED`, the provider then dropped the tracking record,
  orphaning the volume as well as billing for it. `DetachedMode` had set
  `terminate` on all three of its launch paths since v0.2.0 (closes #76).
- One-shot command failures reported `COMPLETED`. #66 specified "command exits
  non-zero → FAILED", but status was derived purely from EC2 instance state,
  which is identical whether the command succeeded or failed. One-shot dispatch
  now goes through the same SSM `SendCommand` path the warm pool uses, so the
  exit code is reported; it is also recorded on the resource as `exit_code`, and
  a failure logs the command's stderr (closes #76).
- `StandardMode._create_spot_instance()` raised
  `ParamValidationError: Unknown parameter in LaunchSpecification: "MinCount"`
  on every call. `run_args` is built for `run_instances` and reused as the spot
  `LaunchSpecification`, which expresses count as the top-level `InstanceCount`;
  boto3 validates parameter names client-side, so `use_spot=True` could never
  submit a job unless `use_spot_fleet=True` routed around it. The
  `RunInstances`-only keys are now stripped (closes #97).
- Instance-profile resolution no longer leaves a profile permanently empty. The
  previous implementation returned early when `get_instance_profile` succeeded,
  so a profile whose role attachment had failed midway kept being reused with no
  role attached and SSM never came online. The role is now attached on all three
  paths (pre-existing profile, freshly created, and lost creation race).
- `state_store_type="s3"` and `"parameter_store"` could not be constructed at
  all — three stacked defects, each hidden behind the previous one. The provider
  passed `session=`/`path=`/`bucket=`/`key=`/`provider_id=`, none of which are
  parameters of `S3State` or `ParameterStoreState` (`TypeError`); the AWS stores
  implement a *keyed* three-method interface while the provider and
  `FileStateStore` used an unkeyed one; and both stores unguardedly read six
  credential attributes that `EphemeralAWSProvider` does not define, building
  their own `boto3.Session` and ignoring `provider.session` entirely
  (`AttributeError`). The `session=` kwarg exists nowhere else in the codebase —
  this path had never been executed. The stores' provider-object contract is
  kept; the provider and `FileStateStore` were adapted to it (closes #57, #77).
- The provider and its operating mode no longer overwrite each other's state.
  Both wrote full-document overwrites, with different field sets, into the same
  slot, so whichever wrote last erased the other's fields. Losing the mode's
  `baked_ami_id`/`owns_baked_ami` meant `cleanup_infrastructure()` could no
  longer see the AMI it owned — **leaking the AMI and its EBS snapshots**, and
  silently re-baking on restart. Losing the provider's `job_map` lost the
  job-to-resource mapping. State is now addressed by key, one per writer
  (closes #78).
- Auto-created IAM instance profiles were handed to `RunInstances` before EC2
  could see them, failing every launch with `InvalidParameterValue: Invalid IAM
  Instance Profile ARN`. IAM is eventually consistent with respect to EC2:
  measured against real AWS, `create_instance_profile` returned the ARN at
  t+4.1s but `RunInstances` did not accept it until t+14.5s. This was
  unreachable until `auto_create_instance_profile` began to take effect (#75).
  `get_or_create_ssm_instance_profile()` now waits for the profile to become
  visible on both its create and creation-race paths, polling a dry-run
  `RunInstances` — `get_instance_profile` succeeds immediately and so proves
  nothing about the path that matters. A timeout warns rather than raising, since
  the launch that follows reports any real error (closes #98).
- The provider never restored persisted state, so nothing survived a restart on
  any backend. Two independent defects: `_load_state()` was not called from
  `__init__` at all (only tests called it), and it refuses a document whose
  `provider_id` differs from its own — while `provider_id` defaults to a fresh
  UUID and does **not** affect where state is stored (only `state_file_path` /
  `parameter_store_path` / `s3_key` do). A successor therefore generated a new
  ID and discarded the very document it was pointed at. `__init__` now loads
  state, and adopts the `provider_id` recorded at that location unless the caller
  supplied one explicitly. Wiring this up was only safe once the provider and its
  mode stopped sharing a slot (#78) (closes #100).
- Provider-ID adoption consulted only the provider's own state key, which does not
  exist until a job has been submitted — the provider writes it from
  `_save_state()`, on submit/status/cancel. A provider that constructed and
  exited without submitting therefore left *only* the mode document behind, the
  successor kept its fresh UUID, and `OperatingMode.load_state()` rejected that
  document on the ID gate. Both keys are now consulted, provider key first. The
  mode document is the one that matters here: it holds the network IDs and the
  baked-AMI ownership flag. Found against real AWS while probing #79 — a resumed
  provider silently took the create path and never noticed its security group had
  been deleted (closes #101).
- `warm_pool_size`, `warm_pool_ttl`, `bake_ami`, `baked_ami_id`, and `one_shot`
  are implemented only by `StandardMode`, and were forwarded only on that branch
  — but the provider acted on them regardless of mode, which made the mismatch
  leak rather than merely no-op. With `mode="detached", warm_pool_size=2` every
  resource was tagged `warm_pool=True`, `_cleanup_resources()` took the warm-pool
  branch, and jobs were set to `STATUS_WARM` — a status no other mode's
  `get_job_status()` recognises — so those instances were **never cleaned up** and
  leaked with no error or warning. `__init__` now raises
  `ProviderConfigurationError` naming every offending option and the mode asked
  for. It is ordered before the SSM instance-profile guard, which would otherwise
  answer `one_shot=True` on detached mode by advising
  `auto_create_instance_profile` — advice that cannot help, since detached mode
  does not implement one-shot at all (closes #80).
- Real-AWS E2E tests read `provider.status(...)[0]["status"]` in 14 places, but
  `status()` has returned `List[JobStatus]` since v0.5.0 and `JobStatus` is not
  subscriptable, so each raised `TypeError`. The pattern dates to when the suite
  was written, against the old `List[Dict[str, str]]`. This mattered more than
  the three visible failures suggest: a polling helper that raises on its first
  call cannot time out — it aborts the test — so no spot, detached, serverless,
  or Globus lifecycle assertion downstream of a `_poll_until` had ever run. All
  call sites now compare `statuses[0].state` against `JobState` (closes #102).
- `_verify_resources()` nulled the network ID it had just found missing instead of
  reporting it. The nulling is a leftover from the create-on-demand era — since
  #69 nothing creates a replacement, so the `None` reached `run_instances` as an
  opaque `InvalidParameterValue` far from the missing resource, and in serverless
  mode re-entered a guard that read a `create_vpc` attribute which no longer
  exists. It now raises `ResourceNotFoundError` naming the offending ID.
  `load_state()` also no longer restores a `None` from a pre-#69 state document
  over a validated ID. The network half of the method was byte-identical in all
  three modes — the condition that let them drift — and now lives once on
  `OperatingMode`; `DetachedMode` still adds its bastion check on top, where a
  missing bastion is correctly *not* an error, since that resource is one the
  mode does create (closes #79).
- Network resources were verified only when resuming from state, so a first-run
  provider — the common case — never checked its IDs at all. `initialize()` now
  verifies on both paths in all three modes. Found by probing #79's fix against
  real AWS: the expected `ResourceNotFoundError` never fired.
- A syntactically invalid network ID escaped as a raw `ClientError`. EC2 answers
  a malformed ID with a distinct code rather than `NotFound` — verified against
  real AWS, where `sg-00000000000000000` yields `InvalidGroupId.Malformed` while
  a subnet or VPC ID of the same shape yields `NotFound`, and the suffix casing
  differs per resource. All six codes are now matched, on the `Error.Code` field
  rather than the rendered message.
- `DetachedMode.initialize()` returned unconditionally when it loaded state, so a
  resumed provider whose bastion had been terminated never rebuilt it — every job
  was dispatched into an SSM path nothing was reading. It also ran its bastion
  check before `load_state()`, which is where `bastion_id` comes from, so on a
  fresh process there was nothing to check.
- `shutdown()` saved an **empty** state document instead of deleting it, so SSM
  parameters and S3 objects survived every shutdown and accumulated per run
  (Parameter Store has an account quota). On any backend the surviving document
  described network IDs — and, for the mode, a baked AMI — that shutdown had just
  released. `delete_state` was implemented in all three stores and declared on
  the ABC, but called from nowhere in the package (closes #99).
- Spot Fleet was unreachable through `EphemeralAWSProvider`. `use_spot_fleet`,
  `instance_types`, `spot_max_price_percentage`, and `nodes_per_block` were
  accepted by no provider parameter, so they landed in `**kwargs`, were stored on
  the never-read `self.kwargs`, and were never forwarded — `StandardMode` kept its
  defaults and `spot_fleet_manager` stayed `None`. `use_spot_fleet=True` appears
  in 13 documentation files and 2 examples; every one of them silently ran single
  on-demand or single-spot instances instead. All three modes already accepted the
  four parameters, so they are now forwarded to each (closes #105).
- `ServerlessMode.get_job_status()` reported rolled-back CloudFormation stacks as
  `RUNNING`. The mapping tested `endswith("FAILED")` then `startswith("DELETE")`,
  and `ROLLBACK_COMPLETE`/`ROLLBACK_IN_PROGRESS`/`UPDATE_ROLLBACK_COMPLETE` match
  neither, so they fell through to the `RUNNING` default. `ROLLBACK_COMPLETE` is
  the *usual* CloudFormation failure state, since automatic rollback on
  `CREATE_FAILED` is the default — so the ordinary serverless failure path was the
  one that misreported. `RUNNING` is not terminal, so the job was polled forever:
  Parsl never learned the task had failed, never retried it, and never released
  the block, while the stack sat in a state that can only be deleted (closes
  #106).
- `EphemeralAWSProvider` accepted an unknown `region` without complaint, then
  failed much later with an opaque `EndpointConnectionError` from whichever AWS
  call ran first — in standard mode from inside `initialize()`, after the state
  store had been created. The region is now checked against botocore's packaged
  endpoint data across all five partitions, so GovCloud, China, and
  newly-launched regions are accepted without any in-tree list to maintain
  (closes #107).
- `EphemeralAWSProvider` accepted contradictory and negative block counts:
  `min_blocks=10, max_blocks=5` and `min_blocks=-3` both constructed. Parsl's
  scaling strategy reads all three counts straight off the provider and validates
  none of them, so an unreachable range pinned the executor — it could not scale
  out to reach `min_blocks` (case 2a refuses at `active_blocks >= max_blocks`) and
  would not scale in because case 1a's `active_blocks <= min_blocks` held at every
  reachable count. The check existed before the v0.1.0 rewrite and was lost; the
  only surviving record was a test in a file that had failed at collection since
  `MODE_STANDARD` was removed from `constants.py` (closes #108).
- Every tagged resource was created with a duplicate `Name` tag key, which EC2
  rejects. The marker tag identifying a resource as provider-managed was emitted
  as `TAG_NAME`, and `TAG_NAME` is the literal string `"Name"` — so each tag list
  carrying a descriptive `Name` *and* the marker sent `Name` twice. Verified
  against real AWS: `run_instances` fails with `InvalidParameterValue: Duplicate
  tag key 'Name' specified.`, `create_security_group` the same, and
  `request_spot_fleet` with `InvalidSpotFleetRequestConfig`. `moto` accepts
  duplicate keys and silently keeps the last value, so the collision was
  invisible under test while overwriting the descriptive name with `"true"`. The
  marker is now a distinct `TAG_MANAGED = "ParslEphemeralManaged"`. `TAG_NAME`
  remains `"Name"` for descriptive tags. Introduced in `f9f7def`, which flattened
  `TAG_NAME` from `"parsl-ephemeral-resource"` to `"Name"` "as an alias for
  backward compatibility" (closes #109).
- `ECSManager._get_or_create_network_resources()` called
  `authorize_security_group_egress` on a security group it had just created. EC2
  attaches an allow-all-outbound rule to every new security group, so
  re-authorizing it raises `InvalidPermission.Duplicate: the specified rule
  "peer: 0.0.0.0/0, ALL, ALLOW" already exists`, which was re-raised as
  `ResourceCreationError` and then wrapped as `JobSubmissionError` — making the
  create-security-group branch impossible to complete. The call is removed;
  Fargate tasks still have the outbound access they need. The ingress path in
  `network/security.py` already treated `InvalidPermission.Duplicate` as benign
  (closes #110).
- A Lambda job that outlived its configured timeout could never reach a terminal
  status. `LambdaManager.get_job_status()` is called with
  `(function_name, request_id)` and locates the job record by scanning, but the
  timeout branch's log message interpolated a `job_id` local that is never bound
  there — raising `NameError`, which the method's blanket `except Exception`
  converted into `"UNKNOWN"`. `status` was therefore never assigned, so the
  stored record stayed `PENDING`: Parsl polled the job forever and never released
  the block. Introduced in `90577ed` (closes #111).
- `get_cf_template()` raised `ModuleNotFoundError` on every call, taking
  `DetachedMode.initialize()` with it. It called `pkg_resources.resource_string`
  but placed the `import pkg_resources` *outside* its own `try`, so once
  setuptools 81 removed the module the `except ModuleNotFoundError` fallback
  became unreachable. It now uses `importlib.resources`. The CloudFormation and
  Terraform templates were also absent from the built wheel —
  `[tool.setuptools.packages.find]` collects modules, not data files — which was
  invisible in development because an editable install resolves to the source
  tree. A missing template now raises `FileNotFoundError` instead of returning a
  placeholder document with no `Outputs` section, which the bastion path then
  indexed for `BastionHostId`, failing several steps from the real cause
  (closes #112).
- `ServerlessMode` loaded both of its CloudFormation templates by filesystem
  path, computed from `__file__`, rather than through `get_cf_template()`. An
  installed wheel raised `FileNotFoundError` on the first Lambda or ECS
  submission (closes #113).
- The Spot Fleet capacity check read `FulfilledCapacity` from the top level of
  the `DescribeSpotFleetRequests` entry, where it does not exist — both
  capacities live in the nested `SpotFleetRequestConfig`. The `.get(..., 0)`
  default meant `0 >= target_capacity` was false for any capacity of 1 or more,
  so a fully provisioned fleet reported `PENDING` forever, never reached a
  terminal state, and Parsl never released the block (closes #114).
- Every `ServerlessMode.submit_job()` raised `KeyError`. Both submit helpers
  finish by calling `self.resources[resource_id].update(...)` to record the
  stack name, but the tracking record was not created until after they returned
  — so the update raised inside the helper's blanket `except Exception` and
  surfaced as an opaque `Failed to submit ECS job: 'serverless-ecs-<job>'`. The
  CloudFormation stack was created before that point and left untracked, so
  nothing could clean it up. The record is now created before dispatch, and a
  failed submit cleans up the partially created stack (closes #115).
- Lambda deployment packages were passed to CloudFormation as a latin1-decoded
  string in the `CodeZipContent` parameter. That is neither the base64 the
  template documents nor legal XML — 117 of its codepoints are control
  characters that XML 1.0 forbids in character data — so CloudFormation's own
  `DescribeStacks` echo of the parameter came back unparseable and every Lambda
  job reported `UNKNOWN` forever. `AWS::Lambda::Function`'s `ZipFile` also takes
  inline source text capped at 4096 bytes, not archive bytes, so even correct
  base64 would have deployed a broken function. Packages are now staged in S3
  and referenced by `CodeS3Bucket`/`CodeS3Key`, which the template already
  supported. A caller-supplied `checkpoint_bucket` is reused if present;
  otherwise a provider-scoped bucket is created and removed on cleanup
  (closes #116).
- All four compute managers ignored `provider.session` and built their own from
  the credential manager, so an explicitly configured session — temporary role
  credentials, a chosen profile, a LocalStack `endpoint_url` — was silently
  replaced by one assembled from ambient environment credentials, possibly
  pointing at a different account. It also meant an injected test double was
  ignored and the manager reached real AWS; a unit test created a live ECS
  cluster this way. New `resolve_manager_session()` prefers the caller's
  session, mirroring the fix already applied to the state stores. The dead
  "legacy fallback" in `LambdaManager` — unreachable by its own `if`, but which
  still dereferenced four provider attributes that `EphemeralAWSProvider` does
  not define — is removed (closes #117).
- `ServerlessMode.save_state()` omitted `worker_type`, the field that determines
  whether the three network IDs are required at all, so a state document could
  not be interpreted without it (closes #118).
- Every credential-resolution failure raised `TypeError` rather than a
  credentials error. `security/credential_manager.py` called
  `NoCredentialsError(f"...")` at seven sites, but botocore's `BotoCoreError`
  accepts keyword arguments only, so each raised
  `TypeError: BotoCoreError.__init__() takes 1 positional argument but 2 were
  given` and the reason never reached the caller. Handlers that catch
  `NoCredentialsError` — in all four compute managers and `error_handling.py` —
  did not catch it. A new `CredentialResolutionError` subclasses the botocore
  class, so those handlers keep working and the message survives (closes #121).
- `SecureStateManager.verify_state_integrity()` returned `True` for a path that
  does not exist. It reported success when `load_secure_state()` raised nothing,
  but that method deliberately swallows `FileNotFoundError` and returns `{}` so
  a first run can proceed with no saved state (closes #122).

### Added
- **One-shot mode** for `StandardMode`: set `one_shot=True` to declare that each
  EC2 instance runs a single command and then terminates, regardless of the
  `auto_shutdown` setting (closes #66).
  - The command is dispatched over SSM `SendCommand` after the instance reports
    ready, so its exit code determines the job status. UserData carries only
    `worker_init` and the readiness marker.
  - The instance is terminated by `_cleanup_resources()` once the command
    reaches a terminal state; a detached `shutdown -h now` in the dispatched
    script bounds the cost if the driver process dies first. It is scheduled
    after the exit code is captured, so it cannot mask a non-zero exit.
  - Raises `ValueError` at construction time if combined with
    `warm_pool_size > 0` (one-shot instances are terminated and cannot be
    reused), or if no instance profile is available.
  - `one_shot=False` (default): zero code-path changes for existing users.
- `tests/aws/test_one_shot_e2e.py` — 8 real-AWS tests covering exit-code
  propagation (`exit 0` → COMPLETED, `exit 1` → FAILED, `exit 42` recorded),
  termination rather than stopping, no leftover billable EBS volume, the
  instance-profile guard, and the absence of the command from UserData.
  Specified in #66 and never written.
- 8 tests in `TestOneShotMode` covering SSM dispatch, the shutdown backstop,
  `InstanceInitiatedShutdownBehavior`, the spot `LaunchSpecification` key
  stripping, exit-code-derived status, dispatch-failure termination, and the
  instance-profile guard.
- `docs/network-prerequisites.md` with Terraform and CloudFormation snippets for
  provisioning the required network resources.
- `tests/unit/test_ecs_manager.py` — 6 tests covering explicit `subnet_id`,
  explicit `subnet_ids` precedence, subnet discovery, default-VPC fallback, and
  both empty-result error paths.
- `tests/unit/test_serverless_mode_contract.py` — 30 tests covering the
  compute-manager attribute contract, the conditional network guard, provider
  parameter plumbing, and the absence of the network-creation helpers.
- `tests/unit/test_lambda_manager.py` — 13 tests that execute the real
  `_generate_lambda_code()` body and compile its output.
- `tests/unit/test_instance_profile.py` — 10 tests covering SSM instance-profile
  resolution against moto and `StandardMode`'s resolution behaviour.
- `STATE_KEY_PROVIDER` and `STATE_KEY_MODE` in `state/base.py`, and
  `OperatingMode.delete_state()` — one state document per writer.
- `FileStateStore` keyed layout: a single file holding one sub-document per key
  under `_states`, versioned with `_version`, read-modify-written under the
  existing `fcntl.flock`. A flat pre-v0.7.0 document is still readable under any
  key and is seeded into both keys on first write.
- `resolve_session(provider)` in `state/base.py` — prefers `provider.session`
  before assembling one from credential fields, so the AWS stores stop
  duplicating the credential plumbing the provider already does via
  `create_session()`.
- 8 tests in `TestFileStateStoreKeying` and 5 in `TestStateKeySeparation`
  covering key isolation, per-key deletion, flat-document upgrade, serialization
  failure leaving prior state intact, and shutdown deleting both documents. The
  key-isolation tests were mutation-checked: collapsing `STATE_KEY_MODE` onto the
  provider's key fails all three separation tests.
- 23 tests in `TestStandardOnlyOptionGuard` covering each StandardMode-only
  option against each mode that cannot honour it, acceptance on standard mode,
  explicitly-passed defaults, multi-option messages, and guard ordering.
  Mutation-checked: disabling the guard fails 14, comparing presence instead of
  the default fails 4, and reordering it after the IAM guard fails 2.
- `one_shot` is now documented on `EphemeralAWSProvider`; it had been accepted
  but absent from the docstring. The four warm-pool and AMI-baking parameters
  are now marked `mode="standard"` only.
- 5 tests in `TestWaitForInstanceProfile` covering the IAM propagation wait —
  including a fake clock driven through `sleep` so the retry-then-give-up path
  genuinely iterates.
- The `tests/aws` `network_ids` fixture now validates the supplied IDs against
  `AWS_TEST_REGION` before any test runs. IDs from another region were not an
  error until a launch was attempted, surfacing minutes in as
  `InvalidSubnetID.NotFound` from inside `RunInstances` — after real instances had
  been billed. `AWS_TEST_REGION` defaults to `us-west-2`, so supplying the IDs
  alone is easy to get wrong; the check now fails in seconds with the region named.
- `get_or_create_ssm_instance_profile()` in `utils/aws.py` — promoted from
  `EC2Manager._get_or_create_instance_profile()` so `StandardMode` and
  `EC2Manager` share one implementation. Resolution order is explicit ARN, then
  auto-creation, then `None`.
- `test_cf_templates.py` falls back to the `tomli` backport, since `tomllib` is
  3.11+ and `requires-python` is `>=3.10`. This is not a new dependency: pytest
  itself requires `tomli` below 3.11.
- `tests/unit/test_manager_session.py` — 5 tests pinning
  `resolve_manager_session()`'s precedence directly: the provider's own session
  is returned by identity, a provider with no session (or no `session` attribute
  at all) falls back to the credential manager, and a falsy region resolves to
  `DEFAULT_REGION` rather than letting boto3 pick one up from the environment.
  Only one test had covered #117, incidentally, via a client the manager happened
  to reach through the injected session.
- `test_templates_are_deployed_through_the_package_loader` — both serverless
  submit paths are asserted to obtain their CloudFormation template from
  `get_cf_template()`. Comparing `TemplateBody` to the loader's output cannot
  establish this alone: in a source checkout, reading the file by a path built
  from `__file__` yields identical bytes, which is precisely why #112 stayed
  invisible in development and failed only from an installed wheel. Both stack
  parameter tests also now assert `TemplateBody`, so deploying the other
  template is caught (refs #112, #113).

### Changed
- **CI is one workflow.** `ci.yml` and `ci-cd.yml` were near-duplicate pipelines
  that between them ran the same unit suite five times, over Python 3.8 and 3.9,
  which `requires-python = ">=3.10"` excludes. `ci-cd.yml` is deleted; `ci.yml`
  now carries lint, type-check, unit, integration, real-AWS, bats, build, and
  docs jobs, all on `uv sync --locked` rather than `pip install -e`. `ci-cd.yml`
  also held a second PyPI publish job triggered by `release: published`, so a tag
  push followed by a published release ran two independent uploads; `release.yml`
  is now the only publish path (closes #83).
- `release.yml` verifies the pushed tag against `parsl_ephemeral_aws.__version__`
  before building. `bump-my-version` has silently missed `__init__.py` before —
  v0.6.0 shipped with `__version__ == "0.1.0"` — and a PyPI version can never be
  reused. Publishing now uses trusted publishing via OIDC instead of a
  `PYPI_API_TOKEN` secret, and the release is created with `gh release create`;
  `actions/create-release` and `actions/upload-release-asset` are both archived.
- The build job asserts the CloudFormation templates are present in the wheel, so
  a packaging regression fails in CI rather than at runtime on a real AWS call
  (refs #112).
- A `workflow_dispatch` job runs the 51 real-AWS tests in `tests/aws`, which no
  workflow referenced — #60 closed with them unreachable in CI. Credentials come
  from OIDC, and a final always-run step reports orphaned resources (refs #83).
- Test selection is by path, not `-m unit`. The Makefile selected `-m unit`, which
  collected 88 of 295 tests and passed while CI ran the full set and failed. An
  unmarked new file now still runs. `pytestmark = pytest.mark.unit` was added to
  the 11 unmarked unit files and to `tests/security`, which had 92 tests and no
  markers at all.
- `--cov-fail-under` in `pyproject.toml` is 25, a smoke floor, because `addopts`
  applies to narrow invocations too — `pytest tests/integration` alone measures
  34%. The gate that matters is `--cov-fail-under=65` on the CI unit-tests job,
  which runs `tests/unit` and `tests/security` together at 68%.
- Formatting is ruff-format only. `black` and `isort` are removed from the `dev`
  extra and their config from `pyproject.toml`: `[tool.black] line-length = 100`
  disagreed with the 88-character default that every file is actually formatted
  to, so running black reformatted ~80 unrelated lines. The `ruff-pre-commit` pin
  moved from v0.2.2 to the version `uv.lock` resolves, because the two disagreed
  on comprehension-conditional parenthesization and each would undo the other's
  output.
- The two standing `bandit` findings are annotated with `# nosec` and a reason
  rather than left to fail the new lint gate: retry jitter is not a security
  decision, and LocalStack's `test`/`test` credentials authenticate against
  nothing real. `bandit -r parsl_ephemeral_aws` now exits 0.
- Non-AWS-marked tests now run with synthetic credentials and `AWS_PROFILE`
  unset. A `@mock_aws`-decorated *class* only wraps its `test_*` methods, so a
  fixture on that class runs outside the mock — one reached the live account and
  failed `VpcLimitExceeded` against production VPCs. An un-mocked call now fails
  as an auth error against a fake account instead of mutating a real one.
- `vpc_id`, `subnet_id`, and `security_group_id` are no longer required for
  Lambda-only serverless mode. `compute/lambda_func.py` references none of them
  and `create_function` passes no `VpcConfig`, so functions run in the
  Lambda-managed VPC. ECS/Fargate still requires them — `awsvpcConfiguration` is
  mandatory — so the guard now keys off the resolved worker type in both
  `EphemeralAWSProvider` and `OperatingMode` (new `require_network_resources`
  flag) (closes #74).
- `EphemeralAWSProvider` now passes `region` explicitly to the operating mode
  rather than letting it fall back to `session.region_name`.
- **`StateStore` is now a keyed interface**: `save_state(state_key, state_data)`,
  `load_state(state_key)`, `delete_state(state_key)`. `FileStateStore` gained the
  key parameter; the two AWS stores already had it and now call
  `super().__init__()`. Callers holding a store directly must pass a key.
- `OperatingMode.load_state()` no longer restores a `None` network ID over a
  validated constructor value. A state document written before these IDs became
  required can carry nulls, which previously surfaced much later as an opaque
  boto3 `InvalidParameterValue` at launch.
- `StandardMode._create_instance()` attaches the IAM instance profile whenever
  one is available, not only when `warm_pool_size > 0`. One-shot dispatch needs
  it too, and an unused profile costs nothing.
- `EC2Manager._get_or_create_instance_profile()` now delegates to
  `utils.aws.get_or_create_ssm_instance_profile()`.
- One-shot mode now requires `auto_create_instance_profile=True` or
  `iam_instance_profile_arn`, because its command is delivered by SSM
  `SendCommand`. The requirement was previously warm-pool-only.
- A failed SSM dispatch now terminates the instance and re-raises instead of
  logging "falling back to UserData execution". There was nothing to fall back
  to — the command is not in the UserData, so the instance would have idled
  until `max_idle_time` while reporting `RUNNING`.
- `EphemeralAWSProvider.__init__` now raises `ProviderConfigurationError` for
  unrecognised keyword arguments instead of absorbing them into `**kwargs`. The
  collected `self.kwargs` attribute was write-only — read nowhere in the package —
  so the permissiveness only ever hid typos and dropped options, which is exactly
  how the Spot Fleet parameters went unnoticed (refs #105).
- `EphemeralAWSProvider.status()` runs `_cleanup_resources()` when `one_shot` is
  set, not only when a warm pool is configured; a one-shot instance has no
  UserData shutdown to end it.
- `vpc_id`, `subnet_id`, and `security_group_id` are now **required** constructor
  arguments; a `ValueError` is raised at init if any are missing.
- `cleanup_infrastructure()` no longer destroys network resources — VPC,
  subnet, and security group are now the caller's responsibility.
- E2E tests read `AWS_TEST_VPC_ID`, `AWS_TEST_SUBNET_ID`, and `AWS_TEST_SG_ID`
  from the environment; tests are skipped (not failed) when these are unset.

### Removed
- The bats test `Required environment variables are set`. It asserted that
  `AWS_REGION` and `AWS_ACCESS_KEY_ID`/`AWS_PROFILE` were set in the ambient
  environment, guarded so that it ran only when `CI` was set — so it failed on
  every CI run since it was written, failing the whole `test-bats` job while the
  other 11 tests passed. It could not have done otherwise: the suite exercises
  shell scripts against a mocked AWS CLI, needs no credentials, and the workflow
  supplies none. The env-file contents the scripts do depend on are covered by
  `test_setup_environment.bats`.
- `SpotFleetManager._create_vpc()`, `_create_subnet()`, `_create_security_group()`,
  and `_cleanup_network_resources()`. `_setup_network_resources()` now only
  resolves the caller-supplied IDs and raises `ResourceCreationError` if any are
  missing (closes #94).
- `create_vpc` parameter — VPC/subnet/security-group creation removed from the
  provider entirely (closes #69).
- `_create_vpc()`, `_create_subnet()`, `_create_security_group()`, and
  `_find_available_vpc_cidr()` helpers deleted from `StandardMode` and
  `DetachedMode`.
- `ServerlessMode._create_vpc()`, `_create_subnet()`, and
  `_create_security_group()` (182 LOC). These built the VPC through
  CloudFormation rather than direct EC2 calls, which is why #69's pass missed
  them; `create_vpc` is gone from `ServerlessMode` as well (closes #73).

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

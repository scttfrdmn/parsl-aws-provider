# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.7.0] - 2026-07-31

### Security
- **IMDSv2 is now required on every instance the package launches.** Nothing set
  `MetadataOptions` anywhere before, so every worker, bastion, spot instance, and
  fleet instance accepted unauthenticated IMDSv1 requests — the shape that turns
  any SSRF in user code into instance-credential theft, and the workers run
  arbitrary submitted commands. `HttpTokens=required` is carried by the launch
  template, so it applies to the on-demand, spot, and fleet paths at once; the
  bastion's own `RunInstances` call and the bastion-manager script's worker
  launches set it directly. `HttpEndpoint` stays `enabled` (SSM reads the instance
  identity document from IMDS, so disabling it would break dispatch), and the hop
  limit is deliberately left at EC2's default of 2 rather than tightened to 1 —
  a request from inside a container spawned by `worker_init` traverses the host
  network namespace and costs a hop. No IMDSv1 fetch exists anywhere in the
  package, so nothing internal depended on the old default (closes #85).
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
- **Every generated Globus Compute endpoint config was unloadable.**
  `generate_endpoint_config()` wrote
  `type: parsl_ephemeral_aws.globus_compute.GlobusComputeProvider`, but Globus
  Compute resolves a provider by plain attribute lookup on the `parsl.providers`
  module — `getattr(parsl.providers, type_name, None)`, raising when the result
  is `None` — and `getattr` does not walk dots, so a dotted path can never
  resolve. The type key is now the bare class name, and importing
  `parsl_ephemeral_aws` assigns the class onto `parsl.providers`.
  Registering at import is necessary but not sufficient, since the endpoint
  daemon has no reason to import this package: `generate_endpoint_config()`
  therefore also writes a small `config.py` shim that imports the package and
  then hands `config.yaml` to Globus Compute's own loader.
  `get_config()` prefers `config.py` when both are present, which is what makes
  the pair work; `config.yaml` remains the single place to edit. Verified against
  `globus-compute-endpoint` 4.15.0, with a regression test that loads a generated
  directory through the real loader and a negative control asserting the bare
  YAML still fails without the shim. Single-user endpoints only — multi-user
  manager endpoints resolve through a different path and are tracked in #133
  (closes #87).
- The generated endpoint config omitted `vpc_id`, `subnet_id`, and
  `security_group_id`, which #69 made required. A config that resolved its
  provider type would then have failed in the constructor with
  `vpc_id, subnet_id, and security_group_id are required` and no indication of
  where to set them (refs #87).
- `ServerlessMode`'s spot fleet path could never have launched an instance. Its
  CloudFormation template declared a `RegionMap` of AMIs that no `FindInMap`
  ever read, so the fleet's launch template carried no `ImageId` and EC2 refused
  every request with `Parameter 'amiIdList' cannot be empty` — confirmed against
  both fleet APIs. The template also padded a fixed set of `!Select` slots by
  repeating an instance type, which EC2 rejects outright with
  `InvalidFleetConfig: The fleet configuration contains duplicate instance
  pools`; a `DryRun` does not catch that, having accepted the identical
  duplicate-bearing request. The fleet is now created directly by
  `_create_job_fleet()`, which resolves the AMI from SSM and deduplicates the
  override list (closes #86).
- The detached mode's default bastion path always failed. `_create_bastion_stack()`
  sent `UseSpotFleet`, `InstanceTypes`, `NodesPerBlock`, and
  `SpotMaxPricePercentage` to `bastion.yml`, which declares none of them, and
  CloudFormation rejects an undeclared parameter outright:
  `ValidationError: Parameters: [UseSpotFleet] do not exist in the template`.
  Since `bastion_host_type` defaults to `"cloudformation"`, this was the default
  path. All four describe a *worker fleet* and the bastion is a single host, so
  they are dropped rather than added to the template; the fleet settings already
  reach the workers through the bastion manager script (refs #86).
- No spot fleet was ever registered with the interruption monitor.
  `StandardMode` iterated a `"fleet_requests"` list on each block — at submit
  time and again after a state load — but the manager records a single
  `"fleet_request_id"` and nothing has ever written `"fleet_requests"`. The loop
  body was therefore unreachable, so every fleet ran unmonitored while
  `spot_interruption_handling=True` reported otherwise (refs #86).
- The orphan sweep missed both kinds of fleet resource it was meant to find.
  `cleanup_all_spot_fleet_resources()` looked for IAM roles named
  `parsl-aws-spot-fleet-role-*` only, but the bastion manager script created
  them as `parsl-ephemeral-spot-fleet-role-*`, so every role that path made was
  leaked. It also enumerated fleets with `describe_spot_fleet_requests`, which
  cannot see an EC2 Fleet at all. Both prefixes are now swept, and fleets are
  found through the `aws:ec2:fleet-id` tag on their instances (refs #86).
- The synthesised "similar instance types" in the bastion manager script
  produced invalid type names for most families. Slicing off the first
  character assumes a single-character family, so `m5a.large` became
  `mm5a.large`/`rm5a.large` and `c6i.large` became `c7i.large` via a
  generation bump that ignored the suffix. Each bad name was a pool the fleet
  could not draw from. The script now uses the configured instance types, or
  the job's single type (refs #86).
- A spot instance that shut itself down was left `stopped` with a billed EBS
  volume that the provider had already forgotten about. `InstanceInitiatedShutdownBehavior`
  is not a member of the `LaunchSpecification` shape `RequestSpotInstances`
  accepts, so the spot path could not set it and EC2's `stop` default applied to
  the `shutdown -h now` that `_prepare_init_script` appends — while
  `EC2_STATUS_MAPPING` maps `stopped` to COMPLETED, so `_cleanup_resources()`
  dropped the tracking record and the volume was orphaned *and* untracked. This is
  the same leak Phase 1.3a closed on the on-demand path, still open on spot. The
  spot path now launches through `RunInstances` with
  `InstanceMarketOptions={"MarketType": "spot"}`, which does accept both
  `InstanceInitiatedShutdownBehavior` and `MetadataOptions` — verified against
  real EC2: the instance reports `InstanceLifecycle=spot` with a spot request ID,
  shutdown behaviour `terminate`, and `HttpTokens=required` (closes #85).
- `SpotFleetManager.terminate_block()` leaked the block's launch template whenever
  no fleet request ID had been recorded for the block. It logged a warning and
  returned before the template was deleted, so a block whose request failed
  between creation and bookkeeping held its template until
  `cleanup_all_resources()` ran — or forever, if the process died first. The
  template belongs to the block, not to the fleet request, and is now deleted on
  that path too (refs #85).
- The `SpotFleetManager` provider stand-in that `StandardMode` builds never
  carried `iam_instance_profile_arn`, so the manager's `getattr` fell through to
  `None` and every fleet instance launched with no instance profile — SSM never
  came online and dispatch silently fell back to UserData. The stand-in is built
  in `__init__`, before `_resolve_instance_profile()` runs, so the resolved ARN is
  now also propagated to it once known (refs #85).
- Spot fleet requests ignored `spot_allocation_strategy` entirely. Both
  `SpotFleetManager._create_spot_fleet_request()` and the fleet request inside
  the generated bastion-manager script hardcoded `lowestPrice`, so the
  configured value was never read at any call site — meaning every spot fleet
  drew from the pools with the *least* spare capacity, the most
  interruption-prone choice available. Both now send the configured strategy
  (closes #84).
- `get_default_ami()` raised `AMINotFoundError` for any region absent from its
  hardcoded table, including regions AWS has added since the table was written.
  It now resolves from SSM and falls back to the table only when that fails
  (closes #84).
- `scale_in()` passed `None` into `cancel()` for any tracked resource carrying no
  `job_id`, which `@typechecked` turned into
  `TypeCheckError: item 0 of argument "job_ids" ... is not an instance of str` —
  aborting the whole scale-in rather than skipping the one resource. A resource
  normally has a `job_id`, but an interrupted `submit()` or a partially-restored
  state document can leave it absent. Such resources are now filtered out. (This,
  not Parsl, was the real source of the non-string-ID hazard #82 describes:
  `BlockProviderExecutor.blocks_to_job_id` is `Dict[str, str]` and `submit()`
  returns `str`, so Parsl round-trips strings safely) (closes #82).
- The `test-bats` CI job failed in setup, before running a test. `sudo` was
  applied to the `npm install` line only, so `mkdir -p /usr/local/lib/bats` ran
  unprivileged and died on `Permission denied` once the runner image stopped
  leaving `/usr/local/lib` group-writable. The `mkdir` and the three helper-library
  clones now run under `sudo` (refs #83).
- The `integration-tests` CI job failed unconditionally. LocalStack OSS is
  end-of-life — the upstream repository was archived read-only in March 2026,
  `4.14.0` is the last community image, and `localstack/localstack:latest` now
  resolves to the Pro build (byte-identical digest to
  `localstack/localstack-pro`). The container exited 55 on
  `License activation failed!` before any step ran, and `continue-on-error: true`
  does not cover service-container startup. The LocalStack service container was
  removed first, leaving the moto-backed tests running and the emulator-gated
  ones skipping; substrate then replaced the endpoint outright, so the gated
  tests now execute too (refs #83, closes #125).
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
- **Spot interruption warnings now arrive before the instance dies.** An
  EventBridge rule matching `EC2 Spot Instance Interruption Warning` delivers to
  an SQS queue that the driver long-polls, giving roughly two minutes of notice
  while the instance is still `running` — the only window in which checkpointing
  can succeed. `SpotInterruptionMonitor` creates the rule and queue in
  `start_monitoring()` and deletes them in `stop_monitoring()`, including when no
  thread was running, since nothing else in the package would reclaim them.
  This **requires new IAM permissions**: `events:PutRule`, `events:PutTargets`,
  `events:RemoveTargets`, `events:DeleteRule`, `events:TagResource`,
  `sqs:CreateQueue`, `sqs:GetQueueAttributes`, `sqs:SetQueueAttributes`,
  `sqs:ReceiveMessage`, `sqs:DeleteMessage`, and `sqs:DeleteQueue`. Failing to
  create the notifier is logged and does **not** fail the workflow: detection
  degrades to the pre-existing EC2-state poll, which still works but can only
  report an interruption post-facto. Set `use_event_bridge=False` on the monitor
  to skip creation deliberately. Verified end to end against real AWS with a
  Fault Injection Simulator experiment
  (`aws:ec2:send-spot-instance-interruptions`): the warning reached the queue
  15.2s after the experiment started, with the instance still `running`, and a
  handler registered through the provider's own monitor fired with
  `Source: "eventbridge"` (closes #86).
  - **No IAM role is created for the target, and none is needed.** Delivery to
    SQS is authorised by the *queue's* resource policy, unlike most target
    types; `put_targets` with an SQS ARN and no `RoleArn` returns
    `FailedEntryCount=0`. The queue policy grants `events.amazonaws.com`
    `sqs:SendMessage` conditioned on `aws:SourceArn` being this rule, so no
    other rule in any account can post to it.
  - **The rule cannot be scoped to this provider's instances.** The event's
    `detail` carries only `instance-id` and `instance-action`, and instance IDs
    are not known until the fleet launches, so the rule necessarily matches
    every spot interruption in the account and region. Each monitor discards
    warnings for instances it does not track; two providers in one account each
    see the other's warnings and ignore them.
  - `EC2 Instance Rebalance Recommendation` is deliberately **not** matched. It
    signals elevated interruption risk rather than an impending reclaim, so
    treating it as an interruption would checkpoint and tear down workers that
    were never going away.
  - A fake warning cannot be used to test this. `put_events` refuses any
    `aws.`-prefixed source with `NotAuthorizedForSourceException`, which is why
    the E2E test drives a real interruption through FIS. Set
    `AWS_TEST_FIS_ROLE_ARN` to run those two tests; the other nine in
    `tests/aws/test_spot_warning_e2e.py` need only the standard E2E network.
- `create_ec2_fleet()`, `describe_ec2_fleet()`, `get_ec2_fleet_instance_ids()`,
  `delete_ec2_fleet()`, `build_fleet_launch_template_configs()`,
  `normalize_ec2_fleet_allocation_strategy()`,
  `create_spot_interruption_notifier()`, and
  `delete_spot_interruption_notifier()` in `utils/aws.py`, plus
  `find_fleet_ids_for_workflow()` and `find_legacy_spot_fleet_request_ids()` in
  `compute/spot_fleet_cleanup.py` (refs #86).
- `tests/unit/test_spot_warning_notifier.py` — 43 tests. The notifier had none.
  Four claims a live probe proves once and that then rot silently are pinned
  here: the queue policy is set *before* the target is added (a warning arriving
  in that window is dropped as unauthorised and never retried, so the two
  minutes elapse and the instance is gone — and a live probe cannot catch this,
  since it creates the rule long before any interruption fires); `put_targets`
  reports refusal in `FailedEntryCount` rather than by raising, so an unchecked
  call wires nothing and the absence of warnings is indistinguishable from the
  absence of interruptions; every message is deleted *before* it is parsed,
  because the rule matches the whole account and an unparseable body would
  otherwise be redelivered on every poll, crowding out usable warnings via
  `MaxNumberOfMessages`; and the notifier is torn down even when no thread was
  running. Ordering assertions span two clients, so both are attached to a
  shared parent mock whose `mock_calls` interleaves them (refs #86).
- `tests/aws/test_spot_warning_e2e.py` — 11 tests against real AWS. Reads back
  what AWS actually stored rather than what was sent: the target has no
  `RoleArn`, the rule is `ENABLED` with the exact pattern, the queue policy's
  `aws:SourceArn` matches the ARN AWS assigned, and the retention period is
  whatever SQS clamped it to (SQS clamps rather than rejects, so reading it back
  is the only way to know). Deletion is asserted inside the tests, not only in
  teardown, so a silent teardown failure cannot hide a leaked rule (refs #86).
- **Launch templates.** `StandardMode.initialize()` now builds one launch template
  that every launch path shares — on-demand, spot, and spot fleet — carrying the
  AMI, instance type, network interface, key pair, IMDSv2 settings,
  `InstanceInitiatedShutdownBehavior`, and the resolved IAM instance profile.
  Per-job `UserData` and tags stay per-launch overrides. The template is tagged
  with the provider ID and deleted by `cleanup_infrastructure()`; its ID and
  version are persisted, and a state document written before this change gets a
  template on resume. This is the hard prerequisite for the EC2 Fleet/ASG
  migration in #86: those APIs accept a template reference and nothing resembling
  `RunInstances` kwargs (closes #85).
- `build_launch_template_data()`, `create_launch_template()`,
  `delete_launch_template()`, and `encode_user_data()` in `utils/aws.py`, plus
  `IMDSV2_METADATA_OPTIONS` and `LAUNCH_TEMPLATE_NAME_PREFIX` in `constants.py`.
  `create_launch_template()` is idempotent: `initialize()` may run again after a
  partial failure, and a duplicate name is rejected with
  `InvalidLaunchTemplateName.AlreadyExistsException`, so an existing template is
  adopted by adding a *new version* rather than reusing the old one — otherwise a
  resumed provider whose AMI or instance type had changed would silently keep
  launching the previous definition.
- Spot Fleet builds a launch template per block. `SpotFleetLaunchSpecification`
  has no `MetadataOptions` member, so a template is the only route to IMDSv2 on
  the fleet path, and `LaunchTemplateConfig.Overrides` carries no `UserData` — the
  per-block user data has to live in a per-block template rather than in
  overrides. If template creation fails the manager falls back to
  `LaunchSpecifications` and logs that IMDSv2 will not be enforced.
- `tests/aws/test_launch_template_e2e.py` — real-AWS coverage for what only a live
  launch can show: that the stored template carries IMDSv2 and `terminate`, that
  an instance launched from it inherits both, that the spot instance really is
  spot rather than a silent on-demand downgrade, and that `shutdown()` deletes the
  template.
- AMIs are resolved at runtime from AWS's public SSM Parameter Store aliases
  (`/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-{arch}`), which
  AWS repoints at every AL2023 release. Any region works, including ones no
  hardcoded table ever listed — verified by booting an instance in
  `ap-southeast-4`. The version-independent `kernel-default` alias is used
  deliberately: naming `6.1`/`6.12`/`6.18` would only become the next stale
  constant (closes #84).
- **arm64/Graviton support.** Nothing in the package distinguished architectures
  before, and every AMI it could reach was x86_64, so a Graviton `instance_type`
  could not launch at all. `architecture_for_instance_type()` derives the
  architecture from the instance-type family and `get_default_ami()` takes an
  `architecture` argument; the provider exposes the resolved value as
  `EphemeralAWSProvider.architecture`. Graviton is a 20–40% price/performance
  gain on the same workload. The classification reads the family's *generation
  suffix* — AWS appends `g` to every arm64 family (`c7g`, `m8g`, `r7gd`, `c8gn`)
  and to no x86_64 one — so the `g5`/`g6e` GPU families, where the `g` is a
  prefix, are correctly x86_64. Validated against `describe_instance_types` for
  all 1,346 types offered in us-east-1: 396 arm64, 950 x86_64, zero
  misclassifications (closes #84).
- `normalize_spot_fleet_allocation_strategy()` in `utils/aws.py`, translating the
  documented kebab-case strategy names to the camelCase spelling
  `RequestSpotFleet` requires, and rejecting unknown values with a message that
  lists the accepted ones rather than waiting for EC2's `InvalidParameterValue`.
- `tests/unit/test_ami_resolution.py` (35 tests) and
  `tests/unit/test_allocation_strategy.py` (25 tests). The allocation-strategy
  suite asserts on the request boto3 actually receives and on the generated
  bastion script's injected literal, not only on the helper — both defects were
  that the configured value was never read, which a helper-level test cannot
  catch.
- `cores_per_node` and `mem_per_node` constructor arguments. Parsl's
  `ExecutionProvider` declares both and `HighThroughputExecutor` sizes its worker
  count from them — with both `None` it takes the
  `# our best guess-- we do not have any provider hints` branch and runs **one
  worker per node** however large the instance. When not supplied they are
  resolved from `ec2:DescribeInstanceTypes` via the new
  `utils.aws.describe_instance_capacity()`, which returns `(None, None)` on any
  failure: it runs during `__init__`, so an unreachable or unauthorized EC2 must
  not stop the provider from constructing. Serverless mode leaves both `None`
  deliberately — Lambda and Fargate have no node to describe, and `memory_size`
  is a per-invocation allocation rather than the same concept (closes #82).
- `TestExecutionProviderConformance` in `tests/unit/test_provider_interface.py`
  asserts the three signatures against `inspect.signature(ExecutionProvider.*)`
  rather than restating them, so a future base-class change surfaces as a test
  failure instead of a runtime `TypeCheckError`. Also covers opaque job IDs
  through `status()`/`cancel()`, positional ordering when opaque and real IDs are
  mixed, and `tests/unit/test_instance_capacity.py` for the capacity lookup
  (closes #82).
- `tests/substrate_support.py` — emulator session/client helpers and VPC
  setup/teardown, replacing the package-internal
  `parsl_ephemeral_aws/utils/localstack.py`. It lives under `tests/` because no
  package code has ever imported it; only test modules did. `SUBSTRATE_ENDPOINT`
  selects the endpoint, with `LOCALSTACK_ENDPOINT` still honoured so an existing
  developer environment keeps working (closes #125).
- An `Emulator conformance` CI step running `tests/test_substrate_emulation.py`,
  which drives raw boto3 and imports no package code — so a failure there means
  the emulator regressed rather than the provider. Gated, since a substrate
  change should not block a provider PR (closes #125).
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
- `tests/unit/test_docs_examples.py` — the check `docs/README.md` and
  `docs/examples.md` had both been promising. Every fenced Python block in the
  documentation and every file in `examples/` is parsed, and each keyword argument
  passed to `EphemeralAWSProvider`, `GlobusComputeProvider`,
  `HighThroughputExecutor`, or `Config` is checked against the real signature.
  The accepted set is the union over `inspect.getmro()`: `**kwargs` is *not*
  treated as "accepts anything", because on this provider it means the opposite —
  it exists only to reject unknown options (#105) — while
  `GlobusComputeProvider` does forward through it. Also asserts the 15 renamed or
  removed option names no longer resolve, that every example disables HTEX
  encryption (#62) and shuts its provider down, and that each example is listed
  in `examples/README.md`. It found two unparseable blocks on its first run.
- `tests/unit/test_license_headers.py` — enumerates tracked files via
  `git ls-files` and asserts no stale copyright year, one canonical copyright
  form, and both SPDX tags on every Python and shell source. The year was wrong
  for seven months before anyone read a header (#119); a grep is cheaper than
  rediscovering it next January. `tools/` is exempt from the *missing*-header
  check only — 90 files there have never carried one and #93 prunes the
  directory in v0.8.0 — but the stale-year checks stay tree-wide, matching how
  ruff and bandit are scoped.
- The CI docs job now builds with `SPHINXOPTS="-W"`, so a documentation warning
  fails the build. The tree carried 398 of them, which is how ~65 toctree entries
  pointing at pages nobody had written stayed invisible (closes #124).

### Changed
- **`GlobusComputeProvider.minimum_iam_policy()` is now actually minimum.** It
  granted `ec2:CreateVpc`, `DeleteVpc`, `CreateSubnet`, `CreateSecurityGroup`,
  `CreateNatGateway`, and `RequestSpotFleet` — permissions the package no longer
  uses, and whose delete half would have let it destroy caller-owned network
  resources it never created. The action lists are now derived from the calls the
  package actually makes: the network grants are read-only (`DescribeVpcs`,
  `DescribeSubnets`, `DescribeSecurityGroups`) since #69 made those IDs
  caller-supplied, Spot Fleet is replaced by `ec2:CreateFleet`/`DescribeFleets`/
  `DeleteFleets` per #86, and the launch-template actions #85 introduced are
  added. A new `SpotInterruptionWarning` statement carries the EventBridge and
  SQS permissions the #86 warning path needs. No IAM delete actions are granted,
  because the provider performs no instance-profile teardown (#132) — granting
  them would permit more than it performs. The module docstring's permission list
  is regenerated from the same source, and both now state what is *not* covered
  (the S3 and Parameter Store state backends, and detached and serverless modes)
  rather than implying whole-package coverage (closes #87).
- The `globus` extra's floor is raised from `globus-compute-{sdk,endpoint}>=2.0.0`
  to `>=4.10.1`. The old floor described nothing installable: `globus-compute-endpoint`
  pins parsl *exactly*, and 4.10.1 is the first release pinning
  `parsl==2026.4.20`, which is this project's floor — every earlier release pins
  an older parsl and cannot resolve alongside it. Separately, the `config.py`
  shim imports
  `globus_compute_endpoint.endpoint.config.utils.load_config_yaml`, and that
  module does not exist before 2.2.0, so a 2.0.x install would have produced a
  generated config that failed at endpoint start rather than at install time.
  The extra now installs alongside `test`, so the config-loading regression test
  runs in the project environment (refs #87).
- **Spot Fleet is replaced by EC2 Fleet.** AWS: "Spot Fleet … uses a legacy API
  with no planned investment." Every `request_spot_fleet` call is now
  `create_fleet` — in `SpotFleetManager`, `ServerlessMode`, and the detached
  mode's bastion manager script — backed by the launch template #85 introduced.
  The migration was only possible after that, because `CreateFleet` has no
  `LaunchSpecifications` member at all: a template is mandatory, not preferred.
  Requires `ec2:CreateFleet`, `ec2:DescribeFleets`, and `ec2:DeleteFleets` in
  place of `ec2:RequestSpotFleet`, `ec2:DescribeSpotFleetRequests`, and
  `ec2:CancelSpotFleetRequests`; the legacy permissions are still used by the
  orphan sweep, which cancels pre-upgrade requests. The `use_spot_fleet` kwarg,
  the `SpotFleetManager` class name, and the `fleet_request_id` state key are all
  unchanged, so existing configuration and state documents keep working.
  Consequences worth knowing:
  - **No IAM service role is created any more.** `CreateFleet` has no
    `IamFleetRole` member, so `_get_iam_fleet_role()` and its
    `AmazonEC2SpotFleetTaggingRole` attachment are gone from both the manager and
    the bastion script, along with the 10-second IAM propagation sleep. The
    orphan sweep still deletes roles left by a pre-upgrade workflow.
  - **Fleet type is `instant`.** It returns the launched instance IDs
    synchronously in the `CreateFleet` response, which is what preserves the
    block → instance-ID mapping the rest of the package is built on; the
    alternatives are asynchronous and would need polling before a block could
    report its instances. The 300-second `_wait_for_fleet_instances()` poll is
    gone with it, and `StandardMode` now fails a block immediately when the fleet
    launched nothing, instead of waiting out a timeout that conflated "no spot
    capacity" with "slow to boot".
  - **An `instant` fleet gets no Capacity Rebalance.** `ReplaceUnhealthyInstances`,
    `TerminateInstancesWithExpiration`, and `SpotOptions.MaintenanceStrategies`
    are not merely ignored for this fleet type — EC2 rejects each with
    `InvalidParameter` ("only compatible with fleet type maintain"), verified
    against real EC2. The two-minute interruption warning is delivered through
    the EventBridge route above instead.
  - **An unfilled fleet is a successful API call.** An `instant` fleet reports
    pools it could not fill in the response body rather than failing, and makes
    no further attempts, so both `StandardMode` and `ServerlessMode` now treat an
    empty fleet as a submission failure and delete it rather than leaving an
    empty fleet behind.
  - **`aws:ec2:fleet-id` is the only way to find an instant fleet's instances.**
    `describe_fleets` with no `FleetIds` returns an empty list for instant fleets
    (AWS: "you must specify the fleet ID in the request, otherwise the fleet does
    not appear in the response"), a tag filter on `describe_fleets` does not find
    them either, and `describe_fleet_instances` refuses them with `Unsupported`.
    The orphan sweep therefore goes through `describe_instances` filtered on that
    reserved tag. moto 5.1.21 supports `create_fleet(Type="instant")` but does
    not apply the tag, so this path is asserted against real AWS only.
  - **`MaxTotalPrice` replaces `SpotPrice`, and is fleet-wide.** The legacy value
    was per instance-hour, so `spot_max_price_percentage` is now multiplied by
    the node count. AWS advises against setting it at all ("can lead to increased
    interruptions"), so it is sent only when configured.
  - **The fleet is no longer built by CloudFormation on any path.** The
    `Overrides` list is variable-length, one entry per instance type, and CFN
    cannot build one: `Fn::ForEach` (`AWS::LanguageExtensions`) expands to a
    *map*, confirmed by reading the processed template off a change set; a fixed
    set of `!Select` slots cannot be left partly filled, because an out-of-range
    `!Select` fails validation even inside the untaken branch of an `!If`; and
    padding the slots by repeating a type is rejected by EC2 as duplicate
    instance pools. `ServerlessMode` calls `CreateFleet` directly, which also
    stops it deploying an ECS cluster, task definition, two IAM roles, and a log
    group that an EC2 fleet never touches — and makes the fleet ID available
    immediately instead of after up to three minutes of polling stack outputs,
    during which an interruption would have gone unhandled.
  - The spot bastion is now an `AWS::EC2::Instance` referencing a launch template
    with `InstanceMarketOptions`, replacing the `AWS::EC2::SpotFleet` resource
    `bastion.yml` used to carry for that case. `AWS::EC2::Instance` does not
    accept `InstanceMarketOptions` directly, but a launch template does, and an
    instance referencing that template launches as spot. One template now serves
    both bastion variants, and the spot bastion gains IMDSv2 with it.
  - Failing to build a fleet's launch template now fails the block. The old
    `LaunchSpecifications` fallback silently launched without IMDSv2 —
    `SpotFleetLaunchSpecification` has no `MetadataOptions` member — and there is
    nothing to fall back to under `CreateFleet` anyway.
- **`warm_pool_ttl` now defaults to 120 seconds, down from 600.** A warm instance
  is left *Running*, not Stopped, so it bills at the full instance rate for the
  whole window whether or not another job arrives. AWS is blunt about this shape
  — keeping warm instances running is "highly discouraged to avoid incurring
  unnecessary charges" — and the native ASG warm pool it recommends instead holds
  them Stopped or Hibernated. This package cannot use the native pool yet:
  dispatch is SSM `SendCommand`, and a Stopped instance runs no SSM agent, so it
  cannot receive one. Moving to a pull model is tracked as #130 for v0.8.0. Until
  then the cost is bounded rather than eliminated — an idle pool costs a fifth of
  what it did, `warm_pool_size` is capped at `MAX_WARM_POOL_SIZE` (20) with a
  `ValueError` above it, and enabling a pool logs a warning stating the instance
  count, the window, and the resulting idle instance-seconds. The docstring says
  so too; the kwarg name alone does not convey that it costs money while doing
  nothing (refs #86).
- Plain spot instances are requested with `RunInstances` +
  `InstanceMarketOptions` instead of `RequestSpotInstances`. The old API cannot
  express either IMDSv2 or the shutdown behaviour, as above. `RequestSpotInstances`
  remains the fallback for accounts without `ec2:CreateLaunchTemplate`, where the
  provider keeps working without IMDSv2 on that one path.
- Four API asymmetries drove the shape of this work and are recorded here because
  each is invisible in a mock and expensive to rediscover, all verified against
  the botocore service model and real EC2:
  - **botocore base64-encodes `UserData` for `RunInstances` only.** The two
    built-in handlers are `before-parameter-build.ec2.RunInstances` and
    `before-parameter-build.autoscaling.CreateLaunchConfiguration`;
    `CreateLaunchTemplate` is not among them. Plaintext user data in a template is
    stored verbatim, base64-*decoded* by cloud-init, and fails silently — the
    instance boots fine and never runs the worker. Encoding twice is the
    mirror-image trap, which is why `encode_user_data()` exists and is applied to
    template data only.
  - **The `InstanceStopped` waiter lists `terminated` as an explicit *failure*
    acceptor.** The AMI-baking builder instance must therefore keep
    `InstanceInitiatedShutdownBehavior="stop"`; inheriting the template's
    `terminate` would fail the bake outright, not merely slow it. The builder
    deliberately does not use the launch template.
  - **`RequestSpotFleet` accepts both `LaunchSpecifications` and
    `LaunchTemplateConfigs` in one request** — a DryRun with both returns
    `DryRunOperation`. The template would silently lose to the specifications,
    taking IMDSv2 with it, so exactly one launch form is sent.
  - **A launch template rejects a `NetworkInterfaces` entry together with
    top-level `SecurityGroupIds`.** The interface form is required for
    `AssociatePublicIpAddress`, so it wins whenever a subnet is known.
- The default spot allocation strategy is now `price-capacity-optimized`, AWS's
  current recommendation, replacing the documented-but-unused
  `capacity-optimized`. It draws from the pools with the deepest spare capacity
  and then the lowest price among those, so it interrupts far less often than
  `lowest-price` at close to the same cost.
- The two fleet APIs spell this enum differently and each rejects the other's
  spelling, verified against real EC2: `RequestSpotFleet` takes camelCase
  (`priceCapacityOptimized`; the kebab-case form returns
  `InvalidParameterValue`), while `CreateFleet` takes kebab-case (the camelCase
  form returns `InvalidParameter`). `spot_allocation_strategy` therefore stays
  kebab-case — as every docstring has always documented — and is normalised at
  the `RequestSpotFleet` boundary. `SPOT_FLEET_DEFAULT_ALLOCATION_STRATEGY` holds
  the camelCase form for the current path and
  `EC2_FLEET_DEFAULT_ALLOCATION_STRATEGY` the kebab-case form for the
  `CreateFleet` migration in #86.
- `DEFAULT_AMI_MAPPING` is demoted from the AMI source of truth to an offline
  fallback for `get_default_ami()`, used only when the SSM lookup fails —
  chiefly so moto- and substrate-backed tests need no network. It has been
  refreshed, but a hardcoded table cannot be kept current: **all 21 entries of
  the previous one, stamped 2026-03-01, were unusable by 2026-07-30** — 9 carried
  a `DeprecationTime` of 2026-05-17, 6 returned `InvalidAMIID.NotFound`, 2 were
  `InvalidAMIID.Malformed`, and the rest were in regions that could not be
  reached. Nothing noticed, because a deprecated AMI still launches until AWS
  deletes it. (The table's header also claimed `kernel-6.12`, while
  `describe_images` reported the us-east-1 entry was built from `kernel-6.1`.)
  The four opt-in regions it listed are dropped, since no value for them could be
  verified; SSM resolves them normally from an account that has them enabled. The
  table is x86_64-only, and `get_default_ami()` raises rather than hand one of
  its entries to an arm64 instance type.
- `submit()`, `status()`, and `cancel()` now carry Parsl's own signatures:
  `submit(command, tasks_per_node, job_name="parsl.auto")`,
  `status(job_ids: Sequence[object])`, and `cancel(job_ids: Sequence[object])`.
  They were `List[str]` with `job_name: Optional[str] = None`, which is narrower
  than the base class — and `@typechecked` on the class makes annotations
  load-bearing at runtime, so a non-`str` ID raised `TypeCheckError` instead of
  being reported as unknown. `job_map` stays string-keyed: each incoming ID is
  narrowed once at the method boundary, and anything that is not a `str` resolves
  to `JobState.UNKNOWN` (or `False` from `cancel()`) rather than raising.
  `"parsl.auto"` is treated as "no caller-chosen name", the same as the `None`
  this provider previously defaulted to. `self.resources` is widened to
  `Dict[object, Any]` to match the base declaration (closes #82).
- `status()` and `cancel()` collect their per-job results by *position* rather
  than in a dict keyed by the job ID. `Sequence[object]` admits unhashable IDs,
  and using one as a dict key raised `TypeError: unhashable type: 'list'` — from
  inside the `except` handler too, so it escaped the method rather than being
  reported as UNKNOWN. Positional collection also means a repeated ID yields one
  entry per occurrence, which is what Parsl indexes (refs #82).
- Minimum Parsl raised from `2026.1.5` to `2026.4.20`. Not the `2026.7` #82
  proposed: `globus-compute-endpoint` pins Parsl *exactly* (4.15.0 →
  `parsl==2026.4.20`), so any floor above that makes the `globus` extra
  unresolvable. The signatures above are identical across the whole range —
  2026.7.x only widened `status`/`cancel` from `List[object]` to
  `Sequence[object]`, and `Sequence` is the wider type, so one declaration
  satisfies every version in the range (refs #82).
- **The AWS emulator is now [substrate](https://github.com/scttfrdmn/substrate),
  not LocalStack.** LocalStack OSS is end-of-life: the repository was archived
  read-only in March 2026, `4.14.0` is the last community image, and
  `localstack/localstack:latest` now shares a digest with the Pro build and exits
  55 on `License activation failed!`. Substrate is a deliberate drop-in — it
  serves `/_localstack/health` and `/_localstack/info` with LocalStack-shaped
  payloads — so the change is largely a rename. It is also a single Go binary
  rather than a Python package plus a bind-mounted Docker socket, which is what
  lets CI run it as a pinned service container. `docker-compose.localstack.yml`
  and `scripts/localstack-wait.sh` are replaced by
  `docker-compose.substrate.yml` and `scripts/substrate-wait.sh`; the `make`
  targets are `substrate-up`/`-wait`/`-down`/`-status`/`-reset`. Two behaviours
  substrate emulates that LocalStack did not are load-bearing here: EC2 instance
  state actually reaches `terminated` (which `EC2_STATUS_MAPPING` and one-shot
  mode depend on), and Lambda `create_function` + `invoke` both work — the
  Lambda conformance test was skipped under LocalStack and now runs
  (closes #125).
- The integration job in `ci.yml` runs a pinned
  `ghcr.io/scttfrdmn/substrate:0.76.0` service container, so the
  emulator-gated integration tests execute in CI for the first time since #69.
  No `--health-cmd` is declared — the substrate image is Alpine-based and ships
  no `curl` — so a `Wait for substrate` step polls `/health` from the runner
  instead (refs #83, closes #125).
- `docs/localstack_testing.md` → `docs/substrate_testing.md`, rewritten rather
  than renamed. The old version documented `use_localstack=True` and
  `localstack_endpoint=...` provider kwargs that have never existed in any
  release. The endpoint is redirected with `AWS_ENDPOINT_URL`, which botocore
  honours globally; the new document is written from a verified full provider
  lifecycle (construct → `submit` → `status` → `cancel`) through that variable
  (closes #125).
- The `test` extra no longer depends on `localstack`, which dissolves the
  `[tool.uv] conflicts` block: localstack pinned `dill==0.3.9` against the
  `globus` extra's `0.3.2`/`0.3.6`, and that conflict was the block's only
  reason to exist. The `globus` and `test` extras now co-resolve
  (closes #125).
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
- **All eight examples run.** Six raised `ValueError` on the first line of the
  constructor because they predate #69 and pass no network IDs; every one also
  used options that no longer exist. `mode=StandardMode(...)` and
  `state_store=FileStateStore(...)` — objects where the provider takes strings —
  `worker_type`, `state_prefix`, and HTEX's `max_workers` (the parameter is
  `max_workers_per_node`) are all gone. Each example now reads its VPC, subnet,
  and security group from `AWS_TEST_VPC_ID`/`AWS_TEST_SUBNET_ID`/`AWS_TEST_SG_ID`
  and exits `2` with the missing name when one is unset, rather than failing
  inside boto3 minutes later. `examples/README.md` lists all eight, where it had
  listed six.
- The `.md` documentation is rewritten against the real 52-parameter signature.
  `docs/state_persistence.md` documented three state-store constructors that
  exist in no form; `docs/operating_modes.md` still showed `create_vpc=True`.
  `docs/examples.md`, `docs/security.md`, and `docs/getting_started.md` all
  configured the provider with removed or renamed options (closes #124).
- `docs/localstack_testing.md` → `docs/substrate_testing.md`, following the
  emulator swap in #125.
- `DetachedMode.cleanup_infrastructure()`'s docstring said it "cleans up the VPC,
  subnet, and security group if they were created by the provider." It has not
  done that since #69 — the caller supplies them and this mode never created
  them. The inline comment claiming the same has been corrected too; it gates
  monitor teardown, not networking.
- The `SPDX-FileCopyrightText` range is now `2025-2026` across 153 tracked files,
  including `LICENSE`'s Apache appendix. `docs/conf.py` and
  `scripts/setup_environment.sh` had no header at all (closes #119).
- The four `GlobusComputeProvider` constructions in
  `tests/aws/test_globus_compute_e2e.py` now supply network IDs. All four raised
  `ValueError`, including the three config-generation tests that touch no AWS
  resources: the guard runs before the operating mode is built, so the existing
  `patch.object(..., "_initialize_operating_mode")` does not bypass it. Those
  three now pass. The module docstring's `pip install` instructions are `uv sync`.

### Removed
- `SpotFleetManager._get_iam_fleet_role()` and `_create_spot_fleet_request()`,
  `_wait_for_fleet_instances()`, and the bastion manager script's
  `get_spot_fleet_role()` and `wait_for_fleet_instances()`. `CreateFleet` needs no
  service role and reports its instances synchronously, so all five became
  unreachable (refs #86).
- `SpotFleetRole` and `SpotFleetRequest` from `ecs_worker.yml`, `SpotBastionHost`
  from `bastion.yml`, and the `UseSpotFleet`/`InstanceTypes`/`NodesPerBlock`/
  `SpotMaxPricePercentage` parameters both templates carried. The
  `SpotFleetLaunchTemplate` in `ecs_worker.yml` goes with them; the fleet is
  created by `CreateFleet` directly. `ecs_worker.yml` also loses a `RegionMap` of
  15 Python-3.9-era AMIs that no `FindInMap` referenced (refs #86).
- `parsl_ephemeral_aws/utils/localstack.py` (422 LOC). Test-only scaffolding that
  shipped in every wheel; no package code has ever imported it. Its replacement,
  `tests/substrate_support.py`, is not distributed (closes #125).
- `localstack` from the `test` extra, and the `[tool.uv] conflicts` block it
  required. Substrate is a container image, so there is no Python package to
  install; dropping it also removes seven transitive dependencies from
  `uv.lock` (closes #125).
- `docker-compose.localstack.yml` and `scripts/localstack-wait.sh`. The wait
  script's service-readiness greps matched on exact whitespace and never gated
  anything — both branches ended in `exit 0` (closes #125).
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
- The parallel reStructuredText documentation tree — 26 `.rst` files, ~7,800 lines,
  under `docs/source/`, `docs/advanced_topics/`, `docs/user_guide/`,
  `docs/getting_started/`, and `docs/operating_modes/`. Nine were index stubs whose
  `toctree` directives named ~65 pages nobody ever wrote, which is where most of
  the 398 build warnings came from. The other sixteen had real content, and that is
  the reason to delete rather than repair them: they documented a provider that
  does not exist. `worker_type`, `use_spot_instances`, `create_vpc`, `state_store=`,
  and `FileState` appear 60 times across them, and `configuration.rst` presented
  `vpc_id` as optional — the opposite of the truth since #69. Two files
  (`gpu_computing.rst`, 1,422 lines, and `mpi_workflows.rst`, 1,068) documented
  GPU-aware scheduling and multi-node MPI at length; the provider has no parameter
  for either, and never had. `docs/examples.md` now names both as unimplemented
  instead. `docs/source/conf.py` was a second, divergent Sphinx config for a
  `SOURCEDIR` that is `.`, and `docs/advanced_usage.md` had the same
  removed-options problem. `docs/api_reference.rst` replaces `api.rst` and
  `api/index.rst`, autodocumenting the modules that are actually reachable
  (closes #124).
- The three mode-diagram SVGs under `docs/images/`. They drew the VPC, subnet, and
  NAT gateway the provider created before #69 and no page referenced them.

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

[Unreleased]: https://github.com/scttfrdmn/parsl-aws-provider/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/scttfrdmn/parsl-aws-provider/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/scttfrdmn/parsl-aws-provider/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/scttfrdmn/parsl-aws-provider/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/scttfrdmn/parsl-aws-provider/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/scttfrdmn/parsl-aws-provider/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/scttfrdmn/parsl-aws-provider/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/scttfrdmn/parsl-aws-provider/releases/tag/v0.1.0

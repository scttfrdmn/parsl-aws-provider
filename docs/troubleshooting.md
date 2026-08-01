# Troubleshooting

Every configuration snippet below uses options that actually exist. Since #105
the provider rejects unknown keyword arguments:

```
ProviderConfigurationError: Unknown configuration option(s): use_ssh_tunnel.
Check the spelling against EphemeralAWSProvider.__init__; an option accepted
here but never read would be silently ignored.
```

If you see that, the option is not real — check
[api_reference.rst](api_reference.rst). Older versions of this document
recommended `use_ssh_tunnel`, `security_group_ingress`, `verify_ssl`,
`fallback_to_on_demand`, `max_cost_per_hour`, and a dozen others that were never
implemented; they now fail loudly instead of being ignored.

## Start here

```python
import logging

logging.basicConfig(level=logging.INFO)
logging.getLogger("parsl_aws_provider").setLevel(logging.DEBUG)
```

Or pass `debug=True` to the provider. Add `logging.getLogger("botocore").setLevel(
logging.DEBUG)` when you need to see the raw AWS calls — it is extremely verbose.

Then, in order:

1. **Confirm credentials resolve.** `aws sts get-caller-identity --profile <p>`.
2. **Confirm the network IDs exist in the right region.** Construction validates
   them, so a `ResourceNotFoundError` here is the answer, not a symptom.
3. **Confirm what the provider thinks it owns** — `provider.list_resources()`.
4. **Get onto the instance.** `aws ssm start-session --target i-...` needs no key
   pair and no inbound rule; `/var/log/cloud-init-output.log` holds the
   `worker_init` output.

## Configuration errors at construction

### `Unknown configuration option(s): ...`

The option does not exist. Common renames:

| Old / imagined | Real |
|---|---|
| `use_spot_instances` | `use_spot` |
| `spot_max_bid` | `spot_max_price` (a **string**) |
| `tags` | `additional_tags` |
| `aws_profile` | `profile_name` |
| `aws_access_key_id` / `aws_secret_access_key` | not accepted — use the environment or a profile |
| `state_store` | `state_store_type` |
| `state_bucket` | `s3_bucket` |
| `state_file` / `state_directory` | `state_file_path` |
| `worker_type` | `compute_type` |
| `idle_timeout_minutes` | `max_idle_time` (**seconds**) |
| `create_vpc` / `use_existing_vpc` | removed — the network is always yours (#69) |

Eight options that were unreachable before v0.8.0 are now accepted:
`idle_timeout`, `preserve_bastion`, `bastion_host_type`, and `workflow_id` on
`mode="detached"`; `lambda_runtime`, `ecs_task_cpu`, `ecs_task_memory`, and
`ecs_container_image` on `mode="serverless"` (#136). Each is accepted only on the
mode that implements it — setting one elsewhere raises
`ProviderConfigurationError` rather than silently keeping the default.
`lambda_memory` and `lambda_timeout` remain spelled `memory_size` and `timeout`
at the provider.

### `vpc_id, subnet_id, security_group_id are required`

As of v0.7.0 the provider never creates network resources. Supply your own; see
[network-prerequisites.md](network-prerequisites.md). The serverless-plus-Lambda
combination is the sole exception — Lambda functions run in the Lambda-managed
VPC, so none of the three is needed.

### `ResourceNotFoundError: subnet_id subnet-... is not usable`

The ID is malformed, deleted, or in a different region or account. Construction
verifies each ID with `describe_*`. Before v0.7.0 the provider silently set the
attribute to `None` and failed much later inside `RunInstances` with an opaque
`InvalidParameterValue`.

### `Invalid compute type: auto`

`compute_type` accepts `ec2`, `lambda`, or `ecs`. There is no `auto` at the
provider level; leaving it at the `ec2` default in serverless mode leaves
`ServerlessMode` on *its* internal `auto` heuristic. Set `lambda` or `ecs`
explicitly.

### `TypeCheckError: argument "mode" ... is not an instance of str`

`mode` is a string — `"standard"`, `"detached"`, `"serverless"`. Passing
`StandardMode(...)` does not work; the provider constructs the mode itself so it
can inject the session, state store, and resolved AMI.

### `warm_pool_size ... is supported only by mode='standard'`

`warm_pool_size`, `warm_pool_ttl`, `bake_ami`, `baked_ami_id`, and `one_shot` are
implemented by `StandardMode` alone. Setting them on another mode used to leak
instances no mode would reclaim (#80), so it is now rejected.

### `warm_pool_size > 0 requires ...`

Warm-pool and one-shot dispatch both go over SSM `SendCommand`, so the instance
needs `AmazonSSMManagedInstanceCore`. Pass either
`auto_create_instance_profile=True` or an explicit `iam_instance_profile_arn`.

## Workers launch but never register

By far the most common failure, and it is almost never on the AWS side.

**Cause.** HTEX workers connect **outbound** to the interchange running on your
client. The client must therefore accept **inbound** TCP on ports 54000–55000. A
laptop behind home or office NAT cannot, no matter how the worker security group
is configured.

**Diagnosis.** The instances reach `running`, `worker_init` completes cleanly in
`/var/log/cloud-init-output.log`, and `runinfo/*/` on the client shows no worker
ever connecting.

**Fixes, in order of preference:**

1. **Run the client on an EC2 instance in the same VPC**, with a security group
   that allows inbound 54000–55000 from the worker security group. Note the
   default VPC security group allows inbound only from itself, so if workers use a
   different group you need an explicit rule.
2. **Use detached mode** — a bastion owns the worker lifecycle, and the client
   need not be reachable at all.
3. **Use one-shot mode** for independent commands; it bypasses HTEX entirely and
   works from anywhere.

**Also check `encrypted=False`:**

```python
HighThroughputExecutor(label="aws", provider=provider, encrypted=False)
```

With encryption on, Parsl generates CurveZMQ certificates in the client's
`run_dir`, which workers cannot read — so they fail to register with no obvious
error. Certificate distribution is
[#62](https://github.com/scttfrdmn/parsl-aws-provider/issues/62).

**Interchange address.** On EC2, an elastic or public IP is not bound to the
interface; only the private IP is. Use `address_by_route()` for a same-VPC
deployment. `address_by_query()` returns the NAT/router WAN address, which nothing
in the VPC can connect back to.

## Instance launch failures

### `InsufficientInstanceCapacity`

AWS has no capacity for that type in that Availability Zone right now.

```python
provider = EphemeralAWSProvider(
    # ... network options ...
    use_spot=True,
    use_spot_fleet=True,
    instance_types=["m5.large", "m5a.large", "m6i.large", "c5.large"],
    spot_allocation_strategy="price-capacity-optimized",
)
```

`instance_types` is a list of type **names** — not `{"type": ..., "weight": ...}`
dicts, and the provider does not synthesize alternatives from `instance_type`.
Note the fleet is confined to the single subnet you pass, so it stays in one AZ;
multi-AZ diversification is not exposed. To move AZ, pass a subnet in a different
one.

### `Unsupported: The requested configuration is currently not supported`

Usually an instance type that does not exist in the AZ your subnet is in. `t3.micro`
is unavailable in `us-east-1e`, for example. Check with:

```bash
aws ec2 describe-instance-type-offerings --location-type availability-zone \
  --filters Name=instance-type,Values=t3.micro --region us-east-1
```

### Instances launch and terminate immediately

Read `/var/log/cloud-init-output.log` over SSM. A `worker_init` failure — a
missing package, a wrong Python version — is the usual cause. Note the instances
run with `InstanceInitiatedShutdownBehavior="terminate"`, so a `shutdown` inside
`worker_init` destroys the instance rather than stopping it.

### `AMINotFoundError`

The AMI is resolved from AWS's public SSM parameters, so this normally means the
`ssm:GetParameter` call failed — check that your credentials allow
`ssm:GetParameters` on `/aws/service/ami-amazon-linux-latest/*`, or pass
`image_id` explicitly. An arm64 AMI is selected automatically for Graviton
instance types.

### `Cannot submit job, already at max_blocks = N`

`max_blocks` caps concurrent submissions, and the provider raises rather than
queueing. Raise `max_blocks`, or let jobs finish first.

## Spot instances

### Frequent interruptions

Diversify across families and generations rather than sizes:

```python
provider = EphemeralAWSProvider(
    # ... network options ...
    use_spot=True,
    use_spot_fleet=True,
    instance_types=["m5.xlarge", "m5a.xlarge", "m5n.xlarge", "m6i.xlarge"],
    spot_allocation_strategy="price-capacity-optimized",  # the default
    spot_interruption_handling=True,
)
```

Keep `price-capacity-optimized`. `lowest-price` trades interruption rate for cost
and is usually the wrong choice for a workflow. Set `retries` on the Parsl
`Config` so reclaimed tasks are re-run.

There is no hibernate-on-interruption support and no on-demand/spot mix —
`spot_interruption_behavior` and `on_demand_percentage` do not exist.

### Interruptions are only noticed after the fact

Set `spot_interruption_handling=True`. The provider then creates an EventBridge
rule matching *EC2 Spot Instance Interruption Warning* with an SQS target and
polls it, giving the full two minutes of notice. Without it, the first sign is the
instance already in `shutting-down`.

An `instant` EC2 Fleet cannot use Capacity Rebalance — `CreateFleet` rejects
`SpotOptions.MaintenanceStrategies` for that type — which is why the warning comes
via EventBridge rather than from the fleet itself.

### The fleet is created but launches nothing

Capacity was unavailable at your price. Check `describe_fleets` for the errors,
add instance types, and raise or unset `spot_max_price_percentage`.

### `InvalidParameterValue` on the allocation strategy

`CreateFleet` wants kebab-case (`price-capacity-optimized`). The camelCase
spelling (`priceCapacityOptimized`) belongs to the legacy Spot Fleet API and the
CloudFormation templates. Neither `DryRun` nor a zero-capacity request validates
the enum, so this only surfaces on a real launch.

## Permissions

### `UnauthorizedOperation` / `AccessDenied`

Generate the exact policy from the code rather than transcribing one:

```python
import json

from parsl_aws_provider import GlobusComputeProvider

print(json.dumps(GlobusComputeProvider.minimum_iam_policy(), indent=2))
```

There is no `check_iam_permissions()` helper — older versions of this document
invented one. To find what is actually missing, read the error: AWS names the
action and the resource.

Add the mode-specific statements from [security.md](security.md) —
CloudFormation for detached, Lambda or ECS for serverless, `ssm:*Parameter` or
`s3:*Object` for the state backend.

### Credentials not found

The provider resolves credentials through botocore; it accepts no key arguments.
Use `AWS_PROFILE`, `~/.aws/credentials`, an instance profile, or
`profile_name="myprofile"`. `CredentialResolutionError` carries the reason —
before v0.7.0 that message was swallowed by a `TypeError`.

### `iam:PassRole` denied

`auto_create_instance_profile=True` creates a role and passes it to EC2. Grant
`iam:PassRole` scoped with `iam:PassedToService: ec2.amazonaws.com`, or supply
`iam_instance_profile_arn` for a role you manage.

## State persistence

### `s3_bucket is required when using 's3' state store`

Pass `s3_bucket`. The provider does not create the bucket; create it yourself and
set default encryption on it.

### State is lost or two runs fight over resources

Use **one state location per workflow**. Two providers pointed at the same
`state_file_path`, `s3_key`, or `parameter_store_path` adopt each other's
`provider_id` and then compete over the same instances. Conversely, that adoption
is exactly how deliberate reconnection works.

### Restart re-bakes the AMI, or loses the job map

Fixed in v0.7.0 (#78). The provider and the mode used to overwrite each other's
state document — the provider wrote `provider_id`/`job_map`, the mode wrote the
baked-AMI and warm-pool fields, and each full-overwrite destroyed the other's
keys. State is now namespaced by key; flat v0.6.0 documents are still read.

If you have a stale `ephemeral_aws_state.json` from an older version, deleting it
is safe once you have confirmed the resources it names are gone:

```bash
python tools/cleanup_aws_resources.py --dry-run --region us-east-1
```

### `StateStoreError` on the file backend

Check directory permissions and that the path is not on a filesystem without
`flock` support — the file store serializes with `fcntl.flock`.

## Serverless mode

### Lambda tasks fail with an import error

`worker_init` has no effect on Lambda; there is no instance to run it on.
Dependencies must be in the deployment package or a layer.

### Lambda tasks time out

Raise `timeout` (seconds). Lambda's own ceiling is 900 s. `memory_size` also
governs CPU — a task that is slow rather than blocked often just needs more
memory.

### Fargate tasks exit immediately

Read the task's `stoppedReason` and its CloudWatch Logs.

If the command fails on a missing import, the default image (`python:3.12-slim`)
carries the standard library only — set `ecs_container_image` to one with your
dependencies. Before v0.8.0 the default was `public.ecr.aws/lambda/python:3.9`,
a *Lambda* base image whose entrypoint is the runtime interface emulator, so it
expected an invocation event rather than the task's command; if you pinned that
image deliberately, that is why tasks exit at once (#136).

Also check that `ecs_task_cpu` and `ecs_task_memory` are a combination Fargate
accepts — an invalid pair fails the task definition, not the task.

### Spot in serverless mode

Supported, in two different forms, and `compute_type` decides which:

- `use_spot=True` with `compute_type="ecs"` sets the cluster's capacity provider to
  **`FARGATE_SPOT`**. Still serverless — no instances of yours.
- `use_spot_fleet=True` with `compute_type="ecs"` is something else entirely: it
  bypasses ECS and launches an **`instant` EC2 Fleet** per job, so you are back to
  managing instances. `instance_types` and `spot_max_price_percentage` apply.
- `compute_type="lambda"` ignores both. Lambda has no spot pricing.

## Detached mode

### The bastion never comes up

Read the CloudFormation stack events:

```bash
aws cloudformation describe-stack-events --stack-name parsl-bastion-<id> --region us-east-1
```

Then SSM to the bastion and read its journal. The bastion needs outbound access
for the SSM agent to register — a private subnet requires a NAT gateway or the
`ssm`/`ssmmessages`/`ec2messages` VPC endpoints.

### The bastion is still running after the workflow finished

That is deliberate: the bastion is preserved so you can reconnect. Call
`provider.shutdown()` when the workflow is genuinely over, or pass
`preserve_bastion=False` so shutdown terminates it. Its own idle-shutdown timer
is `idle_timeout` (minutes, default 30); `max_idle_time` is unrelated — that
governs when the *provider* reclaims a long-`RUNNING` resource.

## Cost

### Resources are still running after the process exited

**There is no `atexit` hook.** Nothing cleans up when the interpreter exits, and
`parsl.clear()` releases Parsl's resources, not AWS ones. Call
`provider.shutdown()`, and prefer a `try`/`finally`.

To find orphans from a crash:

```bash
python tools/cleanup_aws_resources.py --dry-run --region us-east-1   # then without --dry-run
```

It sweeps by the `ParslResource=true` tag, so it finds resources the state file no
longer names.

### The bill is higher than expected

The provider has no cost monitoring — `max_cost_per_hour` and
`enable_cost_monitoring` do not exist. What it does have:

- `min_blocks=0` so nothing runs when nothing is queued
- `max_blocks` as a hard ceiling
- `auto_shutdown=True` with `max_idle_time` (seconds) to reclaim idle resources
- `use_spot=True`, optionally with `spot_max_price_percentage`

Set AWS Budgets and a CloudWatch billing alarm; tag with `additional_tags` for
cost allocation.

Two things to know specifically:

- **Warm-pool instances are held `Running`** and bill at the full rate for up to
  `warm_pool_ttl` seconds per idle period. Native ASG warm pools, which hold
  instances `Stopped`, are
  [#130](https://github.com/scttfrdmn/parsl-aws-provider/issues/130).
- **A role created by `auto_create_instance_profile=True` is not deleted** on
  shutdown ([#132](https://github.com/scttfrdmn/parsl-aws-provider/issues/132)).
  It costs nothing, but it accumulates.

### Stopped instances with billed EBS volumes

Fixed in v0.7.0. Standard mode never set
`InstanceInitiatedShutdownBehavior`, so EC2's `stop` default applied to the
`shutdown -h now` that one-shot mode appends — leaving a stopped instance and a
billed volume that the provider had already dropped from tracking, because
`stopped` maps to `COMPLETED`. All launch paths now set `terminate`. Sweep for
pre-v0.7.0 leftovers with the cleanup tool.

## Slow startup

The default `worker_init` installs Python 3.11 and Parsl on every launch, which
dominates a short workflow's wall clock. Options, in increasing effectiveness:

1. **Bake an AMI** — standard mode, `bake_ami=True`. Runs `worker_init` once into
   a custom image at construction; every later launch skips it. Pass
   `baked_ami_id="ami-..."` to reuse the image across runs.
2. **Use the warm pool** — `warm_pool_size=N`, standard mode. Finished instances
   are reused instead of relaunched. Read the cost note above.
3. **Bring your own AMI** — `image_id="ami-..."` with everything pre-installed.
4. **Keep `worker_init` minimal.** Install only what the tasks import.

## AWS API throttling

`RequestLimitExceeded`, or botocore logging "max retries" — the provider wraps AWS
calls in exponential backoff with jitter, so occasional throttling is absorbed.
If it is persistent, raise `status_polling_interval` (default 60 s) and
`waiter_delay` (default 5 s), and lower `max_blocks`.

## Getting help

Open an issue at
[github.com/scttfrdmn/parsl-aws-provider/issues](https://github.com/scttfrdmn/parsl-aws-provider/issues)
with:

- What you ran, including the full provider configuration with IDs redacted
- The full traceback
- The provider version (`python -c "import parsl_aws_provider; print(parsl_aws_provider.__version__)"`)
  and the Parsl version
- `/var/log/cloud-init-output.log` from a worker, if the workers launched

For a suspected vulnerability, open a
[security advisory](https://github.com/scttfrdmn/parsl-aws-provider/security/advisories/new)
instead.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors

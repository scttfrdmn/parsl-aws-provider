# Operating Modes

Three modes, differing in who owns the worker lifecycle and where the work runs.
Select one with the `mode` string; every other option is a flat keyword argument
on `EphemeralAWSProvider`.

```python
provider = EphemeralAWSProvider(
    mode="standard",  # or "detached", "serverless"
    # ... the rest of the options, all flat keyword arguments
)
```

`mode` is a **string**, not a mode object. Passing `StandardMode(...)` raises
`TypeCheckError` — the provider builds the mode itself so it can wire in the
session, state store, and resolved AMI.

All three need `vpc_id`, `subnet_id`, and `security_group_id`
([network-prerequisites.md](network-prerequisites.md)). The one exception is
serverless mode with `compute_type="lambda"`, which runs in the Lambda-managed
VPC and requires none of them.

Unknown keyword arguments raise `ProviderConfigurationError` rather than being
absorbed, so a stale option from an older tutorial fails at construction instead
of being silently ignored.

## Standard Mode

The client talks directly to workers. The simplest mode, and the one to use for
development.

### Key features

- Direct client-to-worker communication over ZMQ
- No intermediary resources beyond a launch template
- Lowest latency for task dispatch and result return
- Supports the warm pool, AMI baking, and one-shot dispatch
- Requires the client to stay reachable for the workflow's duration

### Configuration

```python
from parsl_aws_provider import EphemeralAWSProvider

provider = EphemeralAWSProvider(
    mode="standard",
    region="us-west-2",
    vpc_id="vpc-0123456789abcdef0",
    subnet_id="subnet-0123456789abcdef0",
    security_group_id="sg-0123456789abcdef0",
    instance_type="t3.medium",
    min_blocks=0,
    max_blocks=4,
    use_public_ips=True,     # False if reaching the subnet over VPN/Direct Connect
    key_name="your-key-pair",  # optional; SSM Session Manager needs no key
    use_spot=True,
)
```

`image_id` is optional: an Amazon Linux 2023 AMI matching the instance type's
architecture is resolved from AWS's public SSM parameters, so x86_64 and arm64
(Graviton) instance types both work without a lookup table.

### The client must accept inbound connections

Workers connect *outbound* to the Parsl interchange, so the client has to accept
inbound TCP on the HTEX port range (54000–55000 by default). A laptop behind
home or office NAT cannot do this without port forwarding or a VPN — use detached
mode instead. Set `encrypted=False` on `HighThroughputExecutor` for now; CurveZMQ
certificate distribution is [#62](https://github.com/scttfrdmn/parsl-aws-provider/issues/62).

### When to use

- Development and testing
- Workflows that finish within hours
- A client with a stable, reachable address (typically an EC2 instance in the
  same VPC)
- Chatty workflows where dispatch latency matters

### Diagram

```
┌─────────────┐     ┌─────────────┐
│             │     │             │
│    Client   │◄────►   Worker 1  │
│  (Your PC)  │     │  (EC2/Spot) │
│             │     │             │
└──────┬──────┘     └─────────────┘
       │
       │            ┌─────────────┐
       │            │             │
       └───────────►│   Worker 2  │
                    │  (EC2/Spot) │
                    │             │
                    └─────────────┘
```

### Standard-mode-only options

These are implemented by `StandardMode` alone, and the provider raises
`ProviderConfigurationError` if you set them on another mode:

| Option | Default | Effect |
|--------|---------|--------|
| `warm_pool_size` | `0` | Keep up to N finished instances alive for reuse. `0` disables. |
| `warm_pool_ttl` | `120` | Seconds a warm instance is held before termination. |
| `bake_ami` | `False` | Run `worker_init` once into a custom AMI at `initialize()`. |
| `baked_ami_id` | `None` | Use an already-baked AMI instead of baking one. |
| `one_shot` | `False` | Dispatch a single command per instance over SSM; no HTEX. |

`warm_pool_size > 0` and `one_shot=True` both dispatch over SSM, so each requires
either `auto_create_instance_profile=True` or an explicit
`iam_instance_profile_arn` — SSM `SendCommand` needs the instance to carry
`AmazonSSMManagedInstanceCore`.

Warm instances are held **Running** and bill at the full instance rate for up to
`warm_pool_ttl` seconds per idle period, which is why `warm_pool_size` is capped.
Migrating to native ASG warm pools, which hold instances Stopped or Hibernated,
is [#130](https://github.com/scttfrdmn/parsl-aws-provider/issues/130).

## Detached Mode

A bastion instance runs an orchestrator loop and owns the worker lifecycle, so
the client can disconnect entirely and reconnect later.

### Key features

- Bastion coordinates workers; the client is not in the data path
- Workflows survive client disconnection, reboot, and unreliable connectivity
- Suits long-running and overnight work
- State is persisted where both the client and the bastion can read it
- The bastion shuts itself down after an idle period

### Configuration

```python
provider = EphemeralAWSProvider(
    mode="detached",
    region="us-west-2",
    vpc_id="vpc-0123456789abcdef0",
    subnet_id="subnet-0123456789abcdef0",
    security_group_id="sg-0123456789abcdef0",
    instance_type="t3.medium",
    bastion_instance_type="t3.micro",
    min_blocks=0,
    max_blocks=10,
    state_store_type="parameter_store",   # readable by client and bastion alike
    parameter_store_path="/parsl/my-workflow-state",
)
```

To reconnect to a running workflow, construct a provider against the **same
state location**. The persisted `provider_id` is adopted automatically, along
with the bastion and the tracked jobs — you do not need to pass an ID back in.

### Detached-mode options

| Option | Default | What it controls |
|---|---|---|
| `idle_timeout` | `30` | Minutes of inactivity before the bastion shuts itself down |
| `preserve_bastion` | `True` | Whether the bastion survives `cleanup_infrastructure()` |
| `bastion_host_type` | `"cloudformation"` | Stack-managed bastion, or `"direct"` for a plain `RunInstances` |
| `workflow_id` | generated UUID | Identifier used in bastion state paths and tags; pass the same value to reconnect |

`preserve_bastion` defaults to `True`, so **the bastion keeps running and keeps
billing after shutdown** — that is what makes a later session able to adopt it.
Pass `preserve_bastion=False` to have it torn down instead.

These are accepted only on `mode="detached"`; setting one on another mode raises
`ProviderConfigurationError`, because the provider forwards them from the
detached branch only and they would otherwise appear configured while having no
effect.

Note `idle_timeout` governs only the bastion. It is unrelated to the provider's
`max_idle_time`, which is deprecated and ignored
([#194](https://github.com/scttfrdmn/parsl-aws-provider/issues/194)) — to reclaim
idle workers, set `max_idletime` on your Parsl `Config`.

### When to use

- Long-running workflows (hours to days)
- A client that will disconnect or is behind NAT
- Overnight or weekend runs
- Workflows that must survive a client reboot

### Diagram

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│             │     │             │     │             │
│    Client   │◄────►   Bastion   │◄────►   Worker 1  │
│  (Your PC)  │     │    Host     │     │  (EC2/Spot) │
│             │     │    (EC2)    │     │             │
└─────────────┘     └──────┬──────┘     └─────────────┘
                           │
                           │            ┌─────────────┐
                           │            │             │
                           └───────────►│   Worker 2  │
                                        │  (EC2/Spot) │
                                        │             │
                                        └─────────────┘
```

The bastion is an autonomous orchestrator with its own polling loop, not a
network tunnel — which is why an EC2 Instance Connect Endpoint cannot replace it
([#88](https://github.com/scttfrdmn/parsl-aws-provider/issues/88)).

## Serverless Mode

Tasks run on Lambda or ECS/Fargate. No EC2 instances.

### Key features

- No instances to manage
- Scales from zero to many concurrent tasks in seconds
- Billed per invocation rather than per instance-hour
- Lambda for short tasks (15-minute ceiling, up to 10 GB memory)
- Fargate for longer tasks needing more CPU or memory
- `compute_type="auto"` is not accepted by the provider; pick `lambda` or `ecs`

### Configuration

```python
# Lambda: no network IDs needed at all
provider = EphemeralAWSProvider(
    mode="serverless",
    region="us-west-2",
    compute_type="lambda",
    memory_size=1024,    # MB
    timeout=300,         # seconds; Lambda's own ceiling is 900
    min_blocks=0,
    max_blocks=100,
)
```

```python
# ECS/Fargate: subnet and security group are mandatory for awsvpcConfiguration
provider = EphemeralAWSProvider(
    mode="serverless",
    region="us-west-2",
    compute_type="ecs",
    vpc_id="vpc-0123456789abcdef0",
    subnet_id="subnet-0123456789abcdef0",
    security_group_id="sg-0123456789abcdef0",
    min_blocks=0,
    max_blocks=100,
)
```

`compute_type` is the provider-facing name for what `ServerlessMode` calls
`worker_type`. `compute_type="ec2"` (the default) has no meaning here and leaves
the mode on its own default of `auto`, which selects Lambda for short single-task
commands and ECS otherwise.

### Serverless-mode options

| Option | Default | What it controls |
|---|---|---|
| `memory_size` | `1024` | Lambda memory in MB (the provider's alias for `lambda_memory`) |
| `timeout` | `300` | Lambda timeout in seconds (alias for `lambda_timeout`) |
| `lambda_runtime` | `"python3.12"` | Lambda runtime identifier |
| `ecs_task_cpu` | `1024` | Fargate CPU units per task (1024 = 1 vCPU) |
| `ecs_task_memory` | `2048` | Fargate memory per task in MB |
| `ecs_container_image` | `"python:3.12-slim"` | Container image for Fargate tasks |

Set `ecs_container_image` to your own image to run a workload with its own
dependencies — that is usually the reason to choose Fargate over Lambda. The
stock default gives you the standard library only:

```python
provider = EphemeralAWSProvider(
    mode="serverless",
    compute_type="ecs",
    ecs_container_image="123456789012.dkr.ecr.us-east-1.amazonaws.com/my-worker:latest",
    ecs_task_cpu=2048,
    ecs_task_memory=4096,
    vpc_id="vpc-...", subnet_id="subnet-...", security_group_id="sg-...",
)
```

`ecs_task_cpu` and `ecs_task_memory` must be a combination Fargate accepts; see
[Fargate task
sizes](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/AWS_Fargate.html).
`lambda_runtime` must be one of the `Runtime` values allowed by
`templates/cloudformation/lambda_worker.yml`, or CloudFormation rejects the
stack.

These are accepted only on `mode="serverless"`; setting one on another mode
raises `ProviderConfigurationError`.

### When to use

- Highly parallel, short-duration tasks
- Sporadic or event-driven workloads
- Intermittent usage where paying for idle instances is the dominant cost
- Unpredictable scaling requirements
- Tasks with few dependencies

### Diagram

#### Lambda
```
┌─────────────┐     ┌─────────────┐
│             │     │  Lambda     │
│    Client   │◄────►  Function   │
│  (Your PC)  │     │  Invocation │
│             │     │             │
└──────┬──────┘     └─────────────┘
       │                   ▲
       │                   │
       │            ┌──────┴──────┐
       │            │  Lambda     │
       └───────────►│  Function   │
                    │  Invocation │
                    │             │
                    └─────────────┘
```

#### Fargate
```
┌─────────────┐     ┌─────────────┐
│             │     │ ECS         │
│    Client   │◄────► Task        │
│  (Your PC)  │     │ (Fargate)   │
│             │     │             │
└──────┬──────┘     └─────────────┘
       │                   ▲
       │                   │
       │            ┌──────┴──────┐
       │            │ ECS         │
       └───────────►│ Task        │
                    │ (Fargate)   │
                    │             │
                    └─────────────┘
```

## Mode selection guide

| Consideration | Standard | Detached | Serverless |
|---------------|----------|----------|------------|
| **Client connectivity** | Must stay reachable | Can disconnect | Can disconnect |
| **Client behind NAT** | No | Yes | Yes |
| **Workflow duration** | Minutes to hours | Hours to days | Seconds to hours |
| **Task duration** | Any | Any | Lambda: <15 min<br>Fargate: any |
| **Scaling** | Moderate | Moderate | Rapid, massive |
| **Startup time** | Minutes | Minutes | Seconds |
| **Cost model** | Per EC2 second | Per EC2 second (+ bastion) | Per invocation |
| **Network IDs required** | Yes | Yes | Lambda: no<br>ECS: yes |
| **Spot support** | Yes | Yes | No |
| **Recovery from client failure** | None | Full | Full |
| **Complexity** | Lowest | Medium | Highest |

## Best practices

### Standard mode
- Use `use_spot=True` for cost savings; add `spot_interruption_handling=True` to
  get the two-minute EventBridge warning
- Set `min_blocks`/`max_blocks` deliberately — `max_blocks` also caps concurrent
  submissions, and a job past that limit raises rather than queueing
- Run the client on an EC2 instance in the same VPC; a NAT'd laptop will not work
- Leave `use_public_ips=True` unless you have a VPN or Direct Connect path

### Detached mode
- Use `state_store_type="parameter_store"` so the bastion and client share state
- Keep one state location per workflow; two providers sharing one will adopt each
  other's `provider_id` and fight over the same resources
- Size `bastion_instance_type` for the orchestrator loop, not for compute — the
  default `t3.micro` is adequate for tens of workers
- The bastion is preserved by default; call `provider.shutdown()` to remove it

### Serverless mode
- Set `compute_type` explicitly rather than relying on the `auto` heuristic,
  which decides on command length and `tasks_per_node`
- For Lambda, keep tasks short and dependencies minimal
- Size `memory_size` first: Lambda CPU scales with memory
- For ECS, set `ecs_container_image` to an image that already has your
  dependencies rather than installing them per task

## Switching between modes

Only the `mode` string and the mode-specific options change; the rest of the
configuration carries over.

1. **Standard → detached** — add `state_store_type="parameter_store"` and a
   `parameter_store_path`; drop any standard-only options
2. **Standard/detached → serverless** — expect to break long tasks up, package
   dependencies for Lambda or a container, and drop the spot options
3. **Serverless → standard/detached** — usually works with minimal changes

## Debugging tips

### Standard mode
- Reach instances with SSM Session Manager
  (`aws ssm start-session --target i-...`); no key pair or open port needed
- Bootstrap output is in `/var/log/cloud-init-output.log` on the instance
- Worker stdout/stderr is in Parsl's `runinfo/` directory on the client
- If workers launch but never register, the client is almost certainly not
  accepting inbound connections on the interchange ports

### Detached mode
- SSM to the bastion and read the orchestrator's journal
- The state document holds the job map: read the SSM parameter directly
- CloudFormation stack events explain a bastion that never came up

### Serverless mode
- CloudWatch Logs for Lambda functions and ECS tasks
- CloudWatch Logs Insights for filtering across invocations
- ECS task `stoppedReason` explains a task that exits immediately
- CloudFormation stack events for deployment failures

### Any mode
- `provider.list_resources()` reports what the provider believes it owns
- `python tools/cleanup_aws_resources.py --dry-run --region <region>` finds
  resources tagged `ParslResource=true` that the state no longer names

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors

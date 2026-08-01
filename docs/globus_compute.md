# Globus Compute integration

Run Python functions on ephemeral AWS EC2 instances through
[Globus Compute](https://globus-compute.readthedocs.io/), from any network
environment — including behind corporate firewalls, university networks, and home
routers.

```{warning}
Read [Known limitations](#known-limitations) before deploying. The generated
`config.yaml` currently drops `worker_init` and hardcodes `encrypted: true`, and
both defects prevent workers from starting
([#138](https://github.com/scttfrdmn/parsl-aws-provider/issues/138)). Two manual
edits work around them, and are given below.
```

## Why it is worth the trouble

Standard mode needs the client to accept **inbound** TCP on the interchange
ports, which a NAT'd laptop cannot. Globus Compute inverts that: the endpoint
runs somewhere reachable, and both your client and the EC2 workers connect
*outward*.

```
Your machine (any network)        Globus Compute service        AWS EC2
┌──────────────────────┐          ┌──────────────────┐      ┌──────────────────┐
│ globus_compute_sdk   │          │ Globus Compute   │      │ GlobusCompute-   │
│ Executor.submit(fn)  │◄──AMQP──►│ message router   │◄────►│ Engine worker    │
│                      │   :443   │ (globus.org)     │ ZMQ  │ + Parsl HTEX     │
└──────────────────────┘          └──────────────────┘      └──────────────────┘
                                            ▲
                                   globus-compute-endpoint
                                   daemon + GlobusComputeProvider
                                   (launches and terminates EC2)
```

The endpoint daemon holds the provider, so **the daemon host is what workers
connect back to** — the same reachability requirement standard mode has, just
moved. Run the endpoint on an EC2 instance in the same VPC as the workers, or in
a subnet they can route to. Running it on a NAT'd laptop does not work, which is
the single most common misreading of this diagram.

What Globus Compute does buy you: your *client* can be anywhere, functions are
addressed by endpoint UUID rather than network location, and authentication is
Globus identity rather than AWS credentials on the client machine.

## Prerequisites

**1. Pre-provisioned network.** Since v0.7.0 `vpc_id`, `subnet_id`, and
`security_group_id` are required — the provider never creates them. See
[network-prerequisites.md](network-prerequisites.md). Every example below passes
all three; omitting them raises at construction:

```
ValueError: vpc_id, subnet_id, and security_group_id are required.
Pre-provision network resources outside the provider.
```

**2. AWS credentials** for the endpoint daemon host. Generate the minimum IAM
policy from the code rather than transcribing one:

```python
import json

from parsl_ephemeral_aws import GlobusComputeProvider

print(json.dumps(GlobusComputeProvider.minimum_iam_policy(), indent=2))
print(json.dumps(GlobusComputeProvider.minimum_iam_policy(include_ecr=True), indent=2))
```

It grants no network-creation actions, which is deliberate — see
[security.md](security.md).

**3. The `globus` extra.**

```bash
uv sync --extra globus
```

`globus-compute-endpoint` requires Python ≥ 3.10, which this project already
does. It pins `parsl` exactly, so the floor is `>=4.10.1` — the first release
pinning a `parsl` version compatible with this package.

**4. Globus authentication**, once, on the daemon host:

```bash
uv run globus-compute-endpoint login
```

Tokens cache in `~/.globus_compute/storage.db` and refresh automatically.

## Quick start

### 1. Generate the config

```python
from parsl_ephemeral_aws import GlobusComputeProvider

provider = GlobusComputeProvider(
    region="us-east-1",
    vpc_id="vpc-0123456789abcdef0",
    subnet_id="subnet-0123456789abcdef0",
    security_group_id="sg-0123456789abcdef0",
    instance_type="t3.medium",
    mode="standard",
    auto_create_instance_profile=True,
    min_blocks=0,
    max_blocks=4,
    display_name="My Ephemeral AWS Endpoint",
)

provider.generate_endpoint_config("~/.globus_compute/my_aws_endpoint")
```

This writes **two** files:

| File | Purpose |
|---|---|
| `config.yaml` | the configuration — the file to edit |
| `config.py` | a loader shim; do not edit |

Both are needed. Globus Compute resolves a provider's `type:` key by
`getattr(parsl.providers, type_name, None)` and raises when that is `None`, so a
class Parsl does not ship is unreachable — and `getattr` cannot walk a dotted
path, so `type: parsl_ephemeral_aws.globus_compute.GlobusComputeProvider` can
never resolve either. Importing `parsl_ephemeral_aws` assigns the class onto
`parsl.providers`, but the endpoint daemon has no reason to import this package.
The `config.py` shim does that import and then hands `config.yaml` to Globus
Compute's own loader; `get_config()` prefers `config.py` when both are present,
which is what makes the pair work
([#87](https://github.com/scttfrdmn/parsl-aws-provider/issues/87)).

### 2. Apply the two required edits

Open `config.yaml` and make both changes. Without them the endpoint starts, EC2
instances launch, and no worker ever registers
([#138](https://github.com/scttfrdmn/parsl-aws-provider/issues/138)).

```yaml
engine:
  type: GlobusComputeEngine
  encrypted: false          # ← change from true; see below
  provider:
    type: GlobusComputeProvider
    region: us-east-1
    # ... generated keys ...
    # ← add this, replacing the default worker_init entirely:
    worker_init: "dnf install -y python3.11 python3.11-pip\nln -sf /usr/bin/python3.11 /usr/bin/python3\npip3.11 install --quiet --upgrade globus-compute-endpoint\n"
```

**Why `worker_init`.** A Globus Compute worker is not launched with Parsl's
`process_worker_pool.py`. The engine rewrites the command to:

```
globus-compute-endpoint python-exec parsl.executors.high_throughput.process_worker_pool -a ...
```

so `globus-compute-endpoint` must be on the worker's `PATH`. The provider's
default `worker_init` installs `parsl` and not `globus-compute-endpoint`, and the
generator drops any value you passed to the constructor — so the reconstructed
provider always gets the default. The worker command is then `command not found`.

**Why `encrypted: false`.** `GlobusComputeEngine` forwards `encrypted` to the
wrapped `HighThroughputExecutor`, which generates CurveZMQ certificates in its
`run_dir` **on the endpoint host** and passes that path to workers as
`--cert_dir`. An EC2 worker has no such path and dies with `FileNotFoundError`
before registering. Same-VPC deployments rely on VPC isolation instead;
certificate distribution is
[#62](https://github.com/scttfrdmn/parsl-aws-provider/issues/62).

```{note}
High-Assurance endpoints reject `encrypted: false`
(`GlobusComputeEngine.assert_ha_compliant()`), so they need #62 resolved rather
than this workaround.
```

Anything else the generator dropped goes in the same place — `additional_tags`,
`state_store_type`, `use_spot_fleet`, `warm_pool_size`, `image_id`, and 32 more.
A hand-edited `config.yaml` accepts every `EphemeralAWSProvider` option; only the
*generator* is selective.

### 3. Start the endpoint

```bash
uv run globus-compute-endpoint start my_aws_endpoint
```

It registers and prints its UUID:

```
Starting endpoint; registered endpoint ID: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

### 4. Submit from anywhere

```python
from globus_compute_sdk import Executor

ENDPOINT_ID = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"


def square(x):
    return x * x


with Executor(endpoint_id=ENDPOINT_ID) as ex:
    print(ex.submit(square, 7).result())   # → 49
```

### 5. Stop it

```bash
uv run globus-compute-endpoint stop my_aws_endpoint
```

This drains pending functions and scales the provider's blocks in. It does not
call `provider.shutdown()`, so sweep for leftovers if the daemon died uncleanly:

```bash
uv run python tools/cleanup_aws_resources.py --dry-run --region us-east-1
```

## Configuration reference

`GlobusComputeProvider` accepts every `EphemeralAWSProvider` parameter plus three
of its own:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `endpoint_id` | `str \| None` | `None` | Endpoint UUID. Emitted into `config.yaml` as a provider key and a trailing comment. |
| `container_image` | `str \| None` | `None` | Image URI. Sets `container_type: docker` and `container_uri` under `engine`. |
| `display_name` | `str` | `"Ephemeral AWS Endpoint"` | Label shown in the Globus Compute web console. |

Parameters that reach `config.yaml` today: `region`, `instance_type`, `mode`,
`vpc_id`, `subnet_id`, `security_group_id`, `min_blocks`, `max_blocks`,
`use_spot`, `spot_interruption_handling`, `auto_create_instance_profile`,
`iam_instance_profile_arn`, `container_image`, `status_polling_interval`,
`waiter_delay`, `waiter_max_attempts`, and `endpoint_id`. Everything else must be
added to the YAML by hand until #138 lands.

Recommended values for a Globus Compute deployment:

| Parameter | Suggested | Why |
|---|---|---|
| `mode` | `"standard"` | Detached mode's bastion duplicates what the endpoint daemon already does |
| `min_blocks` | `0` | Scale to zero when idle |
| `max_blocks` | `4`–`20` | Hard ceiling on concurrent instances |
| `use_spot` | `True` | Large saving for fault-tolerant functions; set `max_retries_on_system_failure` on the engine |
| `auto_create_instance_profile` | `True` | Creates a role with `AmazonSSMManagedInstanceCore` |
| `auto_shutdown` + `max_idle_time` | `True`, `300` | Reclaim idle instances |
| `bake_ami` | `True` | Runs `worker_init` once into an AMI instead of on every launch |

`mode="serverless"` is not useful here — Lambda and Fargate cannot run the
long-lived `globus-compute-endpoint` worker process the engine expects.

## Examples

Each of these still needs the two edits from step 2.

### Spot endpoint

```python
from parsl_ephemeral_aws import GlobusComputeProvider

provider = GlobusComputeProvider(
    region="us-east-1",
    vpc_id="vpc-0123456789abcdef0",
    subnet_id="subnet-0123456789abcdef0",
    security_group_id="sg-0123456789abcdef0",
    instance_type="c5.large",
    mode="standard",
    use_spot=True,
    spot_interruption_handling=True,
    checkpoint_bucket="my-parsl-checkpoints",  # gates detection, see below
    min_blocks=0,
    max_blocks=10,
    auto_create_instance_profile=True,
    display_name="Spot AWS Endpoint",
)

provider.generate_endpoint_config("~/.globus_compute/spot_aws")
```

What actually protects your work is a retry policy on the engine, not the
interruption handler:

```yaml
engine:
  type: GlobusComputeEngine
  encrypted: false
  max_retries_on_system_failure: 3    # this is what re-runs reclaimed functions
```

Two limitations, both
[#137](https://github.com/scttfrdmn/parsl-aws-provider/issues/137):
`checkpoint_bucket` is an accidental prerequisite for the interruption monitor
being constructed *at all* — without it you get one WARNING at startup and no
detection — and what the warning triggers today is a log line, not task
recovery. Treat it as observability.

`checkpoint_bucket` is also one of the dropped parameters (#138), so add it to
the YAML by hand.

### Container endpoint

```python
provider = GlobusComputeProvider(
    region="us-west-2",
    vpc_id="vpc-0123456789abcdef0",
    subnet_id="subnet-0123456789abcdef0",
    security_group_id="sg-0123456789abcdef0",
    instance_type="t3.large",
    mode="standard",
    container_image="python:3.11-slim",
    min_blocks=0,
    max_blocks=5,
    auto_create_instance_profile=True,
    display_name="Python 3.11 Container Endpoint",
)

provider.generate_endpoint_config("~/.globus_compute/python311_aws")
```

`container_type: docker` means the engine wraps the worker command in
`docker run`, so `worker_init` must install and start Docker *and* the image must
contain `globus-compute-endpoint`:

```yaml
    worker_init: "dnf install -y docker\nsystemctl start docker\n"
```

`python:3.11-slim` does not contain it, so build your own image or pass one that
does. For a private ECR image, add the permissions from
`minimum_iam_policy(include_ecr=True)`.

### One endpoint per region

```python
NETWORK = {
    "us-east-1": ("vpc-0aaa", "subnet-0aaa", "sg-0aaa"),
    "eu-west-1": ("vpc-0bbb", "subnet-0bbb", "sg-0bbb"),
}

for region, name in [("us-east-1", "aws-us-east"), ("eu-west-1", "aws-eu-west")]:
    vpc_id, subnet_id, security_group_id = NETWORK[region]
    GlobusComputeProvider(
        region=region,
        vpc_id=vpc_id,
        subnet_id=subnet_id,
        security_group_id=security_group_id,
        instance_type="c5.xlarge",
        mode="standard",
        auto_create_instance_profile=True,
        display_name=f"AWS {region}",
    ).generate_endpoint_config(f"~/.globus_compute/{name}")
```

Network IDs are per-region — a VPC in `us-east-1` does not exist in `eu-west-1`,
so the loop needs a mapping rather than one shared set of IDs. Then route
functions to the nearest endpoint:

```python
from globus_compute_sdk import Executor

ENDPOINTS = {"us-east-1": "uuid-for-us-east", "eu-west-1": "uuid-for-eu-west"}

with Executor(endpoint_id=ENDPOINTS["us-east-1"]) as ex:
    print(ex.submit(my_function, data).result())
```

Each endpoint needs its own daemon process and its own `globus-compute-endpoint
start`.

## Known limitations

- **`worker_init` is dropped and `encrypted: true` is hardcoded** in the
  generated config; both prevent workers from registering, and both need the
  manual edits from step 2
  ([#138](https://github.com/scttfrdmn/parsl-aws-provider/issues/138)).
- **37 of 52 provider parameters are dropped** by the generator, including
  `additional_tags`, the state-backend selection, and every fleet, warm-pool, and
  AMI-baking option (#138). Add them to `config.yaml` by hand.
- **Multi-user (manager) endpoints are unsupported**
  ([#133](https://github.com/scttfrdmn/parsl-aws-provider/issues/133)). Those
  render `user_config_template.yaml.j2` to a string and the forked user-endpoint
  process calls `load_config_yaml()` on it directly, never going through
  `get_config()` — so there is no `config.py` hook and the "not a valid provider"
  failure returns. Fixing it needs dotted-path support upstream.
- **The endpoint daemon must be reachable by workers.** Globus Compute removes
  the reachability requirement from your *client*, not from the daemon host.
- **`GlobusComputeExecutor` now ships inside Parsl** and talks to a Globus
  Compute endpoint directly, bypassing providers. If you want to submit *to* an
  existing endpoint, use that instead; this class is for standing an endpoint
  *up* on ephemeral AWS.

## Testing

```bash
uv sync --extra globus
uv run globus-compute-endpoint login
export GLOBUS_COMPUTE_ENDPOINT_ID="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

AWS_PROFILE=aws uv run pytest tests/aws/test_globus_compute_e2e.py \
    -m "aws and globus" --no-cov -v
```

Config-generation tests need no running endpoint or AWS resources:

```bash
uv run pytest tests/unit/test_globus_compute_provider.py --no-cov -v
```

## Troubleshooting

### The endpoint starts, instances launch, no worker registers

The two step-2 edits. In order of likelihood:

1. **`globus-compute-endpoint` is not installed on the worker.** Reach the
   instance with `aws ssm start-session --target i-...` and read
   `/var/log/cloud-init-output.log`; then run `which globus-compute-endpoint`.
2. **`encrypted: true` is still set.** The worker dies with `FileNotFoundError`
   on the `--cert_dir` path before it can log anything useful.
3. **The daemon host is not reachable from the subnet.** Workers connect back to
   it over ZMQ. Check the daemon host's own security group, not the workers'.

### `'GlobusComputeProvider' is not a valid provider`

`config.py` is missing or was deleted. Regenerate with
`generate_endpoint_config()`, which writes both files. If you are using a
multi-user endpoint, this is #133 and has no workaround yet.

### `globus-compute-endpoint start` hangs

It is waiting for a worker. Beyond the above:

- **IAM**: the instance needs `AmazonSSMManagedInstanceCore` — set
  `auto_create_instance_profile=True` or pass `iam_instance_profile_arn`.
- **Egress**: a private subnet needs a NAT gateway or the `ssm`, `ssmmessages`,
  and `ec2messages` VPC endpoints, plus a route to whatever package index
  `worker_init` uses.
- **AMI**: resolved from SSM for the region and architecture. A custom `image_id`
  must exist in the target region — and note `image_id` is a dropped parameter
  (#138), so confirm it actually reached the YAML.

### `ResourceNotFoundException` on start

The UUID in `~/.globus_compute/<name>/` no longer exists in the service. Delete
the directory and re-register:

```bash
rm -rf ~/.globus_compute/my_aws_endpoint
uv run globus-compute-endpoint configure my_aws_endpoint
```

Regenerate the config afterwards — `configure` writes a default one.

### `TaskExecutionFailed`

The worker ran your function and it raised. Read
`~/.globus_compute/my_aws_endpoint/endpoint.log`.

### Instances are still running after the endpoint stopped

`globus-compute-endpoint stop` scales blocks in but does not call
`provider.shutdown()`, and there is no `atexit` hook. Sweep by tag:

```bash
uv run python tools/cleanup_aws_resources.py --region us-east-1   # --dry-run first
```

### SSM reports the instance as unreachable

```bash
aws ssm describe-instance-information --profile aws
```

The instance needs `AmazonSSMManagedInstanceCore` via an instance profile,
outbound 443 to `ssm.<region>.amazonaws.com`, and IMDS enabled — the provider
sets `HttpTokens: required` with `HttpEndpoint: enabled` precisely because the
SSM agent needs it.

## See also

- [network-prerequisites.md](network-prerequisites.md) — the VPC, subnet, and
  security group you must supply
- [security.md](security.md) — IAM policy, instance profiles, encryption
- [troubleshooting.md](troubleshooting.md) — provider-level failures
- [spot_fleet.md](spot_fleet.md) — diversified instance types
- [Globus Compute documentation](https://globus-compute.readthedocs.io/)

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors

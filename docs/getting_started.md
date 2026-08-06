# Getting Started

## Installation

```bash
uv add parsl-ephemeral-provider
```

For unreleased changes, install from the repository instead:

```bash
uv add git+https://github.com/scttfrdmn/parsl-ephemeral-provider
```

Or, working from a clone:

```bash
uv sync --extra dev --extra test
```

Published since v0.9.0
([#180](https://github.com/scttfrdmn/parsl-ephemeral-provider/issues/180)); v0.9.0 is
the first release on PyPI, so there are no earlier versions to pin to.

Python 3.10 or newer is required (Parsl 2026.x dropped 3.9).

## Prerequisites

1. **AWS credentials.** Any source botocore understands: environment variables,
   `~/.aws/credentials`, or an instance profile when running on EC2. Pass
   `profile_name="myprofile"` to select a named profile.

2. **An existing VPC, subnet, and security group.** As of v0.7.0 the provider
   never creates or deletes network resources — you supply `vpc_id`, `subnet_id`,
   and `security_group_id`, and they are validated at construction. See
   [network-prerequisites.md](network-prerequisites.md) for what the security
   group must allow.

3. **A reachable client, or detached mode.** Workers connect *outbound* to the
   Parsl interchange, so in standard mode your client must accept inbound TCP on
   ports 54000–55000. A laptop behind NAT cannot; use `mode="detached"`.

You do **not** need to pick an AMI. Leave `image_id` unset and an Amazon Linux
2023 image matching your instance type's architecture is resolved from AWS's
public SSM parameters — x86_64 and arm64 alike, in every region.

## Basic configuration

```python
import parsl
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
from parsl_ephemeral_provider import EphemeralProvider

provider = EphemeralProvider(
    region="us-east-1",
    vpc_id="vpc-0123456789abcdef0",
    subnet_id="subnet-0123456789abcdef0",
    security_group_id="sg-0123456789abcdef0",
    instance_type="t3.medium",
    init_blocks=0,
    min_blocks=0,
    max_blocks=10,
)

config = Config(
    executors=[
        HighThroughputExecutor(
            label="aws_executor",
            provider=provider,
            # CurveZMQ certificates live in the client's run_dir, which workers
            # cannot read, so a same-VPC deployment relies on VPC isolation. Across
            # networks, set distribute_certificates=True on the provider and drop
            # this line (see docs/security.md).
            encrypted=False,
        )
    ]
)

parsl.load(config)
```

Construction is not free: it creates a launch template and validates the network
IDs against AWS, so it needs working credentials.

## Operating modes

Select a mode with the `mode` **string**. Full details in
[operating_modes.md](operating_modes.md).

### Standard (default)

The client talks directly to workers.

```python
provider = EphemeralProvider(
    mode="standard",
    region="us-east-1",
    vpc_id="vpc-0123456789abcdef0",
    subnet_id="subnet-0123456789abcdef0",
    security_group_id="sg-0123456789abcdef0",
    instance_type="t3.medium",
)
```

### Detached

A bastion owns the worker lifecycle, so the client can disconnect.

```python
provider = EphemeralProvider(
    mode="detached",
    region="us-east-1",
    vpc_id="vpc-0123456789abcdef0",
    subnet_id="subnet-0123456789abcdef0",
    security_group_id="sg-0123456789abcdef0",
    instance_type="m5.large",
    bastion_instance_type="t3.micro",
    state_store_type="parameter_store",
    parameter_store_path="/parsl/my-workflow-state",
    worker_init="pip3 install --quiet numpy scipy pandas\n",
)
```

### Serverless

Lambda or ECS/Fargate, no EC2 instances. Lambda needs no network IDs at all.

```python
provider = EphemeralProvider(
    mode="serverless",
    region="us-east-1",
    compute_type="lambda",  # or "ecs", which does need subnet + security group
    memory_size=1024,  # MB
    timeout=300,  # seconds
    max_blocks=100,
)
```

## Cost optimization

### Spot instances

```python
provider = EphemeralProvider(
    region="us-east-1",
    vpc_id="vpc-0123456789abcdef0",
    subnet_id="subnet-0123456789abcdef0",
    security_group_id="sg-0123456789abcdef0",
    instance_type="t3.medium",
    use_spot=True,
    spot_max_price_percentage=80,  # cap at 80% of on-demand
    spot_interruption_handling=True,  # act on the two-minute warning
)
```

`spot_interruption_handling=True` creates an EventBridge rule and SQS queue so the
provider learns of an interruption two minutes ahead, rather than discovering the
instance already `shutting-down`.

### Scale to zero

```python
provider = EphemeralProvider(
    # ... network and compute options ...
    min_blocks=0,  # no floor; nothing runs when nothing is queued
    max_blocks=10,
    auto_shutdown=True,  # a worker terminates itself when its command finishes
)
```

`max_blocks` also caps concurrent submissions — a job past the limit raises
rather than queueing.

To reclaim instances that are up but *idle*, use Parsl's `max_idletime`, not a
provider option — only Parsl's interchange knows how many tasks a worker holds:

```python
from parsl.config import Config

config = Config(executors=[...], max_idletime=300.0)
```

`HighThroughputExecutor.scale_in` applies it to blocks that are both idle past
the limit and holding zero tasks. The provider's own `max_idle_time` is
deprecated and ignored: it compared against a timestamp taken at submission, so
it terminated any task that simply ran longer than the limit
([#194](https://github.com/scttfrdmn/parsl-ephemeral-provider/issues/194)).

### Multiple instance types

Diversifying across instance types materially reduces spot interruption rates.
`instance_types` is a list of **type names**:

```python
provider = EphemeralProvider(
    # ... network options ...
    use_spot=True,
    use_spot_fleet=True,
    instance_types=["t3.medium", "m5.large", "c5.large"],
    spot_allocation_strategy="price-capacity-optimized",  # the default
)
```

This uses the EC2 Fleet API (`CreateFleet`) with `Type="instant"`. **Both flags are
required**: `use_spot_fleet=True` on its own builds no fleet manager, so the block
falls through to a single on-demand instance with no error
([#137](https://github.com/scttfrdmn/parsl-ephemeral-provider/issues/137)). With both
set, the fleet path takes precedence over the single-spot-instance path — see
[spot_fleet.md](spot_fleet.md).

### Graviton

arm64 instance types work with no extra configuration; the AMI architecture is
inferred from the type name.

```python
provider = EphemeralProvider(
    # ... network options ...
    instance_type="c7g.large",  # arm64 AMI resolved automatically
)
```

## Worker initialization

`worker_init` runs on each worker before Parsl starts. It executes as root via
cloud-init, so no `sudo`.

```python
provider = EphemeralProvider(
    # ... network and compute options ...
    worker_init="""
        dnf install -y python3.11 python3.11-pip
        ln -sf /usr/bin/python3.11 /usr/bin/python3
        pip3.11 install --quiet --upgrade parsl numpy scipy pandas
        aws s3 cp s3://your-bucket/data/ /tmp/data/ --recursive
    """,
)
```

The default installs Parsl on Amazon Linux 2023 and nothing else. If `worker_init`
is slow, consider `bake_ami=True` (standard mode) to run it once into a custom AMI
rather than on every launch.

## Monitoring

The provider logs through the standard `logging` module under the
`parsl_ephemeral_provider` hierarchy:

```python
import logging

logging.basicConfig(level=logging.INFO)
logging.getLogger("parsl_ephemeral_provider").setLevel(logging.DEBUG)
```

Or pass `debug=True` to the provider.

In the AWS console, look for resources tagged:

- `ParslResource: true`
- `ParslWorkflowId: <provider_id>`

`provider.list_resources()` reports what the provider believes it owns.

## Cleaning up

Resources are **not** removed at interpreter exit — there is no `atexit` hook.
Call `shutdown()` explicitly:

```python
provider.shutdown()
```

This cancels tracked jobs, terminates compute, deletes the launch template and
any AMI the provider baked, plus the IAM role and instance profile if
`auto_create_instance_profile` created them, and deletes the persisted state. In
detached mode the bastion survives shutdown by default so a later session can
adopt it — pass `preserve_bastion=False` to have it terminated instead.

`parsl.clear()` does not reliably kill the HTEX interchange subprocess; the
pattern in `examples/parsl_integration.py` handles that.

To find anything a crash left behind:

```bash
parsl-ephemeral-cleanup --dry-run --region us-east-1
```

## Troubleshooting

Fuller coverage in [troubleshooting.md](troubleshooting.md).

**Workers launch but never register.** The client is almost certainly not
accepting inbound connections on the interchange ports. Confirm with the security
group attached to your *client*, not the workers. From a NAT'd laptop, switch to
detached mode.

**`ProviderConfigurationError: Unknown configuration option(s): ...`.** The option
does not exist on this provider. Since #105 unknown keywords are rejected rather
than ignored, which is why examples from older versions fail loudly — check the
name against [api_reference.rst](api_reference.rst).

**`ResourceNotFoundError` on construction.** One of the three network IDs no
longer exists, or belongs to another region. Earlier versions silently blanked the
ID and failed later inside `RunInstances`.

**Frequent spot interruptions.** Add instance types via `instance_types`, keep
`spot_allocation_strategy="price-capacity-optimized"`, and enable
`spot_interruption_handling=True`.

**State persistence problems.** Check IAM permissions for the backend, and keep
one state location per workflow — two providers sharing one adopt each other's
`provider_id` and fight over the same resources. See
[state_persistence.md](state_persistence.md).

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors

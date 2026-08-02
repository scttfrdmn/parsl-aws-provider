# Examples

Every configuration below is checked against the real
`EphemeralAWSProvider` signature by `tests/unit/test_docs_examples.py`. Unknown
keyword arguments raise `ProviderConfigurationError`, so an option copied from an
older tutorial fails at construction rather than being ignored.

Runnable scripts live in [`examples/`](https://github.com/scttfrdmn/parsl-aws-provider/tree/main/examples).
They read the network IDs from the environment:

```bash
export AWS_PROFILE=aws
export AWS_TEST_REGION=us-east-1
export AWS_TEST_VPC_ID=vpc-...
export AWS_TEST_SUBNET_ID=subnet-...
export AWS_TEST_SG_ID=sg-...
uv run python examples/standard_mode.py
```

## Standard mode with EC2

```python
import parsl
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
from parsl_aws_provider import EphemeralAWSProvider

provider = EphemeralAWSProvider(
    region="us-east-1",
    vpc_id="vpc-0123456789abcdef0",
    subnet_id="subnet-0123456789abcdef0",
    security_group_id="sg-0123456789abcdef0",
    instance_type="t3.micro",
    mode="standard",
    min_blocks=0,
    max_blocks=10,
    worker_init="pip3 install --quiet --upgrade parsl\n",
)

config = Config(
    executors=[
        HighThroughputExecutor(
            label="aws_executor",
            provider=provider,
            encrypted=False,  # see #62
        )
    ]
)

parsl.load(config)


@parsl.python_app
def hello(name):
    import platform

    return f"Hello {name} from {platform.node()}"


print(hello("World").result())

parsl.clear()
provider.shutdown()
```

`worker_init` runs as root through cloud-init — no `sudo`, and no `#!/bin/bash`
line needed. `image_id` is omitted, so an Amazon Linux 2023 AMI matching
`t3.micro`'s architecture is resolved from SSM.

`shutdown()` is not optional: `parsl.clear()` releases Parsl's own resources, not
the provider's AWS ones, and nothing runs at interpreter exit.

## Detached mode with a bastion

```python
import parsl
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
from parsl_aws_provider import EphemeralAWSProvider

provider = EphemeralAWSProvider(
    region="us-east-1",
    vpc_id="vpc-0123456789abcdef0",
    subnet_id="subnet-0123456789abcdef0",
    security_group_id="sg-0123456789abcdef0",
    instance_type="m5.large",
    mode="detached",
    bastion_instance_type="t3.micro",
    min_blocks=0,
    max_blocks=10,
    state_store_type="parameter_store",
    parameter_store_path="/parsl/stats-workflow",
    worker_init="pip3 install --quiet --upgrade parsl numpy scipy\n",
)

config = Config(
    executors=[
        HighThroughputExecutor(
            label="aws_detached_executor",
            provider=provider,
            max_workers_per_node=4,
            encrypted=False,
        )
    ]
)

parsl.load(config)


@parsl.python_app
def compute_stats(data):
    import numpy as np

    return {
        "mean": float(np.mean(data)),
        "std": float(np.std(data)),
        "min": float(np.min(data)),
        "max": float(np.max(data)),
    }


import numpy as np

print(compute_stats(np.random.normal(size=1000).tolist()).result())

parsl.clear()
```

Note the deliberate absence of `provider.shutdown()`. In detached mode the bastion
keeps running so you can reconnect; shutting down would defeat the point. To
reconnect from a new process, construct a provider against the same
`parameter_store_path` — the `provider_id`, bastion, and job map are adopted
automatically. Call `shutdown()` when the workflow is genuinely finished.

## Serverless mode with Lambda

```python
import parsl
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
from parsl_aws_provider import EphemeralAWSProvider

# Lambda runs in the Lambda-managed VPC, so no network IDs are required.
provider = EphemeralAWSProvider(
    region="us-east-1",
    mode="serverless",
    compute_type="lambda",
    memory_size=1024,  # MB; Lambda CPU scales with memory
    timeout=300,       # seconds
    max_blocks=50,
)

config = Config(
    executors=[
        HighThroughputExecutor(
            label="aws_lambda_executor",
            provider=provider,
            encrypted=False,
        )
    ]
)

parsl.load(config)


@parsl.python_app
def process_data(x):
    return float(x**2 + abs(x) ** 0.5)


futures = [process_data(i) for i in range(-10, 11)]
print([f.result() for f in futures])

parsl.clear()
provider.shutdown()
```

`worker_init` has no effect on Lambda — there is no instance to run it on.
Dependencies must be in the deployment package or a layer. For ECS/Fargate, use
`compute_type="ecs"`, supply the three network IDs, and set
`ecs_container_image` to an image carrying your dependencies (the default,
`python:3.12-slim`, gives you the standard library only).

## Spot instances

```python
import parsl
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
from parsl_aws_provider import EphemeralAWSProvider

provider = EphemeralAWSProvider(
    region="us-east-1",
    vpc_id="vpc-0123456789abcdef0",
    subnet_id="subnet-0123456789abcdef0",
    security_group_id="sg-0123456789abcdef0",
    instance_type="m5.large",
    mode="standard",
    min_blocks=0,
    max_blocks=20,
    use_spot=True,
    spot_max_price="0.05",  # a string: dollars per hour
    spot_allocation_strategy="price-capacity-optimized",
    spot_interruption_handling=True,
    worker_init="pip3 install --quiet --upgrade parsl scikit-learn\n",
)

config = Config(
    executors=[
        HighThroughputExecutor(
            label="aws_spot_executor",
            provider=provider,
            max_workers_per_node=4,
            encrypted=False,
        )
    ],
    retries=3,  # a reclaimed instance loses its in-flight tasks
)

parsl.load(config)


@parsl.python_app
def fit_model(n_estimators):
    import numpy as np
    from sklearn.ensemble import RandomForestRegressor
    from sklearn.model_selection import cross_val_score

    rng = np.random.default_rng(0)
    x = rng.random((2000, 10))
    y = x[:, 0] + x[:, 1] ** 2 + rng.normal(scale=0.1, size=2000)

    model = RandomForestRegressor(n_estimators=n_estimators, random_state=0)
    return {
        "n_estimators": n_estimators,
        "r2": float(cross_val_score(model, x, y, cv=3, scoring="r2").mean()),
    }


futures = [fit_model(n) for n in (50, 100, 200, 400)]
for result in sorted((f.result() for f in futures), key=lambda r: -r["r2"]):
    print(f"n_estimators={result['n_estimators']:>3}  r2={result['r2']:.4f}")

parsl.clear()
provider.shutdown()
```

`spot_max_price` is a **string**. `spot_interruption_handling=True` creates an
EventBridge rule and SQS queue so the provider learns of the reclaim about two
minutes ahead rather than noticing the instance already `shutting-down`.

What a detected reclaim does is mark the block `FAILED`, which is what makes Parsl
stop dispatching to it and re-run its tasks elsewhere. **`retries` on the Parsl
`Config` is therefore what actually saves your work** — the provider cannot
checkpoint a task, because it is never told which tasks a block is running
([#137](https://github.com/scttfrdmn/parsl-aws-provider/issues/137)). Set
`retries` to at least 1 whenever `use_spot=True`; without it a reclaim fails the
app instead of retrying it.

## Diversified instance types via EC2 Fleet

```python
provider = EphemeralAWSProvider(
    region="us-east-1",
    vpc_id="vpc-0123456789abcdef0",
    subnet_id="subnet-0123456789abcdef0",
    security_group_id="sg-0123456789abcdef0",
    mode="standard",
    max_blocks=10,
    use_spot=True,                  # required: use_spot_fleet alone is ignored (#137)
    use_spot_fleet=True,
    instance_types=["m5.large", "m5a.large", "m6i.large", "c5.large"],
    spot_max_price_percentage=80,   # cap at 80% of on-demand
    spot_allocation_strategy="price-capacity-optimized",
)
```

`instance_types` is a list of type **names** — not weighted dicts. Diversifying
across types and families is the single most effective way to reduce interruption
rates. Details in [spot_fleet.md](spot_fleet.md).

Set `use_spot=True` as well. `use_spot_fleet=True` on its own is currently
ignored — no fleet manager is built and the block launches as a single on-demand
instance, with no error
([#137](https://github.com/scttfrdmn/parsl-aws-provider/issues/137)).

## Graviton (arm64)

```python
provider = EphemeralAWSProvider(
    region="us-east-1",
    vpc_id="vpc-0123456789abcdef0",
    subnet_id="subnet-0123456789abcdef0",
    security_group_id="sg-0123456789abcdef0",
    instance_type="c7g.large",   # arm64 AMI resolved automatically
    mode="standard",
    max_blocks=4,
)
```

The architecture is inferred from the instance type name and the matching arm64
AMI is resolved from SSM. Make sure `worker_init` installs arm64-compatible
wheels.

## One-shot dispatch, without HTEX

For a batch of independent commands, one-shot mode skips Parsl's interchange
entirely: each job gets one instance, the command is delivered over SSM, the exit
code becomes the job status, and the instance terminates.

```python
import time

from parsl_aws_provider import EphemeralAWSProvider

provider = EphemeralAWSProvider(
    region="us-east-1",
    vpc_id="vpc-0123456789abcdef0",
    subnet_id="subnet-0123456789abcdef0",
    security_group_id="sg-0123456789abcdef0",
    instance_type="t3.micro",
    mode="standard",
    one_shot=True,
    auto_create_instance_profile=True,  # SSM SendCommand needs an IAM role
    max_blocks=5,
)

job_id = provider.submit("python3 -c 'print(1 + 1)'", tasks_per_node=1)

# status() returns a list of parsl.jobs.states.JobStatus
while provider.status([job_id])[0].state.name not in ("COMPLETED", "FAILED"):
    time.sleep(15)

print(provider.status([job_id])[0].state.name)
provider.shutdown()
```

A non-zero exit reports `FAILED`, which the HTEX path cannot distinguish. This
mode needs no client reachability at all, so it works from behind NAT.

## Warm pool

Reuse finished instances instead of paying the boot and `worker_init` cost again.
Standard mode only.

```python
provider = EphemeralAWSProvider(
    region="us-east-1",
    vpc_id="vpc-0123456789abcdef0",
    subnet_id="subnet-0123456789abcdef0",
    security_group_id="sg-0123456789abcdef0",
    instance_type="t3.micro",
    mode="standard",
    warm_pool_size=2,
    warm_pool_ttl=300,                  # seconds held before termination
    auto_create_instance_profile=True,  # required: dispatch is over SSM
    max_blocks=10,
)
```

Warm instances are held **Running** and bill at the full rate for up to
`warm_pool_ttl` seconds per idle period, which is why `warm_pool_size` is capped.
Migration to native ASG warm pools, which hold instances Stopped or Hibernated, is
[#130](https://github.com/scttfrdmn/parsl-aws-provider/issues/130).

## AMI baking

If `worker_init` is slow, run it once into a custom AMI rather than on every
launch. Standard mode only.

```python
provider = EphemeralAWSProvider(
    region="us-east-1",
    vpc_id="vpc-0123456789abcdef0",
    subnet_id="subnet-0123456789abcdef0",
    security_group_id="sg-0123456789abcdef0",
    instance_type="t3.micro",
    mode="standard",
    bake_ami=True,
    worker_init="dnf install -y python3.11 gcc gcc-c++ make\n"
                "pip3.11 install --quiet parsl numpy scipy pandas\n",
    max_blocks=10,
)
```

Construction blocks for several minutes while the builder instance runs and the
image is created. The provider owns that AMI and deregisters it (with its EBS
snapshots) on `shutdown()`. To reuse an image across runs, pass
`baked_ami_id="ami-..."` instead — the provider then treats it as yours and leaves
it alone.

## State persistence and reconnection

```python
from parsl_aws_provider import EphemeralAWSProvider

network = dict(
    region="us-east-1",
    vpc_id="vpc-0123456789abcdef0",
    subnet_id="subnet-0123456789abcdef0",
    security_group_id="sg-0123456789abcdef0",
)

# First process
provider = EphemeralAWSProvider(
    mode="detached",
    state_store_type="s3",
    s3_bucket="my-parsl-state-bucket",
    s3_key="stats-workflow/state.json",
    **network,
)
job_id = provider.submit("python3 long_running.py", tasks_per_node=1)

# Later, in a new process: same store and key, so the bastion and job map are
# adopted along with the persisted provider_id.
provider = EphemeralAWSProvider(
    mode="detached",
    state_store_type="s3",
    s3_bucket="my-parsl-state-bucket",
    s3_key="stats-workflow/state.json",
    **network,
)
print(provider.status([job_id]))
```

Keep one state location per workflow — two concurrent providers sharing one adopt
each other's `provider_id` and fight over the same resources. See
[state_persistence.md](state_persistence.md).

## Debugging a failed launch

```python
import logging

import parsl
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
from parsl_aws_provider import EphemeralAWSProvider

parsl.set_stream_logger()
logging.getLogger("parsl_aws_provider").setLevel(logging.DEBUG)

provider = EphemeralAWSProvider(
    region="us-east-1",
    vpc_id="vpc-0123456789abcdef0",
    subnet_id="subnet-0123456789abcdef0",
    security_group_id="sg-0123456789abcdef0",
    instance_type="t3.micro",
    mode="standard",
    min_blocks=0,
    max_blocks=1,
    debug=True,
    key_name="my-key",  # optional; SSM Session Manager needs no key
)

config = Config(
    executors=[
        HighThroughputExecutor(
            label="debug_executor",
            provider=provider,
            address_probe_timeout=120,
            heartbeat_threshold=120,
            encrypted=False,
        )
    ]
)

parsl.load(config)

print(provider.list_resources())
```

When workers launch but never register, the fault is almost always on the client
side — the interchange ports are not reachable inbound. Reach the instance with
`aws ssm start-session --target i-...` and read
`/var/log/cloud-init-output.log`.

## Resource tracking and cleanup

```python
provider = EphemeralAWSProvider(
    region="us-east-1",
    vpc_id="vpc-0123456789abcdef0",
    subnet_id="subnet-0123456789abcdef0",
    security_group_id="sg-0123456789abcdef0",
    instance_type="t3.micro",
    mode="standard",
    min_blocks=0,
    max_blocks=5,
    auto_shutdown=True,
    additional_tags={"Project": "parsl-demo", "CostCenter": "research"},
)

# ...submit work...

for resource_type, entries in provider.list_resources().items():
    print(f"{resource_type}: {len(entries)}")

provider.cleanup_all()   # terminate compute, keep the provider usable
provider.shutdown()      # ...or tear everything down and delete the state
```

Every resource is tagged `ParslResource=true` and
`ParslWorkflowId=<provider_id>` in addition to `additional_tags`. To find
anything a crash left behind:

```bash
python tools/cleanup_aws_resources.py --dry-run --region us-east-1
```

## Migrating from Parsl's `AWSProvider`

```python
# Before
from parsl.providers import AWSProvider

provider = AWSProvider(
    image_id="ami-0123456789abcdef0",
    instance_type="t2.medium",
    region="us-east-1",
    key_name="my-key",
    state_file="ec2_state.json",
    spot_max_bid="0.1",
)
```

```python
# After
from parsl_aws_provider import EphemeralAWSProvider

provider = EphemeralAWSProvider(
    # image_id is now optional — omit it to resolve the latest AL2023 AMI
    instance_type="t3.medium",
    region="us-east-1",
    key_name="my-key",
    state_file_path="ec2_state.json",
    use_spot=True,
    spot_max_price="0.1",       # was spot_max_bid
    # New and required: this provider never creates network resources
    vpc_id="vpc-0123456789abcdef0",
    subnet_id="subnet-0123456789abcdef0",
    security_group_id="sg-0123456789abcdef0",
    mode="standard",
    min_blocks=0,
    max_blocks=10,
)
```

Name changes: `spot_max_bid` → `spot_max_price`, `state_file` →
`state_file_path`, and `spot_max_bid` as a float becomes a string. `AWSProvider`
creates a VPC for you; this one does not — see
[network-prerequisites.md](network-prerequisites.md).

## Not supported

Some capabilities appear in older versions of this document but have no
implementation:

- **GPU-aware scheduling.** GPU instance types launch like any other, but nothing
  detects, reserves, or reports on GPUs.
- **MPI / multi-node blocks.** `nodes_per_block` is accepted and is honoured only
  by the EC2 Fleet path as a fleet target capacity. The single-instance paths
  launch one instance per block regardless, and no host file or launcher wiring
  exists, so `MPIExecutor` will not work.
- **S3 data staging.** `parsl.data_provider.S3Storage` does not exist in current
  Parsl. Stage data in `worker_init` or inside the app with `boto3`. The
  provider's `s3_bucket` option is for *state storage* only.
- **Placement groups, VPC endpoints, cost/budget monitoring, flow logs,
  hibernation, and additional ingress rules.** None are implemented; there is no
  configuration option for any of them.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors

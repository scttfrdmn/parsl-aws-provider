# Parsl Ephemeral Provider for AWS

**Ephemeral AWS compute for [Parsl](https://parsl.readthedocs.io/) — EC2, Spot
Fleet, Lambda, or Fargate, scaled from zero and torn down when you are done.**

[![CI](https://github.com/scttfrdmn/parsl-ephemeral-provider/actions/workflows/ci.yml/badge.svg)](https://github.com/scttfrdmn/parsl-ephemeral-provider/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Docs](https://img.shields.io/badge/docs-github.io-blue.svg)](https://scttfrdmn.github.io/parsl-ephemeral-provider/)
[![Status](https://img.shields.io/badge/Status-Alpha-yellow.svg)](https://github.com/scttfrdmn/parsl-ephemeral-provider/blob/main/CHANGELOG.md)

> **Independent project.** This is unaffiliated, community-maintained work. It is
> **not** an official or endorsed product of Amazon Web Services, the Parsl
> project, or Globus. It is not the AWS provider that ships with Parsl — that is
> `parsl.providers.AWSProvider`, maintained by the Parsl project, with a different
> configuration contract. See [NOTICE](https://github.com/scttfrdmn/parsl-ephemeral-provider/blob/main/NOTICE) for trademark attribution.

> **Alpha.** The public interface is settling but not settled, and several paths
> are covered only against mocks and a local emulator rather than real AWS —
> [#166](https://github.com/scttfrdmn/parsl-ephemeral-provider/issues/166) tracks which.
> Everything here creates billable resources.

## What it gives you

- **Three operating modes.** `standard` (your client talks to the workers),
  `detached` (a bastion owns the worker lifecycle so your client can disconnect),
  and `serverless` (Lambda or ECS/Fargate, no instances at all).
- **Scale to zero.** `min_blocks=0` means nothing runs, and nothing bills, when
  nothing is queued.
- **Spot, with diversification.** Multiple instance types through the EC2 Fleet
  API, `price-capacity-optimized` allocation, and interruption warnings delivered
  two minutes ahead over EventBridge and SQS.
- **No AMI to pick.** Amazon Linux 2023 is resolved from AWS's public SSM
  parameters for your region and your instance type's architecture — x86_64 and
  Graviton alike.
- **State that outlives the process.** A local file, S3, or SSM Parameter Store,
  so a later session can adopt a running workflow.
- **Globus Compute endpoints**, via `EphemeralComputeProvider`, which generates the
  endpoint directory for you.

## Install

```bash
uv add parsl-ephemeral-provider
```

Or, for unreleased changes, from the repository:

```bash
uv add git+https://github.com/scttfrdmn/parsl-ephemeral-provider
```

Python 3.10 or newer (Parsl 2026.x dropped 3.9). This project manages
environments with [uv](https://docs.astral.sh/uv/); from a clone, `uv sync --extra
dev --extra test`.

## Before your first run

Two prerequisites are easy to miss, and each one produces a failure that does not
look like its cause.

**1. You supply the network.** Since v0.7.0 the provider creates and deletes no
network resources: `vpc_id`, `subnet_id`, and `security_group_id` are required and
validated against AWS at construction. See
[network-prerequisites.md](https://scttfrdmn.github.io/parsl-ephemeral-provider/network-prerequisites.html).

**2. In standard mode your client must be reachable.** Parsl's HTEX workers dial
*outbound* to an interchange that runs next to your client, so the client has to
accept **inbound TCP on ports 54000–55000** from the workers. A laptop behind a
home or office NAT cannot, and there is no tunnel that changes that — the workers
will launch, never register, and time out. Run the client on EC2 in the same VPC,
or use `mode="detached"`, where a bastion does the coordinating and nothing needs
to reach your client.

Construction itself calls AWS — it creates a launch template and validates those
IDs — so it needs working credentials, and it is not free.

## Quick start

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
    instance_type="c5.large",
    min_blocks=0,
    max_blocks=5,
)

config = Config(
    executors=[
        HighThroughputExecutor(
            label="aws_executor",
            provider=provider,
            # CurveZMQ certificates are generated in the client's run_dir, which
            # remote workers cannot read, so they would fail to register with no
            # useful error. Same-VPC deployments rely on VPC isolation instead;
            # certificate distribution is #62.
            encrypted=False,
        )
    ]
)

parsl.load(config)


@parsl.python_app
def integrate(n):
    import math

    return sum(math.sqrt(i) for i in range(n))


try:
    futures = [integrate(100_000) for _ in range(10)]
    print([f.result() for f in futures])
finally:
    parsl.clear()
    # Not optional: parsl.clear() releases Parsl's resources, not AWS ones, and
    # there is no atexit hook. Omitting this leaves instances running.
    provider.shutdown()
```

Credentials come from anywhere botocore looks — environment variables,
`~/.aws/credentials`, or an instance profile on EC2. Pass
`profile_name="myprofile"` to select a named profile; the provider accepts no key
arguments.

## Operating modes

`mode` is a **string**. Full details in
[operating_modes.md](https://scttfrdmn.github.io/parsl-ephemeral-provider/operating_modes.html).

```python
# Detached: a bastion owns the workers, so the client may disconnect and reconnect.
provider = EphemeralProvider(
    mode="detached",
    region="us-east-1",
    vpc_id="vpc-0123456789abcdef0",
    subnet_id="subnet-0123456789abcdef0",
    security_group_id="sg-0123456789abcdef0",
    instance_type="m5.large",
    bastion_instance_type="t3.micro",
    state_store_type="parameter_store",  # readable by client and bastion alike
    parameter_store_path="/parsl/my-workflow-state",
)
```

```python
# Serverless: Lambda needs no network IDs at all; ECS/Fargate needs subnet + SG.
provider = EphemeralProvider(
    mode="serverless",
    region="us-east-1",
    compute_type="lambda",  # or "ecs"
    memory_size=1024,  # MB
    timeout=300,  # seconds
    max_blocks=100,
)
```

To reconnect to a detached workflow, construct a provider against the **same**
state location: the `provider_id`, the bastion, and the job map are adopted
automatically. Note that `preserve_bastion` defaults to `True`, so the bastion
keeps running — and keeps billing — after `shutdown()`. That is what makes
adoption possible; pass `preserve_bastion=False` to tear it down instead.

## Spot instances

```python
provider = EphemeralProvider(
    region="us-east-1",
    vpc_id="vpc-0123456789abcdef0",
    subnet_id="subnet-0123456789abcdef0",
    security_group_id="sg-0123456789abcdef0",
    use_spot=True,
    use_spot_fleet=True,  # both flags are required; see below
    instance_types=["c5.large", "c5a.large", "m5.large"],
    spot_max_price_percentage=80,  # cap at 80% of on-demand
    spot_interruption_handling=True,  # act on the two-minute warning
)
```

Diversifying across instance types materially reduces interruption rates. **Both
spot flags are required**: `use_spot_fleet=True` alone builds no fleet manager and
the block quietly falls through to a single on-demand instance
([#137](https://github.com/scttfrdmn/parsl-ephemeral-provider/issues/137)). See
[spot_fleet.md](https://scttfrdmn.github.io/parsl-ephemeral-provider/spot_fleet.html).

## Globus Compute

> **Full guide**: [docs/globus_compute.md](https://scttfrdmn.github.io/parsl-ephemeral-provider/globus_compute.html) — architecture,
> IAM setup, spot and container examples, multi-region deployment, troubleshooting.

`EphemeralComputeProvider` generates the whole endpoint directory rather than making
you hand-write YAML — including the two pieces that are easy to get wrong: the
`engine:` block belongs in `user_config_template.yaml.j2` and not in `config.yaml`,
or `start` refuses the endpoint outright; and the forked user-endpoint process
never imports this package, so a `sitecustomize` bootstrap on its `PYTHONPATH` is
what makes Globus Compute's attribute lookup on `parsl.providers` find the class.

```bash
uv sync --extra globus
uv run globus-compute-endpoint login
```

```python
from parsl_ephemeral_provider import EphemeralComputeProvider

provider = EphemeralComputeProvider(
    region="us-east-1",
    vpc_id="vpc-0123456789abcdef0",
    subnet_id="subnet-0123456789abcdef0",
    security_group_id="sg-0123456789abcdef0",
    instance_type="c5.large",
    auto_create_instance_profile=True,
    min_blocks=0,
    max_blocks=20,
    display_name="AWS Research Endpoint",
)

provider.generate_endpoint_config("~/.globus_compute/aws_research_endpoint")
```

```bash
uv run globus-compute-endpoint start aws_research_endpoint
```

Because a Globus Compute endpoint reaches *outbound* to the Globus service, this
is also the way to drive AWS workers from a client that cannot accept inbound
connections.

## Moving data

Keep bulk data out of the Parsl control channel: have workers pull from S3 or
HTTPS directly, and return summaries.

```python
from parsl import bash_app


@bash_app
def process_s3_dataset(s3_input_uri, s3_output_uri):
    """Large data over S3 (AWS-internal); only the completion message goes back."""
    return f"""
    aws s3 cp {s3_input_uri} /tmp/data.csv
    python3 /opt/analysis.py /tmp/data.csv /tmp/results.json
    aws s3 cp /tmp/results.json {s3_output_uri}
    """
```

The instance profile must carry the S3 permissions for this; see
[security.md](https://scttfrdmn.github.io/parsl-ephemeral-provider/security.html). More patterns in
[docs/examples.md](https://scttfrdmn.github.io/parsl-ephemeral-provider/examples.html).

## Worker setup

`worker_init` runs on each worker through cloud-init, as root — no `sudo`, no
shebang. The default installs Python 3.11 and Parsl on Amazon Linux 2023 and
nothing else.

```python
provider = EphemeralProvider(
    # ... network options ...
    worker_init="""
        dnf install -y python3.11 python3.11-pip
        ln -sf /usr/bin/python3.11 /usr/bin/python3
        pip3.11 install --quiet --upgrade parsl numpy scipy
    """,
)
```

If that is slow, `bake_ami=True` (standard mode) runs it once into a custom AMI
instead of on every launch.

## Cleaning up

Nothing runs at interpreter exit. `provider.shutdown()` cancels tracked jobs,
terminates compute, deletes the launch template and any AMI the provider baked,
and removes the persisted state. Resources carry `ParslResource=true` and
`ParslWorkflowId=<provider_id>` tags, and `provider.list_resources()` reports what
the provider believes it owns.

To find what a crash left behind:

```bash
parsl-ephemeral-cleanup --dry-run --region us-east-1
```

## Examples

Eight runnable scripts in [`examples/`](https://github.com/scttfrdmn/parsl-ephemeral-provider/tree/main/examples/), each reading its network IDs
from the environment. Every one creates real AWS resources.

| Example | What it shows |
|---|---|
| [standard_mode.py](https://github.com/scttfrdmn/parsl-ephemeral-provider/blob/main/examples/standard_mode.py) | The common case: EC2 workers connecting back to your client |
| [basic_usage.py](https://github.com/scttfrdmn/parsl-ephemeral-provider/blob/main/examples/basic_usage.py) | One provider configured for all three modes, side by side |
| [parsl_integration.py](https://github.com/scttfrdmn/parsl-ephemeral-provider/blob/main/examples/parsl_integration.py) | A full Parsl workflow, driven from EC2 in the same VPC |
| [detached_mode.py](https://github.com/scttfrdmn/parsl-ephemeral-provider/blob/main/examples/detached_mode.py) | A bastion owning the workers, and how reconnection works |
| [serverless_mode.py](https://github.com/scttfrdmn/parsl-ephemeral-provider/blob/main/examples/serverless_mode.py) | Lambda functions or Fargate tasks instead of instances |
| [spot_fleet_example.py](https://github.com/scttfrdmn/parsl-ephemeral-provider/blob/main/examples/spot_fleet_example.py) | An EC2 Fleet across several instance types |
| [spot_interruption_example.py](https://github.com/scttfrdmn/parsl-ephemeral-provider/blob/main/examples/spot_interruption_example.py) | Detecting a reclaim two minutes ahead |
| [serverless_spot_fleet_example.py](https://github.com/scttfrdmn/parsl-ephemeral-provider/blob/main/examples/serverless_spot_fleet_example.py) | Serverless mode's fleet path — an unusual corner |

## Documentation

<https://scttfrdmn.github.io/parsl-ephemeral-provider/>

- [Getting Started](https://scttfrdmn.github.io/parsl-ephemeral-provider/getting_started.html) — install, configure, first run
- [Network Prerequisites](https://scttfrdmn.github.io/parsl-ephemeral-provider/network-prerequisites.html) — VPC, subnet, and security group requirements
- [Operating Modes](https://scttfrdmn.github.io/parsl-ephemeral-provider/operating_modes.html) — standard, detached, serverless, and the options each accepts
- [Spot Fleet](https://scttfrdmn.github.io/parsl-ephemeral-provider/spot_fleet.html) — diversification and interruption handling
- [State Persistence](https://scttfrdmn.github.io/parsl-ephemeral-provider/state_persistence.html) — file, S3, and Parameter Store backends
- [Security](https://scttfrdmn.github.io/parsl-ephemeral-provider/security.html) — least-privilege IAM, instance profiles, encryption
- [Architecture](https://scttfrdmn.github.io/parsl-ephemeral-provider/architecture.html) — how the pieces fit
- [Troubleshooting](https://scttfrdmn.github.io/parsl-ephemeral-provider/troubleshooting.html) — common failures and how to clear them
- [Examples](https://scttfrdmn.github.io/parsl-ephemeral-provider/examples.html) — annotated configurations

## AWS permissions

Do not copy a policy out of a README — it drifts. Generate the current one from
the code, which cannot:

```python
import json

from parsl_ephemeral_provider import EphemeralComputeProvider

print(json.dumps(EphemeralComputeProvider.minimum_iam_policy(), indent=2))
```

It is a `@staticmethod`, so you need no provider instance — and it describes the
base provider just as well, despite living on the Globus subclass.
[security.md](https://scttfrdmn.github.io/parsl-ephemeral-provider/security.html) explains what it deliberately omits — no
`ec2:CreateVpc`, no `CreateSecurityGroup` — and what each mode adds.

## Troubleshooting

Fuller coverage in [troubleshooting.md](https://scttfrdmn.github.io/parsl-ephemeral-provider/troubleshooting.html).

**Workers launch but never register.** Almost always the inbound-port
prerequisite above. Check the security group on your *client*, not the workers.

**`ProviderConfigurationError: Unknown configuration option(s): ...`.** The option
does not exist. Unknown keywords are rejected rather than ignored, which is why
configurations from older versions fail loudly — check the name against
[api_reference.rst](https://scttfrdmn.github.io/parsl-ephemeral-provider/api_reference.html).

**`ResourceNotFoundError` on construction.** One of the three network IDs does not
exist, or belongs to another region.

```bash
aws sts get-caller-identity                                   # credentials work?
aws ssm describe-instance-information --region us-east-1       # SSM sees anything?
```

## Contributing

Issues and pull requests are welcome. All work goes on a feature branch and merges
via a PR; issues carry `severity:`, `type:`, and `component:` labels and a
milestone. See [CLAUDE.md](https://github.com/scttfrdmn/parsl-ephemeral-provider/blob/main/CLAUDE.md) for the development conventions and
[docs/ci_cd.md](https://scttfrdmn.github.io/parsl-ephemeral-provider/ci_cd.html) for what CI checks.

```bash
git clone https://github.com/scttfrdmn/parsl-ephemeral-provider
cd parsl-ephemeral-provider

# uv only — no pip, venv, or pyenv
uv sync --extra dev --extra test

uv run pytest tests/unit tests/security --no-cov -q
uv run ruff check . && uv run ruff format --check .
```

Integration tests run against [substrate](https://github.com/scttfrdmn/substrate),
a local AWS emulator (`make substrate-up`) — see
[substrate_testing.md](https://scttfrdmn.github.io/parsl-ephemeral-provider/substrate_testing.html). Tests under `tests/aws/` need
real credentials and cost money.

## License

Apache License 2.0 — see [LICENSE](https://github.com/scttfrdmn/parsl-ephemeral-provider/blob/main/LICENSE) and [NOTICE](https://github.com/scttfrdmn/parsl-ephemeral-provider/blob/main/NOTICE).

## Support

- [GitHub Issues](https://github.com/scttfrdmn/parsl-ephemeral-provider/issues)
- [Parsl documentation](https://parsl.readthedocs.io)
- [Globus Compute documentation](https://globus-compute.readthedocs.io)

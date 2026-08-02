# Examples

Eight runnable scripts. Every one creates real AWS resources and costs real money.

All of them read the network IDs from the environment, because as of v0.7.0 the
provider never creates network resources — see
[network-prerequisites.md](../docs/network-prerequisites.md).

```bash
export AWS_PROFILE=aws
export AWS_TEST_REGION=us-east-1
export AWS_TEST_VPC_ID=vpc-...
export AWS_TEST_SUBNET_ID=subnet-...
export AWS_TEST_SG_ID=sg-...

uv run python examples/standard_mode.py
```

An example that exits `2` is telling you one of those is unset. Credentials resolve
through botocore — use `AWS_PROFILE` or a profile, not key arguments; the provider
accepts none.

## Where to start

| Example | What it shows |
|---|---|
| [standard_mode.py](standard_mode.py) | The common case: EC2 workers connecting back to your client |
| [basic_usage.py](basic_usage.py) | The same provider configured for all three modes, side by side |
| [parsl_aws_integration.py](parsl_aws_integration.py) | A full Parsl workflow, run from an EC2 driver in the same VPC |
| [detached_mode.py](detached_mode.py) | A bastion owns the workers; also how reconnection works |
| [serverless_mode.py](serverless_mode.py) | Lambda functions or Fargate tasks instead of instances |
| [spot_fleet_example.py](spot_fleet_example.py) | An EC2 Fleet across several instance types |
| [spot_interruption_example.py](spot_interruption_example.py) | Detecting a reclaim two minutes ahead, and what actually recovers the work |
| [serverless_spot_fleet_example.py](serverless_spot_fleet_example.py) | Serverless mode's fleet path — an unusual corner |

Start with `standard_mode.py`. If your client is a laptop behind NAT, start with
`detached_mode.py` instead, and read the "Workers launch but never register" section
of [troubleshooting.md](../docs/troubleshooting.md) first — it is by far the most
common failure and it is not an AWS problem.

## Two things every example does deliberately

**`encrypted=False` on the executor.** With encryption on, Parsl generates CurveZMQ
certificates in the client's `run_dir`, which remote workers cannot read, so they
fail to register with no useful error. Certificate distribution is
[#62](https://github.com/scttfrdmn/parsl-aws-provider/issues/62).

**`provider.shutdown()` in a `finally`.** There is no `atexit` hook, and
`parsl.clear()` releases Parsl's resources, not AWS ones. If an example is killed
hard, sweep for orphans:

```bash
parsl-aws-cleanup --dry-run --region us-east-1
```

It matches the `ParslResource=true` tag, so it finds resources no state file names.

## Options these examples do not use

The provider rejects unknown keyword arguments since
[#105](https://github.com/scttfrdmn/parsl-aws-provider/issues/105), so a stale
configuration raises at construction rather than being silently ignored:

```
ProviderConfigurationError: Unknown configuration option(s): tags
```

Earlier versions of these examples passed mode *objects* (`mode=StandardMode(...)`)
and state-store *instances* (`state_store=FileStateStore(...)`). Neither works:
`mode` is a string and the backend is chosen with `state_store_type`, because the
provider constructs both itself so it can inject the session, resolved AMI, and
credentials. See the rename table in
[troubleshooting.md](../docs/troubleshooting.md).

## More

- [Documentation index](../docs/README.md)
- [Getting started](../docs/getting_started.md)
- [GitHub repository](https://github.com/scttfrdmn/parsl-aws-provider)

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors

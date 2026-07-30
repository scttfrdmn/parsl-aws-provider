# Testing with substrate

This guide explains how to test the Parsl Ephemeral AWS Provider against
[substrate](https://github.com/scttfrdmn/substrate), a local AWS emulator, without
creating real AWS resources or incurring charges.

Renamed from `localstack_testing.md` in #125. If you are looking for the LocalStack
instructions, see [Why not LocalStack?](#why-not-localstack) below — they no longer
work.

## What is substrate?

Substrate is a single Go binary that emulates AWS service APIs on `localhost:4566`.
It is a deliberate drop-in for LocalStack: it answers `GET /_localstack/health` with
a LocalStack-shaped per-service payload, so existing tooling that polls that route
keeps working, and it adds `POST /v1/state/reset`, which LocalStack never offered.

Unlike LocalStack it needs no license token, no Python package, and no Docker
socket mount.

## Why not LocalStack?

LocalStack OSS is end-of-life:

- The upstream repository was archived read-only in March 2026.
- `4.14.0` is the last community image.
- `localstack/localstack:latest` now resolves to the Pro build — a byte-identical
  digest to `localstack/localstack-pro` — which exits 55 with
  `License activation failed!` unless `LOCALSTACK_AUTH_TOKEN` is set.

That last point is why CI's integration job failed unconditionally for months:
`continue-on-error` does not cover service-container startup, so the job died before
any step ran.

## Starting substrate

The compose file is pinned to a specific image tag, deliberately — an emulator that
silently changes under CI turns an unrelated PR red.

Do not try to confirm the pin from `/health`: released images report
`"version":"dev"` whatever their tag ([substrate#402](https://github.com/scttfrdmn/substrate/issues/402)).
Use `podman image inspect ghcr.io/scttfrdmn/substrate:0.76.0 --format '{{.Digest}}'`
instead.

```bash
make substrate-up      # start and wait for /health
make substrate-status  # container state plus the health payload
make substrate-reset   # wipe all emulator state
make substrate-down    # stop
```

`make substrate-up` is also what `make test-integration` depends on, so running the
integration suite starts the emulator for you.

The Makefile auto-detects the container runtime, preferring `podman` over `docker`.
To run the binary directly instead of a container:

```bash
go install github.com/scttfrdmn/substrate/cmd/substrate@latest
substrate server
```

State lives in memory for the lifetime of the server process. That matters for test
design: a fixed resource name collides with `ResourceConflictException` on a second
run against a long-lived emulator, so tests that create named resources should
suffix them with `uuid.uuid4().hex[:8]` or call `make substrate-reset` between runs.

## Pointing the provider at substrate

There is no `use_substrate=` or `endpoint_url=` provider argument, and there never
was a `use_localstack=` one either — the old version of this document documented
kwargs that do not exist in any release. Instead, set botocore's standard
environment variable, which every client the provider builds picks up
automatically:

```bash
export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_ACCESS_KEY_ID=substrate-test
export AWS_SECRET_ACCESS_KEY=substrate-test-secret
export AWS_DEFAULT_REGION=us-east-1
```

Credentials are structural rather than secret: substrate authenticates against
nothing, but botocore refuses to sign a request without them.

Since #69 the provider creates no network resources, so `vpc_id`, `subnet_id`, and
`security_group_id` must all exist beforehand:

```python
import boto3
from parsl_ephemeral_aws import EphemeralAWSProvider

ec2 = boto3.client("ec2")
vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]

# Read the AZ rather than assuming f"{region}a": that guess is how the real-AWS
# suite ended up pinned to a zone that does not offer t3.micro.
az = ec2.describe_availability_zones()["AvailabilityZones"][0]["ZoneName"]
subnet = ec2.create_subnet(
    VpcId=vpc, CidrBlock="10.0.0.0/24", AvailabilityZone=az
)["Subnet"]["SubnetId"]
sg = ec2.create_security_group(
    GroupName="parsl-test", Description="Parsl test", VpcId=vpc
)["GroupId"]

provider = EphemeralAWSProvider(
    image_id="ami-12345678",   # any value; the emulator does not resolve AMIs
    instance_type="t3.micro",
    region="us-east-1",
    vpc_id=vpc,
    subnet_id=subnet,
    security_group_id=sg,
    init_blocks=0,
    max_blocks=1,
)

job_id = provider.submit("echo hello", tasks_per_node=1)
print(provider.status([job_id]))
provider.cancel([job_id])
```

## Test suite integration

`tests/substrate_support.py` holds the helpers. It lives under `tests/` rather than
inside the shipped package, because no package code imports it — its predecessor,
`parsl_ephemeral_aws/utils/localstack.py`, shipped to users who had no use for it.

```python
from tests.substrate_support import (
    get_substrate_session,     # boto3.Session pointed at the emulator
    is_substrate_available,    # never raises; gate a module skip on this
    reset_substrate_state,     # POST /v1/state/reset
    setup_substrate_vpc,       # provision vpc + subnet + igw + route table + sg
    cleanup_substrate_vpc,     # tear it down, dependents first
)
```

`setup_substrate_vpc()` returns a dict of `vpc_id`, `subnet_id`,
`security_group_id`, `route_table_id`, and `internet_gateway_id`, with TCP 22 and
the HTEX range 54000–55000 open plus a self-referencing rule.

`tests/conftest.py` provides fixtures on top of these: `substrate_endpoint`,
`substrate_running`, `substrate_available` (session-scoped, checks the per-service
health map for ec2/lambda/s3/ssm), `substrate_session` (a session whose `.client`
is pre-bound to the endpoint, so code under test that builds its own clients still
reaches the emulator), and `test_session`, which routes `@pytest.mark.aws` tests to
real AWS and everything else to substrate.

Two pytest markers are relevant:

- `integration` — lives in `tests/integration/`. A subset is pure-moto and needs no
  endpoint at all.
- `substrate` — requires a running emulator. Pair it with a
  `skipif(not is_substrate_available())`: a marker only *selects* tests, it never
  skips them, so a marker alone leaves the test erroring rather than skipping when
  the emulator is down.

Running the suites:

```bash
make substrate-up
uv run pytest tests/integration -v                   # emulator-gated tests skip without one
uv run pytest tests/test_substrate_emulation.py -v   # emulator conformance only
```

`tests/test_substrate_emulation.py` is the suite that answers "does the emulator
still support what the provider relies on?" — every test there drives raw boto3 and
imports no package code, so an emulator regression is not misattributed to the
provider.

## Known gaps

Verified against substrate `0.76.0`. None of these block the current suite, but
they shape what can be tested where:

| Gap | Effect | Upstream |
|---|---|---|
| `describe_instances`/`describe_vpcs` with an unknown ID return HTTP 200 and an empty list instead of `Invalid*ID.NotFound` | `modes/base.py::_verify_resources` never raises, so the whole #69 network-validation guard is silently skipped and the test still reports green | [substrate#391](https://github.com/scttfrdmn/substrate/issues/391) |
| `Error.Code` carries the HTTP status (`"404"`) rather than the symbolic code for SSM/Lambda not-found; IAM returns `NoSuchEntityException` where the wire code is `NoSuchEntity`; `s3.get_object` reports `NoSuchKey` for a missing *bucket* | `ParameterStoreState.save_state()` branches on `Code == "ParameterNotFound"` to choose between put-with-Overwrite and create-with-Tags, so its create path cannot be covered here | [substrate#392](https://github.com/scttfrdmn/substrate/issues/392) |
| `lambda.invoke` sets `FunctionError` to `""` on success, where AWS omits the key | Cosmetic. `compute/lambda_func.py` reads it with `.get()`, so a falsy value behaves correctly | [substrate#393](https://github.com/scttfrdmn/substrate/issues/393) |
| CloudFormation is not exposed over HTTP; `create_stack` returns `ServiceNotAvailable` | `DetachedMode`'s bastion stack cannot be exercised here. The one test that touches it patches `_create_cloudformation_stack` out; real coverage is in `tests/aws/` | — |
| `RequestSpotFleet`, `RequestSpotInstances`, and `CreateFleet` are unimplemented | No impact: every spot-fleet test uses moto rather than an endpoint | — |
| Released images report `"version":"dev"` from `/health` regardless of tag | The image pin cannot be confirmed from the running emulator; verify with `podman image inspect` instead | [substrate#402](https://github.com/scttfrdmn/substrate/issues/402) |

Substrate does emulate two things LocalStack did not, and both are load-bearing
here: EC2 instance state actually transitions to `terminated` (which
`EC2_STATUS_MAPPING` and one-shot mode both depend on), and Lambda
`create_function` + `invoke` both work.

## Real AWS

Some behaviour cannot be emulated. `tests/aws/` runs against a real account and is
gated on three environment variables plus `AWS_PROFILE=aws`:

```bash
export AWS_PROFILE=aws
export AWS_TEST_REGION=us-east-1
export AWS_TEST_VPC_ID=vpc-… AWS_TEST_SUBNET_ID=subnet-… AWS_TEST_SG_ID=sg-…
uv run pytest tests/aws -m aws -v --no-cov
```

These create billable resources. `tools/cleanup_aws_resources.py --dry-run`
reports anything left behind.

## Troubleshooting

**Connection refused.** Check the container and the health payload:

```bash
make substrate-status
curl -s http://localhost:4566/health
```

**Tests skip instead of running.** `substrate_available` polls
`/_localstack/health` and requires ec2, lambda, s3, and ssm to all report
`available`. Inspect the map directly:

```bash
curl -s http://localhost:4566/_localstack/health | python3 -m json.tool
```

**`ResourceConflictException` or `InvalidParameterValue` on a rerun.** Emulator
state persists for the server process's lifetime. Run `make substrate-reset`.

**Port 4566 already in use.** Point the suite elsewhere; `SUBSTRATE_ENDPOINT` wins,
and `LOCALSTACK_ENDPOINT` is still honoured for the transition:

```bash
SUBSTRATE_ENDPOINT=http://localhost:4599 uv run pytest tests/integration -v
```

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors

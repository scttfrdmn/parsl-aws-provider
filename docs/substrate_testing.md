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

`/health` reports the real version as of 0.85.0 — `{"status":"ok","version":"v0.85.0"}` —
so `make substrate-status` is enough to confirm the pin. Released images used to
report `"version":"dev"` whatever their tag
([substrate#402](https://github.com/scttfrdmn/substrate/issues/402)); against an
older image, use
`podman image inspect ghcr.io/scttfrdmn/substrate:0.85.0 --format '{{.Digest}}'`
instead.

When bumping the pin, change it in **two** places — `docker-compose.substrate.yml`
and the `services.substrate.image` in `.github/workflows/ci.yml`. They drifted
once (CI on 0.76.0, compose on 0.82.0), which meant CI validated against an
emulator six releases behind the one developers ran locally.

Note also that a merged substrate fix is not a released one: substrate cuts
release commits, so a fix merged to `main` lands *after* the newest tag and is
invisible to an image pin. `git tag --contains <sha>` is the check.

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
from parsl_aws_provider import EphemeralAWSProvider

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
`parsl_aws_provider/utils/localstack.py`, shipped to users who had no use for it.

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

Verified by probing substrate `0.85.0` directly, not read off the milestones.
Only two gaps remain, and both are why part of the suite still uses moto:

| Gap | Effect | Upstream |
|---|---|---|
| CloudFormation is not exposed over HTTP; `create_stack` returns `ServiceNotAvailable` | `DetachedMode`'s bastion stack and six `ServerlessMode` tests that drive `cf_client` cannot run here. `tests/integration/test_serverless_mode_spot_fleet_integration.py` stays on moto for exactly this reason; real coverage is in `tests/aws/` | — |
| EventBridge is not emulated; `PutRule` returns `501 service not emulated: awsevents` | The spot-warning notifier's degradation path is testable *because* of this. The warning path itself is covered end to end by wiring an SQS queue directly, since the monitor only ever reads warnings through SQS | — |
| IAM does not serve AWS-managed policies — `get_policy` on `arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore` returns `NoSuchEntity` (though `attach_role_policy` accepts it) | The unit suite keeps moto with `MOTO_IAM_LOAD_MANAGED_POLICIES=true`, which serves the real policy documents | — |
| The instance-type catalog is partial — `t3.micro` resolves, `m5.xlarge` does not | `describe_instance_capacity` tests stay on moto, which knows the full catalog | — |

Fixed since, and no longer worked around anywhere:

| Was | Now | Upstream |
|---|---|---|
| Unknown instance/VPC IDs returned HTTP 200 + empty list, silently skipping the whole #69 network-validation guard | Raises `InvalidInstanceID.NotFound`, so `_verify_resources` is genuinely exercised | [substrate#391](https://github.com/scttfrdmn/substrate/issues/391) |
| `Error.Code` carried the HTTP status (`"404"`) rather than the symbolic code | `ParameterNotFound`, `NoSuchEntity`, `NoSuchBucket` all correct, so `ParameterStoreState.save_state()`'s create-path branch is coverable | [substrate#392](https://github.com/scttfrdmn/substrate/issues/392) |
| `lambda.invoke` set `FunctionError` to `""` on success where AWS omits it | Key omitted, matching AWS | [substrate#393](https://github.com/scttfrdmn/substrate/issues/393) |
| `CreateFleet` unimplemented | Implemented; an instant fleet launches its full `TotalTargetCapacity` | [substrate#387](https://github.com/scttfrdmn/substrate/issues/387) |
| Fleet instances carried no `aws:ec2:fleet-id` tag, so tests hand-applied it | Substrate stamps it, and now *rejects* manual `aws:`-prefixed keys as reserved — so the old workaround is an error, not merely redundant | [substrate#443](https://github.com/scttfrdmn/substrate/issues/443) |
| `?publicAccessBlock` was unrouted: `PUT` hit `CreateBucket`, `DELETE` **deleted the bucket** | Routed, so `S3State(create_bucket_if_not_exists=True)` is covered instead of xfailed | [substrate#446](https://github.com/scttfrdmn/substrate/issues/446) |
| `/health` reported `"version":"dev"` regardless of tag | Reports the real version | [substrate#402](https://github.com/scttfrdmn/substrate/issues/402) |

`RequestSpotFleet` and `RequestSpotInstances` are still unimplemented and are not
coming back here: #86 moved this provider onto `CreateFleet`, so nothing calls them.

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

These create billable resources. `parsl-aws-cleanup --dry-run`
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
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors

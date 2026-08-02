# Testing with substrate

This guide explains how to test the Parsl AWS Provider against
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

`/health` reports the real version since 0.85.0 — `{"status":"ok","version":"v0.87.0"}` —
so `make substrate-status` is enough to confirm the pin. Released images used to
report `"version":"dev"` whatever their tag
([substrate#402](https://github.com/scttfrdmn/substrate/issues/402)); against an
older image, use
`podman image inspect ghcr.io/scttfrdmn/substrate:0.87.0 --format '{{.Digest}}'`
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
subnet = ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.0.0/24", AvailabilityZone=az)[
    "Subnet"
]["SubnetId"]
sg = ec2.create_security_group(
    GroupName="parsl-test", Description="Parsl test", VpcId=vpc
)["GroupId"]

provider = EphemeralAWSProvider(
    image_id="ami-12345678",  # any value; the emulator does not resolve AMIs
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
    get_substrate_session,  # boto3.Session pointed at the emulator
    is_substrate_available,  # never raises; gate a module skip on this
    reset_substrate_state,  # POST /v1/state/reset
    setup_substrate_vpc,  # provision vpc + subnet + igw + route table + sg
    cleanup_substrate_vpc,  # tear it down, dependents first
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

Verified by probing substrate `0.87.0` directly, not read off the milestones.
CloudFormation is why part of the suite still uses moto (#183); the EventBridge
row is a gap this project turns to its advantage rather than one it works around.

| Gap | Effect | Upstream |
|---|---|---|
| CloudFormation stacks reach `CREATE_COMPLETE` having created nothing the caller can find — see below, this is two distinct defects | `tests/integration/test_serverless_mode_spot_fleet_integration.py` and `test_detached_mode_spot_fleet_integration.py` stay on moto; real coverage is in `tests/aws/` | [substrate#483](https://github.com/scttfrdmn/substrate/issues/483) reopened the door; the remainder is [substrate#516](https://github.com/scttfrdmn/substrate/issues/516) and [substrate#517](https://github.com/scttfrdmn/substrate/issues/517) |
| EventBridge is not emulated; `PutRule` returns `501 service not emulated: awsevents` | The spot-warning notifier's degradation path is testable *because* of this. The warning path itself is covered end to end by wiring an SQS queue directly, since the monitor only ever reads warnings through SQS | — |

### CloudFormation

Read this before assuming CFN either works or is absent — through `0.85.0` it was
neither. It was *implemented* (`emulator/betty_cfn.go` plus ~40
`betty_cfn_v*_plugins.go` files, covering parameters, outputs, conditions, change
sets and drift) but registered no `cloudformation` plugin, so `create_stack`
returned `ServiceNotAvailable` and the support was reachable only from Go, through
the in-process `betty.Deploy` client. The similarly-named `cfPlugin` is CloudFront.

`0.87.0` registers a real plugin
([substrate#483](https://github.com/scttfrdmn/substrate/issues/483)), and the whole
API surface this repo drives works over the wire: `create_stack`, `describe_stacks`
with `Outputs`, `describe_stack_resources`, `delete_stack`, both waiters,
`Parameters` and `Capabilities`. `DescribeStackEvents` has no backing event model
([substrate#501](https://github.com/scttfrdmn/substrate/issues/501)) but nothing
here calls it.

Two defects behind that surface are why moto stays.

**YAML short-form intrinsics are not resolved**
([substrate#516](https://github.com/scttfrdmn/substrate/issues/516)). `!Sub`, `!Ref`, `!If`, `!GetAtt`
are stripped and the raw scalar used as a literal, while the `Fn::`-prefixed long
forms are correct — `Fn::Sub: 'x-${P}'` substitutes, `Fn::If` picks the right
branch on both true and false conditions, but `!Sub 'x-${P}'` yields the physical
ID `x-${p}`. Every template in `parsl_aws_provider/templates/cloudformation/` uses
the short forms, so deploying `ecs_worker.yml` produces a task definition whose ARN
embeds an unevaluated condition array:

```
arn:aws:ecs:us-east-1:123456789012:task-definition/["HasTaskFamily","TaskFamily","parsl-task-${WorkflowId}-${JobId}"]:1
```

The cause is upstream of the intrinsics engine, which is why the long forms are
unaffected: `parseCFNTemplate` (`emulator/betty_cfn.go:3358`) unmarshals directly
into its template struct with `go.yaml.in/yaml/v3`, and that library has no notion
of the CloudFormation tag shorthands — it discards the tag and keeps the node value,
so nothing downstream can tell a `!Sub` string from a literal one.

**Resources are written to a different account than the caller reads**
([substrate#517](https://github.com/scttfrdmn/substrate/issues/517)).
`StackDeployer.dispatch` (`emulator/betty_cfn.go:2847`) synthesises its
`RequestContext` from the constants `testAccountID` (`123456789012`) and
`defaultRegion` rather than threading the inbound request's identity. The caller is
account `000000000000`, so `AWS::EC2::Instance`, `AWS::EC2::LaunchTemplate`,
`AWS::ECS::Cluster` and `AWS::Logs::LogGroup` all report `CREATE_COMPLETE` with
plausible physical IDs that resolve nowhere in any region — `describe_instances`
raises `InvalidInstanceID.NotFound`, `list_clusters` returns empty. A direct
`run_instances` is visible immediately, so this is not general EC2 breakage; it is
the same partitioning that
[substrate#391](https://github.com/scttfrdmn/substrate/issues/391) covered for
regions. `AWS::IAM::Role` and `AWS::IAM::InstanceProfile` cross the boundary
intact because IAM is global, which is what makes the split look like missing
resource handlers rather than one misplaced struct — the handlers exist, and
`deployEC2Instance` really does dispatch `RunInstances`.

`bastion.yml` needs `AWS::EC2::Instance` and `AWS::EC2::LaunchTemplate`, so
`DetachedMode` gets a stack it cannot then query. Relatedly, `delete_stack` removes
the stack record — `describe_stacks` correctly raises `ValidationError` afterwards —
but not the stack's resources: an S3 bucket and an IAM role both outlive their
stack. A teardown assertion that only checks the stack is gone passes for the wrong
reason.

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
| IAM served no service-role managed policies — `get_policy` on `arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore` raised `NoSuchEntity` for an ARN `attach_role_policy` had just accepted | All five this project attaches resolve, with documents and version IDs | [substrate#484](https://github.com/scttfrdmn/substrate/issues/484) |
| The instance-type catalog held eight hardcoded types (`m5.xlarge` absent), an unknown type returned HTTP 200 + an empty list, and `describe_instance_type_offerings` ignored the `instance-type` filter entirely — so an offerings-based availability assertion could not fail | Every type this project names resolves, unknown types raise `InvalidInstanceType`, and the offerings filter discriminates | [substrate#485](https://github.com/scttfrdmn/substrate/issues/485) |

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

# Testing with substrate

This guide explains how to test the Parsl Ephemeral Provider against
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

`/health` reports the real version since 0.85.0 — `{"status":"ok","version":"v0.88.0"}` —
so `make substrate-status` is enough to confirm the pin. Released images used to
report `"version":"dev"` whatever their tag
([substrate#402](https://github.com/scttfrdmn/substrate/issues/402)); against an
older image, use
`podman image inspect ghcr.io/scttfrdmn/substrate:0.88.0 --format '{{.Digest}}'`
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

**Put the random part first, not last.** The provider truncates the IDs it embeds in
resource names — `parsl-bastion-{workflow_id[:8]}` (`modes/detached.py:458`),
`parsl-{ecs,lambda}-{job_id[:8]}` (`modes/serverless.py:564,717`),
`parsl-lambda-code-{provider_id[:8]}` (`:645`) — so `f"test-job-{uuid…}"` yields the
single name `test-job` for every test that uses it. #183 found four such collisions.
This is the failure mode moto structurally hid: a fresh mock per test meant a
same-named resource never met its predecessor. It is also a real provider constraint
rather than a test artefact — two live workflows whose IDs share eight characters
collide on one stack.

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
from parsl_ephemeral_provider import EphemeralProvider

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

provider = EphemeralProvider(
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
`parsl_ephemeral_provider/utils/localstack.py`, shipped to users who had no use for it.

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

- `integration` — lives in `tests/integration/`. Every file there needs a running
  emulator as of #183; moto is gone from this directory. It is still a dependency,
  and still used by `tests/unit/`, which must stay container-free.
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

Verified by probing substrate `0.88.0` directly, not read off the milestones.
**`0.88.0` closed the four gaps that kept `ecs_worker.yml` on moto** — see the
resolved list below. What remains is three CloudFormation rows, none of them a
blocker: each is a hazard to what a *teardown or assertion* can be trusted to prove.
The EventBridge row is a gap this project turns to its advantage rather than one it
works around.

> [!IMPORTANT]
> **The pin stays on `0.88.0`, and bumping it is blocked on
> [substrate#560](https://github.com/scttfrdmn/substrate/issues/560).** `0.89.0`
> through `0.92.0` fix all three rows below — including both defects this repository
> filed, #544 and #545 — so the bump is wanted. It cannot land yet: substrate gives
> a resource with no explicit physical name **the logical ID verbatim**, and a
> logical ID is only unique within a stack while an IAM role name is account-global.
> So a second stack from the same template collides with `EntityAlreadyExists`, and
> 6 integration tests fail. `bastion.yml` (`BastionHostRole`) and `ecs_worker.yml`
> (`TaskRole`, `TaskExecutionRole`) all deliberately omit the name, which is the
> practice that makes a template deployable more than once.
>
> The create was *already* failing on `0.88.0` — `0.89.0`'s rollback is only what
> made it visible, since the stack previously still reported `CREATE_COMPLETE`. So
> these six tests pass on the current pin for the wrong reason, which is worth
> knowing before trusting them.
>
> Diagnosing it needed `DisableRollback=True` plus `DescribeStackResources`:
> `DescribeStackEvents` answers `UnsupportedOperation` ("substrate models stack
> status, not per-resource stack events"), so the failing resource is not reachable
> that way.
>
> **Re-evaluated against `0.92.0`.** Same 6 failures, and probed to be #560
> unchanged rather than a new cause — `0.92.0` switches on IAM enforcement for a
> stack's resource calls, so that had to be ruled out. With `DisableRollback=True`:
>
> ```
> BastionHostRole             CREATE_FAILED  EntityAlreadyExists: Role with name BastionHostRole already exists.
> BastionHostInstanceProfile  CREATE_FAILED  EntityAlreadyExists: Instance Profile BastionHostInstanceProfile already exists.
> ```
>
> The new enforcement does not reach this project: `0.92.0` documents `test`/`test`
> as resolving to no principal and therefore authorized against nothing, which is
> what `tests/conftest.py` uses.
>
> Bumping now also needs **a second substrate fix**, reachable only because #518 and
> #544 are fixed: `DeleteStack` deletes an `AWS::IAM::InstanceProfile` without
> removing its role first, so the delete is refused —
> `DeleteConflict: Cannot delete entity, must detach all roles first` — and the stack
> ends `DELETE_FAILED` with the profile standing under its logical-ID name. Note the
> inverted order in `DescribeStackResources`: `BastionHostRole` reaches
> `DELETE_COMPLETE` while the profile referencing it fails. So even with #560 fixed
> for roles, the profile alone would keep the collision. Not yet filed upstream.
>
> One unrelated gap found while probing, **pre-existing and not a `0.92.0`
> regression** (0.88.0 fails identically): `GetFunctionConfiguration` answers
> `InvalidAction: LambdaPlugin: unknown operation "Unknown"`, while `GetFunction`
> on the same path prefix works and carries the same `CodeSize`. This project calls
> neither, so it is recorded rather than blocking.

All three CloudFormation rows below are **fixed upstream and still live here**,
because the pin cannot move past `0.88.0` — see the note above. Each was re-probed
on both versions, so the "fixed" claim is measured rather than read off an issue:
on `0.92.0` a CFN-created bucket is deleted with its stack, `delete_stack` and
`describe_stacks` both resolve a stack ARN, and a CFN-deployed function reports its
true `CodeSize` (1,361 B for a zip that `0.88.0` reports as `0`).

| Gap | Effect | Upstream |
|---|---|---|
| `DeleteStack` does not delete the stack's resources | The stack record goes and `describe_stacks` then raises `ValidationError` correctly, but an S3 bucket and an IAM role both outlive their stack. A teardown assertion that only checks the stack passes for the wrong reason. Re-probed against `0.88.0`: a CFN-created bucket is still listed after `DeleteStack` | [substrate#518](https://github.com/scttfrdmn/substrate/issues/518) |
| Neither `DeleteStack` nor `DescribeStacks` resolves a stack **ARN**, only a name | `delete_stack(StackName=<arn>)` returns **HTTP 200 and leaves the stack standing**; `describe_stacks(StackName=<arn>)` raises `ValidationError … does not exist`. Real CloudFormation accepts either form. This bites teardown directly: `DetachedMode.cleanup_infrastructure()` deletes by `self.bastion_id`, which *is* an ARN, so bastion teardown silently no-ops and the next test in the file meets `AlreadyExists`. Found while moving `test_detached_mode_spot_fleet_integration.py` off moto in #183 | [substrate#544](https://github.com/scttfrdmn/substrate/issues/544) |
| A CFN-deployed `AWS::Lambda::Function` reports `CodeSize: 0` | A direct `create_function` reports the true size; only the CloudFormation path zeroes it, and `invoke` still returns 200. So `CodeSize` cannot be used to prove a stack staged real code — which is why `test_submit_lambda_job_stages_a_real_zip_in_s3` asserts on the `DescribeStacks` parameter echo instead. That is the better assertion regardless: #116's actual symptom was an unparseable parameter echo, not a wrong size | [substrate#545](https://github.com/scttfrdmn/substrate/issues/545) |
| EventBridge is not emulated; `PutRule` returns `501 service not emulated: awsevents` | The spot-warning notifier's degradation path is testable *because* of this. The warning path itself is covered end to end by wiring an SQS queue directly, since the monitor only ever reads warnings through SQS | — |

### Closed in `0.88.0`

All four were filed from this repository and each was re-probed against the image
before this list was written — a closed issue is not the same as a released fix, and
substrate cuts release commits, so a merged fix can sit on `main` past the newest
tag.

| Was | Now | Upstream |
|---|---|---|
| `Fn::Split` resolved to its first element only, so `ecs_worker.yml:208`'s `!Split [',', !Ref Command]` silently lost everything after the first item | An `Fn::Select` of index 2 over a three-element split returns the third element | [substrate#521](https://github.com/scttfrdmn/substrate/issues/521) |
| An intrinsic nested inside a structured property was never resolved — the same `Command`, nested in `ContainerDefinitions`, was not walked into | An `Fn::Sub` inside a `ContainerDefinitions` entry resolves | [substrate#526](https://github.com/scttfrdmn/substrate/issues/526) |
| A CFN-deployed task definition's `ContainerDefinitions` kept CloudFormation's PascalCase keys, so `describe_task_definition` returned `containerDefinitions: [{}]` | The container list parses to camelCase and reads back complete | [substrate#527](https://github.com/scttfrdmn/substrate/issues/527) |
| CloudWatch Logs read operations returned PascalCase members, so botocore parsed every field to `None` and a caller saw `[{}]` with HTTP 200 and no error | `describe_log_groups` and `get_log_events` round-trip correctly, which unblocks *verifying* `compute/ecs.py`'s log-group create (`:377`) and cleanup (`:829`) rather than asserting on a count that passed for the wrong reason. `PutRetentionPolicy` was noted on the same issue — check it before relying on it | [substrate#528](https://github.com/scttfrdmn/substrate/issues/528) |

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

`0.87.0` had two further defects behind that surface, both filed from here and both
fixed in **`0.87.1`**. They are worth keeping on record, because each one failed
*silently with a success status* — the shape of bug that a green suite hides:

- **YAML short-form intrinsics were not resolved**
  ([substrate#516](https://github.com/scttfrdmn/substrate/issues/516)) — `!Sub`,
  `!Ref`, `!If`, `!GetAtt` were stripped and the raw scalar used as a literal, while
  the `Fn::` long forms were correct. The cause sat upstream of the intrinsics
  engine, which is why the long forms were unaffected: `parseCFNTemplate` unmarshals
  with `go.yaml.in/yaml/v3`, which has no notion of the CloudFormation tag
  shorthands and so discarded the tag and kept the node value.
- **Resources were written to a different account than the caller read**
  ([substrate#517](https://github.com/scttfrdmn/substrate/issues/517)) —
  `StackDeployer.dispatch` synthesised its `RequestContext` from constants, so a
  stack ARN said `000000000000` while its contents lived in `123456789012`. EC2, ECS
  and Logs partition their state keys by account and region and so were unreachable;
  S3 and IAM do not and crossed the boundary invisibly, which is what made it look
  like the EC2 resource handlers were missing when they were not.

**The upshot for `bastion.yml` is that substrate is now ahead of moto, not behind
it.** The full template deploys against `0.87.1`: `AWS::EC2::Instance` resolves its
`ImageId` through `AWS::EC2::LaunchTemplate`, the instance is queryable with
`describe_instances`, and `Outputs` resolve — `BastionHostId` returns the real
`i-…`. moto cannot do this at all: its CloudFormation handler for
`AWS::EC2::Instance` reads `properties["ImageId"]` directly
(`moto/ec2/models/instances.py:400`) and resolves no launch template, so it raises
`KeyError: 'ImageId'` on a stack real CloudFormation accepts. That is why
`test_detached_mode_spot_fleet_integration.py` used to pass
`bastion_host_type="direct"`. It no longer does: #183 moved that file onto substrate
and onto the **default** `cloudformation` bastion path, so the tests now exercise
what users get rather than a fallback chosen to route around a simulator gap.

Two related fixes shipped in `0.87.1` alongside those, both found upstream while
fixing #516 and each worth knowing:

- A plugin's 4xx *response* with a nil Go error was neither an error nor a status
  ([substrate#519](https://github.com/scttfrdmn/substrate/issues/519)), so **every
  S3 and IAM resource failure in a stack was swallowed** and reported
  `CREATE_COMPLETE`. Now such a resource reports `CREATE_FAILED` — a behaviour
  change to be aware of when reading a stack status against `0.87.1` or later.
- `Default: ''` was treated as no default at all, which **inverted**
  `!Not [!Equals [!Ref X, '']]`. That idiom is how an optional parameter is spelled
  and appears 21 times across this project's five templates, so it would have bitten
  immediately once #516 landed.

Substrate still does not roll a failed stack back
([substrate#520](https://github.com/scttfrdmn/substrate/issues/520)).

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

These create billable resources. `parsl-ephemeral-cleanup --dry-run`
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

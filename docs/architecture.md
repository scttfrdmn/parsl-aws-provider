# Parsl Ephemeral AWS Provider Architecture

This document provides an overview of the architecture and design of the Parsl Ephemeral AWS Provider.

## Overview

The Parsl Ephemeral AWS Provider runs Parsl workflows on AWS compute that is
created when work arrives and destroyed when it finishes.

**Networking is not ephemeral.** Since v0.7.0 the VPC, subnet, and security group
are supplied by you and never created or deleted by the provider — see
[network-prerequisites.md](network-prerequisites.md). Ephemerality applies to
compute (instances, fleets, Lambda functions, ECS tasks) and to the launch
templates, IAM instance profiles, and CloudFormation stacks the provider creates
to run it.

## Key components

```
parsl_aws_provider/
├── provider.py                 # EphemeralAWSProvider — the Parsl interface
├── globus_compute.py           # GlobusComputeProvider subclass
├── constants.py                # AWS constants and defaults
├── exceptions.py               # Exception hierarchy
├── error_handling.py           # Retry/backoff framework (used by compute/)
├── modes/
│   ├── base.py                 # OperatingMode interface
│   ├── standard.py             # Direct client-to-worker
│   ├── detached.py             # Bastion orchestrates; client may disconnect
│   └── serverless.py           # Lambda / ECS-Fargate workers
├── compute/
│   ├── spot_fleet.py           # EC2 Fleet (migrated off Spot Fleet in v0.7.0)
│   ├── spot_fleet_cleanup.py
│   ├── spot_interruption.py    # EventBridge → SQS interruption warnings
│   ├── lambda_func.py
│   └── ecs.py
├── state/
│   ├── base.py                 # Keyed store interface
│   ├── file.py, s3.py, parameter_store.py
├── security/                   # Audit logging, credentials, policy helpers
├── config/security_config.py
├── utils/aws.py                # Session, AMI resolution, tagging, waiters
└── templates/cloudformation/   # bastion, ec2_worker, lambda_worker, ecs_worker
```

`network/`, `compute/ec2.py`, `utils/logging.py`, `security/encryption.py`, and
`templates/terraform/` are not reachable from any live path; they are scheduled
for removal in v0.8.0 ([#90](https://github.com/scttfrdmn/parsl-aws-provider/issues/90)).

## Operating modes

See [operating_modes.md](operating_modes.md) for configuration. In outline:

### Standard mode

The client communicates directly with workers over ZMQ. Suitable when the client
has a stable, reachable address — workers connect *outbound* to the interchange,
so the client must accept inbound TCP on the HTEX port range. A client behind NAT
cannot use this mode without port forwarding or a VPN; use detached mode instead.

1. `initialize()` creates a launch template (IMDSv2 required) and, if asked, an
   IAM instance profile.
2. Each `submit()` launches instances or an EC2 Fleet into your subnet.
3. Workers connect back to the interchange on the client.
4. `cancel()` and shutdown terminate instances and delete the launch template.

### Detached mode

A bastion instance runs an orchestrator loop and owns the worker lifecycle, so the
client can disconnect entirely. This suits long-running workflows and clients
behind NAT.

1. The client launches a bastion from a CloudFormation stack.
2. The bastion polls for pending jobs, launches workers, tracks status, and
   handles cancellations.
3. The client may disconnect; the workflow continues.
4. The bastion shuts down after `idle_timeout` minutes with no work.

The bastion is an autonomous orchestrator, not a network tunnel — which is why an
EC2 Instance Connect Endpoint cannot replace it
([#88](https://github.com/scttfrdmn/parsl-aws-provider/issues/88)).

### Serverless mode

Tasks run on Lambda or ECS/Fargate with no EC2 instances. Best for short,
sporadic tasks. Lambda workers run in the Lambda-managed VPC and therefore need
none of the three network IDs; ECS/Fargate needs a subnet and security group.

## Resource management

The provider creates and manages:

- **Compute**: EC2 instances, EC2 Fleets, Lambda functions, ECS tasks
- **Launch templates**: one per provider, carrying IMDSv2, shutdown behaviour,
  and the instance profile
- **IAM instance profile and role**: only when `auto_create_instance_profile=True`
  (note: not yet deleted on shutdown —
  [#132](https://github.com/scttfrdmn/parsl-aws-provider/issues/132))
- **CloudFormation stacks**: bastion (detached), Lambda and ECS workers
  (serverless)
- **State storage**: a local file, an S3 object, or an SSM parameter

It does **not** create or delete VPCs, subnets, or security groups.

EC2 resources are tagged `ParslResource=true` and `ParslWorkflowId=<provider_id>`
so anything left behind is findable. `tools/cleanup_aws_resources.py --dry-run`
reports orphans.

## State management

State persistence supports resource tracking across sessions, detached-mode
handoff, and cleanup after a crash.

Three backends, selected with `state_store_type`:

1. **`file`** (default) — local JSON, `fcntl` locked
2. **`s3`** — an S3 object; requires `s3_bucket`
3. **`parameter_store`** — an SSM parameter

All three are **keyed**: the provider writes under `"provider"` and the operating
mode under `"mode"`, so the two no longer overwrite each other's document (the
v0.6.0 defect that leaked baked AMIs and lost `job_map`). See
[state_persistence.md](state_persistence.md).

## Infrastructure as code

CloudFormation templates ship inside the wheel and are loaded with
`get_cf_template()`, not by filesystem path — a wheel install has no source tree.
The Terraform modules under `templates/terraform/` are referenced by nothing and
are slated for removal in v0.8.0.

## Error handling and recovery

- A custom exception hierarchy in `exceptions.py`
- Cleanup on initialization failure, scoped to resources the provider created
- Exponential backoff with jitter for transient AWS API errors, via
  `error_handling.py`

`error_handling.py` is used by the `compute/` managers. The `modes/` hand-roll
their own polling loops instead; keeping both is tracked as
[#91](https://github.com/scttfrdmn/parsl-aws-provider/issues/91).

## Security considerations

- Least-privilege IAM: `GlobusComputeProvider.minimum_iam_policy()` returns the
  actual action set the provider uses
- IMDSv2 required on every launch path, set in the launch template
- No long-lived credentials on instances — workers use an instance profile
- Resource isolation by provider ID in tags and state keys

Instance profiles created with `auto_create_instance_profile=True` are not deleted
on shutdown ([#132](https://github.com/scttfrdmn/parsl-aws-provider/issues/132));
supply `iam_instance_profile_arn` if you need to control that lifecycle yourself.

## Testing with a local AWS emulator

For testing AWS interactions without real AWS resources, the suite runs against
[substrate](https://github.com/scttfrdmn/substrate), a local AWS emulator. See
[substrate_testing.md](substrate_testing.md); it replaced LocalStack in #125.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors

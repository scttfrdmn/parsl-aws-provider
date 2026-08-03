# Security

## What the provider does for you

- **IMDSv2 is required on every launch.** `HttpTokens: "required"` is set in the
  launch template and on every direct `RunInstances` path, so the metadata service
  rejects the unauthenticated IMDSv1 `GET` — the request an SSRF-ed application
  can be tricked into making to read the instance's role credentials.
  `HttpEndpoint` stays enabled because the SSM agent needs it. The hop limit stays
  at EC2's default of 2, since a container's request costs a hop and a limit of 1
  would make metadata unreachable from inside one.
- **No long-lived credentials on instances.** Workers get an IAM instance profile;
  nothing writes access keys into user data.
- **No network resources are created or deleted.** Since v0.7.0 the VPC, subnet,
  and security group are yours. The provider cannot widen your network posture,
  and cannot delete a security group you supplied — a real hazard before v0.7.0,
  when `ServerlessMode.cleanup_infrastructure()` called `delete_security_group` on
  a user-supplied ID with no ownership check.
- **Every resource is tagged** `ParslResource=true` and
  `ParslWorkflowId=<provider_id>`, so an orphan is findable and attributable.
- **Instances terminate rather than stop.**
  `InstanceInitiatedShutdownBehavior="terminate"` means a finished worker leaves
  no billed EBS volume behind.

## Least-privilege IAM policy

The authoritative action set is generated from the code, so it cannot drift from
what the provider actually calls:

```python
import json

from parsl_ephemeral_provider import EphemeralComputeProvider

print(json.dumps(EphemeralComputeProvider.minimum_iam_policy(), indent=2))
print(
    json.dumps(EphemeralComputeProvider.minimum_iam_policy(include_ecr=True), indent=2)
)
```

This is a `@staticmethod` — you do not need to construct a provider to call it,
and it applies to `EphemeralProvider` just as much as to the Globus subclass.

It returns five statements: `SessionValidation`, `EC2Management`,
`SSMCommandsAndParameters`, `SpotInterruptionWarning`, and `IAMInstanceProfile`,
plus `ECRContainerImages` when `include_ecr=True`.

Note what it does **not** grant: no `ec2:CreateVpc`, `CreateSubnet`,
`CreateSecurityGroup`, `CreateNatGateway`, or `RequestSpotFleet`. Older versions
of this document asked for all of them. The provider creates no network resources
(#69) and uses `CreateFleet` rather than the legacy Spot Fleet API (#86). Nor any
Session Manager action — `ssm:StartSession` and its four companions were granted
for a tunnel this package does not have, and were removed in
[#195](https://github.com/scttfrdmn/parsl-ephemeral-provider/issues/195).

The IAM statement grants **both halves** of the instance-profile lifecycle. The
teardown grants are not symmetry for its own sake: `cleanup_infrastructure()`
deletes the role and profile it created (#132), and cleanup logs rather than
raises, so a policy granting only the creates leaks a standing privileged
principal on every run without ever reporting an error. That is exactly how 94
orphaned roles accumulated in a real account, and the policy reproduced it until
#195.

Every statement uses `Resource: "*"`. Most of these EC2 and IAM actions do not
support resource-level permissions, but you should scope what you can with
condition keys — `ec2:ResourceTag/ParslResource` on the terminate and tag actions,
and `iam:PassedToService: ec2.amazonaws.com` on `iam:PassRole`.

### Additional permissions by mode

**Detached mode** — the bastion runs from a CloudFormation stack:

```json
{
    "Effect": "Allow",
    "Action": [
        "cloudformation:CreateStack",
        "cloudformation:DescribeStacks",
        "cloudformation:DescribeStackEvents",
        "cloudformation:DeleteStack",
        "iam:PassRole"
    ],
    "Resource": "*"
}
```

**Serverless mode (Lambda)**:

```json
{
    "Effect": "Allow",
    "Action": [
        "lambda:CreateFunction",
        "lambda:InvokeFunction",
        "lambda:DeleteFunction",
        "lambda:GetFunction",
        "lambda:UpdateFunctionCode",
        "lambda:UpdateFunctionConfiguration",
        "iam:PassRole"
    ],
    "Resource": "*"
}
```

**Serverless mode (ECS/Fargate)**:

```json
{
    "Effect": "Allow",
    "Action": [
        "ecs:CreateCluster",
        "ecs:DeleteCluster",
        "ecs:RegisterTaskDefinition",
        "ecs:DeregisterTaskDefinition",
        "ecs:RunTask",
        "ecs:StopTask",
        "ecs:ListTasks",
        "ecs:DescribeTasks",
        "iam:PassRole"
    ],
    "Resource": "*"
}
```

**State backends** — Parameter Store:

```json
{
    "Effect": "Allow",
    "Action": [
        "ssm:PutParameter",
        "ssm:GetParameter",
        "ssm:DeleteParameter"
    ],
    "Resource": "arn:aws:ssm:*:*:parameter/parsl/*"
}
```

S3:

```json
{
    "Effect": "Allow",
    "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject", "s3:ListBucket"],
    "Resource": [
        "arn:aws:s3:::my-parsl-state-bucket",
        "arn:aws:s3:::my-parsl-state-bucket/*"
    ]
}
```

No `s3:CreateBucket` — the bucket must already exist.

## Instance profiles

### Worker instance profile

With `auto_create_instance_profile=True` the provider creates a role carrying
`AmazonSSMManagedInstanceCore` and nothing else. That is the minimum for SSM
`SendCommand`, which the warm-pool and one-shot paths need.

If your workload needs more — reading S3, writing CloudWatch Logs — create the
role yourself and pass `iam_instance_profile_arn`:

```python
provider = EphemeralProvider(
    # ... network and compute options ...
    iam_instance_profile_arn="arn:aws:iam::123456789012:instance-profile/parsl-worker",
)
```

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
            "Resource": [
                "arn:aws:s3:::your-data-bucket",
                "arn:aws:s3:::your-data-bucket/*"
            ]
        },
        {
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents"
            ],
            "Resource": "arn:aws:logs:*:*:*"
        }
    ]
}
```

Attach `AmazonSSMManagedInstanceCore` as well if you use the warm pool or one-shot
mode.

**A role created with `auto_create_instance_profile=True` is deleted on shutdown**
since v0.8.0 ([#132](https://github.com/scttfrdmn/parsl-ephemeral-provider/issues/132)),
along with its instance profile. Deletion is gated on ownership, so a profile you
supplied through `iam_instance_profile_arn` is never deleted. Your policy must
grant the teardown actions listed above — cleanup logs rather than raises, so
without them the roles accumulate silently
([#195](https://github.com/scttfrdmn/parsl-ephemeral-provider/issues/195)).

### Bastion instance profile (detached mode)

The CloudFormation template creates this role, carrying
`AmazonSSMManagedInstanceCore` plus EC2 and Parameter Store access — the bastion
launches and terminates workers itself, so it needs `RunInstances`,
`TerminateInstances`, `DescribeInstances`, `CreateTags`, and read/write on
`/parsl/*` parameters. The bastion is a compute host with real authority; treat
its role as the most sensitive part of the deployment.

## Network security

The provider requires an existing VPC, subnet, and security group — there is no
`create_vpc` option, and passing one raises `ProviderConfigurationError`. See
[network-prerequisites.md](network-prerequisites.md) for the required rules.

### Scope the security group tightly

Workers need outbound to reach the interchange, AWS APIs, and any package index
`worker_init` uses. Inbound is where the exposure is.

```python
import boto3

ec2 = boto3.client("ec2", region_name="us-east-1")
sg = ec2.create_security_group(
    GroupName="parsl-workers",
    Description="Parsl ephemeral workers",
    VpcId="vpc-0123456789abcdef0",
)["GroupId"]

# Workers dial the interchange outbound, so no inbound rule is needed for Parsl
# itself. Add one only for a client that must reach the workers directly.
ec2.authorize_security_group_ingress(
    GroupId=sg,
    IpPermissions=[
        {
            "IpProtocol": "tcp",
            "FromPort": 54000,
            "ToPort": 55000,
            "IpRanges": [{"CidrIp": "203.0.113.10/32"}],  # your client, /32
        }
    ],
)

provider = EphemeralProvider(
    region="us-east-1",
    vpc_id="vpc-0123456789abcdef0",
    subnet_id="subnet-0123456789abcdef0",
    security_group_id=sg,
)
```

No SSH rule appears above deliberately. Reach instances with SSM Session Manager
instead:

```bash
aws ssm start-session --target i-0123456789abcdef0
```

That needs no inbound rule, no key pair, and no public IP, and every session is
logged in CloudTrail. `key_name` remains available if you want SSH, but it is not
required for access.

### Private subnets

```python
provider = EphemeralProvider(
    # ... other options ...
    subnet_id="subnet-private0123456789",
    use_public_ips=False,
)
```

Instances then need a NAT gateway, or VPC endpoints for `ssm`, `ssmmessages`,
`ec2messages`, and `s3`, to reach AWS APIs and register with SSM. The provider
does not create either.

## Data security

### Encryption in transit

Traffic to AWS APIs is HTTPS. Worker-to-interchange ZMQ traffic is a different
matter: **set `encrypted=False` on `HighThroughputExecutor` for now**. Parsl's
CurveZMQ certificates are generated in the client's `run_dir`, which workers
cannot read, so leaving encryption on makes workers fail to register. Same-VPC
deployments rely on VPC isolation; certificate distribution for cross-VPC and
internet paths is
[#62](https://github.com/scttfrdmn/parsl-ephemeral-provider/issues/62).

### Encryption at rest

**S3 state store** — configure default encryption on the bucket itself. There is
no provider option for this; the provider does not create the bucket, so bucket
policy is where it belongs:

```bash
aws s3api put-bucket-encryption --bucket my-parsl-state-bucket \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
```

**Parameter Store** — `ParameterStoreState` supports `SecureString` via
`use_secure_string=True`, but the provider always constructs it with the default
`False` and exposes no option. To use it, replace the store after construction and
before the first `submit()`:

```python
from parsl_ephemeral_provider.state.parameter_store import ParameterStoreState

provider.state_store = ParameterStoreState(
    provider=provider,
    prefix="/parsl/my-workflow",
    use_secure_string=True,
)
```

State documents hold resource IDs and job commands, not credentials — assess
whether that warrants `SecureString` for your environment.

**EBS volumes** — there is no `block_device_mappings` option. Set
[EBS encryption by default](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/EBSEncryption.html#encryption-by-default)
for the account and region, which covers every volume the provider launches
without needing an option at all.

### Credentials

Never put credentials in code. The provider takes no `aws_access_key_id` or
`aws_secret_access_key` — it resolves credentials through botocore, so
environment variables, `~/.aws/credentials`, and instance profiles all work.
Select a named profile with `profile_name`:

```python
provider = EphemeralProvider(
    profile_name="parsl-profile",
    # ... other options ...
)
```

Prefer an instance profile or IAM Identity Center over static keys. If you must
use keys, rotate them on your usual schedule.

## Auditing and monitoring

### Tagging

```python
provider = EphemeralProvider(
    # ... other options ...
    additional_tags={
        "Project": "MyDataScience",
        "Environment": "Development",
        "Owner": "someone@example.com",
        "CostCenter": "12345",
    },
)
```

The option is `additional_tags`, not `tags`. These are applied alongside
`ParslResource` and `ParslWorkflowId`, so cost allocation and orphan sweeps both
work.

### Audit logging

`parsl_ephemeral_provider.security.audit` provides `AuditLogger` and
`SecurityMonitor`. `ParameterStoreState` emits `STATE_ACCESS` events when the
provider has an `audit_logger` attribute — but the provider takes no such
constructor argument and sets no such attribute, so you must attach one yourself:

```python
from parsl_ephemeral_provider.security.audit import AuditLogger

provider.audit_logger = AuditLogger(log_file="parsl-audit.jsonl")
```

Only the Parameter Store backend consults it today. Broader adoption is not yet
implemented.

### CloudTrail and CloudWatch

- **CloudTrail** records every API call the provider makes, including each
  `RunInstances`, `SendCommand`, and IAM role creation and deletion. This is your
  real audit trail.
- **Budgets and CloudWatch alarms** on EC2 spend are worth setting up: the
  provider has no cost controls of its own beyond `max_blocks`, `auto_shutdown`,
  and the warm-pool cap. Reclaiming idle-but-running instances is Parsl's
  `max_idletime`, not a provider setting.
- **CloudWatch Logs** hold cloud-init output only if you configure the agent in
  `worker_init`; the provider does not install it.

## Compliance

If your workflows touch regulated data:

- **VPC endpoints** for `ssm`, `ssmmessages`, `ec2messages`, `s3`, and `logs` keep
  traffic off the public internet. Create them yourself — there is no provider
  option.
- **AWS Config** rules can assert that launched instances match your baseline.
- **Security Hub** aggregates findings across accounts.
- Note the default AMI is a public Amazon Linux 2023 image resolved from SSM. If
  you need a hardened or approved base, pass `image_id` explicitly, or use
  `bake_ami` with `worker_init` doing the hardening.

## Emergency shutdown

To tear down everything a provider owns:

```python
provider.cleanup_all()  # terminate compute, keep the provider usable
provider.shutdown()  # ...and delete the launch template, AMI, and state
```

Neither takes a `force` argument. To reach the resources of a workflow started by
another process, construct a provider against the same state location — the
persisted `provider_id` is adopted automatically:

```python
provider = EphemeralProvider(
    mode="detached",
    region="us-east-1",
    vpc_id="vpc-0123456789abcdef0",
    subnet_id="subnet-0123456789abcdef0",
    security_group_id="sg-0123456789abcdef0",
    state_store_type="parameter_store",
    parameter_store_path="/parsl/compromised-workflow",
)
provider.shutdown()
```

If the state is gone, sweep by tag instead:

```bash
parsl-ephemeral-cleanup --region us-east-1            # --dry-run first
```

## Reporting a vulnerability

Open a
[security advisory](https://github.com/scttfrdmn/parsl-ephemeral-provider/security/advisories/new)
rather than a public issue.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors

# Security Policy

## Supported Versions

The following versions of Parsl Ephemeral Provider are currently being supported with security updates:

| Version | Supported          |
| ------- | ------------------ |
| 0.8.x   | :white_check_mark: |
| < 0.8   | :x:                |

The project is pre-1.0 and alpha: only the latest minor line receives security
fixes, and there are no backports. Nothing has been published to PyPI yet
([#180](https://github.com/scttfrdmn/parsl-ephemeral-provider/issues/180)), so every
existing install is from source.

## Reporting a Vulnerability

We take the security of the Parsl Ephemeral Provider seriously. If you believe you've found a security vulnerability, please follow these steps:

1. **Do not disclose the vulnerability publicly** on the issue tracker, mailing lists, or social media.

2. **Report it privately through GitHub**, using
   [Report a vulnerability](https://github.com/scttfrdmn/parsl-ephemeral-provider/security/advisories/new)
   under the repository's Security tab. That opens a private advisory visible only
   to you and the maintainers. This replaces the previous "email the core
   maintainers" instruction, which named no address and so left reporters with no
   usable channel. Include:
   - A description of the vulnerability
   - Steps to reproduce the issue
   - Potential impact of the vulnerability
   - Any potential solutions you've identified

3. **Allow time for response and assessment**. This is a single-maintainer
   project, so expect acknowledgement within a week rather than within hours.

4. **Maintain confidentiality** until the vulnerability is fixed and announced. We will work with you to ensure proper credit for the discovery.

## Security Best Practices

When using the Parsl Ephemeral Provider, follow these security best practices:

### AWS IAM Permissions

- Use the principle of least privilege when configuring IAM permissions
- Create specific IAM roles for the provider with only the necessary permissions
- Regularly audit and rotate credentials
- Consider using IAM instance profiles instead of hardcoded credentials

### Network Configuration

The provider creates no VPC, subnet, or security group, and deletes none — you
pre-provision all three and pass their IDs. That makes the network your
responsibility rather than a default it picks for you:

- Limit inbound rules to what the workers need. HTEX workers connect *outbound*
  to the interchange, so they generally need no inbound rule at all
- Omit `key_name` so no SSH key is installed, and reach instances over SSM
  Session Manager instead — IAM-authorized and CloudTrail-logged, unlike SSH
- Use private subnets for worker nodes when possible, with `use_public_ips=False`
- Enable VPC flow logs for network monitoring

### Resource Isolation

- Use separate AWS accounts for development, testing, and production
- Tag all resources for tracking and auditing
- Enable AWS CloudTrail for auditing API calls
- Consider using AWS Organizations for centralized management

### Data Protection

- Use encryption for data at rest and in transit
- Be careful when storing credentials in configuration files
- Avoid hardcoding secrets in worker initialization scripts
- Use AWS Secrets Manager or Parameter Store for sensitive information

### Template Security

The provider deploys CloudFormation stacks for the detached-mode bastion and for
the serverless Lambda and ECS workers. Those are the only templates it ships: the
unused Terraform modules were removed in v0.9.0 (#90), so there is no longer a
second set of infrastructure definitions to review or keep current.

- Review the CloudFormation templates before running a mode that deploys them
- Use AWS CloudFormation Guard or other policy-as-code tools
- Keep infrastructure-as-code templates under version control

## Security Updates

Security updates are announced through:

1. GitHub security advisories
2. The `Security` section of `CHANGELOG.md`

## Security-related Configuration

Every option below is real. The provider rejects unknown keyword arguments
outright rather than ignoring them, so a misspelling raises
`ProviderConfigurationError` at construction — an option accepted but never read
would be a silent false assurance.

```python
provider = EphemeralProvider(
    # Network: pre-provisioned by you, and never modified by the provider.
    # It creates no VPC, subnet, or security group, and deletes none.
    vpc_id="vpc-...",
    subnet_id="subnet-...",  # a private subnet, if your workers can use one
    security_group_id="sg-...",
    use_public_ips=False,  # no public IP; reach instances over SSM instead
    # Credentials on the instance: an instance profile, never static keys.
    # Supply your own ARN, or let the provider create a least-privilege role
    # carrying only AmazonSSMManagedInstanceCore. A profile it created is
    # deleted on shutdown; one you supplied is never touched.
    auto_create_instance_profile=True,
    # No SSH. Omitting key_name launches instances with no key pair, so the
    # only access path is SSM Session Manager, which is IAM-authorized and
    # CloudTrail-logged. Set key_name only if you need SSH.
    # Terminate rather than stop, so no EBS volume outlives the work.
    auto_shutdown=True,
    # Tags, for attribution and for the orphan sweep to find leftovers.
    additional_tags={
        "Environment": "Production",
        "SecurityContact": "security@example.com",
    },
)
```

Two options deliberately absent from that list:

- **EBS encryption** is not a provider option. Enable
  [EC2 encryption by default](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/EBSEncryption.html#encryption-by-default)
  at the account level, which covers every volume regardless of what launches it.
- **State-store encryption** depends on the backend, not on a flag here. The S3
  and Parameter Store backends inherit the bucket's or parameter's encryption; the
  file backend writes plaintext JSON to local disk. Prefer S3 or Parameter Store
  when the state is sensitive — see `docs/state_persistence.md`.

## Vulnerability Disclosure

The intended sequence is: report received and confirmed → scope assessed → fix
developed and tested → release → public disclosure via a GitHub security
advisory.

No day-by-day schedule is promised. This is a single-maintainer alpha project,
and a calendar that cannot be met is worse than none. If you need a disclosure
deadline for your own process, say so in the report and it will be agreed
explicitly.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors

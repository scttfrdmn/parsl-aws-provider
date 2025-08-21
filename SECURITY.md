# Security Policy

## Supported Versions

The following versions of Parsl Ephemeral AWS Provider are currently being supported with security updates:

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

## Reporting a Vulnerability

We take the security of the Parsl Ephemeral AWS Provider seriously. If you believe you've found a security vulnerability, please follow these steps:

1. **Do not disclose the vulnerability publicly** on the issue tracker, mailing lists, or social media.

2. **Email the core maintainers** directly with details of the issue. Include the following information:
   - A description of the vulnerability
   - Steps to reproduce the issue
   - Potential impact of the vulnerability
   - Any potential solutions you've identified

3. **Allow time for response and assessment**. The maintainers will acknowledge your report within 48 hours and provide an estimated timeline for a fix.

4. **Maintain confidentiality** until the vulnerability is fixed and announced. We will work with you to ensure proper credit for the discovery.

## Security Best Practices

When using the Parsl Ephemeral AWS Provider, follow these security best practices:

### AWS IAM Permissions

- Use the principle of least privilege when configuring IAM permissions
- Create specific IAM roles for the provider with only the necessary permissions
- Regularly audit and rotate credentials
- Consider using IAM instance profiles instead of hardcoded credentials

### Network Configuration

- Limit inbound security group rules to only necessary ports
- Use private subnets for worker nodes when possible
- Configure security groups to allow only necessary traffic
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

- Review CloudFormation and Terraform templates for security issues
- Use AWS CloudFormation Guard or other policy-as-code tools
- Validate templates before deployment
- Keep infrastructure-as-code templates under version control

## Security Updates

Security updates will be announced through:

1. GitHub security advisories
2. Release notes
3. Direct notification to users who have starred/watched the repository

## Security-related Configuration

The Parsl Ephemeral AWS Provider includes several security-focused configuration options:

```python
provider = EphemeralAWSProvider(
    # Security-related configuration options
    use_public_ips=False,              # Use only private IPs when possible
    encrypt_storage=True,              # Encrypt EBS volumes
    enable_detailed_monitoring=True,   # Enable detailed CloudWatch monitoring
    security_group_rules=[             # Customize security group rules
        {
            'IpProtocol': 'tcp',
            'FromPort': 22,
            'ToPort': 22,
            'IpRanges': [{'CidrIp': '10.0.0.0/16'}]
        }
    ],
    tags={                             # Tag resources for security tracking
        'Environment': 'Production',
        'SecurityContact': 'security@example.com'
    }
)
```

## Vulnerability Disclosure Timeline

Our typical vulnerability disclosure timeline is:

1. **Day 0**: Report received, issue confirmed
2. **Day 1-2**: Scope assessment and remediation planning
3. **Day 3-14**: Fix development and testing
4. **Day 15-21**: Release preparation
5. **Day 22**: Fix released
6. **Day 23+**: Public disclosure

This timeline may vary based on the severity and complexity of the issue.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors

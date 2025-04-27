# Troubleshooting Guide

This document provides solutions for common issues encountered when using the Parsl Ephemeral AWS Provider.

## General Troubleshooting Steps

When encountering issues with the Parsl Ephemeral AWS Provider, follow these general steps:

1. **Check logs**: Enable detailed logging to see what's happening
2. **Verify AWS credentials**: Ensure your AWS credentials are valid and have necessary permissions
3. **Check AWS resource limits**: Verify you haven't hit AWS service limits
4. **Examine network connectivity**: Ensure network connectivity between client and AWS resources
5. **Review security groups**: Check security group rules allow required traffic

## Enabling Detailed Logging

For troubleshooting, increase the log level to get more detailed output:

```python
import logging
from parsl_ephemeral_aws.utils.logging import configure_logger

# Set up detailed logging
configure_logger(
    level=logging.DEBUG,
    file_path='parsl_aws_debug.log',
    include_boto3=True
)
```

## Common Issues and Solutions

### Connection Issues

#### Worker Nodes Can't Connect to Client

**Symptoms:**
- Tasks get submitted but never complete
- Log errors about connection timeouts or refused connections

**Possible Causes:**
1. Client behind NAT/firewall without proper port forwarding
2. Security groups not properly configured
3. Network connectivity issues between client and worker nodes

**Solutions:**
1. Use detached mode with a bastion host:
   ```python
   provider = EphemeralAWSProvider(
       image_id='ami-12345678',
       region='us-west-2',
       mode='detached',
       bastion_instance_type='t3.micro'
   )
   ```

2. Ensure security groups allow traffic on Parsl ports:
   ```python
   provider = EphemeralAWSProvider(
       image_id='ami-12345678',
       region='us-west-2',
       security_group_ingress=[
           {'ip_protocol': 'tcp', 'from_port': 54000, 'to_port': 55000, 'cidr_blocks': ['0.0.0.0/0']}
       ]
   )
   ```

3. For clients with dynamic IP addresses, use the SSH tunnel option:
   ```python
   provider = EphemeralAWSProvider(
       image_id='ami-12345678',
       region='us-west-2',
       use_ssh_tunnel=True,
       ssh_port=2222
   )
   ```

#### SSL Certificate Validation Errors

**Symptoms:**
- SSL/TLS certificate validation errors in logs
- Connection errors with security/certificate messages

**Solutions:**
1. Ensure time synchronization on all machines:
   ```python
   provider = EphemeralAWSProvider(
       image_id='ami-12345678',
       region='us-west-2',
       worker_init='''
           # Ensure time synchronization
           sudo yum install -y chrony
           sudo systemctl start chronyd
           sudo systemctl enable chronyd
       '''
   )
   ```

2. If needed, disable certificate verification (not recommended for production):
   ```python
   provider = EphemeralAWSProvider(
       image_id='ami-12345678',
       region='us-west-2',
       verify_ssl=False
   )
   ```

### Authentication and Permissions Issues

#### AWS Credential Errors

**Symptoms:**
- "Unable to locate credentials" errors
- "Access denied" errors when accessing AWS resources

**Solutions:**
1. Explicitly provide credentials in provider configuration:
   ```python
   provider = EphemeralAWSProvider(
       image_id='ami-12345678',
       region='us-west-2',
       aws_access_key_id='YOUR_ACCESS_KEY',
       aws_secret_access_key='YOUR_SECRET_KEY'
   )
   ```

2. Use a named profile from AWS config:
   ```python
   provider = EphemeralAWSProvider(
       image_id='ami-12345678',
       region='us-west-2',
       aws_profile='my-profile'
   )
   ```

3. For EC2 instances, ensure instance profile has necessary permissions:
   ```python
   provider = EphemeralAWSProvider(
       image_id='ami-12345678',
       region='us-west-2',
       use_instance_profile=True
   )
   ```

#### Insufficient IAM Permissions

**Symptoms:**
- "AccessDenied" or "UnauthorizedOperation" errors in logs
- Resources fail to create with permission errors

**Solution:**
1. Use the iam:PassRole permission checker utility:
   ```python
   from parsl_ephemeral_aws.utils.aws import check_iam_permissions

   # Check required permissions
   missing_permissions = check_iam_permissions([
       'ec2:RunInstances',
       'ec2:CreateTags',
       'ec2:CreateVpc',
       'ssm:PutParameter'
   ])

   if missing_permissions:
       print(f"Missing required permissions: {missing_permissions}")
       print("Please add these permissions to your IAM policy")
   ```

2. Apply the recommended IAM policy from the documentation

### Resource Creation Issues

#### VPC Creation Fails

**Symptoms:**
- Errors creating VPC or associated resources
- Timeouts waiting for resources to become available

**Solutions:**
1. Use an existing VPC instead of creating a new one:
   ```python
   provider = EphemeralAWSProvider(
       image_id='ami-12345678',
       region='us-west-2',
       use_existing_vpc=True,
       vpc_id='vpc-12345678',
       subnet_id='subnet-12345678',
       security_group_id='sg-12345678'
   )
   ```

2. Check if you've hit AWS service limits and request an increase if needed

#### EC2 Instance Launch Failures

**Symptoms:**
- Instances fail to launch or terminate immediately after launching
- "Insufficient capacity" errors

**Solutions:**
1. Try different instance types or availability zones:
   ```python
   provider = EphemeralAWSProvider(
       image_id='ami-12345678',
       region='us-west-2',
       instance_types=[
           {'type': 't3.medium', 'weight': 1},
           {'type': 'm5.large', 'weight': 1},
           {'type': 'c5.large', 'weight': 1}
       ],
       availability_zones=['us-west-2a', 'us-west-2b', 'us-west-2c']
   )
   ```

2. Use the capacity-optimized allocation strategy:
   ```python
   provider = EphemeralAWSProvider(
       image_id='ami-12345678',
       region='us-west-2',
       use_spot_instances=True,
       spot_allocation_strategy='capacity-optimized'
   )
   ```

### Spot Instance Issues

#### Frequent Spot Interruptions

**Symptoms:**
- Spot instances are frequently interrupted
- Tasks fail to complete due to instance termination

**Solutions:**
1. Use instance types with lower interruption rates:
   ```python
   provider = EphemeralAWSProvider(
       image_id='ami-12345678',
       region='us-west-2',
       use_spot_instances=True,
       # Use instance types with historically lower interruption rates
       instance_types=[
           {'type': 'c5.large', 'weight': 1},
           {'type': 'm5.large', 'weight': 1},
           {'type': 'r5.large', 'weight': 1}
       ]
   )
   ```

2. Configure interruption behavior to hibernate instead of terminate:
   ```python
   provider = EphemeralAWSProvider(
       image_id='ami-12345678',
       region='us-west-2',
       use_spot_instances=True,
       spot_interruption_behavior='hibernate',
       state_store='s3'  # Required for hibernation state
   )
   ```

3. Use a mix of spot and on-demand instances:
   ```python
   provider = EphemeralAWSProvider(
       image_id='ami-12345678',
       region='us-west-2',
       use_spot_instances=True,
       on_demand_percentage=20  # Keep 20% of instances as on-demand
   )
   ```

#### High Spot Prices

**Symptoms:**
- Spot requests not being fulfilled
- Spot prices exceeding your maximum bid

**Solutions:**
1. Adjust your maximum price or use on-demand instances:
   ```python
   provider = EphemeralAWSProvider(
       image_id='ami-12345678',
       region='us-west-2',
       use_spot_instances=True,
       spot_max_price_percentage=100,  # Up to on-demand price
       fallback_to_on_demand=True      # Use on-demand if spot unavailable
   )
   ```

2. Try alternative regions with lower spot prices:
   ```python
   provider = EphemeralAWSProvider(
       image_id='ami-12345678',
       region='us-east-1',  # Try a different region
       use_spot_instances=True
   )
   ```

### Task Execution Issues

#### Long Startup Times

**Symptoms:**
- Tasks take a long time to start running
- Workers spend excessive time in initialization

**Solutions:**
1. Use a custom AMI with pre-installed dependencies:
   ```python
   provider = EphemeralAWSProvider(
       image_id='ami-customized-12345678',  # Custom AMI with dependencies
       region='us-west-2'
   )
   ```

2. Use containers for faster startup:
   ```python
   provider = EphemeralAWSProvider(
       region='us-west-2',
       mode='serverless',
       worker_type='ecs',
       ecs_container_image='your-container-image:latest'
   )
   ```

3. Optimize worker initialization script:
   ```python
   provider = EphemeralAWSProvider(
       image_id='ami-12345678',
       region='us-west-2',
       worker_init='''
           # Use package caching
           mkdir -p ~/.pip
           echo "[global]" > ~/.pip/pip.conf
           echo "cache-dir=/tmp/pip-cache" >> ~/.pip/pip.conf
           
           # Install only what's needed
           pip install --no-cache-dir numpy
       '''
   )
   ```

#### Out of Memory Errors

**Symptoms:**
- Workers terminate unexpectedly
- "Out of memory" errors in logs

**Solutions:**
1. Use larger instance types:
   ```python
   provider = EphemeralAWSProvider(
       image_id='ami-12345678',
       region='us-west-2',
       instance_type='r5.2xlarge'  # Memory-optimized instance
   )
   ```

2. Configure swap space in worker initialization:
   ```python
   provider = EphemeralAWSProvider(
       image_id='ami-12345678',
       region='us-west-2',
       worker_init='''
           # Create swap space
           sudo dd if=/dev/zero of=/swapfile bs=1M count=4096
           sudo chmod 600 /swapfile
           sudo mkswap /swapfile
           sudo swapon /swapfile
           echo '/swapfile swap swap defaults 0 0' | sudo tee -a /etc/fstab
       '''
   )
   ```

### State Persistence Issues

#### Failed to Save/Load State

**Symptoms:**
- Errors about state persistence
- Failure to restore from previous state

**Solutions:**
1. Check permissions for the state storage:
   ```python
   provider = EphemeralAWSProvider(
       image_id='ami-12345678',
       region='us-west-2',
       state_store='parameter_store',
       # Verify IAM permissions include:
       # - ssm:PutParameter
       # - ssm:GetParameter
       # - ssm:DeleteParameter
   )
   ```

2. Try a different state store:
   ```python
   provider = EphemeralAWSProvider(
       image_id='ami-12345678',
       region='us-west-2',
       state_store='s3',
       state_bucket='my-parsl-state'
   )
   ```

3. For debugging, use file-based state:
   ```python
   provider = EphemeralAWSProvider(
       image_id='ami-12345678',
       region='us-west-2',
       state_store='file',
       state_directory='/path/to/state/directory'
   )
   ```

### Cost Management Issues

#### Unexpected High Costs

**Symptoms:**
- AWS bill higher than expected
- Resources remain running after workflows complete

**Solutions:**
1. Enable auto-shutdown and budget controls:
   ```python
   provider = EphemeralAWSProvider(
       image_id='ami-12345678',
       region='us-west-2',
       auto_shutdown=True,
       idle_timeout_minutes=30,
       max_cost_per_hour=5.0,
       enable_cost_monitoring=True
   )
   ```

2. Explicitly call shutdown at the end of your workflow:
   ```python
   # At the end of your workflow
   provider.shutdown()
   ```

3. Set up AWS Budget alerts in your AWS account

4. Use resource tagging for cost tracking:
   ```python
   provider = EphemeralAWSProvider(
       image_id='ami-12345678',
       region='us-west-2',
       tags={
           'Project': 'research-project-123',
           'CostCenter': 'cc-456'
       }
   )
   ```

## Diagnosing with Logging

### Capturing Comprehensive Logs

For advanced troubleshooting, enable comprehensive logging:

```python
import logging
from parsl_ephemeral_aws.utils.logging import configure_logger

# Set up comprehensive logging
configure_logger(
    level=logging.DEBUG,
    file_path='parsl_aws_debug.log',
    include_boto3=True,
    log_format='[%(asctime)s] [%(name)s] [%(levelname)s] [%(filename)s:%(lineno)d] %(message)s'
)
```

### Log Analysis Examples

Here are some common log patterns and what they indicate:

1. **Network connectivity issues**:
   ```
   [2025-01-15 10:15:23] [parsl] [ERROR] Cannot connect to worker at 10.0.1.5:54321
   ```
   - Check security groups and network routes

2. **AWS API throttling**:
   ```
   [2025-01-15 10:20:15] [botocore.client] [WARNING] API call reached max retries: client.describe_instances()
   ```
   - Reduce API call frequency or implement exponential backoff

3. **Instance startup failures**:
   ```
   [2025-01-15 10:25:45] [parsl_ephemeral_aws.compute.ec2] [ERROR] Instance i-1234abcd failed to reach running state
   ```
   - Check AMI validity, instance type availability, or quota limits

## Getting Support

If you're unable to resolve an issue using this guide:

1. **Open an issue** on the GitHub repository with:
   - Detailed description of the problem
   - Relevant log snippets
   - Your provider configuration (with sensitive information removed)
   - Steps to reproduce the issue

2. **Join the Parsl Slack channel** for community support

3. **Check AWS Service Health Dashboard** for AWS service disruptions

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
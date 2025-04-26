# Getting Started with Parsl Ephemeral AWS Provider

This guide will help you get started with using the Parsl Ephemeral AWS Provider for your workflows.

## Installation

Install the provider using pip:

```bash
pip install parsl-ephemeral-aws
```

## Prerequisites

1. **AWS Credentials**: You need valid AWS credentials with permissions to create and manage EC2 instances, VPCs, and other AWS resources. You can provide these credentials in several ways:
   - Environment variables (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`)
   - AWS credentials file (`~/.aws/credentials`)
   - IAM instance profile (if running on EC2)

2. **AMI Selection**: You need an Amazon Machine Image (AMI) that has Python installed and is compatible with your workflow. The provider defaults to using Amazon Linux 2 AMIs, but you can specify any AMI.

3. **Networking**: You need to understand your networking requirements, especially if you have specific VPC or subnet requirements.

## Basic Configuration

Here's a minimal example of how to configure Parsl to use the Ephemeral AWS Provider:

```python
import parsl
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
from parsl_ephemeral_aws import EphemeralAWSProvider

# Configure the ephemeral AWS provider
provider = EphemeralAWSProvider(
    image_id='ami-12345678',  # Replace with a valid AMI ID
    instance_type='t3.medium',
    region='us-west-2',
    init_blocks=1,
    max_blocks=10
)

# Create Parsl configuration
config = Config(
    executors=[
        HighThroughputExecutor(
            label='aws_executor',
            provider=provider,
        )
    ]
)

# Load the configuration
parsl.load(config)
```

## Operating Modes

### Standard Mode

This is the default mode where your client directly communicates with worker nodes.

```python
provider = EphemeralAWSProvider(
    image_id='ami-12345678',
    instance_type='t3.medium',
    region='us-west-2',
    mode='standard'
)
```

### Detached Mode

In this mode, a bastion host manages workers, allowing your client to disconnect.

```python
provider = EphemeralAWSProvider(
    image_id='ami-12345678',
    instance_type='m5.large',
    region='us-west-2',
    mode='detached',
    bastion_instance_type='t3.micro',
    state_store='parameter_store',
    worker_init='''
        pip install numpy scipy pandas
    '''
)
```

### Serverless Mode

Uses AWS Lambda or ECS/Fargate for execution without EC2 instances.

```python
provider = EphemeralAWSProvider(
    image_id='ami-12345678',  # Still needed for some operations
    region='us-west-2',
    mode='serverless',
    worker_type='lambda',
    lambda_memory=1024,
    lambda_timeout=900
)
```

## Cost Optimization

Use these features to optimize costs:

### Spot Instances

```python
provider = EphemeralAWSProvider(
    image_id='ami-12345678',
    instance_type='t3.medium',
    region='us-west-2',
    use_spot_instances=True,
    spot_max_price_percentage=80  # Max 80% of on-demand price
)
```

### Auto-Scaling

```python
provider = EphemeralAWSProvider(
    image_id='ami-12345678',
    instance_type='t3.medium',
    region='us-west-2',
    min_blocks=0,
    max_blocks=10,
    # Scale down to zero when not in use
)
```

### Multiple Instance Types

```python
provider = EphemeralAWSProvider(
    image_id='ami-12345678',
    region='us-west-2',
    use_ec2_fleet=True,
    instance_types=[
        {'type': 't3.medium', 'weight': 1},
        {'type': 'm5.large', 'weight': 2},
        {'type': 'c5.large', 'weight': 2},
    ]
)
```

## Using Worker Initialization Scripts

You can provide a script to run on each worker during initialization:

```python
provider = EphemeralAWSProvider(
    image_id='ami-12345678',
    instance_type='t3.medium',
    region='us-west-2',
    worker_init='''
        # Install dependencies
        pip install numpy scipy pandas matplotlib
        
        # Set up environment
        export PYTHONPATH=$PYTHONPATH:/path/to/your/modules
        
        # Download data
        aws s3 cp s3://your-bucket/data/ /tmp/data/ --recursive
    '''
)
```

## Monitoring

The provider captures detailed logs about resource provisioning and task execution:

```python
from parsl_ephemeral_aws.utils.logging import configure_logger
import logging

# Set up logging
configure_logger(level=logging.INFO, file_path='parsl_aws.log')
```

You can also monitor resources via the AWS Management Console by looking for resources tagged with:
- `ParslResource: true`
- `ParslWorkflowId: <your-workflow-id>`

## Cleaning Up

The provider automatically cleans up all resources when:
1. The `shutdown()` method is called
2. The Python process exits normally
3. The bastion host times out (in detached mode)

To manually clean up resources:

```python
# Clean up all resources
provider.shutdown()
```

## Troubleshooting

### Connection Issues

If workers can't connect to your client:
- Check if your client has a public IP or is behind a NAT
- Consider using the detached mode
- Verify security group rules

### Spot Instance Interruptions

If your spot instances are being interrupted frequently:
- Try different instance types
- Increase the spot max price percentage
- Use the spot interruption behavior setting
- Consider a mix of spot and on-demand instances

### State Persistence Issues

If you're having issues with state persistence:
- Check AWS permissions for Parameter Store or S3
- Try using the file state storage for debugging
- Ensure your workflow ID is unique

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
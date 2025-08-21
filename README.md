# Parsl Ephemeral AWS Provider

A modern, flexible AWS provider for the Parsl parallel scripting library that leverages ephemeral resources for cost-effective, scalable scientific computation.

[![PyPI version](https://badge.fury.io/py/parsl-ephemeral-aws.svg)](https://badge.fury.io/py/parsl-ephemeral-aws)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python Versions](https://img.shields.io/pypi/pyversions/parsl-ephemeral-aws.svg)](https://pypi.org/project/parsl-ephemeral-aws/)
[![Documentation Status](https://readthedocs.org/projects/parsl-ephemeral-aws/badge/?version=latest)](https://parsl-ephemeral-aws.readthedocs.io/en/latest/?badge=latest)
[![Build Status](https://github.com/scttfrdmn/parsl-aws-provider/actions/workflows/ci.yml/badge.svg)](https://github.com/scttfrdmn/parsl-aws-provider/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/scttfrdmn/parsl-aws-provider/branch/main/graph/badge.svg)](https://codecov.io/gh/scttfrdmn/parsl-aws-provider)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.placeholder.svg)](https://doi.org/10.5281/zenodo.placeholder)

## Overview

The Parsl Ephemeral AWS Provider enables seamless execution of Parsl workflows on dynamically provisioned AWS resources with true ephemerality - resources are created when needed and destroyed when not, minimizing costs while maximizing scalability.

Unlike the standard Parsl AWS provider, this implementation:

- **Truly ephemeral**: All resources (including VPC, security groups, etc.) are cleaned up automatically
- **Flexible compute options**: Supports EC2, Spot instances, Lambda, and ECS/Fargate
- **Modern AWS integration**: Uses EC2 Fleet, Spot Fleet, auto-scaling groups, and other advanced AWS features
- **Resilient execution**: Intelligently handles spot interruptions with state persistence
- **Multi-mode operation**: Choose between standard, detached, or serverless execution modes

## Development

This project supports Python 3.9+ and uses pyenv for Python version management.

### Setting Up Development Environment

```bash
# Clone the repository
git clone https://github.com/scttfrdmn/parsl-aws-provider.git
cd parsl-aws-provider

# Ensure you have the correct Python version via pyenv
pyenv install 3.9.16
pyenv local 3.9.16

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate  # On Linux/macOS
# OR
.venv\Scripts\activate     # On Windows

# Install development dependencies
pip install -e ".[dev,test]"
```

### Running Tests

```bash
# Run tests
pytest

# Run tests with coverage
pytest --cov=parsl_ephemeral_aws

# Run linting and type checking
flake8 parsl_ephemeral_aws tests
mypy parsl_ephemeral_aws

# Format code
black parsl_ephemeral_aws tests
```

### Development Guidelines

- Always use a virtual environment
- Run linting and tests before submitting PRs
- Follow PEP 8 style guidelines
- Document all public APIs with docstrings
- Write unit tests for new functionality
- Ensure backward compatibility when making changes

## Installation

```bash
pip install parsl-ephemeral-aws
```

## Quick Start

```python
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
from parsl_ephemeral_aws import EphemeralAWSProvider

# Configure the ephemeral AWS provider
provider = EphemeralAWSProvider(
    # Core parameters
    image_id='ami-12345678',  # Amazon Linux 2 AMI
    instance_type='t3.medium',
    region='us-west-2',

    # Block parameters
    init_blocks=1,
    min_blocks=0,
    max_blocks=10,
    nodes_per_block=1,

    # Ephemeral settings
    use_spot_instances=True,
    spot_max_price_percentage=80,  # 80% of on-demand price
    instance_termination_policy='terminate',  # 'terminate', 'stop', or 'hibernate'

    # State persistence
    state_store='parameter_store',  # 'parameter_store', 's3', 'file', 'none'
    state_prefix='/parsl/workflows',

    # Network settings
    use_public_ips=True,

    # Worker initialization
    worker_init='pip install -r requirements.txt',
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
import parsl
parsl.load(config)

# Define and run your Parsl workflows
@parsl.python_app
def hello_world():
    return "Hello, World!"

result = hello_world()
print(result.result())
```

## Architecture

The Ephemeral AWS Provider operates with the following components:

1. **Local Provider Process**: The Python process running on your client machine that interfaces with Parsl and AWS APIs

2. **Bastion/Coordinator Instance** (optional): A small AWS instance that serves as the communication hub between your client and worker nodes

3. **Worker Compute Resources**: Dynamically provisioned compute resources that execute Parsl tasks, which can be:
   - EC2 Instances (on-demand or spot)
   - Lambda Functions (for short-running tasks)
   - ECS Containers (via Fargate)
   - Auto Scaling Groups

4. **State Management**: Configurable state persistence via Parameter Store, S3, or local files

### Operating Modes

The Ephemeral AWS Provider supports three distinct operating modes to accommodate different workflow requirements and environments:

#### Standard Mode

In Standard mode, your client machine directly communicates with worker nodes in AWS. This mode:

- Provides the simplest deployment architecture
- Requires your client to maintain a stable connection for the duration of the workflow
- Offers the lowest latency for task submission and result retrieval
- Works well for development, testing, and smaller production workflows
- Requires your client to have outbound connectivity to the worker nodes

Example configuration:
```python
provider = EphemeralAWSProvider(
    image_id='ami-12345678',
    instance_type='t3.medium',
    region='us-west-2',
    mode='standard',  # This is the default mode
    # Other configuration parameters...
)
```

#### Detached Mode

Detached mode runs a small bastion/coordinator instance in AWS that manages the worker fleet. This mode:

- Allows your client to disconnect after workflow submission
- Continues running your workflow even if your client loses connectivity
- Uses a persistent coordinator instance to manage job distribution
- Is ideal for long-running workflows, unstable client connections, or clients behind NAT/firewalls
- Provides built-in workflow state persistence
- Supports auto-shutdown of the coordinator when workflow completes

Example configuration:
```python
provider = EphemeralAWSProvider(
    image_id='ami-12345678',
    instance_type='t3.medium',
    region='us-west-2',
    mode='detached',
    bastion_instance_type='t3.micro',
    bastion_idle_timeout=30,  # Minutes before auto-shutdown when idle
    state_store='parameter_store',  # Required for workflow resumption
    # Other configuration parameters...
)
```

#### Serverless Mode

Serverless mode eliminates the need for persistent EC2 instances by using AWS Lambda and/or ECS/Fargate. This mode:

- Offers true pay-per-use pricing with no idle costs
- Scales from zero to thousands of concurrent tasks in seconds
- Is ideal for event-driven, sporadic, or burst workloads
- Works best with short-running tasks (under 15 minutes for Lambda)
- Provides automatic cleanup with zero maintenance
- Supports both compute-optimized and memory-optimized workloads
- Supports AWS SpotFleet for more reliable and cost-effective EC2 resources when needed

Example configuration:
```python
provider = EphemeralAWSProvider(
    region='us-west-2',
    mode='serverless',
    worker_type='lambda',  # Or 'ecs', or 'auto' to let the provider choose
    lambda_memory=1024,    # MB
    lambda_timeout=900,    # Seconds (max 15 minutes)
    # For ECS/Fargate:
    # ecs_task_cpu=1024,     # CPU units
    # ecs_task_memory=2048,  # MB
    # Other configuration parameters...
)
```

For workloads that need more substantial compute power but still benefit from serverless management, you can enable SpotFleet in ServerlessMode:

```python
provider = EphemeralAWSProvider(
    region='us-west-2',
    mode='serverless',
    worker_type='ecs',
    use_spot_fleet=True,
    instance_types=["t3.medium", "t3a.medium", "m5.large"],
    nodes_per_block=2,
    spot_max_price_percentage=80,  # 80% of on-demand price
)
```

## Advanced Features

### MPI Support

For tasks that require multi-node processing, the provider supports MPI execution:

```python
from parsl_ephemeral_aws import EphemeralAWSProvider
from parsl.launchers import MpiRunLauncher

provider = EphemeralAWSProvider(
    # Basic configuration...
    nodes_per_block=4,  # Request 4 nodes per block for MPI
    launcher=MpiRunLauncher(),
)
```

### Spot Instance Handling

Configure how spot instance interruptions are handled:

```python
provider = EphemeralAWSProvider(
    # Basic configuration...
    use_spot_instances=True,
    spot_max_price_percentage=80,
    spot_interruption_behavior='hibernate',  # 'terminate', 'stop', or 'hibernate'

    # Enable checkpointing to handle interruptions
    spot_interruption_handling=True,
    checkpoint_bucket='my-parsl-checkpoints',
    checkpoint_prefix='workflow/checkpoints',
    checkpoint_interval=60,  # Seconds between checkpoints
)
```

### Auto-Shutdown Bastion

For detached mode, configure auto-shutdown of the bastion when idle:

```python
provider = EphemeralAWSProvider(
    # Basic configuration...
    bastion_instance_type='t3.micro',
    bastion_idle_timeout=30,  # Minutes
    auto_shutdown=True,
)
```

### Lambda Function Workers

For short-running tasks, use Lambda functions as workers:

```python
provider = EphemeralAWSProvider(
    # Basic configuration...
    worker_type='lambda',  # 'ec2', 'lambda', 'ecs', or 'auto'
    lambda_memory=1024,    # MB
    lambda_timeout=900,    # Seconds (max 15 minutes)
)
```

### ECS/Fargate Workers

For containerized workloads:

```python
provider = EphemeralAWSProvider(
    # Basic configuration...
    worker_type='ecs',
    ecs_task_cpu=1024,     # CPU units
    ecs_task_memory=2048,  # MB
    ecs_container_image='my-custom-image:latest',
)
```

### EC2 Fleet and Spot Fleet for Diverse Instance Types

Use multiple instance types for better availability and pricing with EC2 Fleet:

```python
provider = EphemeralAWSProvider(
    # Basic configuration...
    use_ec2_fleet=True,
    instance_types=[
        {'type': 't3.medium', 'weight': 1},
        {'type': 'm5.large', 'weight': 2},
        {'type': 'c5.large', 'weight': 2},
    ],
)
```

Or use AWS Spot Fleet for more reliable spot instance management:

```python
provider = EphemeralAWSProvider(
    # Basic configuration...
    use_spot=True,
    use_spot_fleet=True,  # Use Spot Fleet instead of individual spot requests
    instance_types=["c5.large", "c5d.large", "m5.large", "r5.large"],
    spot_max_price_percentage=100,  # Maximum percentage of on-demand price
)
```

### GPU Acceleration

For compute-intensive tasks:

```python
provider = EphemeralAWSProvider(
    # Basic configuration...
    instance_type='g4dn.xlarge',  # GPU instance
    worker_init='''
        # Install CUDA drivers and libraries
        sudo amazon-linux-extras install -y epel
        sudo yum install -y cuda-drivers-fabricmanager-11-4
        pip install torch==1.11.0+cu113 -f https://download.pytorch.org/whl/cu113/torch_stable.html
    ''',
)
```

## Configuration Reference

For a complete list of configuration options, see the [Configuration Reference](https://parsl-ephemeral-aws.readthedocs.io/en/latest/configuration.html).

## Examples

The `examples/` directory contains detailed examples for each operating mode:

- [`standard_mode.py`](examples/standard_mode.py) - Direct client-to-worker communication via EC2 instances
- [`detached_mode.py`](examples/detached_mode.py) - Persistent infrastructure with bastion host for long-running workflows
- [`serverless_mode.py`](examples/serverless_mode.py) - Lambda and Fargate execution for serverless workloads
- [`basic_usage.py`](examples/basic_usage.py) - Combined example showing all three modes
- [`spot_fleet_example.py`](examples/spot_fleet_example.py) - Using Spot Fleet for reliable spot instance management
- [`spot_interruption_example.py`](examples/spot_interruption_example.py) - Handling spot instance interruptions with checkpointing

Each example includes comprehensive comments explaining mode-specific features and configuration options.

### Basic Workflow with Auto-Scaling

```python
import parsl
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
from parsl_ephemeral_aws import EphemeralAWSProvider

provider = EphemeralAWSProvider(
    image_id='ami-12345678',
    instance_type='t3.medium',
    region='us-west-2',
    init_blocks=1,
    min_blocks=0,
    max_blocks=5,
    use_spot_instances=True,
)

config = Config(
    executors=[
        HighThroughputExecutor(
            label='aws_executor',
            provider=provider,
        )
    ]
)

parsl.load(config)

# Define a compute-intensive app
@parsl.python_app
def compute(x):
    import time
    import math
    time.sleep(2)  # Simulate work
    return math.sqrt(x)

# Submit 100 tasks
results = []
for i in range(100):
    results.append(compute(i))

# Wait for all tasks to complete
for r in results:
    print(r.result())
```

### Detached Mode with Hibernation

```python
provider = EphemeralAWSProvider(
    image_id='ami-12345678',
    instance_type='m5.large',
    region='us-west-2',
    init_blocks=2,
    max_blocks=10,
    use_spot_instances=True,
    spot_interruption_behavior='hibernate',
    mode='detached',
    bastion_instance_type='t3.micro',
    state_store='parameter_store',
    worker_init='''
        sudo yum update -y
        sudo yum install -y python3-devel
        pip3 install --upgrade pip
        pip3 install numpy scipy pandas
    ''',
)
```

### Multi-Node MPI Execution

```python
from parsl.launchers import MpiRunLauncher

provider = EphemeralAWSProvider(
    image_id='ami-12345678',
    instance_type='c5.2xlarge',
    region='us-west-2',
    nodes_per_block=4,
    init_blocks=1,
    max_blocks=5,
    launcher=MpiRunLauncher(),
    worker_init='''
        sudo yum update -y
        sudo yum install -y openmpi-devel
        pip3 install mpi4py
    ''',
)

# Define an MPI app
@parsl.bash_app
def mpi_hello(nodes, ranks_per_node, stdout=parsl.AUTO_LOGNAME, stderr=parsl.AUTO_LOGNAME):
    return f"mpirun -n {nodes * ranks_per_node} -npernode {ranks_per_node} python3 mpi_hello.py"
```

## AWS Permissions

The ephemeral AWS provider requires the following AWS permissions:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "ec2:RunInstances",
                "ec2:TerminateInstances",
                "ec2:StopInstances",
                "ec2:StartInstances",
                "ec2:CreateTags",
                "ec2:DescribeInstances",
                "ec2:DescribeInstanceStatus",
                "ec2:DescribeImages",
                "ec2:DescribeVpcs",
                "ec2:DescribeSubnets",
                "ec2:DescribeSecurityGroups",
                "ec2:CreateVpc",
                "ec2:CreateSubnet",
                "ec2:CreateSecurityGroup",
                "ec2:DeleteVpc",
                "ec2:DeleteSubnet",
                "ec2:DeleteSecurityGroup",
                "ec2:AuthorizeSecurityGroupIngress",
                "ec2:RequestSpotInstances",
                "ec2:CancelSpotInstanceRequests",
                "ec2:DescribeSpotInstanceRequests",
                "ec2:CreateFleet",
                "ec2:DeleteFleet",
                "ec2:DescribeFleets",
                "ec2:RequestSpotFleet",
                "ec2:CancelSpotFleetRequests",
                "ec2:DescribeSpotFleetRequests",
                "ec2:DescribeSpotFleetInstances",
                "ec2:ModifySpotFleetRequest",
                "ec2:DescribeSpotFleetRequestHistory",
                "ssm:PutParameter",
                "ssm:GetParameter",
                "ssm:DeleteParameter",
                "lambda:CreateFunction",
                "lambda:InvokeFunction",
                "lambda:DeleteFunction",
                "ecs:CreateCluster",
                "ecs:DeleteCluster",
                "ecs:RegisterTaskDefinition",
                "ecs:DeregisterTaskDefinition",
                "ecs:RunTask",
                "ecs:StopTask",
                "ecs:DescribeTasks",
                "iam:PassRole",
                "iam:CreateRole",
                "iam:DeleteRole",
                "iam:AttachRolePolicy",
                "iam:DetachRolePolicy",
                "iam:GetRole",
                "iam:ListAttachedRolePolicies"
            ],
            "Resource": "*"
        }
    ]
}
```

## Cost Management

The ephemeral AWS provider is designed to minimize costs by:

1. **Automatic cleanup** of all resources when no longer needed
2. **Spot instance support** for up to 90% cost savings
3. **Right-sizing** resources for your workload
4. **Auto-scaling** to match resource demand
5. **Multiple compute options** to optimize for specific workloads

Use the following best practices to further reduce costs:

- Set appropriate `min_blocks` and `max_blocks` values
- Use spot instances when possible
- Choose the right instance types for your workload
- Configure `worker_init` to minimize startup time
- For long-running jobs, consider hibernation instead of termination

## Limitations

- Lambda functions have a maximum execution time of 15 minutes
- ECS tasks have maximum resource limits (30 GB RAM, 4 vCPU per task)
- Spot instances can be interrupted with only 2 minutes of notice
- MPI execution is not supported on Lambda or ECS/Fargate

## Troubleshooting

Common issues and solutions:

### Workers can't connect to the coordinator

- Check security groups and network ACLs
- Ensure the coordinator has a public IP or is in the same VPC
- Verify AWS credentials have the necessary permissions

### Spot instances being interrupted frequently

- Use Spot Fleet (`use_spot_fleet=True`) for more reliable spot instance management
- Try different instance types or availability zones
- Increase the spot max price percentage
- Configure multiple instance types to improve availability and pricing
- Switch to on-demand instances for critical workloads

### Long workflow initialization times

- Use a pre-built AMI with dependencies pre-installed
- Consider using container images for faster startup
- Optimize `worker_init` scripts

## Contributing

Contributions are welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors

## Acknowledgments

- The Parsl development team for creating an excellent parallel scripting library
- AWS for providing robust cloud services for scientific computing
- Contributors and users who provide feedback and improvements

## Citation

If you use this provider in your research, please cite:

```bibtex
@software{parsl_ephemeral_aws,
  author = {Friedman, Scott and Contributors},
  title = {Parsl Ephemeral AWS Provider},
  url = {https://github.com/scttfrdmn/parsl-aws-provider},
  version = {0.1.0},
  year = {2025},
}
```

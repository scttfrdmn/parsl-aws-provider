# Advanced Usage Guide

This document covers advanced usage scenarios for the Parsl Ephemeral AWS Provider.

## MPI Multi-Node Configuration

### Basic MPI Setup

For scientific computing workloads that require MPI, the provider supports multi-node configurations:

```python
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
from parsl.launchers import MpiRunLauncher
from parsl_ephemeral_aws import EphemeralAWSProvider

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
    '''
)

config = Config(
    executors=[
        HighThroughputExecutor(
            label='mpi_cluster',
            provider=provider,
        )
    ]
)
```

### Running MPI Tasks

With this configuration, you can run MPI-enabled apps:

```python
import parsl
from parsl.app.app import bash_app

parsl.load(config)

@bash_app
def mpi_hello(nodes, ranks_per_node, stdout=parsl.AUTO_LOGNAME, stderr=parsl.AUTO_LOGNAME):
    return f"mpirun -n {nodes * ranks_per_node} -npernode {ranks_per_node} python3 mpi_hello.py"

# Run a 16-process MPI job across 4 nodes
future = mpi_hello(4, 4)
print(future.result())
```

### MPI with Placement Groups

For HPC workloads that require low latency networking, use placement groups:

```python
provider = EphemeralAWSProvider(
    image_id='ami-12345678',
    instance_type='c5n.18xlarge',  # Instances with enhanced networking
    region='us-west-2',
    nodes_per_block=8,
    use_placement_groups=True,  # Enable placement groups
    placement_group_strategy='cluster',  # Cluster instances for low latency
    launcher=MpiRunLauncher(),
    worker_init='''
        # Install MPI and configure for high performance
        sudo yum install -y openmpi-devel
        echo "btl_tcp_if_include = eth0" >> /etc/openmpi-x86_64/openmpi-mca-params.conf
    '''
)
```

## Spot Fleet Management

For advanced spot instance management:

```python
provider = EphemeralAWSProvider(
    image_id='ami-12345678',
    region='us-west-2',
    use_spot_instances=True,
    spot_max_price_percentage=70,
    use_ec2_fleet=True,
    instance_types=[
        {'type': 'm5.large', 'weight': 1, 'max_price_percentage': 70},
        {'type': 'm5a.large', 'weight': 1, 'max_price_percentage': 70},
        {'type': 'c5.large', 'weight': 1, 'max_price_percentage': 80},
        {'type': 'r5.large', 'weight': 2, 'max_price_percentage': 60}
    ],
    spot_allocation_strategy='capacity-optimized',
    spot_interruption_behavior='stop',
    spot_instance_pools=4
)
```

### Handling Spot Interruptions

To make your workflow resilient to spot interruptions:

```python
provider = EphemeralAWSProvider(
    image_id='ami-12345678',
    region='us-west-2',
    use_spot_instances=True,
    # Enable instance hibernation on interruption
    spot_interruption_behavior='hibernate',  
    # Enable state persistence to resume jobs
    state_store='s3',
    state_bucket='my-parsl-state',
    state_prefix='workflow-states',
    # Enable task retry on failure
    retries=3,
    # Enable checkpointing
    enable_checkpointing=True,
    checkpoint_interval=30  # minutes
)
```

## Custom VPC and Networking

If you need to use an existing VPC or require custom networking:

```python
provider = EphemeralAWSProvider(
    image_id='ami-12345678',
    region='us-west-2',
    # Use existing network resources
    use_existing_vpc=True,
    vpc_id='vpc-12345678',
    subnet_ids=['subnet-12345678', 'subnet-87654321'],
    security_group_ids=['sg-12345678'],
    # Additional network configuration
    assign_public_ips=True,
    use_vpc_endpoints=True,  # Create VPC endpoints for AWS services
)
```

### VPC Endpoint Configuration

For secure communication without internet access:

```python
provider = EphemeralAWSProvider(
    image_id='ami-12345678',
    region='us-west-2',
    use_vpc_endpoints=True,
    vpc_endpoint_services=[
        's3', 'dynamodb', 'ssm', 'logs', 'ecr.api', 'ecr.dkr'
    ],
    use_public_ips=False,  # Disable public IPs
)
```

## Cost Management and Budgeting

### Budget Control

Set budget limits to avoid unexpected costs:

```python
provider = EphemeralAWSProvider(
    image_id='ami-12345678',
    region='us-west-2',
    # Budget controls
    max_cost_per_hour=10.0,  # Maximum cost per hour in USD
    cost_monitoring_interval=15,  # minutes
    shutdown_on_budget_exceeded=True,
)
```

### Cost Reporting

Enable detailed cost reporting:

```python
provider = EphemeralAWSProvider(
    image_id='ami-12345678',
    region='us-west-2',
    # Cost reporting
    enable_cost_reporting=True,
    cost_report_bucket='my-cost-reports',
    cost_report_prefix='parsl-reports',
)
```

## GPU Acceleration

### Basic GPU Configuration

For workloads requiring GPUs:

```python
provider = EphemeralAWSProvider(
    image_id='ami-12345678',  # AMI with NVIDIA drivers
    instance_type='g4dn.xlarge',  # GPU instance
    region='us-west-2',
    worker_init='''
        # Verify GPU is visible
        nvidia-smi
        
        # Install CUDA toolkit and libraries
        pip install torch==2.0.0+cu118 torchvision==0.15.1+cu118 -f https://download.pytorch.org/whl/torch_stable.html
    '''
)
```

### Multi-GPU Configuration

For multi-GPU workloads:

```python
provider = EphemeralAWSProvider(
    image_id='ami-12345678',
    instance_type='p3.8xlarge',  # 4 x V100 GPUs
    region='us-west-2',
    worker_init='''
        # Install CUDA toolkit
        pip install torch==2.0.0+cu118
        
        # Configure for multi-GPU
        export CUDA_VISIBLE_DEVICES=0,1,2,3
    '''
)
```

## Custom AMI Creation

For faster startup times, create a custom AMI with pre-installed dependencies:

```python
from parsl_ephemeral_aws.utils.ami import create_custom_ami

# Create a custom AMI with pre-installed dependencies
ami_id = create_custom_ami(
    base_ami='ami-12345678',
    region='us-west-2',
    name='parsl-worker-ami',
    description='Custom AMI for Parsl workers',
    installation_script='''
        # Update system
        sudo yum update -y
        
        # Install Python dependencies
        sudo yum install -y python3-devel gcc
        
        # Install commonly used packages
        pip3 install numpy scipy pandas tensorflow
        
        # Install MPI
        sudo yum install -y openmpi-devel
        pip3 install mpi4py
        
        # Clean up
        sudo yum clean all
        rm -rf ~/.cache/pip
    '''
)

# Use the custom AMI in the provider
provider = EphemeralAWSProvider(
    image_id=ami_id,
    region='us-west-2'
)
```

## Hybrid Cloud Configuration

Combine AWS resources with on-premises resources:

```python
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
from parsl_ephemeral_aws import EphemeralAWSProvider
from parsl.providers import SlurmProvider

# AWS provider for cloud resources
aws_provider = EphemeralAWSProvider(
    image_id='ami-12345678',
    region='us-west-2',
    init_blocks=1,
    max_blocks=10
)

# Slurm provider for on-premises resources
slurm_provider = SlurmProvider(
    partition='compute',
    nodes_per_block=2,
    init_blocks=1,
    max_blocks=5
)

# Create configuration with both providers
config = Config(
    executors=[
        HighThroughputExecutor(
            label='cloud',
            provider=aws_provider,
        ),
        HighThroughputExecutor(
            label='local',
            provider=slurm_provider,
        )
    ]
)
```

## Security Hardening

Enhance security for production deployments:

```python
provider = EphemeralAWSProvider(
    image_id='ami-12345678',
    region='us-west-2',
    # Security enhancements
    use_imdsv2=True,  # Use Instance Metadata Service v2
    block_public_access=True,  # Block public IP access by default
    vpc_flow_logs=True,  # Enable VPC flow logs
    cloudtrail_enabled=True,  # Enable CloudTrail for API logging
    security_group_ingress=[
        # Restrict SSH access to specific IPs
        {'ip_protocol': 'tcp', 'from_port': 22, 'to_port': 22, 'cidr_blocks': ['123.456.789.0/24']}
    ]
)
```

## Serverless Workflows

### Lambda Configuration

For short-running tasks using Lambda:

```python
provider = EphemeralAWSProvider(
    region='us-west-2',
    mode='serverless',
    worker_type='lambda',
    lambda_memory=1024,
    lambda_timeout=900,  # 15 minutes (maximum)
    lambda_runtime='python3.9',
    lambda_code_package='./lambda_package.zip',
    lambda_layers=[
        'arn:aws:lambda:us-west-2:123456789012:layer:numpy:1',
        'arn:aws:lambda:us-west-2:123456789012:layer:pandas:1'
    ]
)
```

### ECS/Fargate Configuration

For containerized workloads:

```python
provider = EphemeralAWSProvider(
    region='us-west-2',
    mode='serverless',
    worker_type='ecs',
    ecs_task_cpu=1024,
    ecs_task_memory=2048,
    ecs_container_image='123456789012.dkr.ecr.us-west-2.amazonaws.com/parsl-worker:latest',
    ecs_use_fargate_spot=True
)
```

## Advanced State Management

### Custom State Backend

Implement a custom state backend:

```python
from parsl_ephemeral_aws import EphemeralAWSProvider
from parsl_ephemeral_aws.state.base import StateStore
from typing import Dict, Any, Optional

# Custom state backend using DynamoDB
class DynamoDBState(StateStore):
    def __init__(self, provider, table_name):
        self.provider = provider
        self.table_name = table_name
        self.dynamodb = boto3.resource('dynamodb')
        self.table = self.dynamodb.Table(table_name)
        
    def save_state(self, state_key, state_data):
        self.table.put_item(
            Item={
                'StateKey': state_key,
                'WorkflowId': self.provider.workflow_id,
                'StateData': json.dumps(state_data)
            }
        )
        
    def load_state(self, state_key):
        response = self.table.get_item(
            Key={'StateKey': state_key}
        )
        if 'Item' in response:
            return json.loads(response['Item']['StateData'])
        return None
        
    def delete_state(self, state_key):
        self.table.delete_item(
            Key={'StateKey': state_key}
        )
        
    def list_states(self, prefix):
        response = self.table.scan(
            FilterExpression=boto3.dynamodb.conditions.Key('StateKey').begins_with(prefix)
        )
        states = {}
        for item in response.get('Items', []):
            states[item['StateKey']] = json.loads(item['StateData'])
        return states

# Use custom state backend
provider = EphemeralAWSProvider(
    image_id='ami-12345678',
    region='us-west-2',
    state_store=DynamoDBState(provider, 'parsl-state-table')
)
```

## Monitoring and Logging

### CloudWatch Integration

Enable detailed monitoring:

```python
provider = EphemeralAWSProvider(
    image_id='ami-12345678',
    region='us-west-2',
    # CloudWatch integration
    enable_monitoring=True,
    cloudwatch_log_group='/parsl/workflows',
    cloudwatch_metrics=[
        'CPUUtilization',
        'MemoryUtilization',
        'NetworkIn',
        'NetworkOut',
        'DiskReadBytes',
        'DiskWriteBytes'
    ],
    cloudwatch_logs_retention_days=14
)
```

### Custom Metrics

Collect custom metrics:

```python
provider = EphemeralAWSProvider(
    image_id='ami-12345678',
    region='us-west-2',
    enable_monitoring=True,
    custom_metrics=[
        {
            'name': 'TasksCompleted',
            'unit': 'Count',
            'collection_interval': 60  # seconds
        },
        {
            'name': 'ProcessingTime',
            'unit': 'Seconds',
            'collection_interval': 60
        }
    ]
)
```

## Resource Tagging

Customize resource tagging:

```python
provider = EphemeralAWSProvider(
    image_id='ami-12345678',
    region='us-west-2',
    tags={
        'Project': 'genomics-analysis',
        'Department': 'research',
        'CostCenter': 'cc-123456',
        'Owner': 'jane.doe@example.com',
        'Environment': 'development'
    }
)
```

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
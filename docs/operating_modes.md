# Operating Modes

The Parsl Ephemeral AWS Provider supports three distinct operating modes, each designed for different use cases and environments. This document provides detailed information about each mode, including configuration options, advantages, and appropriate use cases.

## Standard Mode

Standard Mode provides direct communication between your client machine and AWS resources. This is the simplest mode and works well for development, testing, and workflows where your client machine maintains connectivity throughout the workflow execution.

### Key Features

- Direct client-to-worker communication
- Simplest architecture with no intermediary resources
- Low latency for task submission and result retrieval
- Resources are terminated when the client disconnects
- Suitable for development and shorter workflows

### Configuration

```python
from parsl_ephemeral_aws import EphemeralAWSProvider
from parsl_ephemeral_aws.modes.standard import StandardMode

provider = EphemeralAWSProvider(
    mode=StandardMode(
        region="us-west-2",
        instance_type="t3.medium",
        image_id="ami-12345678",
        use_public_ips=True,  # Set to False if using VPN/Direct Connect
        min_blocks=0,
        max_blocks=4,
        key_name="your-key-pair",  # Optional: For SSH access to instances
        create_vpc=True,          # Create a new VPC or use existing
    ),
    # Other provider parameters...
)
```

### When to Use

- During development and testing
- For workflows that complete within hours
- When your client has stable connectivity to AWS
- For workflows with frequent client-worker communication

### Diagram

```
┌─────────────┐     ┌─────────────┐
│             │     │             │
│    Client   │◄────►   Worker 1  │
│  (Your PC)  │     │  (EC2/Spot) │
│             │     │             │
└──────┬──────┘     └─────────────┘
       │
       │            ┌─────────────┐
       │            │             │
       └───────────►│   Worker 2  │
                    │  (EC2/Spot) │
                    │             │
                    └─────────────┘
```

## Detached Mode

Detached Mode creates a persistent bastion host in AWS that coordinates the worker fleet. This allows your client to disconnect after submitting a workflow, with execution continuing in AWS. The client can reconnect later to check status or retrieve results.

### Key Features

- Persistent bastion host coordinates workers
- Workflows continue running when client disconnects
- Supports long-running or overnight jobs
- State is persisted for reconnection
- Bastion host can auto-terminate when idle
- Works with restricted network environments

### Configuration

```python
from parsl_ephemeral_aws import EphemeralAWSProvider
from parsl_ephemeral_aws.modes.detached import DetachedMode

provider = EphemeralAWSProvider(
    mode=DetachedMode(
        region="us-west-2",
        workflow_id="unique-workflow-id",  # Can be used for reconnection
        instance_type="t3.medium",
        bastion_instance_type="t3.micro",
        bastion_host_type="cloudformation",  # Or "direct"
        idle_timeout=30,  # Minutes before bastion auto-shutdown
        preserve_bastion=True,  # Keep bastion for reconnection
        key_name="your-key-pair",  # Required for SSH access
        min_blocks=0,
        max_blocks=10,
    ),
    # Other provider parameters...
)
```

### When to Use

- For long-running workflows (hours to days)
- When the client may disconnect during execution
- For workflows that run overnight or over weekends
- In environments with unreliable client connectivity
- When workflows need to survive client reboots

### Diagram

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│             │     │             │     │             │
│    Client   │◄────►   Bastion   │◄────►   Worker 1  │
│  (Your PC)  │     │    Host     │     │  (EC2/Spot) │
│             │     │  (EC2/ECS)  │     │             │
└─────────────┘     └──────┬──────┘     └─────────────┘
                           │
                           │            ┌─────────────┐
                           │            │             │
                           └───────────►│   Worker 2  │
                                        │  (EC2/Spot) │
                                        │             │
                                        └─────────────┘
```

## Serverless Mode

Serverless Mode leverages AWS Lambda functions and/or ECS/Fargate for job execution without requiring any EC2 instances. This provides true pay-per-use pricing with rapid scaling capabilities.

### Key Features

- No instances to manage (zero infrastructure)
- Automatic scaling from 0 to 1000s of concurrent tasks
- Pay only for compute time used
- Rapid startup time
- Support for both Lambda (short tasks) and Fargate (longer tasks)
- Lambda has memory up to 10GB with 15-minute maximum duration
- Fargate supports longer running tasks with more memory/CPU

### Configuration

```python
from parsl_ephemeral_aws import EphemeralAWSProvider
from parsl_ephemeral_aws.modes.serverless import ServerlessMode

provider = EphemeralAWSProvider(
    mode=ServerlessMode(
        region="us-west-2",
        worker_type="auto",  # "lambda", "ecs", or "auto"
        
        # Lambda configuration
        lambda_memory=1024,  # MB
        lambda_timeout=900,  # Seconds (max 15 minutes)
        lambda_runtime="python3.9",
        
        # Fargate configuration
        ecs_task_cpu=1024,  # CPU units
        ecs_task_memory=2048,  # MB
        ecs_container_image="123456789012.dkr.ecr.us-west-2.amazonaws.com/parsl-worker:latest",
        
        min_blocks=0,
        max_blocks=100,  # Scales to many concurrent invocations
    ),
    # Other provider parameters...
)
```

### When to Use

- For highly parallel, short-duration tasks
- For sporadic or event-driven workloads
- To minimize costs for intermittent usage
- When scaling requirements are unpredictable
- For simple tasks with minimal dependencies
- When infrastructure management should be minimized

### Diagram

#### Lambda Mode
```
┌─────────────┐     ┌─────────────┐
│             │     │  Lambda     │
│    Client   │◄────►  Function   │
│  (Your PC)  │     │  Invocation │
│             │     │             │
└──────┬──────┘     └─────────────┘
       │                   ▲
       │                   │
       │            ┌──────┴──────┐
       │            │  Lambda     │
       └───────────►│  Function   │
                    │  Invocation │
                    │             │
                    └─────────────┘
```

#### Fargate Mode
```
┌─────────────┐     ┌─────────────┐
│             │     │ ECS         │
│    Client   │◄────► Task        │
│  (Your PC)  │     │ (Fargate)   │
│             │     │             │
└──────┬──────┘     └─────────────┘
       │                   ▲
       │                   │
       │            ┌──────┴──────┐
       │            │ ECS         │
       └───────────►│ Task        │
                    │ (Fargate)   │
                    │             │
                    └─────────────┘
```

## Mode Selection Guide

| Consideration | Standard Mode | Detached Mode | Serverless Mode |
|---------------|--------------|--------------|----------------|
| **Client connectivity** | Must stay connected | Can disconnect | Can disconnect |
| **Workflow duration** | Minutes to hours | Hours to days | Seconds to hours |
| **Task duration** | Any | Any | Lambda: <15 min<br>Fargate: Any |
| **Scaling** | Moderate | Moderate | Rapid, massive |
| **Startup time** | Minutes | Minutes | Seconds |
| **Cost model** | Pay for EC2 uptime | Pay for EC2 uptime | Pay only for execution |
| **Network access** | Direct to workers | Via bastion | Via AWS services |
| **Infrastructure** | EC2 instances | EC2 (bastion + workers) | No infrastructure |
| **Recovery from client failure** | None | Full recovery | Full recovery |
| **Complexity** | Lowest | Medium | Highest |

## Best Practices

### Standard Mode
- Use spot instances for cost savings
- Set appropriate min/max blocks to control scaling
- Consider hibernation for longer workflows
- Use public IPs unless you have a VPN/Direct Connect

### Detached Mode
- Always use a key_name for SSH access to the bastion
- Set a reasonable idle_timeout to avoid costs when idle
- Use AWS Parameter Store for state persistence
- For long-running workflows, ensure the bastion instance type has sufficient resources

### Serverless Mode
- For Lambda, keep tasks short and limit dependencies
- Use Lambda layers for common dependencies
- For ECS/Fargate, create optimized container images
- Use the "auto" worker_type for intelligent worker selection
- Set memory/CPU allocations based on task requirements

## Switching Between Modes

Workflows designed for one mode may need adjustments to work in another mode:

1. **Standard → Detached**: Usually works with minimal changes 
2. **Standard/Detached → Serverless**: May require:
   - Breaking down long-running tasks
   - Packaging dependencies for Lambda/containers
   - Adjusting resource expectations

3. **Serverless → Standard/Detached**: Usually works with minimal changes

## Debugging Tips

### Standard Mode
- SSH directly to worker instances
- Check CloudWatch logs for instance bootstrap logs
- Examine worker stdout/stderr in Parsl runinfo directory

### Detached Mode
- SSH to bastion host
- Check CloudWatch logs for both bastion and workers
- Examine Parameter Store for task state

### Serverless Mode
- Check CloudWatch logs for Lambda functions
- Use CloudWatch Insights for filtering and searching
- Check ECS task details in the AWS console
- Examine CloudFormation stack events for deployment issues
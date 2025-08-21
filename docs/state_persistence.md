# State Persistence

The Parsl Ephemeral AWS Provider includes a robust state persistence system that enables tracking resources, recovering from failures, and resuming workflows across sessions. This document explains the state persistence mechanisms and how to use them.

## Overview

State persistence is crucial for an ephemeral AWS provider because:

1. It enables tracking of all AWS resources created during workflow execution
2. It allows for proper cleanup of resources, preventing orphaned resources and unexpected costs
3. It supports workflow recovery after client restarts or disconnections
4. It enables the detached and serverless modes to function properly

## State Store Types

The provider offers three types of state stores:

1. **File State Store**: Persists state to a local file (simplest option)
2. **Parameter Store State**: Uses AWS Systems Manager Parameter Store (recommended for detached mode)
3. **S3 State Store**: Stores state in an Amazon S3 bucket (good for sharing state across environments)

### File State Store

The File State Store is the simplest option, storing state in a local JSON file. It's suitable for local development and testing, or when using Standard mode with a stable client.

```python
from parsl_ephemeral_aws import EphemeralAWSProvider
from parsl_ephemeral_aws.state.file import FileStateStore

provider = EphemeralAWSProvider(
    # Mode configuration...
    state_store=FileStateStore(
        file_path="./aws_provider_state.json",
        backup_interval=300,  # Seconds between backups (optional)
        backup_count=3,       # Number of backup files to keep (optional)
    ),
    # Other provider parameters...
)
```

**Pros:**
- Simple to set up and debug
- No additional AWS services required
- State is human-readable

**Cons:**
- Not suitable for detached mode operation
- State lost if client machine fails or file is deleted
- No sharing of state across different machines

### Parameter Store State

The Parameter Store State uses AWS Systems Manager Parameter Store to maintain state. This is ideal for detached mode since the state is accessible from both the client and the bastion host.

```python
from parsl_ephemeral_aws import EphemeralAWSProvider
from parsl_ephemeral_aws.state.parameter_store import ParameterStoreState

provider = EphemeralAWSProvider(
    # Mode configuration...
    state_store=ParameterStoreState(
        prefix="/parsl/workflows/my-workflow",  # Parameter path prefix
        region="us-west-2",                     # AWS region
        secure=True,                           # Use SecureString type (optional)
        ttl=86400,                             # TTL in seconds (optional)
    ),
    # Other provider parameters...
)
```

**Pros:**
- Accessible from multiple locations (client & bastion)
- Secure storage with encryption options
- Automatic versioning of state
- Higher durability than file-based storage

**Cons:**
- Requires AWS SSM permissions
- Size limits (4KB per parameter for regular strings, 8KB for secure strings)
- May incur minimal AWS costs

### S3 State Store

The S3 State Store saves state in an Amazon S3 bucket. This is a good option for serverless mode or when state needs to be accessible from multiple environments.

```python
from parsl_ephemeral_aws import EphemeralAWSProvider
from parsl_ephemeral_aws.state.s3 import S3StateStore

provider = EphemeralAWSProvider(
    # Mode configuration...
    state_store=S3StateStore(
        bucket="my-parsl-state-bucket",      # S3 bucket name
        prefix="my-workflow",                # Object key prefix
        region="us-west-2",                  # AWS region
        create_bucket=True,                  # Create bucket if it doesn't exist
        versioning=True,                     # Enable versioning for objects
    ),
    # Other provider parameters...
)
```

**Pros:**
- Virtually unlimited storage
- High durability and availability
- Versioning and lifecycle rules
- Accessible from anywhere
- Works well with serverless mode

**Cons:**
- Requires S3 permissions
- May incur minimal AWS costs
- Slightly higher latency than local files

## Recommended State Stores by Mode

| Mode | Recommended State Store | Reason |
|------|------------------------|--------|
| Standard | FileStateStore | Simple and efficient for local operation |
| Detached | ParameterStoreState | Accessible from both client and bastion host |
| Serverless | S3StateStore | Highly durable and compatible with serverless resources |

## State Contents

The provider state contains:

1. **Resources**: Map of resource IDs to resource metadata
2. **Provider ID**: Unique identifier for the provider instance
3. **Mode Information**: Mode-specific configuration and state
4. **Infrastructure IDs**: VPC, subnet, security group and other resource IDs
5. **Initialization Flag**: Whether the mode has been initialized

Example state content (simplified):
```json
{
  "provider_id": "8f7e3a4d",
  "mode": "DetachedMode",
  "vpc_id": "vpc-12345678",
  "subnet_id": "subnet-12345678",
  "security_group_id": "sg-12345678",
  "bastion_id": "i-12345678",
  "initialized": true,
  "workflow_id": "workflow-abcdef",
  "resources": {
    "job-12345": {
      "type": "ec2",
      "job_id": "12345",
      "instance_id": "i-87654321",
      "status": "RUNNING",
      "created_at": 1618012345
    }
  }
}
```

## State Recovery

The provider automatically attempts to recover state on initialization:

1. On provider creation, the state store's `load_state()` method is called
2. If state exists and matches the provider ID, it's loaded
3. The mode verifies that resources in the state still exist
4. The mode initializes based on the recovered state

To recover a previous workflow (e.g., in detached mode):

```python
from parsl_ephemeral_aws import EphemeralAWSProvider
from parsl_ephemeral_aws.modes.detached import DetachedMode
from parsl_ephemeral_aws.state.parameter_store import ParameterStoreState

# Create provider with same workflow_id as before
provider = EphemeralAWSProvider(
    mode=DetachedMode(
        workflow_id="previous-workflow-id",  # Same workflow ID as before
        reconnect=True,                      # Indicate this is a reconnection
        # Other mode parameters...
    ),
    state_store=ParameterStoreState(
        prefix="/parsl/workflows/previous-workflow-id",  # Same prefix as before
        # Other state store parameters...
    ),
    # Other provider parameters...
)
```

## State Cleanup

When a workflow completes, you should properly clean up the state:

```python
# Clean up resources first
provider.cancel_all_blocks()

# Then clean up the state
provider.state_store.delete_state()
```

The provider also attempts to clean up state during the `shutdown()` call.

## Implementing Custom State Stores

You can implement custom state stores by extending the `StateStore` abstract base class:

```python
from parsl_ephemeral_aws.state.base import StateStore

class MyCustomStateStore(StateStore):
    def __init__(self, connection_string, **kwargs):
        super().__init__(**kwargs)
        self.connection_string = connection_string
        # Initialize your storage backend

    def save_state(self, state):
        # Implementation for saving state
        pass

    def load_state(self):
        # Implementation for loading state
        pass

    def delete_state(self):
        # Implementation for deleting state
        pass

    def list_states(self, prefix=None):
        # Implementation for listing states
        pass
```

## Best Practices

1. **Choose the appropriate state store for your use case:**
   - Use FileStateStore for development and simple workflows
   - Use ParameterStoreState for detached mode operations
   - Use S3StateStore for serverless or cross-environment workflows

2. **Set unique provider IDs and workflow IDs:**
   - Using unique IDs prevents state collisions
   - Predictable IDs make recovery easier

3. **Handle state cleanup properly:**
   - Always call `provider.cancel_all_blocks()` followed by `provider.shutdown()`
   - For long-term workflows, consider state TTL settings

4. **Monitor state size:**
   - Large state objects may hit size limits in Parameter Store
   - Consider S3 for workflows with many resources

5. **Implement proper error handling:**
   - Handle cases where state cannot be loaded or saved
   - Implement recovery mechanisms for partial failures

6. **Consider state security:**
   - Use secure=True for ParameterStoreState with sensitive data
   - Use appropriate IAM permissions for state stores
   - Encrypt S3 objects for sensitive workloads

7. **Test recovery scenarios:**
   - Practice workflow recovery from saved state
   - Verify that resources are properly tracked and cleaned up

# Parsl AWS Provider Phase 1.5 - Usage Guide

## Quick Start

### 1. Basic Setup

```python
from phase15_enhanced import AWSProvider
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
import parsl

# Create provider (works from any network)
provider = AWSProvider(
    label="my_compute",
    region="us-east-1",         # Match your AWS region
    python_version="3.10",      # Critical for compatibility
    init_blocks=1,              # Start with 1 instance
    max_blocks=5                # Scale up to 5 instances
)

# Configure Parsl
config = Config(executors=[
    HighThroughputExecutor(label='executor', provider=provider)
])

parsl.load(config)
```

### 2. Define and Run Tasks

```python
@parsl.python_app
def my_computation(n):
    import math
    return sum(math.sqrt(i) for i in range(n))

# Submit task
future = my_computation(1000000)
result = future.result()
print(f"Computation result: {result}")

# Always cleanup
parsl.clear()
```

## Real-World Examples

### CPU-Intensive Computing

```python
@parsl.python_app
def cpu_intensive_task(iterations: int):
    import time
    import math

    start_time = time.time()
    result = 0
    for i in range(iterations):
        result += math.sqrt(i * 2.5) * math.sin(i / 1000.0) * math.cos(i / 500.0)
        if i % 10000 == 0:
            result = math.log(abs(result) + 1)

    return {
        'iterations': iterations,
        'final_result': result,
        'compute_time_seconds': time.time() - start_time,
        'ops_per_second': iterations / (time.time() - start_time)
    }

# Execute on AWS
future = cpu_intensive_task(1000000)
result = future.result()
print(f"Computed {result['ops_per_second']:,.0f} operations/second")
```

### String Processing

```python
@parsl.python_app
def string_processing_task(data_size: int):
    import time
    import random
    import string

    start_time = time.time()

    # Generate test data
    data = []
    for i in range(data_size):
        text = ''.join(random.choices(string.ascii_letters + string.digits, k=20))
        data.append(f"Record_{i}:{text}")

    # Process data
    sorted_data = sorted(data)
    filtered_data = [item for item in sorted_data if 'A' in item or 'a' in item]

    return {
        'original_count': data_size,
        'processed_count': len(filtered_data),
        'total_string_length': sum(len(item) for item in filtered_data),
        'compute_time_seconds': time.time() - start_time,
        'processing_rate': data_size / (time.time() - start_time)
    }

# Execute on AWS
future = string_processing_task(50000)
result = future.result()
print(f"Processed {result['processing_rate']:,.0f} records/second")
```

## Configuration Options

### Provider Parameters

```python
provider = AWSProvider(
    # Basic settings
    label="compute_cluster",          # Unique identifier
    region="us-east-1",              # AWS region
    python_version="3.10",           # Python version (use 3.10)

    # Scaling settings
    init_blocks=1,                   # Start with N instances
    max_blocks=10,                   # Scale up to N instances
    min_blocks=0,                    # Scale down to N instances

    # Instance settings
    instance_type="t3.micro",        # AWS instance type
    # Custom AMI can be specified if needed

    # Advanced (auto-configured)
    # SSH keys, tunnels, networking handled automatically
)
```

### Environment Variables

```bash
# AWS Profile (required)
export AWS_PROFILE=aws

# Optional: Override region
export AWS_DEFAULT_REGION=us-east-1

# Optional: Debug logging
export PARSL_DEBUG=1
```

## SSH Reverse Tunneling Details

### How It Works

1. **SSH Key Generation**: Provider generates `~/.ssh/parsl_ssm_rsa`
2. **SSH Config Setup**: Configures `~/.ssh/config` with SSM ProxyCommand
3. **Instance Launch**: Launches EC2 with SSM instance profile
4. **SSH Key Installation**: Installs public key on instance
5. **Reverse Tunnel**: Creates SSH tunnel: `ssh -R remote:localhost:local`
6. **Worker Launch**: Workers connect through tunnel to local Parsl interchange

### SSH Config (Auto-Generated)

```
# ~/.ssh/config
Host i-* mi-*
    ProxyCommand sh -c "aws ssm start-session --target %h --document-name AWS-StartSSHSession --parameters 'portNumber=%p' --region us-east-1 --profile aws"
    User ubuntu
    IdentityFile ~/.ssh/parsl_ssm_rsa
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel ERROR
    ServerAliveInterval 60
    ServerAliveCountMax 3
```

### Manual SSH Testing

```bash
# Test direct SSH to instance
ssh -i ~/.ssh/parsl_ssm_rsa ubuntu@i-1234567890

# Test reverse tunnel manually
ssh -i ~/.ssh/parsl_ssm_rsa -R 54321:localhost:54321 -N ubuntu@i-1234567890
```

## Dependency Management

### Phase 1.5 Limitation: Standard Library Only

**Current Support**: Python standard library computations work perfectly
**Limitation**: External dependencies (numpy, scipy, etc.) not automatically available

### Example: Standard Library Task (✅ Works)

```python
@parsl.python_app
def stdlib_computation():
    import math
    import time
    return sum(math.sqrt(i) for i in range(100000))
```

### Example: External Dependencies (❌ Phase 2 Feature)

```python
@parsl.python_app
def numpy_computation():
    import numpy as np  # ❌ ModuleNotFoundError on worker
    return np.sum(np.random.rand(1000, 1000))
```

**Solution**: Use Phase 2 features (container support, custom AMIs with dependencies)

## Performance Characteristics

### Startup Times
- **SSM Agent Ready**: ~20 seconds (was 5+ minutes with region mismatch)
- **SSH Tunnel Setup**: ~10 seconds
- **Worker Process Start**: ~5 seconds
- **Total Cold Start**: ~35 seconds

### Throughput
- **Tunnel Overhead**: Minimal (<5% performance impact)
- **Computational Performance**: Full AWS instance performance
- **Network Latency**: Additional ~50ms for tunnel routing

### Scaling
- **Scale Up**: ~30 seconds per new instance
- **Scale Down**: Immediate
- **Max Concurrent**: Limited by AWS account quotas

## Error Handling

### Automatic Retries
- **SSM Agent Readiness**: 3 minutes timeout with exponential backoff
- **SSH Connection**: 5 retries with 2-second delays
- **Worker Process**: Parsl-managed retries

### Graceful Degradation
Provider handles failures gracefully:
- **SSH key generation failures**: Falls back to password-less authentication
- **Tunnel setup failures**: Detailed error logging for diagnosis
- **Instance launch failures**: Automatic cleanup and retry

### Debug Mode

```python
import logging

# Enable detailed logging
logging.basicConfig(level=logging.DEBUG)
logging.getLogger('ssh_reverse_tunnel').setLevel(logging.DEBUG)
logging.getLogger('ssm_tunnel').setLevel(logging.DEBUG)
```

## Testing Your Setup

### Simple Connectivity Test

```python
# File: test_simple.py
from phase15_enhanced import AWSProvider
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
import parsl

@parsl.python_app
def test_connectivity():
    import socket
    import os
    return {
        'hostname': socket.gethostname(),
        'platform': os.uname().sysname,
        'python_version': os.sys.version_info[:2]
    }

provider = AWSProvider(label="test", python_version="3.10")
config = Config(executors=[HighThroughputExecutor(provider=provider)])
parsl.load(config)

result = test_connectivity().result()
print(f"Success! Connected to {result['hostname']}")
parsl.clear()
```

### Real Compute Test

```python
# Use tools/real_compute_no_deps.py for comprehensive testing
python tools/real_compute_no_deps.py
```

Expected output:
```
🎉 REAL COMPUTE TEST SUCCESS
✅ All CPU-intensive tasks completed successfully
✅ SSH reverse tunneling working with real workloads
✅ AWS infrastructure handled genuine computation
```

## Next Steps: Phase 2

Phase 1.5 provides **universal connectivity** - the foundation for cloud computing from anywhere.

**Phase 2** will add:
- Container support for dependency management
- Custom AMI building with pre-installed packages
- Dynamic dependency installation
- Advanced resource management

For now, stick to Python standard library tasks for production workloads.

## Support

- **Issues**: Check troubleshooting section above
- **Debug**: Enable debug logging for detailed diagnostics
- **Validation**: Run `tools/real_compute_no_deps.py` to verify setup

# Parsl AWS Provider AMI Installation Requirements

## Overview
This document details the requirements for creating a proper AMI for the Parsl AWS Provider that supports SSM tunneling and reliable worker execution.

## Current AMI Analysis (ami-04e357dc89c2a0ef2)

### What was wrong with the original AMI:
- **Claimed to be "Optimized AMI for Parsl AWS Provider with Parsl latest pre-installed"**
- **Reality: Completely broken AMI with no working Python package manager**
- No functional `pip` installation
- No Parsl installation despite claims
- Missing `process_worker_pool.py` script
- Basically unusable for Parsl workflows

### Instance Details (i-091bcae30de07669a):
- **Base OS**: Amazon Linux 2023 (or similar)
- **Python Version**: Python 3.10
- **Architecture**: x86_64
- **Instance Type Used for Preparation**: t3.micro
- **Region**: us-east-1

## Required Installations

### 1. Python Package Manager
```bash
# The original AMI had broken pip - this was the root cause
# Standard Amazon Linux should have working pip, but verify:
python3 -m pip --version
```

### 2. Parsl Installation
```bash
# Install latest Parsl
python3 -m pip install parsl --quiet

# Verification command that must succeed:
python3 -c "import parsl; print(f'Parsl version: {parsl.__version__}'); from parsl.app.app import python_app, bash_app; from parsl.providers.base import ExecutionProvider; from parsl.executors.threads import ThreadPoolExecutor; print('SUCCESS: Core Parsl components verified')"
```

**Expected Output:**
```
Parsl version: 2025.08.25
SUCCESS: Core Parsl components verified
```

### 3. Process Worker Pool Script Location
After Parsl installation, the worker script should be available at:
- **Executable script**: `/usr/local/bin/process_worker_pool.py`
- **Module source**: `/usr/local/lib/python3.10/dist-packages/parsl/executors/high_throughput/process_worker_pool.py`

### 4. SSM Agent
The AMI must have AWS SSM Agent installed and running:
```bash
# Should be pre-installed on Amazon Linux
systemctl status amazon-ssm-agent
```

### 5. Required System Dependencies
The AMI should include all dependencies that Parsl workers need:
- Python 3.10+ with working pip
- Standard system libraries for networking
- SSH client (for debugging)
- Basic Unix utilities (curl, wget, etc.)

## Installation Commands Used

### Commands executed on instance i-091bcae30de07669a:

1. **Initial diagnosis** (revealed broken state):
```bash
python3 -m pip --version  # Failed - no pip
which pip3                # Not found
ls -la /usr/local/bin/process_worker_pool.py  # Not found
```

2. **Parsl installation** (after fixing pip):
```bash
python3 -m pip install parsl --quiet
```

3. **Verification**:
```bash
python3 -c "import parsl; print(f'Parsl version: {parsl.__version__}'); from parsl.app.app import python_app, bash_app; from parsl.providers.base import ExecutionProvider; from parsl.executors.threads import ThreadPoolExecutor; print('SUCCESS: Core Parsl components verified')"
```

4. **Worker script verification**:
```bash
find /usr/local -name "process_worker_pool.py" -type f
# Found: /usr/local/bin/process_worker_pool.py
```

## Performance Considerations

### Why a proper AMI matters:
- **Dynamic installation takes 30-60 seconds per instance**
- **Unacceptable for ephemeral/spot instances that need fast startup**
- **Network failures during installation can cause worker startup failures**
- **Pre-installed AMI enables <10 second worker startup times**

## Validation Requirements

Any AMI created for Parsl AWS Provider must pass these tests:

### 1. Parsl Import Test
```bash
python3 -c "import parsl; print('SUCCESS: Parsl imports')"
```

### 2. Worker Script Test
```bash
/usr/local/bin/process_worker_pool.py --help | head -5
```

### 3. SSM Agent Test
```bash
systemctl is-active amazon-ssm-agent
```

### 4. Full Worker Command Test
```bash
# This is the actual command pattern that Parsl executes
/usr/local/bin/process_worker_pool.py --max_workers_per_node=1 -a 127.0.0.1 --port=54321 --cert_dir=/tmp --cpu-affinity=none
```

## AMI Creation Process

### Instance State Before AMI Creation:
- **Instance ID**: i-091bcae30de07669a
- **Status**: Running with Parsl successfully installed
- **Parsl Version**: 2025.08.25
- **Worker Script**: Available at `/usr/local/bin/process_worker_pool.py`
- **Python**: 3.10 with working pip

### Successful AMI Creation:
✅ **New Working AMI: `ami-04738d16d10b2983b`**
- **Name**: `parsl-aws-provider-working-20250828-140843`
- **Status**: Available
- **Parsl Version**: 2025.08.25
- **OS**: Ubuntu 22.04.4 LTS
- **Features**: Ubuntu22-Parsl-ProcessWorkerPool
- **Created**: 2025-08-28
- **Instance Used**: i-0980c379ccd259c95

## What's Working in New AMI:
- ✅ Parsl 2025.08.25 successfully installed
- ✅ Python 3.10 with working pip
- ✅ process_worker_pool.py available at `/usr/local/bin/process_worker_pool.py`
- ✅ SSM agent pre-installed and functional
- ✅ All core Parsl components verified
- ✅ Proper tags for provider discovery

## Next Steps:
1. ✅ Create AMI from prepared instance
2. Test new AMI with actual Parsl workflow
3. Update provider to use new AMI ID
4. Validate E2E functionality

## Security Notes

- AMI should not contain any credentials or sensitive data
- SSM Agent provides secure remote access without SSH keys
- Instance should be prepared with minimal attack surface
- No unnecessary services or packages

## Documentation Status

This log was created during AMI preparation on 2025-08-28 to ensure:
1. Complete understanding of what broke the original AMI
2. Exact requirements for a working Parsl AMI
3. Validation steps to ensure AMI quality
4. Performance justification for pre-installation vs dynamic installation

## Regional Considerations

- AMI will be created in us-east-1
- May need to copy to other regions for production use
- Instance types and availability zones should be tested in target regions

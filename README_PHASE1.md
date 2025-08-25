# Phase 1: AWS Provider - Reproduction Guide

This guide shows how to reproduce the Phase 1 AWS Provider functionality. The provider uses proper AWS waiters and comprehensive error handling to ensure reliable operation.

## Prerequisites

1. **AWS Account**: Access to an AWS account with EC2 permissions
2. **AWS CLI**: Configured with AWS credentials
   ```bash
   aws configure
   ```
3. **Python 3.9+**: With virtual environment support
4. **Parsl Familiarity**: Basic understanding of Parsl ExecutionProviders

## Required AWS Permissions

Your AWS credentials need these permissions:
```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "ec2:RunInstances",
                "ec2:TerminateInstances",
                "ec2:DescribeInstances",
                "ec2:DescribeImages",
                "ec2:DescribeVpcs",
                "ec2:CreateSecurityGroup",
                "ec2:DeleteSecurityGroup",
                "ec2:DescribeSecurityGroups",
                "ec2:AuthorizeSecurityGroupIngress",
                "sts:GetCallerIdentity"
            ],
            "Resource": "*"
        }
    ]
}
```

## Setup

1. **Clone and navigate to project**:
   ```bash
   git clone <repository>
   cd parsl-aws-provider
   ```

2. **Create virtual environment**:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Linux/macOS
   # or .venv\Scripts\activate  # Windows
   ```

3. **Install dependencies**:
   ```bash
   pip install parsl boto3 botocore
   ```

4. **Verify AWS access**:
   ```bash
   aws sts get-caller-identity
   ```

## Running the Provider Test

To test Phase 1:

```bash
python tools/phase1.py
```

This test will:
- Create AWSProvider with comprehensive validation
- Use proper AWS waiters for security group creation
- Launch real EC2 instance with comprehensive error handling
- Wait for instance to be visible in AWS (no eventual consistency issues)
- Track job status through complete lifecycle: PENDING → RUNNING
- Clean up all AWS resources with dependency handling
- Verify complete cleanup

**Expected output:**
```
TESTING FINAL BULLETPROOF PHASE 1 PROVIDER
============================================================
1. Creating provider...
✓ Provider created: Phase 1-abc12345
  Security Group: sg-0123456789abcdef0

2. Submitting test job...
✓ Job submitted: job-def67890

3. Checking job status...
Status: [{'job_id': 'job-def67890', 'status': 'PENDING'}]

4. Waiting 60 seconds to let job run...
Status after 60s: [{'job_id': 'job-def67890', 'status': 'RUNNING'}]

✓ FINAL BULLETPROOF PROVIDER TEST: SUCCESS

Cleaning up...
✓ Cleanup completed
```

## Manual Testing

For manual verification, you can use the Phase 1 provider directly:

```python
#!/usr/bin/env python3
import parsl
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
from tools.phase1 import AWSProvider

# Create Phase 1 provider
provider = AWSProvider(
    region='us-east-1',
    instance_type='t3.micro',
    max_blocks=1
)

# Configure Parsl
config = Config(executors=[
    HighThroughputExecutor(
        label='manual_test',
        provider=provider
    )
])
parsl.load(config)

# Define and run job
@parsl.bash_app
def hello_aws():
    return 'echo "Hello from Phase 1 AWS!"; hostname; date; uptime'

# Execute
future = hello_aws()
result = future.result()
print(f"Result: {result}")

# Cleanup
parsl.clear()
provider.cleanup()
```

## Resource Management

### Check for leftover resources:
```bash
python tools/cleanup_resources.py
```

### Clean up any remaining resources:
```bash
# Run interactively (asks for confirmation)
python tools/cleanup_resources.py

# Or modify the script to run with dry_run=False
```

### Monitor resources via AWS CLI:
```bash
# Check instances
aws ec2 describe-instances \
    --filters Name=tag:CreatedBy,Values=AWSProvider \
    --query 'Reservations[*].Instances[*].[InstanceId,State.Name,Tags]'

# Check security groups
aws ec2 describe-security-groups \
    --filters Name=tag:CreatedBy,Values=AWSProvider \
    --query 'SecurityGroups[*].[GroupId,GroupName,Tags]'
```

## What Phase 1 Provides

**Core Functionality**:
- ✅ **Proper Parsl ExecutionProvider interface**: Complete implementation
- ✅ **Robust AWS integration**: Proper waiters, no eventual consistency issues
- ✅ **EC2 instance lifecycle management**: Create → Monitor → Cleanup
- ✅ **Security group management**: Create with rules, proper dependency cleanup
- ✅ **Comprehensive error handling**: Detailed error messages, no silent failures
- ✅ **Job lifecycle tracking**: PENDING → RUNNING → COMPLETED states
- ✅ **Complete resource cleanup**: Terminates instances, deletes security groups
- ✅ **Production-ready logging**: Detailed operation tracking
- ✅ **AWS resource validation**: Pre-flight checks for AMIs, credentials, limits

**Architecture Strengths**:
- **AWS Waiters**: Uses proper `wait_for_security_group()` and `wait_for_instance()`
- **Thorough Validation**: Every AWS operation is confirmed before proceeding
- **Error Transparency**: All failures surface immediately with detailed context
- **Resource Tracking**: Complete visibility into all created AWS resources
- **Clean Dependencies**: Proper cleanup order (instances before security groups)

**Intentional Limitations** (addressed in later phases):
- ❌ No spot instance support (Phase 2)
- ❌ No detached mode with bastion host (Phase 3)
- ❌ No serverless compute - Lambda/ECS (Phase 4)
- ❌ No MPI support (Phase 5)
- ❌ No hibernation support (Phase 5)
- ❌ No advanced networking/custom VPCs (Phase 5)

## Troubleshooting

### Common Issues:

1. **AWS Credentials**: Ensure 'aws' profile is configured
   ```bash
   aws configure list --profile aws
   ```

2. **Permissions**: Provider needs these AWS permissions:
   - EC2: RunInstances, TerminateInstances, DescribeInstances
   - EC2: CreateSecurityGroup, DeleteSecurityGroup, AuthorizeSecurityGroupIngress
   - EC2: DescribeVpcs, DescribeSecurityGroups

3. **Instance Limits**: Check EC2 instance limits in your AWS account

4. **Region**: Ensure you're using a region where you have permissions

5. **Timeout Issues**: Jobs timeout after 5 minutes - increase if needed

### Debug Mode:

Enable detailed logging:
```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

## Success Criteria

Phase 1 is successful when:

1. ✅ **Phase 1 test passes**: `python tools/phase1.py` shows SUCCESS
2. ✅ **Real AWS instances launched**: EC2 instances actually created and visible in AWS
3. ✅ **Complete lifecycle**: PENDING → RUNNING status progression confirmed
4. ✅ **No resource leaks**: All AWS resources cleaned up automatically
5. ✅ **Reproducible**: Multiple runs work consistently
6. ✅ **Error transparency**: All failures surface with detailed error messages
7. ✅ **Production ready**: Comprehensive error handling throughout

## Next Steps

After Phase 1 is confirmed working:
- **Phase 1.5**: Pre-baked AMI with Parsl pre-installed for faster startup
- **Phase 2**: Spot instance support for cost optimization
- **Phase 3**: Detached mode with bastion host for persistent execution
- **Phase 4**: Serverless compute with Lambda and ECS

## Architecture

Phase 1 implements Phase 1 AWS integration:

```
User Code (Parsl Apps)
    ↓
Parsl HighThroughputExecutor
    ↓
AWSProvider (Phase 1)
    ↓
AWS Waiters + Validation
    ↓
AWS EC2 Instances
```

**The Phase 1 provider lifecycle:**

1. **Initialization**: Validates AWS credentials, AMIs, and regions
2. **Security Group**: Creates with proper waiters, no eventual consistency issues
3. **Job Submission**: Launches EC2 with comprehensive error handling
4. **Instance Validation**: Waits for instance to be visible in AWS
5. **Status Tracking**: Monitors PENDING → RUNNING → COMPLETED lifecycle
6. **Resource Cleanup**: Terminates instances, deletes security groups with dependency handling

**Key Architectural Improvements:**
- **AWS Waiters**: `wait_for_security_group()` and `wait_for_instance()`
- **Thorough Validation**: Every AWS operation is confirmed before proceeding
- **Error Transparency**: All failures surface immediately with context
- **Production Logging**: Complete operation tracking and debugging info
- **Clean Dependencies**: Resources cleaned up in proper order

This provides the **Phase 1 foundation** for all future phases.

# Tools Directory

This directory contains utility scripts and tools for the Parsl AWS Provider project.

## Production Tools

### `final_bulletproof_phase1.py`
The production-ready Phase 1 AWS provider with comprehensive error handling.

**Usage:**
```bash
# Test the bulletproof provider
python final_bulletproof_phase1.py

# Use in your own code
from tools.final_bulletproof_phase1 import FinalBulletproofAWSProvider
provider = FinalBulletproofAWSProvider(region='us-east-1')
```

**Features:**
- Bulletproof AWS integration with proper waiters
- Comprehensive error handling and validation
- Real EC2 instance management
- Complete resource cleanup
- Production-ready logging

### `cleanup_resources.py`
Utility to find and clean up AWS resources created during development.

**Usage:**
```bash
# Dry run - show what would be deleted
python cleanup_resources.py

# Actually clean up resources
python -c "
from cleanup_resources import cleanup_resources
cleanup_resources(dry_run=False)
"
```

**Features:**
- Finds all resources tagged with `CreatedBy: ParslBasicAWSProvider` or similar
- Supports dry-run mode for safety
- Handles EC2 instances, security groups, and volumes
- Interactive confirmation before deletion

## Development/Testing Tools

### `test_security_group_waiter.py`
Tool to test and validate AWS security group creation with proper waiters.

**Usage:**
```bash
python test_security_group_waiter.py
```

**Features:**
- Tests security group lifecycle with waiters
- Validates instance launch after security group creation
- Demonstrates proper AWS waiter patterns
- Complete resource cleanup

## Usage Notes

1. **AWS Credentials**: All tools require AWS credentials configured with the 'aws' profile:
   ```bash
   aws configure --profile aws
   ```

2. **Python Environment**: Use the project virtual environment:
   ```bash
   source .venv/bin/activate
   ```

3. **Permissions**: Tools require EC2 permissions for instance and security group management

4. **Safety**: Always test with dry-run options first before making changes

## Integration

These tools are designed to work together:
- Use `final_bulletproof_phase1.py` for production provider functionality
- Use `cleanup_resources.py` to clean up after testing/development
- Use `test_security_group_waiter.py` to validate AWS integration patterns

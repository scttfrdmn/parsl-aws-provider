---
name: Bug report
about: Create a report to help us improve
title: '[BUG] '
labels: bug
assignees: ''

---

**Describe the bug**
A clear and concise description of what the bug is.

**To Reproduce**
Steps to reproduce the behavior:
1. Configuration used:
```python
# Your provider configuration here
```
2. Code sample that reproduces the issue:
```python
# Your code here
```
3. Error messages (if any):
```
# Error output
```

**Expected behavior**
A clear and concise description of what you expected to happen.

**Environment (please complete the following information):**
 - OS: [e.g. Ubuntu 22.04, macOS 14.0]
 - Python version: [e.g. 3.12.0]
 - Parsl version: [e.g. 1.2.0]
 - Provider version: [e.g. 0.1.0]
 - AWS Region: [e.g. us-east-1]
 - Instance Types: [e.g. t3.micro]
 - Operating Mode: [standard, detached, serverless]

**AWS-specific information**
- Are you using spot instances? [Yes/No]
- Are you using a custom VPC? [Yes/No]
- Are you using the default AMI or a custom one? [Default/Custom]
- Are there any VPC settings, security groups, or IAM roles that might be relevant?

**Additional context**
Add any other context about the problem here. This might include:
- AWS console errors or logs
- CloudWatch logs
- IAM permission issues
- Network connectivity information
- Any recent changes to your AWS environment

**Debugging information**
If possible, please provide debugging output by setting:
```python
import logging
logging.basicConfig(level=logging.DEBUG)
logging.getLogger('parsl_ephemeral_aws').setLevel(logging.DEBUG)
```

**Screenshots**
If applicable, add screenshots to help explain your problem.

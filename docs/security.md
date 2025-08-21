# Security Best Practices

This document outlines security best practices for using the Parsl Ephemeral AWS Provider in your AWS environment.

## AWS IAM Permissions

### Principle of Least Privilege

The Parsl Ephemeral AWS Provider requires specific IAM permissions to function. Following the principle of least privilege, here's a baseline IAM policy that grants only the necessary permissions:

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
                "ec2:DescribeInstanceStatus",
                "ec2:CreateTags",
                "ec2:CreateVpc",
                "ec2:CreateSubnet",
                "ec2:CreateSecurityGroup",
                "ec2:AuthorizeSecurityGroupIngress",
                "ec2:AuthorizeSecurityGroupEgress",
                "ec2:DescribeVpcs",
                "ec2:DescribeSubnets",
                "ec2:DescribeSecurityGroups",
                "ec2:DeleteVpc",
                "ec2:DeleteSubnet",
                "ec2:DeleteSecurityGroup",
                "ec2:ModifyVpcAttribute",
                "ec2:CreateInternetGateway",
                "ec2:AttachInternetGateway",
                "ec2:DetachInternetGateway",
                "ec2:DeleteInternetGateway",
                "ec2:CreateRouteTable",
                "ec2:CreateRoute",
                "ec2:DeleteRouteTable",
                "ec2:AssociateRouteTable",
                "ec2:DisassociateRouteTable",
                "ec2:DescribeInternetGateways",
                "ec2:DescribeRouteTables"
            ],
            "Resource": "*"
        },
        {
            "Effect": "Allow",
            "Action": [
                "ssm:PutParameter",
                "ssm:GetParameter",
                "ssm:DeleteParameter",
                "ssm:GetParametersByPath",
                "ssm:DescribeParameters"
            ],
            "Resource": "arn:aws:ssm:*:*:parameter/parsl/*"
        },
        {
            "Effect": "Allow",
            "Action": [
                "s3:CreateBucket",
                "s3:PutObject",
                "s3:GetObject",
                "s3:DeleteObject",
                "s3:ListBucket",
                "s3:GetBucketLocation"
            ],
            "Resource": [
                "arn:aws:s3:::parsl-*",
                "arn:aws:s3:::parsl-*/*"
            ]
        }
    ]
}
```

### Additional Permissions for Specific Modes

For specific operating modes, additional permissions may be required:

#### Detached Mode
```json
{
    "Effect": "Allow",
    "Action": [
        "iam:PassRole",
        "cloudformation:CreateStack",
        "cloudformation:DescribeStacks",
        "cloudformation:DeleteStack",
        "cloudformation:DescribeStackEvents"
    ],
    "Resource": "*"
}
```

#### Serverless Mode (Lambda)
```json
{
    "Effect": "Allow",
    "Action": [
        "lambda:CreateFunction",
        "lambda:InvokeFunction",
        "lambda:DeleteFunction",
        "lambda:GetFunction",
        "lambda:UpdateFunctionCode",
        "lambda:UpdateFunctionConfiguration",
        "lambda:AddPermission",
        "lambda:RemovePermission",
        "iam:PassRole"
    ],
    "Resource": "*"
}
```

#### Serverless Mode (ECS/Fargate)
```json
{
    "Effect": "Allow",
    "Action": [
        "ecs:CreateCluster",
        "ecs:DeleteCluster",
        "ecs:RegisterTaskDefinition",
        "ecs:DeregisterTaskDefinition",
        "ecs:ListTasks",
        "ecs:DescribeTasks",
        "ecs:RunTask",
        "ecs:StopTask",
        "ecs:DescribeServices",
        "ecs:CreateService",
        "ecs:DeleteService",
        "ecs:UpdateService",
        "iam:PassRole"
    ],
    "Resource": "*"
}
```

## IAM Instance Profiles

### Worker Instance Profile

For EC2 worker instances, you should create an IAM role and instance profile with only the permissions required for your specific workload. Here's an example minimal policy for worker instances:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents"
            ],
            "Resource": "arn:aws:logs:*:*:*"
        },
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetObject",
                "s3:PutObject",
                "s3:ListBucket"
            ],
            "Resource": [
                "arn:aws:s3:::your-data-bucket",
                "arn:aws:s3:::your-data-bucket/*"
            ]
        }
    ]
}
```

### Bastion Instance Profile (Detached Mode)

For the bastion host in detached mode, a specific role is required to manage EC2 instances and communicate with Parameter Store:

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
                "ec2:CreateTags"
            ],
            "Resource": "*"
        },
        {
            "Effect": "Allow",
            "Action": [
                "ssm:PutParameter",
                "ssm:GetParameter",
                "ssm:DeleteParameter",
                "ssm:GetParametersByPath"
            ],
            "Resource": "arn:aws:ssm:*:*:parameter/parsl/workflows/*"
        },
        {
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents"
            ],
            "Resource": "arn:aws:logs:*:*:*"
        },
        {
            "Effect": "Allow",
            "Action": [
                "cloudwatch:PutMetricData"
            ],
            "Resource": "*"
        }
    ]
}
```

## Network Security

### VPC Security

By default, the provider creates a VPC with a single public subnet and security group. For enhanced security:

1. **Use Existing VPC**: Provide your own VPC ID to use an existing VPC with proper security controls
   ```python
   provider = EphemeralAWSProvider(
       mode=StandardMode(
           vpc_id="vpc-12345678",
           subnet_id="subnet-12345678",
           security_group_id="sg-12345678",
           create_vpc=False,
           # Other parameters...
       ),
       # Other parameters...
   )
   ```

2. **Restrict Security Group Rules**: Create a security group with minimal access rules
   ```python
   # First create a security group with specific rules
   ec2 = boto3.client('ec2', region_name='us-west-2')
   response = ec2.create_security_group(
       GroupName='parsl-restricted-sg',
       Description='Restricted security group for Parsl workers',
       VpcId='vpc-12345678'
   )
   security_group_id = response['GroupId']

   # Add minimal ingress rules
   ec2.authorize_security_group_ingress(
       GroupId=security_group_id,
       IpPermissions=[
           {
               'IpProtocol': 'tcp',
               'FromPort': 22,
               'ToPort': 22,
               'IpRanges': [{'CidrIp': 'your-ip-address/32'}]
           },
           {
               'IpProtocol': 'tcp',
               'FromPort': 55000,
               'ToPort': 55100,
               'IpRanges': [{'CidrIp': 'your-ip-address/32'}]
           }
       ]
   )

   # Then use this security group in your provider
   provider = EphemeralAWSProvider(
       mode=StandardMode(
           security_group_id=security_group_id,
           # Other parameters...
       ),
       # Other parameters...
   )
   ```

3. **Use Private Subnets with NAT Gateway**: For enhanced security, use private subnets with a NAT gateway
   ```python
   provider = EphemeralAWSProvider(
       mode=StandardMode(
           subnet_id="private-subnet-12345678",
           use_public_ips=False,
           # Other parameters...
       ),
       # Other parameters...
   )
   ```

### Inbound Traffic Control

By default, worker instances in Standard or Detached modes will need inbound access from your client machine. For better security:

1. **Restrict Source IP Ranges**: Limit inbound access to your IP address or CIDR range
2. **Use a VPN or AWS Direct Connect**: Establish a private connection to your AWS environment
3. **Consider Detached Mode with SSM**: Use AWS Systems Manager for secure bastion access without opening SSH ports

### Encryption in Transit

All communication between the provider and AWS services uses HTTPS/TLS. For worker-to-worker communication:

1. **Within VPC**: Communication within a VPC is secure by default
2. **Between Workers and Client**: Use HTTPS or SSH tunneling for secure communication

## Data Security

### Encryption at Rest

1. **S3 State Store**: Enable default encryption for your S3 bucket
   ```python
   provider = EphemeralAWSProvider(
       state_store=S3StateStore(
           bucket="my-parsl-state-bucket",
           encryption="AES256",  # or "aws:kms"
           # Other parameters...
       ),
       # Other parameters...
   )
   ```

2. **Parameter Store**: Use SecureString type for sensitive parameters
   ```python
   provider = EphemeralAWSProvider(
       state_store=ParameterStoreState(
           prefix="/parsl/workflows/my-workflow",
           secure=True,  # Uses SecureString type with AWS managed key
           # Other parameters...
       ),
       # Other parameters...
   )
   ```

3. **Worker Volumes**: Enable EBS volume encryption for worker instances
   ```python
   provider = EphemeralAWSProvider(
       mode=StandardMode(
           block_device_mappings=[
               {
                   'DeviceName': '/dev/sda1',
                   'Ebs': {
                       'VolumeSize': 50,
                       'VolumeType': 'gp3',
                       'DeleteOnTermination': True,
                       'Encrypted': True
                   }
               }
           ],
           # Other parameters...
       ),
       # Other parameters...
   )
   ```

### Credentials Management

1. **Avoid Hardcoded Credentials**: Never include AWS credentials in your code
   ```python
   # DON'T DO THIS
   provider = EphemeralAWSProvider(
       aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
       aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
       # Other parameters...
   )

   # INSTEAD, use environment variables, IAM roles, or AWS profiles
   provider = EphemeralAWSProvider(
       aws_profile="parsl-profile",  # Or omit to use default credentials
       # Other parameters...
   )
   ```

2. **Use IAM Roles**: For EC2 instances and serverless functions, use IAM roles instead of access keys
3. **Rotate Credentials**: If using access keys, rotate them regularly according to your security policy

## Auditing and Monitoring

### Resource Tagging

All resources created by the provider are tagged for tracking and billing. You can add custom tags:

```python
provider = EphemeralAWSProvider(
    tags={
        "Project": "MyDataScience",
        "Environment": "Development",
        "Owner": "username@example.com",
        "CostCenter": "12345"
    },
    # Other parameters...
)
```

### CloudTrail and CloudWatch

1. **Enable CloudTrail**: Track all API calls made by the provider
2. **Set up CloudWatch Alarms**: Monitor for unexpected resource usage or costs
3. **Log Analysis**: Analyze CloudWatch Logs for security events

## Security Updates

1. **Keep Dependencies Updated**: Regularly update the provider and its dependencies
2. **AMI Updates**: Use up-to-date AMIs with security patches
3. **Security Bulletins**: Monitor AWS security bulletins for relevant issues

## Compliance Considerations

If your workflows involve sensitive or regulated data, consider:

1. **VPC Endpoints**: Use VPC endpoints to keep traffic within the AWS network
2. **AWS Artifact**: Access compliance reports for your regulatory needs
3. **AWS Config**: Continuously monitor and assess your AWS resource configurations
4. **AWS Security Hub**: Comprehensive view of security alerts and compliance status

## Emergency Shutdown

In case of a security incident or unintended resource usage:

```python
# Create provider with existing workflow ID
provider = EphemeralAWSProvider(
    mode=DetachedMode(
        workflow_id="compromised-workflow-id",
        # Minimal parameters needed for connection
    ),
    # Other minimal parameters...
)

# Emergency cleanup - force termination of all resources
provider.cleanup_all(force=True)
```

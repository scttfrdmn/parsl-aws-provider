# Network Prerequisites

Starting with v0.7.0, `EphemeralProvider` no longer creates VPC, subnet, or
security-group resources.  You must provision these before instantiating the
provider and pass their IDs as required constructor arguments.

---

## Required Resources

| Parameter | Description |
|-----------|-------------|
| `vpc_id` | ID of an existing VPC (e.g. `vpc-0abc123`) |
| `subnet_id` | ID of a public subnet inside that VPC with a route to an Internet Gateway |
| `security_group_id` | ID of a security group that allows the outbound traffic your workers need |

If any of the three is missing, the constructor raises `ValueError` immediately.

---

## Minimum Security Group Rules

Workers only need **outbound** internet access (to reach the Parsl interchange and
PyPI/package mirrors).  No inbound rules are required by the provider itself.

| Direction | Protocol | Port | CIDR | Purpose |
|-----------|----------|------|------|---------|
| Egress | TCP | 443 | 0.0.0.0/0 | HTTPS (pip install, AWS API) |
| Egress | TCP | 54000–55000 | interchange IP/32 | Parsl ZMQ interchange |
| Egress | All | All | 0.0.0.0/0 | (permissive alternative) |

The egress rule for 54000–55000 assumes the interchange is reachable from EC2.
If your driver is behind NAT it is not — see
[Optional: an EC2 Instance Connect Endpoint](#optional-an-ec2-instance-connect-endpoint),
which removes that rule entirely.

---

## Optional: an EC2 Instance Connect Endpoint

Needed only for `instance_connect_endpoint_id`, which lets workers reach an
interchange on a driver that has no routable address — a laptop behind home or
office NAT
([#134](https://github.com/scttfrdmn/parsl-ephemeral-provider/issues/134)). See
[Reverse tunnels](operating_modes.md#reverse-tunnels) for what it does and when
to use it.

Like the VPC, subnet, and security group, the endpoint is **caller-supplied and
never created by the provider**: creation takes several minutes, which is not
something to discover inside `initialize()`. A bad ID fails there anyway,
alongside the other three, rather than surfacing later as an `ssh` process
exiting for no visible reason.

One endpoint serves every instance in its subnet, so this is a one-off.

```bash
aws ec2 create-instance-connect-endpoint \
  --subnet-id subnet-0def5678 \
  --security-group-ids sg-09ab0000 \
  --region us-east-1
# wait for State=create-complete, then:
aws ec2 describe-instance-connect-endpoints \
  --query 'InstanceConnectEndpoints[].{Id:InstanceConnectEndpointId,State:State}'
```

Terraform:

```hcl
resource "aws_ec2_instance_connect_endpoint" "parsl" {
  subnet_id          = aws_subnet.parsl_public.id
  security_group_ids = [aws_security_group.parsl_workers.id]
}

output "parsl_eice_id" { value = aws_ec2_instance_connect_endpoint.parsl.id }
```

The security group needs **inbound TCP 22 from the endpoint** — that is the only
port the tunnel ever opens, and the ZMQ ports ride inside the SSH session:

| Direction | Protocol | Port | Source | Purpose |
|-----------|----------|------|--------|---------|
| Ingress | TCP | 22 | the endpoint's SG, or the VPC CIDR | EICE reaches sshd |

With a tunnel in use the **egress rule for 54000–55000 is not needed at all**:
the interchange arrives on the worker's own loopback, so nothing leaves the VPC
for it. The subnet does not need a public IP or a NAT gateway either, which is
the configuration this was verified against.

---

## Minimum IAM Permissions

Do not copy a policy out of a document — this one had drifted into being
unusable. Generate the current policy for the principal that *runs* the
provider:

```python
import json

from parsl_ephemeral_provider import EphemeralComputeProvider

print(json.dumps(EphemeralComputeProvider.minimum_iam_policy(), indent=2))
```

It is a `@staticmethod`, so no provider instance is needed, and it describes the
base `EphemeralProvider` just as well despite living on the Globus subclass.
Every action it grants has a call site in the package, and a unit test asserts
that in both directions.

The seven-action policy previously printed here failed at **construction**:
`__init__` ends by calling `initialize()`, which resolves the AMI from SSM and
creates a launch template, so it needed `ssm:GetParameter`,
`ec2:CreateLaunchTemplate`, `ec2:DescribeInstanceTypes`, `ec2:DescribeVpcs`, and
`sts:GetCallerIdentity` — none of which it granted
([#195](https://github.com/scttfrdmn/parsl-ephemeral-provider/issues/195)).

If you use a reverse tunnel, the driver needs three more actions, which
`minimum_iam_policy()` does not include because the feature is opt-in. Generate
them the same way rather than copying:

```python
import json

from parsl_ephemeral_provider.network import eice_iam_statements

print(json.dumps(eice_iam_statements(endpoint_id="eice-0abc1234"), indent=2))
```

The grant is `ec2-instance-connect:OpenTunnel` **conditioned on
`remotePort == 22`**, `SendSSHPublicKey` conditioned on
`ec2:osuser == ec2-user`, and describe. Both conditions are load-bearing and
IAM's own evaluator is asked to confirm them in
`tests/aws/test_eice_tunnel_e2e.py`: unconditioned, `OpenTunnel` reaches any port
on any instance the holder can name, and `SendSSHPublicKey` authorises a key for
any OS user including root.

The **workers** are separate and need much less: attaching the AWS-managed
`AmazonSSMManagedInstanceCore` to the instance profile is the whole requirement,
which `auto_create_instance_profile=True` does for you.

---

## Terraform Snippet

```hcl
resource "aws_vpc" "parsl" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags = { Name = "parsl-workers" }
}

resource "aws_internet_gateway" "parsl" {
  vpc_id = aws_vpc.parsl.id
}

resource "aws_subnet" "parsl_public" {
  vpc_id                  = aws_vpc.parsl.id
  cidr_block              = "10.0.1.0/24"
  map_public_ip_on_launch = true
  tags = { Name = "parsl-public" }
}

resource "aws_route_table" "parsl_public" {
  vpc_id = aws_vpc.parsl.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.parsl.id
  }
}

resource "aws_route_table_association" "parsl_public" {
  subnet_id      = aws_subnet.parsl_public.id
  route_table_id = aws_route_table.parsl_public.id
}

resource "aws_security_group" "parsl_workers" {
  name   = "parsl-workers"
  vpc_id = aws_vpc.parsl.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

output "parsl_vpc_id"    { value = aws_vpc.parsl.id }
output "parsl_subnet_id" { value = aws_subnet.parsl_public.id }
output "parsl_sg_id"     { value = aws_security_group.parsl_workers.id }
```

---

## CloudFormation Snippet

```yaml
Resources:
  ParslVPC:
    Type: AWS::EC2::VPC
    Properties:
      CidrBlock: 10.0.0.0/16
      EnableDnsSupport: true
      EnableDnsHostnames: true

  ParslIGW:
    Type: AWS::EC2::InternetGateway

  ParslIGWAttachment:
    Type: AWS::EC2::VPCGatewayAttachment
    Properties:
      VpcId: !Ref ParslVPC
      InternetGatewayId: !Ref ParslIGW

  ParslSubnet:
    Type: AWS::EC2::Subnet
    Properties:
      VpcId: !Ref ParslVPC
      CidrBlock: 10.0.1.0/24
      MapPublicIpOnLaunch: true

  ParslRouteTable:
    Type: AWS::EC2::RouteTable
    Properties:
      VpcId: !Ref ParslVPC

  ParslRoute:
    Type: AWS::EC2::Route
    Properties:
      RouteTableId: !Ref ParslRouteTable
      DestinationCidrBlock: 0.0.0.0/0
      GatewayId: !Ref ParslIGW

  ParslSubnetRTA:
    Type: AWS::EC2::SubnetRouteTableAssociation
    Properties:
      SubnetId: !Ref ParslSubnet
      RouteTableId: !Ref ParslRouteTable

  ParslSG:
    Type: AWS::EC2::SecurityGroup
    Properties:
      GroupDescription: Parsl worker security group
      VpcId: !Ref ParslVPC
      SecurityGroupEgress:
        - IpProtocol: "-1"
          CidrIp: 0.0.0.0/0

Outputs:
  VpcId:    { Value: !Ref ParslVPC }
  SubnetId: { Value: !Ref ParslSubnet }
  SgId:     { Value: !GetAtt ParslSG.GroupId }
```

---

## Using the IDs with the Provider

```python
from parsl_ephemeral_provider import EphemeralProvider

provider = EphemeralProvider(
    region="us-east-1",
    instance_type="t3.small",
    vpc_id="vpc-0abc1234",
    subnet_id="subnet-0def5678",
    security_group_id="sg-09ab0000",
    auto_create_instance_profile=True,
)
```

For E2E tests, export the IDs as environment variables:

```bash
export AWS_TEST_VPC_ID=vpc-0abc1234
export AWS_TEST_SUBNET_ID=subnet-0def5678
export AWS_TEST_SG_ID=sg-09ab0000
AWS_PROFILE=aws pytest tests/aws/ -m aws --no-cov -v
```

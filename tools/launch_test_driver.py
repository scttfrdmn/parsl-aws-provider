#!/usr/bin/env python3
"""
Launch a temporary EC2 "driver" instance for end-to-end Parsl testing.

Running the Parsl driver on EC2 sidesteps NAT — workers launched in the
same VPC can reach the interchange directly without port forwarding.

Usage:
    AWS_PROFILE=aws python3 tools/launch_test_driver.py [--region us-west-2]

The script:
  1. Launches a t3.small Amazon Linux 2023 instance in the default VPC
     with AdministratorAccess (benchmark-builder-profile).
  2. Waits for SSM to become available (~1-2 min).
  3. Installs Python deps and copies the integration script.
  4. Prints the aws ssm start-session command to connect and run the test.
  5. On Ctrl-C or after --timeout seconds, terminates the driver.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import argparse
import logging
import os
import signal
import subprocess  # nosec B404
import sys
import time

import boto3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("launch-driver")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_REGION = os.environ.get("AWS_TEST_REGION", "us-west-2")
AWS_PROFILE = os.environ.get("AWS_TEST_PROFILE", "aws")

# Amazon Linux 2023 AMIs per region (from parsl_ephemeral_aws/constants.py)
AL2023_AMI = {
    "us-east-1": "ami-080e1f13689e07408",
    "us-east-2": "ami-03d21eed81858c120",
    "us-west-1": "ami-0d5b7dce3973d8817",
    "us-west-2": "ami-075b5421f670d735c",
    "eu-west-1": "ami-09961115387019735",
    "eu-central-1": "ami-06ca3d9ec5caa8d5c",
    "ap-northeast-1": "ami-0df7d959e1ae99093",
    "ap-southeast-1": "ami-05400835b426ad39e",
    "ap-southeast-2": "ami-068d77de57cf72650",
}

# IAM instance profile that has AdministratorAccess (for creating workers)
DRIVER_INSTANCE_PROFILE = "benchmark-builder-profile"

# UserData: install python3.11, git, clone the repo, install the package.
# AL2023 ships python3.9 by default; parsl>=2026.1.5 requires Python 3.10+.
DRIVER_USER_DATA = """\
#!/bin/bash
set -euo pipefail
dnf install -y git python3.11 python3.11-pip
ln -sf /usr/bin/python3.11 /usr/bin/python3
cd /home/ec2-user
git clone https://github.com/scttfrdmn/parsl-aws-provider.git
cd parsl-aws-provider
pip3.11 install --quiet -e '.[test]'
chown -R ec2-user:ec2-user /home/ec2-user/parsl-aws-provider
echo "DRIVER_READY" >> /var/log/driver-setup.log
"""


def get_default_vpc(ec2) -> dict:
    """Return the default VPC (id and CIDR block)."""
    resp = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])
    vpcs = resp.get("Vpcs", [])
    if not vpcs:
        raise RuntimeError("No default VPC found in this region.")
    return {"VpcId": vpcs[0]["VpcId"], "CidrBlock": vpcs[0]["CidrBlock"]}


def get_default_subnet(ec2, region: str) -> str:
    """Return the first default-for-AZ subnet in the default VPC."""
    resp = ec2.describe_subnets(
        Filters=[
            {"Name": "defaultForAz", "Values": ["true"]},
            {"Name": "availabilityZone", "Values": [f"{region}a"]},
        ]
    )
    subnets = resp.get("Subnets", [])
    if not subnets:
        resp = ec2.describe_subnets(
            Filters=[{"Name": "defaultForAz", "Values": ["true"]}]
        )
        subnets = resp.get("Subnets", [])
    if not subnets:
        raise RuntimeError(
            "No default subnet found — create one or specify --subnet-id"
        )
    return subnets[0]["SubnetId"]


def get_or_create_driver_sg(ec2, vpc_id: str, vpc_cidr: str) -> str:
    """Return (creating if needed) a security group for the Parsl driver.

    The driver's interchange must accept inbound ZMQ connections from workers
    on ports 54000-55000.  The default VPC security group only allows inbound
    from instances in the same SG; workers created by the provider use a
    separate SG, so a dedicated driver SG is required.
    """
    sg_name = "parsl-test-driver-sg"
    resp = ec2.describe_security_groups(
        Filters=[
            {"Name": "group-name", "Values": [sg_name]},
            {"Name": "vpc-id", "Values": [vpc_id]},
        ]
    )
    if resp.get("SecurityGroups"):
        sg_id = resp["SecurityGroups"][0]["GroupId"]
        log.info("Re-using existing driver SG %s", sg_id)
        return sg_id

    resp = ec2.create_security_group(
        GroupName=sg_name,
        Description="Parsl test driver: allows ZMQ worker connections",
        VpcId=vpc_id,
        TagSpecifications=[
            {
                "ResourceType": "security-group",
                "Tags": [
                    {"Key": "Name", "Value": sg_name},
                    {"Key": "Purpose", "Value": "ParslIntegrationTestDriver"},
                    {"Key": "AutoCleanup", "Value": "true"},
                ],
            }
        ],
    )
    sg_id = resp["GroupId"]
    log.info("Created driver SG %s", sg_id)

    # Allow inbound ZMQ from anywhere in the VPC (workers connect outbound to us)
    ec2.authorize_security_group_ingress(
        GroupId=sg_id,
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": 54000,
                "ToPort": 55000,
                "IpRanges": [
                    {
                        "CidrIp": vpc_cidr,
                        "Description": "Parsl HTEX ZMQ worker connections",
                    }
                ],
            }
        ],
    )
    return sg_id


def get_al2023_ami(ec2, region: str) -> str:
    """Dynamically look up the latest AL2023 AMI for the region."""
    fallback = AL2023_AMI.get(region)
    try:
        resp = ec2.describe_images(
            Owners=["amazon"],
            Filters=[
                {"Name": "name", "Values": ["al2023-ami-2023*-x86_64"]},
                {"Name": "state", "Values": ["available"]},
            ],
        )
        images = sorted(
            resp.get("Images", []), key=lambda i: i["CreationDate"], reverse=True
        )
        if images:
            ami = images[0]["ImageId"]
            log.info(
                "Latest AL2023 AMI for %s: %s (%s)", region, ami, images[0]["Name"]
            )
            return ami
    except Exception as exc:
        log.warning("AMI lookup failed (%s); using hardcoded fallback", exc)
    if fallback:
        return fallback
    raise ValueError(f"No AL2023 AMI available for region {region}.")


def launch_driver(region: str) -> str:
    """Launch the driver instance; return its instance ID."""
    session = boto3.Session(profile_name=AWS_PROFILE, region_name=region)
    ec2 = session.client("ec2")

    ami = get_al2023_ami(ec2, region)

    vpc = get_default_vpc(ec2)
    vpc_id = vpc["VpcId"]
    vpc_cidr = vpc["CidrBlock"]
    log.info("Using default VPC %s (CIDR %s) in %s", vpc_id, vpc_cidr, region)

    subnet_id = get_default_subnet(ec2, region)
    log.info("Using default subnet %s", subnet_id)

    # Create (or reuse) a driver SG that allows worker ZMQ connections inbound.
    # The default VPC SG only allows inbound from the same SG; workers use a
    # separate SG created by the provider.
    sg_id = get_or_create_driver_sg(ec2, vpc_id, vpc_cidr)

    resp = ec2.run_instances(
        ImageId=ami,
        InstanceType="t3.small",
        MinCount=1,
        MaxCount=1,
        SubnetId=subnet_id,
        SecurityGroupIds=[sg_id],
        IamInstanceProfile={"Name": DRIVER_INSTANCE_PROFILE},
        UserData=DRIVER_USER_DATA,
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Name", "Value": "parsl-test-driver"},
                    {"Key": "Purpose", "Value": "ParslIntegrationTestDriver"},
                    {"Key": "AutoCleanup", "Value": "true"},
                ],
            }
        ],
        MetadataOptions={"HttpTokens": "required"},  # IMDSv2
    )
    instance_id = resp["Instances"][0]["InstanceId"]
    log.info("Launched driver instance %s (SG %s)", instance_id, sg_id)
    return instance_id


def wait_for_ssm(region: str, instance_id: str, timeout: int = 300) -> None:
    """Poll SSM until the instance appears as Online."""
    session = boto3.Session(profile_name=AWS_PROFILE, region_name=region)
    ssm = session.client("ssm")
    deadline = time.time() + timeout
    log.info(
        "Waiting for SSM to report %s as Online (up to %ds) ...", instance_id, timeout
    )
    while time.time() < deadline:
        resp = ssm.describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
        )
        infos = resp.get("InstanceInformationList", [])
        if infos and infos[0].get("PingStatus") == "Online":
            log.info("SSM Online: %s", instance_id)
            return
        time.sleep(15)
    raise TimeoutError(f"Instance {instance_id} not SSM-Online after {timeout}s")


def terminate_instance(region: str, instance_id: str) -> None:
    """Terminate the driver instance."""
    session = boto3.Session(profile_name=AWS_PROFILE, region_name=region)
    ec2 = session.client("ec2")
    try:
        ec2.terminate_instances(InstanceIds=[instance_id])
        log.info("Terminated driver instance %s", instance_id)
    except Exception as exc:
        log.warning("Could not terminate %s: %s", instance_id, exc)


def run_integration_test(region: str, instance_id: str) -> int:
    """Run the integration script on the driver via SSM send-command; stream output."""
    log.info("Running integration test on %s ...", instance_id)
    cmd = [
        "aws",
        "ssm",
        "send-command",
        "--profile",
        AWS_PROFILE,
        "--region",
        region,
        "--instance-ids",
        instance_id,
        "--document-name",
        "AWS-RunShellScript",
        "--parameters",
        (
            "commands=["
            "'cd /home/ec2-user/parsl-aws-provider',"
            f"'AWS_TEST_REGION={region} python3 examples/parsl_aws_integration.py'"
            "]"
        ),
        "--timeout-seconds",
        "720",
        "--query",
        "Command.CommandId",
        "--output",
        "text",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)  # nosec B603 B607
    if result.returncode != 0:
        log.error("send-command failed: %s", result.stderr)
        return 1

    command_id = result.stdout.strip()
    log.info("SSM command ID: %s  (polling for completion)", command_id)

    deadline = time.time() + 720
    while time.time() < deadline:
        time.sleep(15)
        poll = subprocess.run(  # nosec B603 B607
            [
                "aws",
                "ssm",
                "get-command-invocation",
                "--profile",
                AWS_PROFILE,
                "--region",
                region,
                "--command-id",
                command_id,
                "--instance-id",
                instance_id,
                "--output",
                "json",
            ],
            capture_output=True,
            text=True,
        )
        if poll.returncode != 0:
            continue
        import json

        inv = json.loads(poll.stdout)
        status = inv.get("StatusDetails", "")
        log.info("Status: %s", status)
        if status in ("Success", "Failed", "TimedOut", "Cancelled"):
            stdout = inv.get("StandardOutputContent", "")
            stderr = inv.get("StandardErrorContent", "")
            if stdout:
                print("\n--- STDOUT ---\n" + stdout)
            if stderr:
                print("\n--- STDERR ---\n" + stderr)
            return 0 if status == "Success" else 1
    log.error("Timed out waiting for command to complete")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch Parsl test driver on EC2")
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Open an interactive SSM shell instead of running the test automatically",
    )
    args = parser.parse_args()
    region = args.region

    instance_id = None

    def _cleanup(signum=None, frame=None):
        if instance_id:
            terminate_instance(region, instance_id)
        sys.exit(0)

    signal.signal(signal.SIGINT, _cleanup)
    signal.signal(signal.SIGTERM, _cleanup)

    try:
        instance_id = launch_driver(region)
        wait_for_ssm(region, instance_id)

        if args.interactive:
            log.info("=" * 60)
            log.info("Driver ready.  Run the test interactively:")
            log.info("")
            log.info(
                "  aws ssm start-session --profile %s --region %s --target %s",
                AWS_PROFILE,
                region,
                instance_id,
            )
            log.info("")
            log.info("  Inside the session:")
            log.info("  cd /home/ec2-user/parsl-aws-provider")
            log.info(
                "  AWS_TEST_REGION=%s python3 examples/parsl_aws_integration.py", region
            )
            log.info("=" * 60)
            log.info("Press Ctrl-C to terminate the driver instance when done.")
            while True:
                time.sleep(60)
        else:
            rc = run_integration_test(region, instance_id)
            return rc

    finally:
        if instance_id:
            terminate_instance(region, instance_id)

    return 0


if __name__ == "__main__":
    sys.exit(main())

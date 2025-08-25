#!/usr/bin/env python3
"""
Test security group creation using proper waiter approach.
"""

import boto3
import time
import uuid
from botocore.exceptions import ClientError


def wait_for_security_group(ec2_client, group_id, max_attempts=30, delay=2):
    """Wait for security group to be available"""
    print(f"Waiting for security group {group_id} to be available...")

    for attempt in range(max_attempts):
        try:
            response = ec2_client.describe_security_groups(GroupIds=[group_id])
            if response["SecurityGroups"]:
                sg = response["SecurityGroups"][0]
                print(f"✓ Security group {group_id} is ready: {sg['GroupName']}")
                return sg
        except ClientError as e:
            if e.response["Error"]["Code"] != "InvalidGroup.NotFound":
                print(f"Unexpected error: {e}")
                raise

        print(f"  Attempt {attempt + 1}/{max_attempts}: waiting {delay}s...")
        time.sleep(delay)

    raise TimeoutError(
        f"Security group {group_id} not available after {max_attempts} attempts"
    )


def test_security_group_lifecycle():
    """Test complete security group lifecycle with proper waiters."""

    print("TESTING SECURITY GROUP LIFECYCLE WITH WAITERS")
    print("=" * 60)

    session = boto3.Session(profile_name="aws")
    ec2 = session.client("ec2", region_name="us-east-1")

    # Get default VPC
    print("1. Getting default VPC...")
    try:
        vpcs = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])
        if not vpcs["Vpcs"]:
            print("✗ No default VPC found")
            return False
        vpc_id = vpcs["Vpcs"][0]["VpcId"]
        print(f"✓ Default VPC: {vpc_id}")
    except Exception as e:
        print(f"✗ Failed to get VPC: {e}")
        return False

    # Create security group
    group_name = f"waiter-test-{uuid.uuid4().hex[:8]}"
    print(f"\n2. Creating security group: {group_name}")

    try:
        response = ec2.create_security_group(
            GroupName=group_name,
            Description=f"Test security group with waiter: {group_name}",
            VpcId=vpc_id,
            TagSpecifications=[
                {
                    "ResourceType": "security-group",
                    "Tags": [
                        {"Key": "Name", "Value": group_name},
                        {"Key": "CreatedBy", "Value": "WaiterTest"},
                        {"Key": "AutoCleanup", "Value": "true"},
                    ],
                }
            ],
        )
        group_id = response["GroupId"]
        print(f"✓ Security group created: {group_id}")
    except Exception as e:
        print(f"✗ Failed to create security group: {e}")
        return False

    # Wait for security group using waiter
    print("\n3. Waiting for security group to be available...")
    try:
        sg = wait_for_security_group(ec2, group_id)
        print("✓ Security group confirmed available")
    except Exception as e:
        print(f"✗ Security group waiter failed: {e}")
        return False

    # Add ingress rules
    print("\n4. Adding ingress rules...")
    try:
        ec2.authorize_security_group_ingress(
            GroupId=group_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "SSH access"}],
                },
                {
                    "IpProtocol": "tcp",
                    "FromPort": 54000,
                    "ToPort": 55000,
                    "IpRanges": [
                        {"CidrIp": "0.0.0.0/0", "Description": "Parsl communication"}
                    ],
                },
            ],
        )
        print("✓ Ingress rules added")
    except Exception as e:
        print(f"✗ Failed to add ingress rules: {e}")
        # Continue with test

    # Verify security group is still accessible
    print("\n5. Final verification...")
    try:
        response = ec2.describe_security_groups(GroupIds=[group_id])
        sg = response["SecurityGroups"][0]
        print(f"✓ Final verification passed: {sg['GroupName']}")
        print(
            f"  Rules: {len(sg['IpPermissions'])} ingress, {len(sg['IpPermissionsEgress'])} egress"
        )
        success = True
    except Exception as e:
        print(f"✗ Final verification failed: {e}")
        success = False

    # Cleanup
    print("\n6. Cleanup...")
    try:
        ec2.delete_security_group(GroupId=group_id)
        print("✓ Security group deleted")
    except Exception as e:
        print(f"⚠ Cleanup failed: {e}")

    return success


def test_instance_launch_with_waiter():
    """Test launching instance after security group creation with waiter."""

    print("\n" + "=" * 60)
    print("TESTING INSTANCE LAUNCH WITH SECURITY GROUP WAITER")
    print("=" * 60)

    session = boto3.Session(profile_name="aws")
    ec2 = session.client("ec2", region_name="us-east-1")

    group_name = f"instance-test-{uuid.uuid4().hex[:8]}"
    group_id = None
    instance_id = None

    try:
        # Get VPC
        vpcs = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])
        vpc_id = vpcs["Vpcs"][0]["VpcId"]

        # Create security group
        print("1. Creating security group...")
        response = ec2.create_security_group(
            GroupName=group_name,
            Description=f"Instance test: {group_name}",
            VpcId=vpc_id,
        )
        group_id = response["GroupId"]
        print(f"✓ Security group: {group_id}")

        # Wait for it to be ready
        print("2. Waiting for security group...")
        wait_for_security_group(ec2, group_id)

        # Add SSH rule
        print("3. Adding SSH rule...")
        ec2.authorize_security_group_ingress(
            GroupId=group_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                }
            ],
        )

        # Launch instance
        print("4. Launching instance...")
        ami_id = "ami-080e1f13689e07408"  # Amazon Linux 2023

        response = ec2.run_instances(
            ImageId=ami_id,
            MinCount=1,
            MaxCount=1,
            InstanceType="t3.micro",
            SecurityGroupIds=[group_id],
            UserData='#!/bin/bash\necho "Test instance launched successfully"',
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "Name", "Value": f"waiter-test-{group_name}"},
                        {"Key": "CreatedBy", "Value": "WaiterTest"},
                        {"Key": "AutoCleanup", "Value": "true"},
                    ],
                }
            ],
        )

        instance_id = response["Instances"][0]["InstanceId"]
        print(f"✓ Instance launched: {instance_id}")

        # Wait for instance to exist
        print("5. Waiting for instance to be visible...")
        max_attempts = 30
        for attempt in range(max_attempts):
            try:
                response = ec2.describe_instances(InstanceIds=[instance_id])
                if response["Reservations"]:
                    instance = response["Reservations"][0]["Instances"][0]
                    state = instance["State"]["Name"]
                    print(f"✓ Instance confirmed: {instance_id} ({state})")
                    return True
            except ClientError as e:
                if e.response["Error"]["Code"] != "InvalidInstanceID.NotFound":
                    raise

            print(f"  Attempt {attempt + 1}: waiting for instance to be visible...")
            time.sleep(2)

        print(f"✗ Instance {instance_id} never became visible")
        return False

    except Exception as e:
        print(f"✗ Test failed: {e}")
        import traceback

        traceback.print_exc()
        return False

    finally:
        # Cleanup
        print("\nCleanup...")
        if instance_id:
            try:
                ec2.terminate_instances(InstanceIds=[instance_id])
                print(f"✓ Terminated instance {instance_id}")
            except Exception as e:
                print(f"⚠ Failed to terminate instance: {e}")

        if group_id:
            # Wait for instance to terminate before deleting security group
            if instance_id:
                print("Waiting 30s for instance termination...")
                time.sleep(30)

            try:
                ec2.delete_security_group(GroupId=group_id)
                print(f"✓ Deleted security group {group_id}")
            except Exception as e:
                print(f"⚠ Failed to delete security group: {e}")


if __name__ == "__main__":
    print("TESTING SECURITY GROUP WAITER APPROACH")
    print("=" * 60)

    # Test 1: Security group lifecycle
    success1 = test_security_group_lifecycle()

    # Test 2: Instance launch with waiter
    success2 = test_instance_launch_with_waiter()

    print("\n" + "=" * 60)
    print("RESULTS:")
    print(f"Security group waiter: {'✓ SUCCESS' if success1 else '✗ FAILED'}")
    print(f"Instance launch test: {'✓ SUCCESS' if success2 else '✗ FAILED'}")

    if success1 and success2:
        print("\n🎉 WAITER APPROACH WORKS - ready to fix Phase 1")
    else:
        print("\n❌ WAITER APPROACH HAS ISSUES")

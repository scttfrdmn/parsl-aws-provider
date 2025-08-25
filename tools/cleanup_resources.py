#!/usr/bin/env python3
"""
Comprehensive AWS resource cleanup for Parsl AWS Provider development.

Finds and cleans up ALL resources created during testing.
"""

import boto3


def find_provider_resources(region="us-east-1"):
    """Find all resources created by the provider."""

    session = boto3.Session(profile_name="aws")
    ec2 = session.client("ec2", region_name=region)

    resources = {"instances": [], "security_groups": [], "volumes": []}

    # Find instances
    try:
        response = ec2.describe_instances(
            Filters=[
                {"Name": "tag:CreatedBy", "Values": ["ParslBasicAWSProvider"]},
                {
                    "Name": "instance-state-name",
                    "Values": ["running", "pending", "stopped", "stopping"],
                },
            ]
        )

        for reservation in response["Reservations"]:
            for instance in reservation["Instances"]:
                resources["instances"].append(
                    {
                        "id": instance["InstanceId"],
                        "state": instance["State"]["Name"],
                        "launch_time": instance["LaunchTime"],
                        "tags": {
                            tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])
                        },
                    }
                )
    except Exception as e:
        print(f"Error finding instances: {e}")

    # Find security groups
    try:
        response = ec2.describe_security_groups(
            Filters=[{"Name": "tag:CreatedBy", "Values": ["ParslBasicAWSProvider"]}]
        )

        for sg in response["SecurityGroups"]:
            resources["security_groups"].append(
                {
                    "id": sg["GroupId"],
                    "name": sg["GroupName"],
                    "vpc_id": sg["VpcId"],
                    "tags": {tag["Key"]: tag["Value"] for tag in sg.get("Tags", [])},
                }
            )
    except Exception as e:
        print(f"Error finding security groups: {e}")

    # Find volumes (from terminated instances)
    try:
        response = ec2.describe_volumes(
            Filters=[{"Name": "tag:CreatedBy", "Values": ["ParslBasicAWSProvider"]}]
        )

        for volume in response["Volumes"]:
            resources["volumes"].append(
                {
                    "id": volume["VolumeId"],
                    "state": volume["State"],
                    "size": volume["Size"],
                    "tags": {
                        tag["Key"]: tag["Value"] for tag in volume.get("Tags", [])
                    },
                }
            )
    except Exception as e:
        print(f"Error finding volumes: {e}")

    return resources


def cleanup_resources(region="us-east-1", dry_run=True):
    """Clean up all provider resources."""

    print(f"{'DRY RUN: ' if dry_run else ''}Cleaning up resources in {region}")
    print("=" * 60)

    session = boto3.Session(profile_name="aws")
    ec2 = session.client("ec2", region_name=region)

    resources = find_provider_resources(region)

    # Show what we found
    total_resources = sum(len(resource_list) for resource_list in resources.values())
    print(f"Found {total_resources} resources to clean up:")

    for resource_type, resource_list in resources.items():
        if resource_list:
            print(f"\n{resource_type.upper()}:")
            for resource in resource_list:
                if resource_type == "instances":
                    print(
                        f"  {resource['id']} ({resource['state']}) - {resource['launch_time']}"
                    )
                elif resource_type == "security_groups":
                    print(f"  {resource['id']} ({resource['name']})")
                elif resource_type == "volumes":
                    print(
                        f"  {resource['id']} ({resource['state']}, {resource['size']}GB)"
                    )

    if dry_run:
        print("\nDRY RUN - No resources were actually deleted.")
        print("Run with dry_run=False to actually delete resources.")
        return resources

    # Actually clean up
    print("\nCleaning up resources...")

    # Terminate instances first
    instance_ids = [inst["id"] for inst in resources["instances"]]
    if instance_ids:
        try:
            print(f"Terminating {len(instance_ids)} instances...")
            ec2.terminate_instances(InstanceIds=instance_ids)

            # Wait for termination
            print("Waiting for instances to terminate...")
            waiter = ec2.get_waiter("instance_terminated")
            waiter.wait(
                InstanceIds=instance_ids, WaiterConfig={"Delay": 15, "MaxAttempts": 20}
            )
            print("✓ Instances terminated")
        except Exception as e:
            print(f"Error terminating instances: {e}")

    # Delete security groups (after instances are gone)
    sg_ids = [sg["id"] for sg in resources["security_groups"]]
    for sg_id in sg_ids:
        try:
            print(f"Deleting security group {sg_id}...")
            ec2.delete_security_group(GroupId=sg_id)
            print("✓ Security group deleted")
        except Exception as e:
            print(f"Error deleting security group {sg_id}: {e}")

    # Delete volumes (if any unattached)
    volume_ids = [
        vol["id"] for vol in resources["volumes"] if vol["state"] == "available"
    ]
    for volume_id in volume_ids:
        try:
            print(f"Deleting volume {volume_id}...")
            ec2.delete_volume(VolumeId=volume_id)
            print("✓ Volume deleted")
        except Exception as e:
            print(f"Error deleting volume {volume_id}: {e}")

    print("\n✓ Cleanup complete")
    return resources


def main():
    """Main cleanup function."""
    print("PARSL AWS PROVIDER RESOURCE CLEANUP")
    print("=" * 50)

    # First, show what we'd clean up
    resources = cleanup_resources(dry_run=True)

    total = sum(len(resource_list) for resource_list in resources.values())
    if total == 0:
        print("No resources found to clean up.")
        return

    # Ask for confirmation
    response = input(f"\nFound {total} resources. Delete them? (yes/no): ")
    if response.lower() in ["yes", "y"]:
        cleanup_resources(dry_run=False)
    else:
        print("Cleanup cancelled.")


if __name__ == "__main__":
    main()

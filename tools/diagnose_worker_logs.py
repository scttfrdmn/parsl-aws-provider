#!/usr/bin/env python3
"""
Diagnostic script to check worker logs and status on AWS instances.
"""

import boto3
import time
import sys
from phase15_enhanced import AWSProvider


def check_instance_worker_status(instance_id, session):
    """Check worker status on a specific instance."""
    print(f"\n🔍 DIAGNOSING INSTANCE {instance_id}")
    print("=" * 60)

    ssm_client = session.client("ssm")

    # Check if instance is responsive to SSM
    try:
        response = ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={
                "commands": [
                    'echo "Instance is responsive"',
                    "whoami",
                    "pwd",
                    'ps aux | grep -E "(python|parsl|worker)" | head -20',
                    'echo "--- Worker Logs ---"',
                    'ls -la /tmp/parsl_logs/ || echo "No parsl_logs directory"',
                    'tail -50 /tmp/parsl_logs/worker.log || echo "No worker log found"',
                    'echo "--- Network Status ---"',
                    'ss -tuln | grep -E "(54000|55000|50000)" || echo "No relevant network connections"',
                    'echo "--- Process Status ---"',
                    "pstree || ps auxf | head -20",
                ]
            },
            TimeoutSeconds=60,
        )

        command_id = response["Command"]["CommandId"]
        print(f"Command sent: {command_id}")

        # Wait for command to complete
        for i in range(12):  # 60s timeout
            time.sleep(5)
            result = ssm_client.get_command_invocation(
                CommandId=command_id, InstanceId=instance_id
            )

            status = result["Status"]
            print(f"  Status check {i+1}: {status}")

            if status in ["Success", "Failed"]:
                break

        if status == "Success":
            stdout = result.get("StandardOutputContent", "")
            stderr = result.get("StandardErrorContent", "")

            print(f"\n✅ STDOUT from {instance_id}:")
            print("-" * 40)
            print(stdout)

            if stderr:
                print(f"\n❌ STDERR from {instance_id}:")
                print("-" * 40)
                print(stderr)
        else:
            print(f"❌ Command failed or timed out: {status}")

    except Exception as e:
        print(f"❌ Error checking instance {instance_id}: {e}")


def get_recent_instances(session):
    """Get recently launched instances with our tag."""
    ec2_client = session.client("ec2")

    # Look for instances with our provider tags
    try:
        response = ec2_client.describe_instances(
            Filters=[
                {"Name": "tag:parsl_provider", "Values": ["aws-enhanced"]},
                {"Name": "instance-state-name", "Values": ["running", "pending"]},
            ]
        )

        instances = []
        for reservation in response["Reservations"]:
            for instance in reservation["Instances"]:
                instances.append(
                    {
                        "id": instance["InstanceId"],
                        "state": instance["State"]["Name"],
                        "launch_time": instance["LaunchTime"],
                    }
                )

        # Sort by launch time, most recent first
        instances.sort(key=lambda x: x["launch_time"], reverse=True)
        return instances[:5]  # Only check 5 most recent

    except Exception as e:
        print(f"❌ Error getting instances: {e}")
        return []


def main():
    """Run worker diagnostics."""
    print("🚀 WORKER DIAGNOSTIC SCRIPT")
    print("=" * 60)

    # Create AWS session
    session = boto3.Session(region_name="us-east-1", profile_name="aws")

    # Get recent instances
    instances = get_recent_instances(session)

    if not instances:
        print("❌ No recent instances found with parsl_provider tag")

        # Let's also try to create a simple test instance to diagnose
        print("\n🧪 Creating test instance for diagnosis...")
        try:
            provider = AWSProvider(
                label="diagnostic_test", init_blocks=1, max_blocks=1, min_blocks=0
            )

            # Submit a simple test job
            job_id = provider.submit("echo 'Diagnostic test'", 1)
            print(f"✅ Test job submitted: {job_id}")

            # Wait for instance to be ready
            print("⏳ Waiting 60s for instance to be ready...")
            time.sleep(60)

            # Get the new instance
            instances = get_recent_instances(session)

        except Exception as e:
            print(f"❌ Could not create test instance: {e}")
            return 1

    if instances:
        print(f"Found {len(instances)} recent instances:")
        for inst in instances:
            print(f"  {inst['id']} - {inst['state']} - {inst['launch_time']}")
        print()

        # Check each instance
        for instance in instances:
            if instance["state"] == "running":
                check_instance_worker_status(instance["id"], session)
            else:
                print(
                    f"⏭️ Skipping {instance['id']} - not running ({instance['state']})"
                )
    else:
        print("❌ Still no instances found")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

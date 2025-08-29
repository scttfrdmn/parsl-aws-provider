#!/usr/bin/env python3
"""
AMI Validation Tool

Validates that optimized AMIs work correctly by launching test instances
and running basic functionality checks.
"""

import argparse
import logging
import sys
import time
import uuid
from typing import Dict

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class AMIValidator:
    """Validates optimized AMIs by launching and testing them."""

    def __init__(self, region: str = "us-east-1"):
        """Initialize AMI validator for specified region."""
        self.region = region
        self.session = boto3.Session()
        self.ec2 = self.session.client("ec2", region_name=region)

    def validate_ami(self, ami_id: str, instance_type: str = "t3.micro") -> Dict:
        """
        Validate AMI functionality by launching and testing it.

        Args:
            ami_id: AMI ID to validate
            instance_type: Instance type for testing

        Returns:
            Validation results dictionary
        """
        test_id = f"ami-test-{uuid.uuid4().hex[:8]}"
        logger.info(f"Starting AMI validation: {ami_id}")

        results = {
            "ami_id": ami_id,
            "region": self.region,
            "test_id": test_id,
            "success": False,
            "startup_time": None,
            "parsl_available": False,
            "errors": [],
        }

        instance_id = None

        try:
            # 1. Launch test instance
            start_time = time.time()
            instance_id = self._launch_test_instance(ami_id, instance_type, test_id)
            logger.info(f"Test instance launched: {instance_id}")

            # 2. Wait for instance running
            self._wait_for_instance_running(instance_id)
            startup_time = time.time() - start_time
            results["startup_time"] = startup_time

            logger.info(f"Instance startup time: {startup_time:.1f} seconds")

            # 3. Wait additional time for user-data completion
            logger.info("Waiting for system initialization...")
            time.sleep(60)  # Give time for any user-data to complete

            # 4. Check if this looks like an optimized AMI
            ami_info = self._get_ami_info(ami_id)
            is_optimized = self._is_optimized_ami(ami_info)

            if is_optimized:
                logger.info("AMI appears to be optimized (has ParslAWSProvider tags)")
                # For optimized AMIs, startup should be fast
                if startup_time > 90:
                    results["errors"].append(
                        f"Slow startup for optimized AMI: {startup_time:.1f}s"
                    )
            else:
                logger.info("AMI appears to be base AMI (no optimization tags)")

            # 5. Basic validation - AMI should at least boot successfully
            results["success"] = True
            logger.info("Basic AMI validation passed")

            return results

        except Exception as e:
            error_msg = f"AMI validation failed: {e}"
            logger.error(error_msg)
            results["errors"].append(error_msg)
            return results

        finally:
            # Always cleanup test instance
            if instance_id:
                self._cleanup_test_instance(instance_id)

    def _launch_test_instance(
        self, ami_id: str, instance_type: str, test_id: str
    ) -> str:
        """Launch test instance from AMI."""

        # Simple user data to test basic functionality
        user_data = f"""#!/bin/bash
exec > >(tee /var/log/ami-test.log) 2>&1

echo "=== AMI VALIDATION TEST START ==="
echo "Test ID: {test_id}"
echo "AMI ID: {ami_id}"
echo "Started: $(date)"

# Test Python availability
echo "Testing Python..."
python3 --version

# Test Parsl availability (if this is an optimized AMI)
echo "Testing Parsl..."
python3 -c "import parsl; print('Parsl version:', parsl.__version__)" || echo "Parsl not available"

echo "Test completed: $(date)"
echo "=== AMI VALIDATION TEST END ==="
"""

        try:
            response = self.ec2.run_instances(
                ImageId=ami_id,
                MinCount=1,
                MaxCount=1,
                InstanceType=instance_type,
                UserData=user_data,
                TagSpecifications=[
                    {
                        "ResourceType": "instance",
                        "Tags": [
                            {"Key": "Name", "Value": f"ami-validation-{test_id}"},
                            {"Key": "Purpose", "Value": "AMI-Validation"},
                            {"Key": "CreatedBy", "Value": "ParslAWSProvider"},
                            {"Key": "TestId", "Value": test_id},
                            {"Key": "AutoCleanup", "Value": "true"},
                        ],
                    }
                ],
            )

            return response["Instances"][0]["InstanceId"]

        except ClientError as e:
            raise Exception(f"Failed to launch test instance: {e}")

    def _wait_for_instance_running(self, instance_id: str, max_attempts: int = 60):
        """Wait for instance to reach running state."""
        logger.info("Waiting for test instance to be running...")

        for attempt in range(max_attempts):
            try:
                response = self.ec2.describe_instances(InstanceIds=[instance_id])
                instance = response["Reservations"][0]["Instances"][0]
                state = instance["State"]["Name"]

                if state == "running":
                    return
                elif state in ["terminated", "shutting-down", "stopped"]:
                    raise Exception(f"Test instance failed: state={state}")

            except ClientError as e:
                if e.response["Error"]["Code"] != "InvalidInstanceID.NotFound":
                    raise

            time.sleep(5)

        raise Exception("Test instance did not reach running state within timeout")

    def _get_ami_info(self, ami_id: str) -> Dict:
        """Get AMI information and tags."""
        try:
            response = self.ec2.describe_images(ImageIds=[ami_id])
            if not response["Images"]:
                raise Exception(f"AMI not found: {ami_id}")

            ami = response["Images"][0]
            tags = {tag["Key"]: tag["Value"] for tag in ami.get("Tags", [])}

            return {
                "ImageId": ami["ImageId"],
                "Name": ami["Name"],
                "Description": ami.get("Description", ""),
                "State": ami["State"],
                "CreationDate": ami["CreationDate"],
                "Tags": tags,
            }

        except ClientError as e:
            raise Exception(f"Failed to get AMI info: {e}")

    def _is_optimized_ami(self, ami_info: Dict) -> bool:
        """Check if AMI appears to be a ParslAWSProvider optimized AMI."""
        tags = ami_info.get("Tags", {})
        return (
            tags.get("CreatedBy") == "ParslAWSProvider" and tags.get("Version") == "1.5"
        )

    def _cleanup_test_instance(self, instance_id: str):
        """Terminate test instance."""
        logger.info(f"Terminating test instance: {instance_id}")

        try:
            self.ec2.terminate_instances(InstanceIds=[instance_id])
            logger.info("Test instance terminated")
        except Exception as e:
            logger.warning(f"Failed to terminate test instance: {e}")

    def list_validation_candidates(self) -> list:
        """List AMIs that could be validated."""
        try:
            # Look for optimized AMIs first
            response = self.ec2.describe_images(
                Owners=["self"],
                Filters=[
                    {"Name": "tag:CreatedBy", "Values": ["ParslAWSProvider"]},
                    {"Name": "tag:Version", "Values": ["1.5"]},
                    {"Name": "state", "Values": ["available"]},
                ],
            )

            amis = []
            for ami in response["Images"]:
                tags = {tag["Key"]: tag["Value"] for tag in ami.get("Tags", [])}
                amis.append(
                    {
                        "ImageId": ami["ImageId"],
                        "Name": ami["Name"],
                        "CreationDate": ami["CreationDate"],
                        "ParslVersion": tags.get("ParslVersion", "unknown"),
                        "Type": "Optimized",
                    }
                )

            # Sort by creation date (newest first)
            amis.sort(key=lambda x: x["CreationDate"], reverse=True)
            return amis

        except ClientError as e:
            logger.error(f"Failed to list AMIs: {e}")
            return []


def main():
    """CLI interface for AMI validator."""
    parser = argparse.ArgumentParser(description="Validate AMI functionality")
    parser.add_argument("--ami", required=True, help="AMI ID to validate")
    parser.add_argument("--region", default="us-east-1", help="AWS region")
    parser.add_argument(
        "--instance-type", default="t3.micro", help="Instance type for testing"
    )
    parser.add_argument(
        "--list", action="store_true", help="List AMIs that can be validated"
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")

    args = parser.parse_args()

    # Configure logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(levelname)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    try:
        validator = AMIValidator(region=args.region)

        if args.list:
            amis = validator.list_validation_candidates()
            if amis:
                print(f"\nAMIs available for validation in {args.region}:")
                print("-" * 80)
                for ami in amis:
                    print(f"AMI ID: {ami['ImageId']}")
                    print(f"Name: {ami['Name']}")
                    print(f"Created: {ami['CreationDate']}")
                    print(f"Type: {ami['Type']}")
                    print("-" * 80)
            else:
                print(f"No optimized AMIs found in {args.region}")
            return

        # Validate specified AMI
        print(f"\nValidating AMI: {args.ami}")
        print(f"Region: {args.region}")
        print(f"Instance type: {args.instance_type}")
        print("-" * 60)

        results = validator.validate_ami(args.ami, args.instance_type)

        print("-" * 60)
        if results["success"]:
            print("SUCCESS: AMI validation passed!")
            print(f"AMI ID: {results['ami_id']}")
            if results["startup_time"]:
                print(f"Startup time: {results['startup_time']:.1f} seconds")
        else:
            print("FAILED: AMI validation failed!")
            for error in results["errors"]:
                print(f"ERROR: {error}")

        if results["errors"]:
            print("\nWarnings/Errors:")
            for error in results["errors"]:
                print(f"- {error}")

    except KeyboardInterrupt:
        print("\nValidation interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\nERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

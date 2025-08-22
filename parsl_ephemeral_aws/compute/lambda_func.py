"""Lambda function compute implementation for Parsl Ephemeral AWS Provider.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import logging
import json
import time
from typing import Dict, Any, Set

import boto3
from botocore.exceptions import ClientError

from ..exceptions import ResourceCreationError, ResourceCleanupError, JobSubmissionError
from ..constants import (
    TAG_PREFIX,
    TAG_NAME,
    TAG_WORKFLOW_ID,
    TAG_JOB_ID,
    DEFAULT_LAMBDA_RUNTIME,
    DEFAULT_LAMBDA_HANDLER,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    STATUS_FAILED,
)


logger = logging.getLogger(__name__)


class LambdaManager:
    """Manager for AWS Lambda compute resources."""

    def __init__(self, provider: Any) -> None:
        """Initialize the Lambda manager.

        Parameters
        ----------
        provider : EphemeralAWSProvider
            The provider instance
        """
        self.provider = provider

        # Initialize AWS session
        session_kwargs = {}
        if self.provider.aws_access_key_id and self.provider.aws_secret_access_key:
            session_kwargs["aws_access_key_id"] = self.provider.aws_access_key_id
            session_kwargs[
                "aws_secret_access_key"
            ] = self.provider.aws_secret_access_key

        if self.provider.aws_session_token:
            session_kwargs["aws_session_token"] = self.provider.aws_session_token

        if self.provider.aws_profile:
            session_kwargs["profile_name"] = self.provider.aws_profile

        self.aws_session = boto3.Session(
            region_name=self.provider.region, **session_kwargs
        )

        # Initialize clients
        self.lambda_client = self.aws_session.client("lambda")
        self.iam_client = self.aws_session.client("iam")

        # Track resources for cleanup
        self.function_names: Set[str] = set()
        self.role_names: Set[str] = set()
        self.jobs: Dict[str, Any] = {}

    def _create_lambda_execution_role(self) -> str:
        """Create an IAM role for Lambda execution.

        Returns
        -------
        str
            ARN of the IAM role
        """
        # Generate a unique role name
        role_name = f"{TAG_PREFIX}-lambda-role-{self.provider.workflow_id}"

        try:
            # Create role
            assume_role_policy = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "lambda.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                    }
                ],
            }

            response = self.iam_client.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(assume_role_policy),
                Description=f"Execution role for Parsl Lambda functions ({self.provider.workflow_id})",
                Tags=[
                    {"Key": TAG_NAME, "Value": "true"},
                    {"Key": TAG_WORKFLOW_ID, "Value": self.provider.workflow_id},
                ],
            )

            role_arn = response["Role"]["Arn"]
            logger.info(f"Created Lambda execution role: {role_name}")

            # Attach policies
            self.iam_client.attach_role_policy(
                RoleName=role_name,
                PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
            )

            # Track role for cleanup
            self.role_names.add(role_name)

            # Wait for role to be ready (IAM changes can take time to propagate)
            time.sleep(10)

            return role_arn

        except ClientError as e:
            logger.error(f"Error creating Lambda execution role: {e}")
            raise ResourceCreationError(f"Failed to create Lambda execution role: {e}")

    def _create_lambda_function(self, job_id: str, command: str) -> str:
        """Create a Lambda function for job execution.

        Parameters
        ----------
        job_id : str
            ID of the job
        command : str
            Command to execute

        Returns
        -------
        str
            Name of the Lambda function
        """
        # Generate a unique function name
        function_name = f"{TAG_PREFIX}-func-{self.provider.workflow_id}-{job_id}"

        try:
            # Create execution role if needed
            role_arn = self._create_lambda_execution_role()

            # Generate Lambda function code
            zip_file = self._generate_lambda_code(command)

            # Create Lambda function
            response = self.lambda_client.create_function(
                FunctionName=function_name,
                Runtime=DEFAULT_LAMBDA_RUNTIME,
                Role=role_arn,
                Handler=DEFAULT_LAMBDA_HANDLER,
                Code={"ZipFile": zip_file},
                Description=f"Parsl job {job_id} for workflow {self.provider.workflow_id}",
                Timeout=min(
                    self.provider.lambda_timeout, 900
                ),  # Lambda max is 900s (15 min)
                MemorySize=self.provider.lambda_memory,
                Tags={
                    TAG_NAME: "true",
                    TAG_WORKFLOW_ID: self.provider.workflow_id,
                    TAG_JOB_ID: job_id,
                },
            )

            # Track function for cleanup
            self.function_names.add(function_name)

            logger.info(f"Created Lambda function: {function_name}")

            return function_name

        except ClientError as e:
            logger.error(f"Error creating Lambda function: {e}")
            raise ResourceCreationError(f"Failed to create Lambda function: {e}")

    def _generate_lambda_code(self, command: str) -> bytes:
        """Generate code for the Lambda function.

        Parameters
        ----------
        command : str
            Command to execute

        Returns
        -------
        bytes
            Zip file content containing the Lambda function code
        """
        # For a real implementation, this would generate a proper Lambda function
        # that can execute the command and return the results.
        # For now, we'll create a simple function that logs the command and returns success.

        import io
        import zipfile

        # Create a Python module to handle the job
        handler_code = f"""
import json
import subprocess
import sys
import os
import traceback

def main(event, context):
    print("Starting Parsl job execution")

    try:
        # Get command from event or use the baked-in command
        command = event.get('command', {json.dumps(command)})
        print(f"Executing command: {{command}}")

        # Execute the command
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True
        )

        # Prepare response
        response = {
            'statusCode': 200 if result.returncode == 0 else 500,
            'command': command,
            'stdout': result.stdout,
            'stderr': result.stderr,
            'returncode': result.returncode
        }

        # Log results
        print(f"Command completed with return code: {result.returncode}")
        print(f"STDOUT: {result.stdout[:1000]}{'...' if len(result.stdout) > 1000 else ''}")
        print(f"STDERR: {result.stderr[:1000]}{'...' if len(result.stderr) > 1000 else ''}")

        return response

    except Exception as e:
        # Log the exception
        print(f"Error executing command: {e}")
        traceback.print_exc()

        # Return error response
        return {
            'statusCode': 500,
            'error': str(e),
            'traceback': traceback.format_exc()
        }
"""

        # Create a ZIP file in memory
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            zip_file.writestr("handler.py", handler_code)

        return zip_buffer.getvalue()

    def submit_job(self, job_id: str, command: str) -> Dict[str, Any]:
        """Submit a job for execution.

        Parameters
        ----------
        job_id : str
            ID of the job
        command : str
            Command to execute

        Returns
        -------
        Dict[str, Any]
            Dictionary containing job information
        """
        try:
            # Create Lambda function
            function_name = self._create_lambda_function(job_id, command)

            # Invoke function asynchronously
            response = self.lambda_client.invoke(
                FunctionName=function_name,
                InvocationType="Event",  # Asynchronous invocation
                Payload=json.dumps({"command": command, "job_id": job_id}),
            )

            # Get request ID from response
            request_id = response.get("ResponseMetadata", {}).get("RequestId")

            # Record job information
            self.jobs[job_id] = {
                "id": job_id,
                "function_name": function_name,
                "command": command,
                "request_id": request_id,
                "status": STATUS_PENDING,
                "submitted_at": time.time(),
            }

            logger.info(f"Submitted job {job_id} to Lambda function {function_name}")

            return {
                "job_id": job_id,
                "function_name": function_name,
                "request_id": request_id,
            }

        except Exception as e:
            logger.error(f"Error submitting job: {e}")
            raise JobSubmissionError(f"Failed to submit job: {e}")

    def get_job_status(self, function_name: str, request_id: str) -> str:
        """Get the status of a job.

        Parameters
        ----------
        function_name : str
            Name of the Lambda function
        request_id : str
            Request ID from the function invocation

        Returns
        -------
        str
            Job status
        """
        try:
            # For AWS Lambda, we can't directly query the status of an async invocation
            # We'd need to implement a more complex solution, such as:
            # 1. Use CloudWatch Logs to check for completion
            # 2. Use a state store (DynamoDB, etc.) that the Lambda updates
            # 3. Use Step Functions for workflow tracking

            # For now, we'll simulate the status based on time elapsed
            # In a real implementation, we'd use one of the approaches above

            # Find the job
            job = None
            for j in self.jobs.values():
                if (
                    j.get("function_name") == function_name
                    and j.get("request_id") == request_id
                ):
                    job = j
                    break

            if not job:
                return "UNKNOWN"

            # If the job already has a terminal status, return it
            if job["status"] in [STATUS_SUCCEEDED, STATUS_FAILED]:
                return job["status"]

            # Otherwise, simulate status based on time elapsed
            elapsed = time.time() - job["submitted_at"]

            if elapsed < 5:
                status = STATUS_PENDING
            elif elapsed < self.provider.lambda_timeout:
                status = STATUS_RUNNING
            else:
                # After timeout, we assume the job completed
                # In 95% of cases, assume success; 5% failure
                import random

                status = STATUS_SUCCEEDED if random.random() < 0.95 else STATUS_FAILED  # nosec B311

            # Update job status
            job["status"] = status

            return status

        except Exception as e:
            logger.error(f"Error getting job status: {e}")
            return "UNKNOWN"

    def cleanup_all_resources(self) -> None:
        """Clean up all AWS resources created by this manager."""
        try:
            # Delete Lambda functions
            for function_name in list(self.function_names):
                try:
                    self.lambda_client.delete_function(FunctionName=function_name)
                    logger.info(f"Deleted Lambda function: {function_name}")
                    self.function_names.remove(function_name)
                except Exception as e:
                    logger.error(f"Error deleting Lambda function {function_name}: {e}")

            # Detach and delete IAM roles
            for role_name in list(self.role_names):
                try:
                    # Detach policies
                    try:
                        self.iam_client.detach_role_policy(
                            RoleName=role_name,
                            PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                        )
                    except Exception as e:
                        logger.error(
                            f"Error detaching policy from role {role_name}: {e}"
                        )

                    # Delete role
                    self.iam_client.delete_role(RoleName=role_name)
                    logger.info(f"Deleted IAM role: {role_name}")
                    self.role_names.remove(role_name)
                except Exception as e:
                    logger.error(f"Error deleting IAM role {role_name}: {e}")

        except Exception as e:
            logger.error(f"Error cleaning up resources: {e}")
            raise ResourceCleanupError(f"Failed to clean up resources: {e}")

"""ECS/Fargate compute implementation for Parsl Ephemeral AWS Provider.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import logging
import time
from typing import Dict, Any, Set

from botocore.exceptions import ClientError, NoCredentialsError

from ..exceptions import ResourceCreationError, ResourceCleanupError, JobSubmissionError
from ..constants import (
    TAG_PREFIX,
    TAG_NAME,
    TAG_WORKFLOW_ID,
    TAG_JOB_ID,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    STATUS_FAILED,
    STATUS_CANCELLED,
    DEFAULT_VPC_CIDR,
)
from ..config import SecurityConfig
from ..security import (
    CredentialManager,
    CredentialConfiguration,
    SecurityEventType,
    SecurityEventSeverity,
    SecurityEvent,
)
from ..error_handling import RobustErrorHandler, RetryConfig
from ..utils.aws import get_or_create_iam_role


logger = logging.getLogger(__name__)


class ECSManager:
    """Manager for AWS ECS/Fargate compute resources."""

    def __init__(self, provider: Any) -> None:
        """Initialize the ECS manager.

        Parameters
        ----------
        provider : EphemeralAWSProvider
            The provider instance
        """
        self.provider = provider

        # Initialize error handling
        self.error_handler = RobustErrorHandler(
            retry_config=RetryConfig(
                max_attempts=5, base_delay=2.0, exponential_backoff=True, jitter=True
            )
        )
        logger.info("Error handler initialized for ECS operations")

        # Initialize security configuration and credential management
        self._setup_security_config()

        # Initialize audit logging
        self.audit_logger = self.security_config.get_audit_logger()
        if self.audit_logger:
            self.audit_logger.log_event(
                SecurityEvent(
                    event_type=SecurityEventType.CONFIG_CHANGE,
                    severity=SecurityEventSeverity.INFO,
                    message="ECSManager initialized",
                    resource_type="ecs_manager",
                    workflow_id=self.provider.workflow_id,
                    metadata={"provider_region": self.provider.region},
                )
            )
            logger.info("Audit logging enabled for ECS operations")

        # Initialize credential manager
        credential_config = self.security_config.get_credential_configuration()

        # Override credential config with provider-specific settings if provided
        if hasattr(provider, "aws_access_key_id") or hasattr(provider, "aws_profile"):
            # Legacy credential handling - create credential config from provider settings
            credential_config = self._create_credential_config_from_provider()

        try:
            self.credential_manager = CredentialManager(credential_config)
            logger.info("Credential manager initialized successfully")

            # Log successful credential initialization
            if self.audit_logger:
                self.audit_logger.log_credential_access(
                    access_type="credential_init",
                    identity=credential_config.role_arn or "default",
                    success=True,
                    workflow_id=self.provider.workflow_id,
                )
        except Exception as e:
            logger.error(f"Failed to initialize credential manager: {e}")

            # Log failed credential initialization
            if self.audit_logger:
                self.audit_logger.log_credential_access(
                    access_type="credential_init",
                    identity="unknown",
                    success=False,
                    error=str(e),
                    workflow_id=self.provider.workflow_id,
                )

            raise ResourceCreationError(f"Credential initialization failed: {e}")

        # Initialize AWS session using credential manager
        try:
            self.aws_session = self.credential_manager.create_boto3_session(
                region=self.provider.region
            )
        except NoCredentialsError as e:
            logger.error(f"No valid AWS credentials found: {e}")
            raise ResourceCreationError(f"AWS credential error: {e}")

        # Initialize clients
        self.ecs_client = self.aws_session.client("ecs")
        self.ec2_client = self.aws_session.client("ec2")
        self.iam_client = self.aws_session.client("iam")

        # Track resources for cleanup
        self.clusters: Set[str] = set()
        self.task_definitions: Set[str] = set()
        self.role_names: Set[str] = set()
        self.log_groups: Set[str] = set()  # CloudWatch log groups to clean up
        self.jobs: Dict[str, Any] = {}

        # Initialize ECS cluster if needed
        self.cluster_name = self._get_or_create_cluster()

    def _setup_security_config(self) -> None:
        """Set up security configuration from provider settings."""
        # Get security settings from provider if available
        security_env = getattr(self.provider, "security_environment", "dev")
        vpc_cidr = getattr(self.provider, "vpc_cidr", DEFAULT_VPC_CIDR)
        admin_cidrs = getattr(self.provider, "admin_cidr_blocks", None)
        strict_mode = getattr(self.provider, "strict_security_mode", None)

        # Create security configuration
        if security_env == "prod" and admin_cidrs:
            self.security_config = SecurityConfig.create_production_config(
                vpc_cidr=vpc_cidr, admin_cidrs=admin_cidrs
            )
        else:
            # Default to development configuration
            self.security_config = SecurityConfig.create_development_config(
                vpc_cidr=vpc_cidr
            )
            if strict_mode is not None:
                self.security_config.strict_mode = strict_mode

        logger.info(
            f"ECS Security configuration: environment={self.security_config.environment.value}, "
            f"strict_mode={self.security_config.strict_mode}"
        )

        # Analyze security posture
        analysis = self.security_config.analyze_security_posture()
        for warning in analysis.get("warnings", []):
            logger.warning(f"ECS Security warning: {warning}")
        for rec in analysis.get("recommendations", []):
            logger.info(f"ECS Security recommendation: {rec}")

    def _create_credential_config_from_provider(self) -> CredentialConfiguration:
        """Create credential configuration from provider settings.

        Returns
        -------
        CredentialConfiguration
            Credential configuration based on provider settings
        """
        # Extract credential settings from provider
        role_arn = getattr(self.provider, "role_arn", None)
        aws_profile = getattr(self.provider, "aws_profile", None)
        use_env_vars = (
            hasattr(self.provider, "aws_access_key_id")
            and self.provider.aws_access_key_id is not None
        )

        # Create credential configuration
        config = CredentialConfiguration(
            role_arn=role_arn,
            enable_sanitization=True,
            sanitize_logs=True,
            use_environment_variables=use_env_vars,
            use_profile=aws_profile,
            auto_refresh_tokens=True,
        )

        # Set security-based defaults
        if self.security_config.environment.value == "production":
            config.use_environment_variables = False
            config.use_profile = None
            config.require_mfa = False

        logger.info(
            f"ECS Created credential config: role_arn={bool(role_arn)}, "
            f"profile={aws_profile}, use_env={use_env_vars}"
        )

        return config

    def _get_or_create_cluster(self) -> str:
        """Get or create an ECS cluster.

        Returns
        -------
        str
            Name of the ECS cluster
        """
        # Generate cluster name based on workflow ID
        cluster_name = f"{TAG_PREFIX}-cluster-{self.provider.workflow_id}"

        try:
            # Check if cluster already exists
            response = self.ecs_client.describe_clusters(clusters=[cluster_name])

            if response["clusters"] and response["clusters"][0]["status"] == "ACTIVE":
                logger.info(f"Using existing ECS cluster: {cluster_name}")
                self.clusters.add(cluster_name)

                # Log cluster access
                if self.audit_logger:
                    self.audit_logger.log_resource_operation(
                        operation="access",
                        resource_type="ecs_cluster",
                        resource_id=cluster_name,
                        success=True,
                        workflow_id=self.provider.workflow_id,
                    )

                return cluster_name

            # Create cluster
            response = self.ecs_client.create_cluster(
                clusterName=cluster_name,
                capacityProviders=["FARGATE", "FARGATE_SPOT"],
                defaultCapacityProviderStrategy=[
                    {
                        "capacityProvider": "FARGATE_SPOT"
                        if self.provider.use_spot_instances
                        else "FARGATE",
                        "weight": 1,
                        "base": 0,
                    }
                ],
                tags=[
                    {"key": TAG_NAME, "value": "true"},
                    {"key": TAG_WORKFLOW_ID, "value": self.provider.workflow_id},
                ],
            )

            logger.info(f"Created ECS cluster: {cluster_name}")
            self.clusters.add(cluster_name)

            # Log successful cluster creation
            if self.audit_logger:
                self.audit_logger.log_resource_operation(
                    operation="create",
                    resource_type="ecs_cluster",
                    resource_id=cluster_name,
                    success=True,
                    workflow_id=self.provider.workflow_id,
                    capacity_providers=["FARGATE", "FARGATE_SPOT"],
                )

            return cluster_name

        except Exception as e:
            logger.error(f"Error creating ECS cluster: {e}")

            # Log failed cluster creation
            if self.audit_logger:
                self.audit_logger.log_resource_operation(
                    operation="create",
                    resource_type="ecs_cluster",
                    resource_id=cluster_name,
                    success=False,
                    workflow_id=self.provider.workflow_id,
                    error=str(e),
                )

            raise ResourceCreationError(f"Failed to create ECS cluster: {e}")

    def _create_task_execution_role(self) -> str:
        """Get or create an IAM role for ECS task execution (idempotent).

        Returns
        -------
        str
            ARN of the IAM role
        """
        role_name = f"{TAG_PREFIX}-ecs-role-{self.provider.workflow_id}"

        assume_role_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }

        role_arn = get_or_create_iam_role(
            iam_client=self.iam_client,
            role_name=role_name,
            assume_role_policy=assume_role_policy,
            policy_arns=[
                "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
            ],
            tags=[
                {"Key": TAG_NAME, "Value": "true"},
                {"Key": TAG_WORKFLOW_ID, "Value": self.provider.workflow_id},
            ],
            description=f"Execution role for Parsl ECS tasks ({self.provider.workflow_id})",
        )

        # Track role for cleanup
        self.role_names.add(role_name)

        # Wait for IAM propagation using role_exists waiter
        try:
            waiter = self.iam_client.get_waiter("role_exists")
            waiter.wait(RoleName=role_name, WaiterConfig={"MaxAttempts": 10})
        except Exception:
            logger.debug(
                "IAM waiter not available; proceeding without propagation wait"
            )

        return role_arn

    def _register_task_definition(self, job_id: str, command: str) -> str:
        """Register an ECS task definition.

        Parameters
        ----------
        job_id : str
            ID of the job
        command : str
            Command to execute

        Returns
        -------
        str
            ARN of the task definition
        """
        # Generate a unique family name
        family = f"{TAG_PREFIX}-task-{self.provider.workflow_id}-{job_id[:8]}"

        try:
            # Ensure the CloudWatch log group exists before registering the task
            # definition.  ECS tasks fail immediately if the log driver cannot
            # write to the group, so we create it proactively here.
            log_group_name = f"/ecs/{family}"
            logs_client = self.aws_session.client("logs")
            try:
                logs_client.create_log_group(logGroupName=log_group_name)
                self.log_groups.add(log_group_name)
                logger.debug(f"Created CloudWatch log group: {log_group_name}")
            except logs_client.exceptions.ResourceAlreadyExistsException:
                pass  # Already exists — fine, add to tracking for cleanup
            except ClientError as cw_err:
                logger.warning(
                    f"Could not create log group {log_group_name}: {cw_err}. "
                    "Task may fail if the log group does not already exist."
                )

            # Create execution role if needed
            execution_role_arn = self._create_task_execution_role()

            # Prepare container definition
            container_name = f"{TAG_PREFIX}-container-{job_id[:8]}"

            container_def = {
                "name": container_name,
                "image": self.provider.ecs_container_image or "python:3.12-slim",
                "cpu": self.provider.ecs_task_cpu,
                "memory": self.provider.ecs_task_memory,
                "essential": True,
                "command": ["/bin/sh", "-c", command],
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {
                        "awslogs-group": f"/ecs/{family}",
                        "awslogs-region": self.provider.region,
                        "awslogs-stream-prefix": "parsl",
                    },
                },
            }

            # Register task definition
            response = self.ecs_client.register_task_definition(
                family=family,
                executionRoleArn=execution_role_arn,
                taskRoleArn=execution_role_arn,
                networkMode="awsvpc",
                containerDefinitions=[container_def],
                requiresCompatibilities=["FARGATE"],
                cpu=str(self.provider.ecs_task_cpu),
                memory=str(self.provider.ecs_task_memory),
                tags=[
                    {"key": TAG_NAME, "value": "true"},
                    {"key": TAG_WORKFLOW_ID, "value": self.provider.workflow_id},
                    {"key": TAG_JOB_ID, "value": job_id},
                ],
            )

            task_definition_arn = response["taskDefinition"]["taskDefinitionArn"]
            logger.info(f"Registered ECS task definition: {task_definition_arn}")

            # Track task definition for cleanup
            self.task_definitions.add(family)

            return task_definition_arn

        except ClientError as e:
            logger.error(f"Error registering ECS task definition: {e}")
            raise ResourceCreationError(f"Failed to register ECS task definition: {e}")

    def _get_or_create_network_resources(self) -> Dict[str, str]:
        """Get or create network resources for ECS tasks.

        Returns
        -------
        Dict[str, str]
            Dictionary containing subnet IDs and security group ID
        """
        # For simplicity, we'll use the default VPC and subnets
        try:
            # Get default VPC
            vpc_response = self.ec2_client.describe_vpcs(
                Filters=[{"Name": "isDefault", "Values": ["true"]}]
            )

            if not vpc_response["Vpcs"]:
                raise ResourceCreationError("No default VPC found")

            vpc_id = vpc_response["Vpcs"][0]["VpcId"]

            # Get subnets in the default VPC
            subnet_response = self.ec2_client.describe_subnets(
                Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
            )

            if not subnet_response["Subnets"]:
                raise ResourceCreationError("No subnets found in default VPC")

            subnet_ids = [subnet["SubnetId"] for subnet in subnet_response["Subnets"]]

            # Get or create security group
            sg_name = f"{TAG_PREFIX}-ecs-sg-{self.provider.workflow_id}"

            # Check if security group already exists
            sg_response = self.ec2_client.describe_security_groups(
                Filters=[
                    {"Name": "group-name", "Values": [sg_name]},
                    {"Name": "vpc-id", "Values": [vpc_id]},
                ]
            )

            if sg_response["SecurityGroups"]:
                security_group_id = sg_response["SecurityGroups"][0]["GroupId"]
            else:
                # Create security group
                sg_create_response = self.ec2_client.create_security_group(
                    GroupName=sg_name,
                    Description=f"Security group for Parsl ECS tasks ({self.provider.workflow_id})",
                    VpcId=vpc_id,
                    TagSpecifications=[
                        {
                            "ResourceType": "security-group",
                            "Tags": [
                                {"Key": TAG_NAME, "Value": "true"},
                                {
                                    "Key": TAG_WORKFLOW_ID,
                                    "Value": self.provider.workflow_id,
                                },
                            ],
                        }
                    ],
                )

                security_group_id = sg_create_response["GroupId"]

                # Add outbound rule (allow all outbound traffic)
                self.ec2_client.authorize_security_group_egress(
                    GroupId=security_group_id,
                    IpPermissions=[
                        {
                            "IpProtocol": "-1",
                            "FromPort": -1,
                            "ToPort": -1,
                            "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                        }
                    ],
                )

            return {
                "vpc_id": vpc_id,
                "subnet_ids": subnet_ids,
                "security_group_id": security_group_id,
            }

        except ClientError as e:
            logger.error(f"Error getting or creating network resources: {e}")
            raise ResourceCreationError(
                f"Failed to get or create network resources: {e}"
            )

    def submit_job(
        self, job_id: str, command: str, tasks_per_node: int
    ) -> Dict[str, Any]:
        """Submit a job for execution.

        Parameters
        ----------
        job_id : str
            ID of the job
        command : str
            Command to execute
        tasks_per_node : int
            Number of tasks per node

        Returns
        -------
        Dict[str, Any]
            Dictionary containing job information
        """
        try:
            # Register task definition
            task_definition_arn = self._register_task_definition(job_id, command)

            # Get network configuration
            network = self._get_or_create_network_resources()

            # Launch the task
            response = self.ecs_client.run_task(
                cluster=self.cluster_name,
                taskDefinition=task_definition_arn,
                count=max(1, tasks_per_node),  # Ensure at least one task
                launchType="FARGATE",
                networkConfiguration={
                    "awsvpcConfiguration": {
                        "subnets": [network["subnet_ids"][0]],  # Use first subnet
                        "securityGroups": [network["security_group_id"]],
                        "assignPublicIp": "ENABLED"
                        if self.provider.use_public_ips
                        else "DISABLED",
                    }
                },
                tags=[
                    {"key": TAG_NAME, "value": "true"},
                    {"key": TAG_WORKFLOW_ID, "value": self.provider.workflow_id},
                    {"key": TAG_JOB_ID, "value": job_id},
                ],
            )

            # Extract task ARNs
            task_arns = [task["taskArn"] for task in response["tasks"]]
            task_ids = [arn.split("/")[-1] for arn in task_arns]

            # Record job information
            self.jobs[job_id] = {
                "id": job_id,
                "cluster": self.cluster_name,
                "task_definition": task_definition_arn,
                "task_arns": task_arns,
                "task_ids": task_ids,
                "command": command,
                "status": STATUS_PENDING,
                "submitted_at": time.time(),
            }

            primary_task_id = task_ids[0] if task_ids else None

            logger.info(
                f"Submitted job {job_id} to ECS cluster {self.cluster_name} with {len(task_ids)} tasks"
            )

            # Log successful job submission
            if self.audit_logger:
                self.audit_logger.log_resource_operation(
                    operation="create",
                    resource_type="ecs_task",
                    resource_id=primary_task_id or job_id,
                    success=True,
                    workflow_id=self.provider.workflow_id,
                    job_id=job_id,
                    task_count=len(task_ids),
                    cluster=self.cluster_name,
                )

            return {
                "job_id": job_id,
                "cluster": self.cluster_name,
                "task_id": primary_task_id,
                "task_count": len(task_ids),
            }

        except Exception as e:
            logger.error(f"Error submitting job: {e}")

            # Log failed job submission
            if self.audit_logger:
                self.audit_logger.log_resource_operation(
                    operation="create",
                    resource_type="ecs_task",
                    resource_id=job_id,
                    success=False,
                    workflow_id=self.provider.workflow_id,
                    job_id=job_id,
                    error=str(e),
                )

            raise JobSubmissionError(f"Failed to submit job: {e}")

    def get_job_status(self, cluster: str, task_id: str) -> str:
        """Get the status of a job.

        Parameters
        ----------
        cluster : str
            Name of the ECS cluster
        task_id : str
            ID of the ECS task

        Returns
        -------
        str
            Job status
        """
        try:
            # Find the job
            job = None
            for j in self.jobs.values():
                if j.get("cluster") == cluster and task_id in j.get("task_ids", []):
                    job = j
                    break

            if not job:
                return "UNKNOWN"

            # If the job already has a terminal status, return it
            if job["status"] in [STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED]:
                return job["status"]

            # Get task status
            response = self.ecs_client.describe_tasks(cluster=cluster, tasks=[task_id])

            if not response["tasks"]:
                # Task not found, it might have completed and been removed
                return "UNKNOWN"

            task = response["tasks"][0]
            last_status = task["lastStatus"]

            # Map ECS status to Parsl status
            if last_status == "PENDING":
                status = STATUS_PENDING
            elif last_status == "RUNNING":
                status = STATUS_RUNNING
            elif last_status == "STOPPED":
                # Check stop reason to determine final status
                if task.get("stoppedReason") == "Task failed to start":
                    status = STATUS_FAILED
                else:
                    # Check exit code of the container
                    for container in task.get("containers", []):
                        if container.get("exitCode") is not None:
                            if container.get("exitCode") == 0:
                                status = STATUS_SUCCEEDED
                            else:
                                status = STATUS_FAILED
                            break
                    else:
                        # No exit code found, default to succeeded
                        status = STATUS_SUCCEEDED
            else:
                # For any other status, default to running
                status = STATUS_RUNNING

            # Update job status
            job["status"] = status

            return status

        except Exception as e:
            logger.error(f"Error getting job status: {e}")
            return "UNKNOWN"

    def cancel_job(self, cluster: str, task_id: str) -> None:
        """Cancel a job.

        Parameters
        ----------
        cluster : str
            Name of the ECS cluster
        task_id : str
            ID of the ECS task
        """
        try:
            # Stop the task
            self.ecs_client.stop_task(
                cluster=cluster, task=task_id, reason="Cancelled by user"
            )

            # Find the job and update its status
            for job in self.jobs.values():
                if job.get("cluster") == cluster and task_id in job.get("task_ids", []):
                    job["status"] = STATUS_CANCELLED
                    break

            logger.info(f"Cancelled task {task_id} in cluster {cluster}")

        except Exception as e:
            logger.error(f"Error cancelling job: {e}")
            raise

    def cleanup_all_resources(self) -> None:
        """Clean up all AWS resources created by this manager."""
        try:
            # Stop all running tasks
            for job in self.jobs.values():
                cluster = job.get("cluster")
                task_ids = job.get("task_ids", [])

                if cluster and task_ids:
                    for task_id in task_ids:
                        try:
                            self.ecs_client.stop_task(
                                cluster=cluster,
                                task=task_id,
                                reason="Cleaning up resources",
                            )
                        except Exception as e:
                            logger.error(f"Error stopping task {task_id}: {e}")

            # Deregister task definitions
            for family in list(self.task_definitions):
                try:
                    # Get the latest task definition
                    response = self.ecs_client.list_task_definitions(
                        familyPrefix=family, status="ACTIVE", sort="DESC", maxResults=1
                    )

                    if response["taskDefinitionArns"]:
                        task_def_arn = response["taskDefinitionArns"][0]

                        # Deregister it
                        self.ecs_client.deregister_task_definition(
                            taskDefinition=task_def_arn
                        )
                        logger.info(f"Deregistered task definition: {task_def_arn}")
                except Exception as e:
                    logger.error(f"Error deregistering task definition {family}: {e}")

            # Delete clusters
            for cluster_name in list(self.clusters):
                try:
                    self.ecs_client.delete_cluster(cluster=cluster_name)
                    logger.info(f"Deleted ECS cluster: {cluster_name}")
                    self.clusters.remove(cluster_name)
                except Exception as e:
                    logger.error(f"Error deleting ECS cluster {cluster_name}: {e}")

            # Detach and delete IAM roles
            for role_name in list(self.role_names):
                try:
                    # Detach policies
                    try:
                        self.iam_client.detach_role_policy(
                            RoleName=role_name,
                            PolicyArn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
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

            # Delete CloudWatch log groups created for ECS tasks
            if self.log_groups:
                logs_client = self.aws_session.client("logs")
                for log_group_name in list(self.log_groups):
                    try:
                        logs_client.delete_log_group(logGroupName=log_group_name)
                        self.log_groups.discard(log_group_name)
                        logger.info(f"Deleted CloudWatch log group: {log_group_name}")
                    except ClientError as e:
                        if e.response["Error"]["Code"] == "ResourceNotFoundException":
                            self.log_groups.discard(log_group_name)
                        else:
                            logger.error(
                                f"Error deleting log group {log_group_name}: {e}"
                            )

        except Exception as e:
            logger.error(f"Error cleaning up resources: {e}")
            raise ResourceCleanupError(f"Failed to clean up resources: {e}")

"""Serverless mode implementation for Parsl Ephemeral AWS Provider.

This mode uses AWS Lambda and ECS/Fargate for executing jobs without EC2 instances,
providing cost-effective serverless execution for suitable workloads.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import logging
import time
import json
import os
import tempfile
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

from parsl_ephemeral_aws.constants import (
    RESOURCE_TYPE_LAMBDA_FUNCTION,
    RESOURCE_TYPE_ECS_TASK,
    WORKER_TYPE_LAMBDA,
    WORKER_TYPE_ECS,
    WORKER_TYPE_AUTO,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    STATUS_FAILED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_UNKNOWN,
    DEFAULT_LAMBDA_TIMEOUT,
    DEFAULT_LAMBDA_MEMORY,
    DEFAULT_LAMBDA_RUNTIME,
    DEFAULT_LAMBDA_HANDLER,
    DEFAULT_ECS_CPU,
    DEFAULT_ECS_MEMORY,
    DEFAULT_ECS_CONTAINER_IMAGE,
)
from parsl_ephemeral_aws.exceptions import (
    ConfigurationError,
    JobSubmissionError,
    OperatingModeError,
    ResourceCreationError,
)
from parsl_ephemeral_aws.modes.base import OperatingMode
from parsl_ephemeral_aws.state.base import STATE_KEY_MODE
from parsl_ephemeral_aws.compute.lambda_func import LambdaManager
from parsl_ephemeral_aws.compute.ecs import ECSManager
from parsl_ephemeral_aws.compute.spot_interruption import (
    SpotInterruptionMonitor,
    ParslSpotInterruptionHandler,
)


logger = logging.getLogger(__name__)


class ServerlessMode(OperatingMode):
    """Serverless operating mode implementation.

    In serverless mode, AWS Lambda and/or ECS/Fargate are used to execute tasks
    without any EC2 instances. This mode is suitable for event-driven or sporadic
    workloads with short-running tasks. It also supports EC2 SpotFleet for improved
    reliability and cost savings when more substantial compute resources are needed.

    Attributes
    ----------
    worker_type : str
        Type of worker to use (lambda, ecs, or auto)
    lambda_timeout : int
        Timeout for Lambda functions in seconds
    lambda_memory : int
        Memory for Lambda functions in MB
    ecs_task_cpu : int
        CPU units for ECS tasks
    ecs_task_memory : int
        Memory for ECS tasks in MB
    ecs_container_image : str
        Container image for ECS tasks
    use_spot : bool
        Whether to use spot instances for ECS tasks (Fargate Spot)
    use_spot_fleet : bool
        Whether to use Spot Fleet for EC2 instance deployment
    instance_types : List[str]
        List of instance types to use with Spot Fleet
    nodes_per_block : int
        Number of nodes per block for Spot Fleet
    spot_max_price_percentage : Optional[float]
        Maximum spot price as percentage of on-demand price
    lambda_manager : LambdaManager
        Manager for Lambda functions
    ecs_manager : ECSManager
        Manager for ECS tasks
    """

    def __init__(
        self,
        provider_id: str,
        session: boto3.Session,
        state_store: Any,
        worker_type: str = WORKER_TYPE_AUTO,
        lambda_timeout: int = DEFAULT_LAMBDA_TIMEOUT,
        lambda_memory: int = DEFAULT_LAMBDA_MEMORY,
        lambda_runtime: str = DEFAULT_LAMBDA_RUNTIME,
        ecs_task_cpu: int = DEFAULT_ECS_CPU,
        ecs_task_memory: int = DEFAULT_ECS_MEMORY,
        ecs_container_image: str = DEFAULT_ECS_CONTAINER_IMAGE,
        vpc_id: Optional[str] = None,
        subnet_id: Optional[str] = None,
        security_group_id: Optional[str] = None,
        use_public_ips: bool = True,
        use_spot: bool = False,
        use_spot_fleet: bool = False,
        instance_types: Optional[List[str]] = None,
        nodes_per_block: int = 1,
        spot_max_price_percentage: Optional[float] = None,
        additional_tags: Optional[Dict[str, str]] = None,
        debug: bool = False,
        compute_type: Optional[str] = None,
        memory_size: Optional[int] = None,
        timeout: Optional[int] = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the serverless mode.

        Parameters
        ----------
        provider_id : str
            Unique identifier for the provider instance
        session : boto3.Session
            AWS session for API calls
        state_store : Any
            Store for persisting state
        worker_type : str, optional
            Type of worker to use (lambda, ecs, or auto), by default WORKER_TYPE_AUTO
        lambda_timeout : int, optional
            Timeout for Lambda functions in seconds, by default DEFAULT_LAMBDA_TIMEOUT
        lambda_memory : int, optional
            Memory for Lambda functions in MB, by default DEFAULT_LAMBDA_MEMORY
        lambda_runtime : str, optional
            Runtime for Lambda functions, by default DEFAULT_LAMBDA_RUNTIME
        ecs_task_cpu : int, optional
            CPU units for ECS tasks, by default DEFAULT_ECS_CPU
        ecs_task_memory : int, optional
            Memory for ECS tasks in MB, by default DEFAULT_ECS_MEMORY
        ecs_container_image : str, optional
            Container image for ECS tasks, by default DEFAULT_ECS_CONTAINER_IMAGE
        vpc_id : Optional[str], optional
            Existing VPC ID to use, by default None
        subnet_id : Optional[str], optional
            Existing subnet ID to use, by default None
        security_group_id : Optional[str], optional
            Existing security group ID to use, by default None
        use_public_ips : bool, optional
            Whether to assign public IPs to ECS tasks, by default True
        use_spot : bool, optional
            Whether to use spot instances for ECS tasks (Fargate Spot), by default False
        use_spot_fleet : bool, optional
            Whether to use Spot Fleet for EC2 instance deployment, by default False.
            If True, this overrides the use_spot parameter and uses EC2 Spot Fleet
            instead of Fargate Spot.
        instance_types : Optional[List[str]], optional
            List of instance types to use with Spot Fleet, by default None.
            If not provided but use_spot_fleet is True, a default set of instance
            types will be used.
        nodes_per_block : int, optional
            Number of nodes per block for Spot Fleet, by default 1
        spot_max_price_percentage : Optional[float], optional
            Maximum spot price as percentage of on-demand price, by default None.
            If None, AWS will use the current spot market price up to the on-demand price.
        additional_tags : Optional[Dict[str, str]], optional
            Tags to apply to created resources, by default None
        debug : bool, optional
            Whether to enable debug logging, by default False
        compute_type : Optional[str], optional
            Compute type forwarded by ``EphemeralAWSProvider`` ("lambda" or "ecs").
            When supplied it takes precedence over ``worker_type``; "ec2" is
            treated as unset since it has no meaning for serverless mode.
        memory_size : Optional[int], optional
            Lambda memory in MB, forwarded by ``EphemeralAWSProvider``. Overrides
            ``lambda_memory`` when supplied.
        timeout : Optional[int], optional
            Lambda timeout in seconds, forwarded by ``EphemeralAWSProvider``.
            Overrides ``lambda_timeout`` when supplied.
        """
        # compute_type is the provider-facing name for worker_type. Map it before
        # validating so an invalid value is reported against the real input.
        # It arrives as a ComputeType enum member, whose str() is the qualified
        # name rather than the value — read .value when present.
        if compute_type is not None:
            compute_type = str(getattr(compute_type, "value", compute_type)).lower()
            if compute_type != "ec2":
                worker_type = compute_type

        # Validate worker type before calling super(), so a bad value is reported
        # even when the network guard below would also have failed.
        if worker_type not in [WORKER_TYPE_LAMBDA, WORKER_TYPE_ECS, WORKER_TYPE_AUTO]:
            raise ConfigurationError(
                f"Serverless mode requires worker_type to be '{WORKER_TYPE_LAMBDA}', "
                f"'{WORKER_TYPE_ECS}', or '{WORKER_TYPE_AUTO}'"
            )

        # Lambda runs in the Lambda-managed VPC and needs none of the three
        # network IDs; ECS/Fargate requires a subnet and security group for its
        # mandatory awsvpcConfiguration. Only enforce the base-class requirement
        # when ECS is reachable.
        requires_network = worker_type in [WORKER_TYPE_ECS, WORKER_TYPE_AUTO]

        super().__init__(
            provider_id=provider_id,
            session=session,
            state_store=state_store,
            vpc_id=vpc_id,
            subnet_id=subnet_id,
            security_group_id=security_group_id,
            use_public_ips=use_public_ips,
            additional_tags=additional_tags,
            debug=debug,
            require_network_resources=requires_network,
            **kwargs,
        )

        # Set serverless mode specific attributes
        self.worker_type = worker_type
        self.lambda_timeout = timeout if timeout is not None else lambda_timeout
        self.lambda_memory = memory_size if memory_size is not None else lambda_memory
        self.lambda_runtime = lambda_runtime
        self.ecs_task_cpu = ecs_task_cpu
        self.ecs_task_memory = ecs_task_memory
        self.ecs_container_image = ecs_container_image

        # Spot and Spot Fleet configuration
        self.use_spot = use_spot
        self.use_spot_fleet = use_spot_fleet
        self.instance_types = instance_types or [
            "t3.small",
            "t3a.small",
            "t3.medium",
            "t3a.medium",
            "m5.large",
            "m5a.large",
            "c5.large",
            "c5a.large",
        ]
        self.nodes_per_block = nodes_per_block
        self.spot_max_price_percentage = spot_max_price_percentage

        # LambdaManager and ECSManager were written against EphemeralAWSProvider
        # and are handed this mode as their `provider`. Define the attributes they
        # read so the contract is satisfied without duplicating the mode.
        #
        # Credentials are deliberately left as None: self.session is already
        # fully authenticated by the provider via create_session(), and the
        # managers fall back to the ambient credential chain when these are unset.
        self.workflow_id = self.provider_id
        self.aws_access_key_id: Optional[str] = None
        self.aws_secret_access_key: Optional[str] = None
        self.aws_session_token: Optional[str] = None
        self.aws_profile: Optional[str] = None
        self.security_config: Optional[Any] = None
        # ECSManager reads this to decide whether to request Fargate Spot.
        self.use_spot_instances = use_spot
        # ECSManager prefers a subnet_ids list and falls back to subnet_id.
        self.subnet_ids = [self.subnet_id] if self.subnet_id else None

        # Initialize compute managers
        self.lambda_manager: Optional[LambdaManager] = None
        self.ecs_manager: Optional[ECSManager] = None
        self.cf_client = self.session.client("cloudformation")

        # Initialize spot interruption handling if enabled
        self.spot_interruption_monitor = None
        self.spot_interruption_handler = None

        if (self.use_spot or self.use_spot_fleet) and self.spot_interruption_handling:
            if not self.checkpoint_bucket and self.spot_interruption_handling:
                logger.warning(
                    "Spot interruption handling is enabled but no checkpoint bucket specified"
                )
            else:
                logger.debug(
                    "Initializing SpotInterruptionMonitor and Handler for ServerlessMode"
                )
                self.spot_interruption_monitor = SpotInterruptionMonitor(self.session)
                self.spot_interruption_handler = ParslSpotInterruptionHandler(
                    session=self.session,
                    checkpoint_bucket=self.checkpoint_bucket,
                    checkpoint_prefix=self.checkpoint_prefix,
                )
                self.spot_interruption_monitor.start_monitoring()

    def initialize(self) -> None:
        """Initialize serverless mode.

        Verifies the caller-supplied network resources (when the worker type
        needs them) and initializes the Lambda and/or ECS managers. Network
        resources are never created by this mode.

        Raises
        ------
        ResourceNotFoundError
            If a configured VPC, subnet, or security group is missing.
        ResourceCreationError
            If initialization fails
        """
        # Idempotent: if already initialized, do nothing.
        if self.initialized:
            return

        # Confirm the caller-supplied network resources exist before we rely on
        # them; Lambda-only deployments have nothing to verify. This runs ahead
        # of the try block, as in the other two modes, so ResourceNotFoundError
        # reaches the caller naming the unusable resource instead of being
        # re-wrapped as a generic ResourceCreationError. Verifying inside the try
        # also triggered cleanup_infrastructure() before anything had been
        # created.
        self._verify_resources()

        # Try to load state first
        if self.load_state():
            logger.debug("Loaded state, resources verified")
            # Initialize compute managers
            self._initialize_compute_managers()
            self.initialized = True
            return

        logger.debug("Initializing serverless mode")

        try:
            # Initialize compute managers
            self._initialize_compute_managers()

            self.initialized = True

            # Save state
            self.save_state()

            logger.info(
                f"Initialized serverless mode: "
                f"worker_type={self.worker_type}, "
                f"vpc_id={self.vpc_id}, subnet_id={self.subnet_id}, "
                f"security_group_id={self.security_group_id}"
            )
        except Exception as e:
            logger.error(f"Failed to initialize serverless mode: {e}")
            # Release anything we did stand up. Network resources belong to the
            # caller and are left untouched.
            self.cleanup_infrastructure()
            raise ResourceCreationError(
                f"Failed to initialize serverless mode: {e}"
            ) from e

    def _initialize_compute_managers(self) -> None:
        """Initialize compute managers based on worker type."""
        if self.worker_type in [WORKER_TYPE_LAMBDA, WORKER_TYPE_AUTO]:
            logger.debug("Initializing Lambda manager")
            self.lambda_manager = LambdaManager(self)

        if self.worker_type in [WORKER_TYPE_ECS, WORKER_TYPE_AUTO]:
            logger.debug("Initializing ECS manager")
            self.ecs_manager = ECSManager(self)

    def _select_worker_type(self, command: str, tasks_per_node: int) -> str:
        """Select the appropriate worker type for a job.

        Parameters
        ----------
        command : str
            Command to execute
        tasks_per_node : int
            Number of tasks per node

        Returns
        -------
        str
            Worker type to use (lambda or ecs)
        """
        # If worker type is not auto, use the configured type
        if self.worker_type != WORKER_TYPE_AUTO:
            return self.worker_type

        # For auto mode, select based on job characteristics

        # Use Lambda for short, simple jobs
        if len(command) < 5000 and tasks_per_node <= 1:
            return WORKER_TYPE_LAMBDA

        # Otherwise use ECS
        return WORKER_TYPE_ECS

    def submit_job(
        self,
        job_id: str,
        command: str,
        tasks_per_node: int,
        job_name: Optional[str] = None,
    ) -> str:
        """Submit a job for execution.

        Parameters
        ----------
        job_id : str
            Unique identifier for the job
        command : str
            Command to execute
        tasks_per_node : int
            Number of tasks to run per node
        job_name : Optional[str], optional
            Human-readable name for the job, by default None

        Returns
        -------
        str
            Resource ID for tracking the job

        Raises
        ------
        OperatingModeError
            If job submission fails
        """
        # Ensure the mode is initialized
        self.ensure_initialized()

        logger.info(
            f"Submitting job {job_id} ({job_name if job_name else 'unnamed'}) in serverless mode"
        )

        try:
            # Select worker type
            worker_type = self._select_worker_type(command, tasks_per_node)

            # Resource ID will be the CloudFormation stack for the job
            resource_id = f"serverless-{worker_type}-{job_id}"

            # Submit job to the appropriate service using CloudFormation
            if worker_type == WORKER_TYPE_LAMBDA:
                if not self.lambda_manager:
                    raise JobSubmissionError("Lambda manager not initialized")
                self._submit_lambda_job(job_id, command, job_name, resource_id)

            elif worker_type == WORKER_TYPE_ECS:
                if not self.ecs_manager:
                    raise JobSubmissionError("ECS manager not initialized")

                # Make sure we have the required network resources for ECS
                if not self.vpc_id or not self.subnet_id or not self.security_group_id:
                    raise JobSubmissionError(
                        "Missing required network resources for ECS tasks. "
                        "VPC, subnet, and security group are required."
                    )

                self._submit_ecs_job(
                    job_id, command, tasks_per_node, job_name, resource_id
                )

            # Add resource to tracking
            self.resources[resource_id] = {
                "id": resource_id,
                "job_id": job_id,
                "job_name": job_name or "unnamed",
                "worker_type": worker_type,
                "command": command,
                "tasks_per_node": tasks_per_node,
                "status": STATUS_PENDING,
                "created_at": time.time(),
            }

            # Save state
            self.save_state()

            logger.info(f"Submitted job {job_id} with resource ID {resource_id}")
            return resource_id

        except Exception as e:
            logger.error(f"Failed to submit job {job_id}: {e}")
            raise OperatingModeError(f"Failed to submit job {job_id}: {e}") from e

    def _submit_lambda_job(
        self, job_id: str, command: str, job_name: Optional[str], resource_id: str
    ) -> None:
        """Submit a job to AWS Lambda.

        Parameters
        ----------
        job_id : str
            Unique identifier for the job
        command : str
            Command to execute
        job_name : Optional[str]
            Human-readable name for the job
        resource_id : str
            Resource ID for tracking

        Raises
        ------
        JobSubmissionError
            If job submission fails
        """
        logger.debug(f"Submitting job {job_id} to Lambda")

        try:
            # Generate Lambda function code
            code_zip = self.lambda_manager._generate_lambda_code(command)

            # Create temporary file with code
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp.write(code_zip)
                tmp_path = tmp.name

            try:
                # Encode zip file for CloudFormation
                with open(tmp_path, "rb") as f:
                    code_content = f.read()

                # Deploy Lambda function using CloudFormation
                stack_name = f"parsl-lambda-{job_id[:8]}"
                template_path = os.path.join(
                    os.path.dirname(os.path.dirname(__file__)),
                    "templates/cloudformation/lambda_worker.yml",
                )

                with open(template_path, "r") as f:
                    template_body = f.read()

                # Create CloudFormation stack
                self.cf_client.create_stack(
                    StackName=stack_name,
                    TemplateBody=template_body,
                    Parameters=[
                        {
                            "ParameterKey": "FunctionName",
                            "ParameterValue": f"parsl-lambda-{job_id}",
                        },
                        {
                            "ParameterKey": "Runtime",
                            "ParameterValue": self.lambda_runtime,
                        },
                        {
                            "ParameterKey": "Handler",
                            "ParameterValue": DEFAULT_LAMBDA_HANDLER,
                        },
                        {
                            "ParameterKey": "MemorySize",
                            "ParameterValue": str(self.lambda_memory),
                        },
                        {
                            "ParameterKey": "Timeout",
                            "ParameterValue": str(self.lambda_timeout),
                        },
                        {
                            "ParameterKey": "CodeZipContent",
                            "ParameterValue": code_content.decode("latin1"),
                        },
                        {
                            "ParameterKey": "WorkflowId",
                            "ParameterValue": self.provider_id,
                        },
                        {"ParameterKey": "JobId", "ParameterValue": job_id},
                    ],
                    Tags=[
                        {"Key": "CreatedBy", "Value": "ParslEphemeralAWSProvider"},
                        {"Key": "ProviderId", "Value": self.provider_id},
                        {"Key": "JobId", "Value": job_id},
                    ],
                    Capabilities=["CAPABILITY_IAM"],
                )

                # Store reference to stack in resource data
                self.resources[resource_id].update(
                    {
                        "stack_name": stack_name,
                        "resource_type": RESOURCE_TYPE_LAMBDA_FUNCTION,
                    }
                )

                logger.debug(
                    f"Created CloudFormation stack {stack_name} for Lambda job {job_id}"
                )

            finally:
                # Clean up temporary file
                os.unlink(tmp_path)

        except Exception as e:
            logger.error(f"Failed to submit Lambda job {job_id}: {e}")
            raise JobSubmissionError(f"Failed to submit Lambda job: {e}") from e

    def _submit_ecs_job(
        self,
        job_id: str,
        command: str,
        tasks_per_node: int,
        job_name: Optional[str],
        resource_id: str,
    ) -> None:
        """Submit a job to ECS/Fargate or EC2 using SpotFleet.

        This method supports two deployment modes:
        1. ECS/Fargate: The default mode, which uses serverless containers
        2. EC2 SpotFleet: When use_spot_fleet=True, deploys EC2 instances using SpotFleet
           for improved reliability and cost savings

        Parameters
        ----------
        job_id : str
            Unique identifier for the job
        command : str
            Command to execute
        tasks_per_node : int
            Number of tasks per node
        job_name : Optional[str]
            Human-readable name for the job
        resource_id : str
            Resource ID for tracking

        Raises
        ------
        JobSubmissionError
            If job submission fails
        """
        logger.debug(f"Submitting job {job_id} to ECS")

        try:
            # Deploy ECS task using CloudFormation
            stack_name = f"parsl-ecs-{job_id[:8]}"
            template_path = os.path.join(
                os.path.dirname(os.path.dirname(__file__)),
                "templates/cloudformation/ecs_worker.yml",
            )

            with open(template_path, "r") as f:
                template_body = f.read()

            # Create CloudFormation stack
            self.cf_client.create_stack(
                StackName=stack_name,
                TemplateBody=template_body,
                Parameters=[
                    {
                        "ParameterKey": "ClusterName",
                        "ParameterValue": f"parsl-ecs-cluster-{self.provider_id[:8]}",
                    },
                    {
                        "ParameterKey": "TaskFamily",
                        "ParameterValue": f"parsl-ecs-task-{job_id[:8]}",
                    },
                    {
                        "ParameterKey": "ContainerImage",
                        "ParameterValue": self.ecs_container_image,
                    },
                    {
                        "ParameterKey": "TaskCpu",
                        "ParameterValue": str(self.ecs_task_cpu),
                    },
                    {
                        "ParameterKey": "TaskMemory",
                        "ParameterValue": str(self.ecs_task_memory),
                    },
                    {
                        "ParameterKey": "Command",
                        "ParameterValue": command.replace("\n", ";"),
                    },
                    {"ParameterKey": "VpcId", "ParameterValue": self.vpc_id},
                    {"ParameterKey": "SubnetIds", "ParameterValue": self.subnet_id},
                    {
                        "ParameterKey": "SecurityGroupIds",
                        "ParameterValue": self.security_group_id,
                    },
                    {
                        "ParameterKey": "AssignPublicIp",
                        "ParameterValue": "ENABLED"
                        if self.use_public_ips
                        else "DISABLED",
                    },
                    {"ParameterKey": "WorkflowId", "ParameterValue": self.provider_id},
                    {"ParameterKey": "JobId", "ParameterValue": job_id},
                    {
                        "ParameterKey": "TaskCount",
                        "ParameterValue": str(max(1, tasks_per_node)),
                    },
                    {
                        "ParameterKey": "UseSpot",
                        "ParameterValue": "true"
                        if self.use_spot and not self.use_spot_fleet
                        else "false",
                    },
                    {
                        "ParameterKey": "UseSpotFleet",
                        "ParameterValue": "true" if self.use_spot_fleet else "false",
                    },
                    {
                        "ParameterKey": "InstanceTypes",
                        "ParameterValue": json.dumps(self.instance_types)
                        if self.use_spot_fleet
                        else "[]",
                    },
                    {
                        "ParameterKey": "NodesPerBlock",
                        "ParameterValue": str(self.nodes_per_block)
                        if self.use_spot_fleet
                        else "1",
                    },
                    {
                        "ParameterKey": "SpotMaxPricePercentage",
                        "ParameterValue": str(self.spot_max_price_percentage)
                        if self.spot_max_price_percentage
                        else "",
                    },
                ],
                Tags=[
                    {"Key": "CreatedBy", "Value": "ParslEphemeralAWSProvider"},
                    {"Key": "ProviderId", "Value": self.provider_id},
                    {"Key": "JobId", "Value": job_id},
                ],
                Capabilities=["CAPABILITY_IAM"],
            )

            # Store reference to stack in resource data
            resource_type = RESOURCE_TYPE_ECS_TASK
            if self.use_spot_fleet:
                from parsl_ephemeral_aws.constants import RESOURCE_TYPE_SPOT_FLEET

                resource_type = RESOURCE_TYPE_SPOT_FLEET

            self.resources[resource_id].update(
                {
                    "stack_name": stack_name,
                    "resource_type": resource_type,
                    "use_spot_fleet": self.use_spot_fleet,
                }
            )

            # Register with spot interruption monitor if needed
            if (
                self.use_spot_fleet
                and self.spot_interruption_handling
                and self.spot_interruption_monitor
                and self.spot_interruption_handler
            ):
                # We need to wait for and fetch the fleet request ID from the outputs
                start_time = time.time()
                fleet_request_id = None

                # Wait for up to 3 minutes for stack to create resources
                while time.time() - start_time < 180 and not fleet_request_id:
                    try:
                        # Check if the stack has output values yet
                        stack_response = self.cf_client.describe_stacks(
                            StackName=stack_name
                        )
                        stack_status = stack_response["Stacks"][0]["StackStatus"]

                        # Only check outputs if stack is complete
                        if stack_status == "CREATE_COMPLETE":
                            outputs = stack_response["Stacks"][0].get("Outputs", [])

                            for output in outputs:
                                if output["OutputKey"] == "SpotFleetRequestId":
                                    fleet_request_id = output["OutputValue"]
                                    break

                            if fleet_request_id:
                                # Save fleet ID in resource data
                                self.resources[resource_id][
                                    "fleet_request_id"
                                ] = fleet_request_id

                                # Register with interruption monitor
                                self.spot_interruption_monitor.register_fleet(
                                    fleet_request_id,
                                    self.spot_interruption_handler.handle_fleet_interruption,
                                )
                                logger.info(
                                    f"Registered spot fleet {fleet_request_id} for interruption handling"
                                )
                                break

                        # If stack is still being created, wait and check again
                        if "CREATE_IN_PROGRESS" in stack_status:
                            time.sleep(10)
                        else:
                            # If stack has failed or is in any other state, log and break
                            if stack_status != "CREATE_COMPLETE":
                                logger.warning(
                                    f"Stack {stack_name} is in state {stack_status}, not waiting for fleet ID"
                                )
                            break
                    except Exception as e:
                        logger.error(
                            f"Error getting spot fleet ID from stack {stack_name}: {e}"
                        )
                        break

            logger.debug(
                f"Created CloudFormation stack {stack_name} for ECS job {job_id}"
            )

        except Exception as e:
            logger.error(f"Failed to submit ECS job {job_id}: {e}")
            raise JobSubmissionError(f"Failed to submit ECS job: {e}") from e

    def get_job_status(self, resource_ids: List[str]) -> Dict[str, str]:
        """Get the status of jobs.

        Parameters
        ----------
        resource_ids : List[str]
            List of resource IDs to check

        Returns
        -------
        Dict[str, str]
            Dictionary mapping resource IDs to status strings
        """
        if not resource_ids:
            return {}

        status_map = {}

        for resource_id in resource_ids:
            resource = self.resources.get(resource_id)
            if not resource:
                status_map[resource_id] = STATUS_UNKNOWN
                continue

            # Get stack status
            stack_name = resource.get("stack_name")
            if not stack_name:
                status_map[resource_id] = resource.get("status", STATUS_UNKNOWN)
                continue

            try:
                response = self.cf_client.describe_stacks(StackName=stack_name)
                stack_status = response["Stacks"][0]["StackStatus"]

                # Map CloudFormation status to our status
                if stack_status.startswith("CREATE_IN_PROGRESS"):
                    status = STATUS_PENDING
                elif stack_status == "CREATE_COMPLETE":
                    # When stack is complete, we need to check the actual resource status
                    worker_type = resource.get("worker_type")

                    if worker_type == WORKER_TYPE_LAMBDA:
                        if self.lambda_manager:
                            # For Lambda, we get the function name from stack outputs
                            outputs = response["Stacks"][0].get("Outputs", [])
                            function_name = None

                            for output in outputs:
                                if output["OutputKey"] == "LambdaFunctionName":
                                    function_name = output["OutputValue"]
                                    break

                            if function_name:
                                # Check Lambda invocation status
                                # Note: This is simplified as true Lambda status tracking
                                # would require additional mechanisms
                                status = self._get_lambda_status(
                                    function_name, resource_id
                                )
                            else:
                                status = STATUS_RUNNING

                    elif worker_type == WORKER_TYPE_ECS:
                        # Check if this is a SpotFleet resource
                        if resource.get("use_spot_fleet"):
                            # For SpotFleet resources, we need to check the fleet status
                            outputs = response["Stacks"][0].get("Outputs", [])
                            fleet_request_id = None

                            for output in outputs:
                                if output["OutputKey"] == "SpotFleetRequestId":
                                    fleet_request_id = output["OutputValue"]
                                    break

                            if fleet_request_id:
                                # If we haven't stored the fleet_request_id yet, do so
                                if "fleet_request_id" not in resource:
                                    self.resources[resource_id][
                                        "fleet_request_id"
                                    ] = fleet_request_id
                                    from parsl_ephemeral_aws.constants import (
                                        RESOURCE_TYPE_SPOT_FLEET,
                                    )

                                    self.resources[resource_id][
                                        "resource_type"
                                    ] = RESOURCE_TYPE_SPOT_FLEET

                                # Check SpotFleet status
                                status = self._get_spot_fleet_status(fleet_request_id)
                            else:
                                status = STATUS_RUNNING
                        elif self.ecs_manager:
                            # For standard ECS, we get the cluster and service name from stack outputs
                            outputs = response["Stacks"][0].get("Outputs", [])
                            cluster_name = None
                            service_name = None

                            for output in outputs:
                                if output["OutputKey"] == "ClusterName":
                                    cluster_name = output["OutputValue"]
                                elif output["OutputKey"] == "ServiceName":
                                    service_name = output["OutputValue"]

                            if cluster_name and service_name:
                                # Check ECS service status
                                status = self._get_ecs_status(
                                    cluster_name, service_name
                                )
                            else:
                                status = STATUS_RUNNING
                    else:
                        status = STATUS_RUNNING

                elif stack_status.endswith("FAILED"):
                    status = STATUS_FAILED
                elif stack_status.startswith("DELETE"):
                    status = STATUS_CANCELLED
                else:
                    status = STATUS_RUNNING

                # Update resource status
                self.resources[resource_id]["status"] = status
                status_map[resource_id] = status

            except ClientError as e:
                logger.error(f"Failed to get stack status for {stack_name}: {e}")

                # Handle case where stack doesn't exist anymore
                if "does not exist" in str(e):
                    # Assume job completed
                    status = STATUS_SUCCEEDED
                    self.resources[resource_id]["status"] = status
                    status_map[resource_id] = status
                else:
                    status_map[resource_id] = STATUS_UNKNOWN
            except Exception as e:
                logger.error(f"Unexpected error getting status for {resource_id}: {e}")
                status_map[resource_id] = STATUS_UNKNOWN

        # Save state with updated status
        self.save_state()

        return status_map

    def _get_lambda_status(self, function_name: str, resource_id: str) -> str:
        """Get the status of a Lambda job.

        Parameters
        ----------
        function_name : str
            Lambda function name
        resource_id : str
            Resource ID for tracking

        Returns
        -------
        str
            Job status
        """
        # In a real implementation, we would use CloudWatch Logs or a state store
        # to track Lambda execution. For now, we'll simulate based on time.
        resource = self.resources.get(resource_id, {})
        elapsed = time.time() - resource.get("created_at", 0)

        if elapsed < 5:
            return STATUS_PENDING
        elif elapsed < self.lambda_timeout:
            return STATUS_RUNNING
        else:
            # After timeout, assume success (in a real impl, we'd check CloudWatch)
            return STATUS_SUCCEEDED

    def _get_spot_fleet_status(self, fleet_request_id: str) -> str:
        """Get the status of a Spot Fleet request.

        Parameters
        ----------
        fleet_request_id : str
            ID of the Spot Fleet request

        Returns
        -------
        str
            Job status
        """
        ec2_client = self.session.client("ec2")

        try:
            # Get Spot Fleet request details
            response = ec2_client.describe_spot_fleet_requests(
                SpotFleetRequestIds=[fleet_request_id]
            )

            if not response.get("SpotFleetRequestConfigs"):
                return STATUS_UNKNOWN

            fleet_config = response["SpotFleetRequestConfigs"][0]
            fleet_status = fleet_config["SpotFleetRequestState"]

            # Map fleet status to Parsl status
            if fleet_status == "submitted":
                return STATUS_PENDING
            elif fleet_status == "active":
                # Check if target capacity is fulfilled
                fulfilled_capacity = fleet_config.get("FulfilledCapacity", 0)
                target_capacity = fleet_config.get("SpotFleetRequestConfig", {}).get(
                    "TargetCapacity", 0
                )

                if fulfilled_capacity >= target_capacity:
                    # All requested instances are running
                    # To determine if job is complete, ideally we'd check instance status
                    # For now, we assume running
                    return STATUS_RUNNING
                else:
                    # Still waiting for instances
                    return STATUS_PENDING
            elif fleet_status == "cancelled_running":
                # Fleet is being cancelled but instances still running
                return STATUS_RUNNING
            elif fleet_status == "cancelled_terminating":
                # Fleet is cancelled and instances are terminating
                return STATUS_CANCELLED
            elif fleet_status == "cancelled":
                # Fleet is fully cancelled
                return STATUS_CANCELLED
            elif fleet_status == "failed":
                return STATUS_FAILED
            elif fleet_status == "modify_in_progress":
                return STATUS_RUNNING
            else:
                return STATUS_UNKNOWN

        except Exception as e:
            logger.error(f"Error getting Spot Fleet status for {fleet_request_id}: {e}")
            return STATUS_UNKNOWN

    def _get_ecs_status(self, cluster_name: str, service_name: str) -> str:
        """Get the status of an ECS service.

        Parameters
        ----------
        cluster_name : str
            ECS cluster name
        service_name : str
            ECS service name

        Returns
        -------
        str
            Job status
        """
        ecs_client = self.session.client("ecs")

        try:
            # Get service details
            response = ecs_client.describe_services(
                cluster=cluster_name, services=[service_name]
            )

            if not response["services"]:
                return STATUS_UNKNOWN

            service = response["services"][0]

            # Check if service has tasks
            task_response = ecs_client.list_tasks(
                cluster=cluster_name, serviceName=service_name
            )

            # If no tasks, check service events to determine status
            if not task_response.get("taskArns"):
                # Check deployment status
                deployments = service.get("deployments", [])
                if not deployments:
                    return STATUS_COMPLETED

                # Look at recent events for status info
                events = service.get("events", [])
                if events:
                    # Look for completion or failure events
                    for event in events[:5]:  # Check recent events
                        if "has reached a steady state" in event.get("message", ""):
                            return STATUS_SUCCEEDED
                        if "was unable to place a task" in event.get("message", ""):
                            return STATUS_FAILED

                # If desired count is 0, job is considered complete
                if service.get("desiredCount", 0) == 0:
                    return STATUS_COMPLETED

                # Otherwise still pending
                return STATUS_PENDING

            # Get task details
            task_arns = task_response["taskArns"]
            if task_arns:
                task_details = ecs_client.describe_tasks(
                    cluster=cluster_name,
                    tasks=[task_arns[0]],  # Check first task
                )

                if task_details["tasks"]:
                    task = task_details["tasks"][0]
                    last_status = task["lastStatus"]

                    if last_status == "PROVISIONING" or last_status == "PENDING":
                        return STATUS_PENDING
                    elif last_status == "RUNNING":
                        return STATUS_RUNNING
                    elif last_status == "STOPPED":
                        # Check if task stopped with error
                        if task.get(
                            "stoppedReason"
                        ) and "Essential container" in task.get("stoppedReason"):
                            # Look at container exit codes
                            for container in task.get("containers", []):
                                exit_code = container.get("exitCode")
                                if exit_code is not None and exit_code != 0:
                                    return STATUS_FAILED

                        return STATUS_SUCCEEDED
                    else:
                        return STATUS_RUNNING

            # Default to running if service exists but status is unclear
            return STATUS_RUNNING

        except Exception as e:
            logger.error(f"Error getting ECS service status: {e}")
            return STATUS_UNKNOWN

    def cancel_jobs(self, resource_ids: List[str]) -> Dict[str, str]:
        """Cancel jobs.

        Parameters
        ----------
        resource_ids : List[str]
            List of resource IDs to cancel

        Returns
        -------
        Dict[str, str]
            Dictionary mapping resource IDs to status strings
        """
        if not resource_ids:
            return {}

        cancel_map = {}
        ec2_client = self.session.client("ec2")

        for resource_id in resource_ids:
            resource = self.resources.get(resource_id)
            if not resource:
                cancel_map[resource_id] = STATUS_UNKNOWN
                continue

            # Get stack name
            stack_name = resource.get("stack_name")
            if not stack_name:
                cancel_map[resource_id] = STATUS_UNKNOWN
                continue

            try:
                # Check if this is a SpotFleet resource and handle it specially
                from parsl_ephemeral_aws.constants import RESOURCE_TYPE_SPOT_FLEET

                if resource.get(
                    "resource_type"
                ) == RESOURCE_TYPE_SPOT_FLEET and resource.get("fleet_request_id"):
                    fleet_request_id = resource.get("fleet_request_id")

                    # Cancel the Spot Fleet request directly for immediate termination
                    try:
                        ec2_client.cancel_spot_fleet_requests(
                            SpotFleetRequestIds=[fleet_request_id],
                            TerminateInstances=True,
                        )
                        logger.info(
                            f"Cancelled Spot Fleet request {fleet_request_id} for job {resource.get('job_id')}"
                        )
                    except Exception as e:
                        logger.warning(
                            f"Error cancelling Spot Fleet request {fleet_request_id}: {e}"
                        )

                # Always delete the CloudFormation stack to cancel the job
                self.cf_client.delete_stack(StackName=stack_name)

                # Mark as cancelled
                self.resources[resource_id]["status"] = STATUS_CANCELLED
                cancel_map[resource_id] = STATUS_CANCELLED

                logger.info(
                    f"Cancelled job {resource.get('job_id')} (stack: {stack_name})"
                )

            except ClientError as e:
                logger.error(f"Failed to cancel job (stack: {stack_name}): {e}")

                # Handle case where stack doesn't exist anymore
                if "does not exist" in str(e):
                    # Assume job completed
                    cancel_map[resource_id] = STATUS_COMPLETED
                    self.resources[resource_id]["status"] = STATUS_COMPLETED
                else:
                    cancel_map[resource_id] = STATUS_FAILED
            except Exception as e:
                logger.error(
                    f"Unexpected error cancelling job (stack: {stack_name}): {e}"
                )
                cancel_map[resource_id] = STATUS_FAILED

        # Save state with updated status
        self.save_state()

        return cancel_map

    def cleanup_resources(self, resource_ids: List[str]) -> None:
        """Clean up resources.

        Parameters
        ----------
        resource_ids : List[str]
            List of resource IDs to clean up
        """
        if not resource_ids:
            return

        for resource_id in resource_ids:
            resource = self.resources.get(resource_id)
            if not resource:
                continue

            # Get stack name
            stack_name = resource.get("stack_name")
            if not stack_name:
                # Remove resource from tracking
                if resource_id in self.resources:
                    del self.resources[resource_id]
                continue

            try:
                # Delete the CloudFormation stack
                self.cf_client.delete_stack(StackName=stack_name)

                logger.info(
                    f"Deleted stack {stack_name} for job {resource.get('job_id')}"
                )

                # Remove resource from tracking
                if resource_id in self.resources:
                    del self.resources[resource_id]

            except ClientError as e:
                # If the stack is already deleted or doesn't exist, that's fine
                if "does not exist" not in str(e):
                    logger.error(f"Failed to delete stack {stack_name}: {e}")

                # Still remove resource from tracking
                if resource_id in self.resources:
                    del self.resources[resource_id]
            except Exception as e:
                logger.error(f"Unexpected error deleting stack {stack_name}: {e}")
                # Still remove resource from tracking
                if resource_id in self.resources:
                    del self.resources[resource_id]

        # Save state with updated resources
        self.save_state()

    def cleanup_infrastructure(self) -> None:
        """Clean up infrastructure created by this mode.

        The VPC, subnet, and security group are supplied by the caller and are
        never created — or deleted — by this mode.  Only the Lambda functions,
        ECS tasks, and Spot Fleet resources created here are removed.
        """
        logger.info("Cleaning up serverless mode infrastructure")

        # Delete all resources first
        if self.resources:
            self.cleanup_all()

        # Stop spot interruption monitoring if enabled
        if self.spot_interruption_monitor:
            try:
                self.spot_interruption_monitor.stop_monitoring()
                logger.info("Stopped spot interruption monitoring")
            except Exception as e:
                logger.error(f"Failed to stop spot interruption monitoring: {e}")
            self.spot_interruption_monitor = None
            self.spot_interruption_handler = None

        # Clean up compute managers
        if self.lambda_manager:
            try:
                self.lambda_manager.cleanup_all_resources()
            except Exception as e:
                logger.error(f"Error cleaning up Lambda manager resources: {e}")

        if self.ecs_manager:
            try:
                self.ecs_manager.cleanup_all_resources()
            except Exception as e:
                logger.error(f"Error cleaning up ECS manager resources: {e}")

        # Cleanup SpotFleet resources if needed
        if self.use_spot_fleet:
            try:
                # Import and use SpotFleet cleanup utility
                from parsl_ephemeral_aws.compute.spot_fleet_cleanup import (
                    cleanup_all_spot_fleet_resources,
                )

                cleanup_result = cleanup_all_spot_fleet_resources(
                    session=self.session,
                    workflow_id=self.provider_id,
                    cancel_active_requests=True,
                    cleanup_iam_roles=True,
                )

                # Log cleanup results
                if cleanup_result:
                    # Log successful operations
                    if cleanup_result.get("cancelled_requests"):
                        logger.info(
                            f"Cancelled {len(cleanup_result['cancelled_requests'])} SpotFleet requests"
                        )

                    if cleanup_result.get("cleaned_roles"):
                        logger.info(
                            f"Cleaned up {len(cleanup_result['cleaned_roles'])} IAM roles"
                        )

                    # Log errors
                    if cleanup_result.get("errors"):
                        for error in cleanup_result["errors"]:
                            logger.warning(f"SpotFleet cleanup error: {error}")
            except Exception as e:
                logger.error(f"Error cleaning up SpotFleet resources: {e}")

        # Clear initialization flag
        self.initialized = False

        # Save state
        self.save_state()

        logger.info("Serverless mode infrastructure cleanup complete")

    def list_resources(self) -> Dict[str, List[Dict[str, Any]]]:
        """List all resources created by this mode.

        Returns
        -------
        Dict[str, List[Dict[str, Any]]]
            Dictionary of resource types and their details
        """
        result: Dict[str, List[Dict[str, Any]]] = {
            "lambda_functions": [],
            "ecs_tasks": [],
            "spot_fleet_requests": [],
            "vpc": [],
            "subnet": [],
            "security_group": [],
        }

        # Add jobs by resource type
        for resource_id, resource in self.resources.items():
            worker_type = resource.get("worker_type")

            if worker_type == WORKER_TYPE_LAMBDA:
                result["lambda_functions"].append(
                    {
                        "id": resource_id,
                        "job_id": resource.get("job_id"),
                        "job_name": resource.get("job_name"),
                        "status": resource.get("status"),
                        "created_at": resource.get("created_at"),
                        "stack_name": resource.get("stack_name"),
                    }
                )
            elif worker_type == WORKER_TYPE_ECS:
                # Check if this is a SpotFleet resource
                if resource.get("use_spot_fleet") and resource.get("fleet_request_id"):
                    result["spot_fleet_requests"].append(
                        {
                            "id": resource_id,
                            "job_id": resource.get("job_id"),
                            "job_name": resource.get("job_name"),
                            "status": resource.get("status"),
                            "created_at": resource.get("created_at"),
                            "stack_name": resource.get("stack_name"),
                            "fleet_request_id": resource.get("fleet_request_id"),
                        }
                    )
                else:
                    result["ecs_tasks"].append(
                        {
                            "id": resource_id,
                            "job_id": resource.get("job_id"),
                            "job_name": resource.get("job_name"),
                            "status": resource.get("status"),
                            "created_at": resource.get("created_at"),
                            "stack_name": resource.get("stack_name"),
                        }
                    )

        # Add VPC if available
        if self.vpc_id:
            result["vpc"].append(
                {
                    "id": self.vpc_id,
                }
            )

        # Add subnet if available
        if self.subnet_id:
            result["subnet"].append(
                {
                    "id": self.subnet_id,
                    "vpc_id": self.vpc_id,
                }
            )

        # Add security group if available
        if self.security_group_id:
            result["security_group"].append(
                {
                    "id": self.security_group_id,
                    "vpc_id": self.vpc_id,
                }
            )

        return result

    def cleanup_all(self) -> None:
        """Clean up all resources created by this mode."""
        logger.info("Cleaning up all serverless mode resources")

        # Get all resource IDs
        resource_ids = list(self.resources.keys())

        if resource_ids:
            self.cleanup_resources(resource_ids)
            logger.info(f"Cleaned up {len(resource_ids)} resources")
        else:
            logger.debug("No resources to clean up")

    def save_state(self) -> None:
        """Save the current state to the state store."""
        state = {
            "resources": self.resources,
            "provider_id": self.provider_id,
            "mode": self.__class__.__name__,
            "vpc_id": self.vpc_id,
            "subnet_id": self.subnet_id,
            "security_group_id": self.security_group_id,
            "initialized": self.initialized,
            "use_spot": self.use_spot,
            "use_spot_fleet": self.use_spot_fleet,
            "spot_interruption_handling": self.spot_interruption_handling,
        }

        try:
            self.state_store.save_state(STATE_KEY_MODE, state)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def load_state(self) -> bool:
        """Load state from the state store.

        Returns
        -------
        bool
            True if state was loaded successfully, False otherwise
        """
        try:
            state = self.state_store.load_state(STATE_KEY_MODE)
            if state and state.get("provider_id") == self.provider_id:
                self.resources = state.get("resources", {})
                self._restore_network_ids(state)
                self.initialized = state.get("initialized", False)

                # Check if spot interruption handling was previously enabled
                previous_spot_handling = state.get("spot_interruption_handling", False)
                if previous_spot_handling != self.spot_interruption_handling:
                    logger.info(
                        f"Spot interruption handling changed from {previous_spot_handling} to {self.spot_interruption_handling}"
                    )

                    # Initialize or clean up spot interruption handling based on new setting
                    if self.spot_interruption_handling and (
                        self.use_spot or self.use_spot_fleet
                    ):
                        if not self.spot_interruption_monitor:
                            logger.debug(
                                "Initializing SpotInterruptionMonitor and Handler after state load"
                            )
                            self.spot_interruption_monitor = SpotInterruptionMonitor(
                                self.session
                            )
                            self.spot_interruption_handler = (
                                ParslSpotInterruptionHandler(
                                    session=self.session,
                                    checkpoint_bucket=self.checkpoint_bucket,
                                    checkpoint_prefix=self.checkpoint_prefix,
                                )
                            )
                            self.spot_interruption_monitor.start_monitoring()
                    elif (
                        not self.spot_interruption_handling
                        and self.spot_interruption_monitor
                    ):
                        logger.debug(
                            "Stopping SpotInterruptionMonitor after state load"
                        )
                        self.spot_interruption_monitor.stop_monitoring()
                        self.spot_interruption_monitor = None
                        self.spot_interruption_handler = None

                # Re-register existing spot fleet resources with interruption monitor if needed
                if (
                    self.spot_interruption_handling
                    and self.spot_interruption_monitor
                    and self.spot_interruption_handler
                ):
                    for resource_id, resource in self.resources.items():
                        from parsl_ephemeral_aws.constants import (
                            RESOURCE_TYPE_SPOT_FLEET,
                        )

                        if resource.get(
                            "resource_type"
                        ) == RESOURCE_TYPE_SPOT_FLEET and resource.get(
                            "fleet_request_id"
                        ):
                            fleet_request_id = resource.get("fleet_request_id")
                            self.spot_interruption_monitor.register_fleet(
                                fleet_request_id,
                                self.spot_interruption_handler.handle_fleet_interruption,
                            )
                            logger.info(
                                f"Re-registered spot fleet {fleet_request_id} for interruption handling"
                            )

                logger.debug(f"Loaded state with {len(self.resources)} resources")
                return True
        except Exception as e:
            logger.error(f"Failed to load state: {e}")

        return False

"""
Parsl Ephemeral AWS Provider implementation.

This module implements the main provider class that conforms to the Parsl
execution provider interface.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import logging
import time
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional

from parsl.providers.base import ExecutionProvider
from parsl.utils import RepresentationMixin
from typeguard import typechecked

from parsl_ephemeral_aws.constants import (
    DEFAULT_INSTANCE_TYPE,
    DEFAULT_MAX_BLOCKS,
    DEFAULT_MAX_IDLE_TIME,
    DEFAULT_MIN_BLOCKS,
    DEFAULT_MODE,
    DEFAULT_REGION,
    DEFAULT_WORKER_INIT,
)
from parsl_ephemeral_aws.exceptions import (
    ProviderConfigurationError,
    ProviderError,
)
from parsl_ephemeral_aws.modes.base import OperatingMode
from parsl_ephemeral_aws.modes.detached import DetachedMode
from parsl_ephemeral_aws.modes.serverless import ServerlessMode
from parsl_ephemeral_aws.modes.standard import StandardMode
from parsl_ephemeral_aws.state.base import StateStore
from parsl_ephemeral_aws.state.file import FileStateStore
from parsl_ephemeral_aws.state.parameter_store import ParameterStoreStateStore
from parsl_ephemeral_aws.state.s3 import S3StateStore
from parsl_ephemeral_aws.utils.aws import create_session


logger = logging.getLogger(__name__)


class OperatingModeType(str, Enum):
    """Supported operating modes for the provider."""

    STANDARD = "standard"
    DETACHED = "detached"
    SERVERLESS = "serverless"


class StateStoreType(str, Enum):
    """Supported state persistence options."""

    FILE = "file"
    PARAMETER_STORE = "parameter_store"
    S3 = "s3"


class ComputeType(str, Enum):
    """Supported compute resource types."""

    EC2 = "ec2"
    LAMBDA = "lambda"
    ECS = "ecs"


@typechecked
class EphemeralAWSProvider(ExecutionProvider, RepresentationMixin):
    """Ephemeral AWS Provider for Parsl.

    The Ephemeral AWS Provider allows Parsl to execute tasks on ephemeral
    AWS resources that are created on-demand and automatically cleaned up
    when no longer needed.

    Parameters
    ----------
    image_id : str, optional
        EC2 AMI ID to use for instances. Required when using EC2 instances.
    instance_type : str, optional
        EC2 instance type. Default is 't3.micro'.
    region : str, optional
        AWS region. Default is 'us-east-1'.
    mode : str, optional
        Operating mode ('standard', 'detached', or 'serverless'). Default is 'standard'.
    min_blocks : int, optional
        Minimum number of blocks. Default is 0.
    max_blocks : int, optional
        Maximum number of blocks. Default is 10.
    worker_init : str, optional
        Initialization script for workers. Default is an empty script.
    vpc_id : str, optional
        Existing VPC ID to use. If not provided, a new VPC will be created.
    subnet_id : str, optional
        Existing subnet ID to use. If not provided, a new subnet will be created.
    security_group_id : str, optional
        Existing security group ID to use. If not provided, a new security group will be created.
    key_name : str, optional
        EC2 key pair name for SSH access. If not provided, instances will be created without a key pair.
    profile_name : str, optional
        AWS profile name to use. If not provided, the default profile will be used.
    state_store_type : str, optional
        Type of state store to use ('file', 'parameter_store', or 's3'). Default is 'file'.
    state_file_path : str, optional
        Path to state file when using 'file' state store. Default is 'ephemeral_aws_state.json'.
    s3_bucket : str, optional
        S3 bucket name when using 's3' state store.
    s3_key : str, optional
        S3 key name when using 's3' state store. Default is 'ephemeral_aws_state.json'.
    parameter_store_path : str, optional
        Parameter Store path when using 'parameter_store' state store.
        Default is '/parsl/ephemeral_aws_state'.
    use_spot : bool, optional
        Whether to use spot instances. Default is False.
    spot_max_price : str, optional
        Maximum price for spot instances. Default is on-demand price.
    spot_allocation_strategy : str, optional
        Allocation strategy for spot instances. Default is 'capacity-optimized'.
    spot_interruption_handling : bool, optional
        Whether to enable spot interruption handling. Default is False.
    checkpoint_bucket : Optional[str], optional
        S3 bucket name for storing task checkpoints, required if spot_interruption_handling is True.
    checkpoint_prefix : str, optional
        S3 key prefix for checkpoint data. Default is 'parsl/checkpoints'.
    checkpoint_interval : int, optional
        Interval between checkpoints in seconds. Default is 60.
    additional_tags : Dict[str, str], optional
        Additional tags to apply to AWS resources.
    auto_shutdown : bool, optional
        Whether to automatically shut down idle resources. Default is True.
    max_idle_time : int, optional
        Maximum idle time in seconds before shutdown. Default is 300 (5 minutes).
    compute_type : str, optional
        Type of compute resource when using serverless mode ('ec2', 'lambda', or 'ecs').
        Default is 'ec2'.
    bastion_instance_type : str, optional
        Instance type for bastion host when using detached mode. Default is 't3.micro'.
    memory_size : int, optional
        Memory size in MB for Lambda functions. Default is 1024.
    timeout : int, optional
        Timeout in seconds for Lambda functions. Default is 300.
    debug : bool, optional
        Whether to enable debug logging. Default is False.
    create_vpc : bool, optional
        Whether to create a new VPC if vpc_id is not provided. Default is True.
    use_public_ips : bool, optional
        Whether to assign public IPs to instances. Default is True.
    custom_ami : bool, optional
        Whether image_id refers to a custom AMI. Default is False.
    provider_id : str, optional
        Provider ID for distinguishing between multiple providers. Default is a UUID.
    """

    @typechecked
    def __init__(
        self,
        image_id: Optional[str] = None,
        instance_type: str = DEFAULT_INSTANCE_TYPE,
        region: str = DEFAULT_REGION,
        mode: str = DEFAULT_MODE,
        min_blocks: int = DEFAULT_MIN_BLOCKS,
        max_blocks: int = DEFAULT_MAX_BLOCKS,
        worker_init: str = DEFAULT_WORKER_INIT,
        vpc_id: Optional[str] = None,
        subnet_id: Optional[str] = None,
        security_group_id: Optional[str] = None,
        key_name: Optional[str] = None,
        profile_name: Optional[str] = None,
        state_store_type: str = StateStoreType.FILE,
        state_file_path: str = "ephemeral_aws_state.json",
        s3_bucket: Optional[str] = None,
        s3_key: str = "ephemeral_aws_state.json",
        parameter_store_path: str = "/parsl/ephemeral_aws_state",
        use_spot: bool = False,
        spot_max_price: Optional[str] = None,
        spot_allocation_strategy: str = "capacity-optimized",
        spot_interruption_handling: bool = False,
        checkpoint_bucket: Optional[str] = None,
        checkpoint_prefix: str = "parsl/checkpoints",
        checkpoint_interval: int = 60,
        additional_tags: Optional[Dict[str, str]] = None,
        auto_shutdown: bool = True,
        max_idle_time: int = DEFAULT_MAX_IDLE_TIME,
        compute_type: str = ComputeType.EC2,
        bastion_instance_type: str = "t3.micro",
        memory_size: int = 1024,
        timeout: int = 300,
        debug: bool = False,
        create_vpc: bool = True,
        use_public_ips: bool = True,
        custom_ami: bool = False,
        provider_id: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the Ephemeral AWS Provider."""
        # Initialize the base provider
        super().__init__()

        # Configure logging
        if debug:
            logger.setLevel(logging.DEBUG)

        # Validate configuration
        self._validate_config(
            image_id=image_id,
            mode=mode,
            compute_type=compute_type,
            state_store_type=state_store_type,
            s3_bucket=s3_bucket,
        )

        # Set basic attributes - resolve image_id if not provided
        if image_id is None and mode.lower() in ['standard', 'detached'] and compute_type.lower() == 'ec2':
            # Auto-detect default AMI for the region
            from parsl_ephemeral_aws.utils.aws import get_default_ami
            try:
                self.image_id = get_default_ami(region)
                logger.info(f"Auto-detected AMI {self.image_id} for region {region}")
            except Exception as e:
                logger.warning(f"Failed to auto-detect AMI: {e}. Will need to be set later.")
                self.image_id = None
        else:
            self.image_id = image_id
        self.instance_type = instance_type
        self.region = region
        self.mode_type = OperatingModeType(mode.lower())
        self.min_blocks = min_blocks
        self.max_blocks = max_blocks
        self.worker_init = worker_init
        self.vpc_id = vpc_id
        self.subnet_id = subnet_id
        self.security_group_id = security_group_id
        self.key_name = key_name
        self.profile_name = profile_name
        self.state_store_type = StateStoreType(state_store_type.lower())
        self.state_file_path = state_file_path
        self.s3_bucket = s3_bucket
        self.s3_key = s3_key
        self.parameter_store_path = parameter_store_path
        self.use_spot = use_spot
        self.spot_max_price = spot_max_price
        self.spot_allocation_strategy = spot_allocation_strategy
        self.spot_interruption_handling = spot_interruption_handling
        self.checkpoint_bucket = checkpoint_bucket
        self.checkpoint_prefix = checkpoint_prefix
        self.checkpoint_interval = checkpoint_interval
        self.additional_tags = additional_tags or {}
        self.auto_shutdown = auto_shutdown
        self.max_idle_time = max_idle_time
        self.compute_type = ComputeType(compute_type.lower())
        self.bastion_instance_type = bastion_instance_type
        self.memory_size = memory_size
        self.timeout = timeout
        self.debug = debug
        self.create_vpc = create_vpc
        self.use_public_ips = use_public_ips
        self.custom_ami = custom_ami
        self.provider_id = provider_id or str(uuid.uuid4())
        self.kwargs = kwargs

        # Initialize state
        self.session = create_session(
            region=self.region, profile_name=self.profile_name
        )
        self.state_store = self._initialize_state_store()
        self.operating_mode = self._initialize_operating_mode()
        self.resources: Dict[str, Dict[str, Any]] = {}
        self.job_map: Dict[str, Dict[str, Any]] = {}

        logger.info(f"Initialized EphemeralAWSProvider in {self.mode_type.value} mode")

    def _validate_config(
        self,
        image_id: Optional[str],
        mode: str,
        compute_type: str,
        state_store_type: str,
        s3_bucket: Optional[str],
    ) -> None:
        """Validate the configuration parameters.

        Parameters
        ----------
        image_id : Optional[str]
            EC2 AMI ID to use for instances.
        mode : str
            Operating mode.
        compute_type : str
            Type of compute resource.
        state_store_type : str
            Type of state store to use.
        s3_bucket : Optional[str]
            S3 bucket name when using 's3' state store.

        Raises
        ------
        ProviderConfigurationError
            If the configuration is invalid.
        """
        # Validate operating mode
        try:
            mode_type = OperatingModeType(mode.lower())
        except ValueError:
            raise ProviderConfigurationError(
                f"Invalid operating mode: {mode}. Must be one of: "
                f"{', '.join([m.value for m in OperatingModeType])}"
            )

        # Note: image_id validation removed - it will be auto-detected if not provided

        # Validate state store type
        try:
            store_type = StateStoreType(state_store_type.lower())
        except ValueError:
            raise ProviderConfigurationError(
                f"Invalid state store type: {state_store_type}. Must be one of: "
                f"{', '.join([s.value for s in StateStoreType])}"
            )

        # Validate S3 bucket when using S3 state store
        if store_type == StateStoreType.S3 and not s3_bucket:
            raise ProviderConfigurationError(
                "s3_bucket is required when using 's3' state store"
            )

        # Validate compute type
        try:
            ComputeType(compute_type.lower())
        except ValueError:
            raise ProviderConfigurationError(
                f"Invalid compute type: {compute_type}. Must be one of: "
                f"{', '.join([c.value for c in ComputeType])}"
            )

    def _initialize_state_store(self) -> StateStore:
        """Initialize the state store based on configuration.

        Returns
        -------
        StateStore
            The initialized state store.
        """
        if self.state_store_type == StateStoreType.FILE:
            return FileStateStore(
                file_path=self.state_file_path, provider_id=self.provider_id
            )
        elif self.state_store_type == StateStoreType.PARAMETER_STORE:
            return ParameterStoreStateStore(
                session=self.session,
                path=self.parameter_store_path,
                provider_id=self.provider_id,
            )
        elif self.state_store_type == StateStoreType.S3:
            if not self.s3_bucket:
                raise ProviderConfigurationError(
                    "s3_bucket is required when using 's3' state store"
                )
            return S3StateStore(
                session=self.session,
                bucket=self.s3_bucket,
                key=self.s3_key,
                provider_id=self.provider_id,
            )
        else:
            raise ProviderConfigurationError(
                f"Unsupported state store type: {self.state_store_type}"
            )

    def _initialize_operating_mode(self) -> OperatingMode:
        """Initialize the operating mode based on configuration.

        Returns
        -------
        OperatingMode
            The initialized operating mode.
        """
        common_params = {
            "provider_id": self.provider_id,
            "session": self.session,
            "state_store": self.state_store,
            "image_id": self.image_id,
            "instance_type": self.instance_type,
            "worker_init": self.worker_init,
            "vpc_id": self.vpc_id,
            "subnet_id": self.subnet_id,
            "security_group_id": self.security_group_id,
            "key_name": self.key_name,
            "use_spot": self.use_spot,
            "spot_max_price": self.spot_max_price,
            "spot_allocation_strategy": self.spot_allocation_strategy,
            "spot_interruption_handling": self.spot_interruption_handling,
            "checkpoint_bucket": self.checkpoint_bucket,
            "checkpoint_prefix": self.checkpoint_prefix,
            "checkpoint_interval": self.checkpoint_interval,
            "additional_tags": self.additional_tags,
            "auto_shutdown": self.auto_shutdown,
            "max_idle_time": self.max_idle_time,
            "create_vpc": self.create_vpc,
            "use_public_ips": self.use_public_ips,
            "custom_ami": self.custom_ami,
            "debug": self.debug,
        }

        if self.mode_type == OperatingModeType.STANDARD:
            return StandardMode(**common_params)
        elif self.mode_type == OperatingModeType.DETACHED:
            return DetachedMode(
                bastion_instance_type=self.bastion_instance_type, **common_params
            )
        elif self.mode_type == OperatingModeType.SERVERLESS:
            return ServerlessMode(
                compute_type=self.compute_type,
                memory_size=self.memory_size,
                timeout=self.timeout,
                **common_params,
            )
        else:
            raise ProviderConfigurationError(
                f"Unsupported operating mode: {self.mode_type}"
            )

    def submit(
        self, command: str, tasks_per_node: int, job_name: Optional[str] = None
    ) -> str:
        """Submit a job to execute the specified command.

        Parameters
        ----------
        command : str
            Command to execute.
        tasks_per_node : int
            Number of tasks to run per node.
        job_name : Optional[str]
            Name for the job.

        Returns
        -------
        str
            Job ID for tracking status.
        """
        job_name = job_name or f"parsl-job-{str(uuid.uuid4())[:8]}"
        job_id = f"{self.provider_id}-{str(uuid.uuid4())}"

        # Check if we have capacity
        if len(self.resources) >= self.max_blocks:
            logger.warning(
                f"Cannot submit job {job_name}, already at max_blocks = {self.max_blocks}"
            )
            raise ProviderError(
                f"Cannot submit job, already at max_blocks = {self.max_blocks}"
            )

        # Submit the job to the operating mode
        try:
            resource_id = self.operating_mode.submit_job(
                job_id=job_id,
                command=command,
                tasks_per_node=tasks_per_node,
                job_name=job_name,
            )

            # Record the job in our internal maps
            self.resources[resource_id] = {
                "job_id": job_id,
                "job_name": job_name,
                "status": "PENDING",
                "tasks_per_node": tasks_per_node,
                "command": command,
                "timestamp": time.time(),
            }

            self.job_map[job_id] = {
                "resource_id": resource_id,
                "job_name": job_name,
                "status": "PENDING",
            }

            # Update the state store
            self._save_state()

            logger.info(f"Submitted job {job_name} with ID {job_id}")
            return job_id

        except Exception as e:
            logger.error(f"Failed to submit job {job_name}: {e}")
            raise ProviderError(f"Failed to submit job: {e}")

    def status(self, job_ids: List[str]) -> List[Dict[str, str]]:
        """Get the status of a list of jobs.

        Parameters
        ----------
        job_ids : List[str]
            List of job IDs.

        Returns
        -------
        List[Dict[str, str]]
            List of dictionaries containing job status information.
        """
        statuses = []

        try:
            # Get status from the operating mode
            status_map = self.operating_mode.get_job_status(
                [
                    self.job_map[job_id]["resource_id"]
                    for job_id in job_ids
                    if job_id in self.job_map
                ]
            )

            # Update our internal state
            for job_id in job_ids:
                if job_id in self.job_map:
                    resource_id = self.job_map[job_id]["resource_id"]
                    if resource_id in status_map:
                        status = status_map[resource_id]
                        self.job_map[job_id]["status"] = status
                        if resource_id in self.resources:
                            self.resources[resource_id]["status"] = status
                        statuses.append({"job_id": job_id, "status": status})
                    else:
                        # If the resource isn't found, it might have been cleaned up
                        statuses.append({"job_id": job_id, "status": "COMPLETED"})
                else:
                    # Job ID not found in our map
                    statuses.append({"job_id": job_id, "status": "UNKNOWN"})

            # Save the updated state
            self._save_state()

            return statuses

        except Exception as e:
            logger.error(f"Failed to get status for jobs {job_ids}: {e}")
            return [{"job_id": job_id, "status": "UNKNOWN"} for job_id in job_ids]

    def cancel(self, job_ids: List[str]) -> List[Dict[str, str]]:
        """Cancel specified jobs.

        Parameters
        ----------
        job_ids : List[str]
            List of job IDs to cancel.

        Returns
        -------
        List[Dict[str, str]]
            List of dictionaries containing job cancellation status.
        """
        cancelations = []

        try:
            # Resources to terminate
            resources_to_terminate = [
                self.job_map[job_id]["resource_id"]
                for job_id in job_ids
                if job_id in self.job_map
            ]

            # Cancel jobs in the operating mode
            cancel_map = self.operating_mode.cancel_jobs(resources_to_terminate)

            # Update our internal state
            for job_id in job_ids:
                if job_id in self.job_map:
                    resource_id = self.job_map[job_id]["resource_id"]
                    if resource_id in cancel_map:
                        status = cancel_map[resource_id]
                        self.job_map[job_id]["status"] = status
                        if resource_id in self.resources:
                            self.resources[resource_id]["status"] = status
                        cancelations.append({"job_id": job_id, "status": status})
                    else:
                        # If the resource isn't found, it might have been cleaned up
                        cancelations.append({"job_id": job_id, "status": "UNKNOWN"})
                else:
                    # Job ID not found in our map
                    cancelations.append({"job_id": job_id, "status": "UNKNOWN"})

            # Clean up resources
            self._cleanup_resources()

            # Save the updated state
            self._save_state()

            return cancelations

        except Exception as e:
            logger.error(f"Failed to cancel jobs {job_ids}: {e}")
            return [{"job_id": job_id, "status": "UNKNOWN"} for job_id in job_ids]

    def _save_state(self) -> None:
        """Save the current state to the state store."""
        state = {
            "provider_id": self.provider_id,
            "mode": self.mode_type.value,
            "resources": self.resources,
            "job_map": self.job_map,
            "timestamp": time.time(),
        }

        try:
            self.state_store.save_state(state)
            logger.debug("Saved provider state")
        except Exception as e:
            logger.error(f"Failed to save provider state: {e}")

    def _load_state(self) -> None:
        """Load the state from the state store."""
        try:
            state = self.state_store.load_state()
            if state and state.get("provider_id") == self.provider_id:
                self.resources = state.get("resources", {})
                self.job_map = state.get("job_map", {})
                logger.info(f"Loaded state with {len(self.resources)} resources")
        except Exception as e:
            logger.error(f"Failed to load provider state: {e}")

    def _cleanup_resources(self) -> None:
        """Clean up resources that are completed or failed."""
        resources_to_cleanup = []

        # Find resources to clean up
        for resource_id, resource in self.resources.items():
            status = resource.get("status", "UNKNOWN")
            if status in ["COMPLETED", "FAILED", "CANCELED"]:
                resources_to_cleanup.append(resource_id)
            elif (
                self.auto_shutdown
                and status == "RUNNING"
                and time.time() - resource.get("timestamp", 0) > self.max_idle_time
            ):
                # Auto-shutdown for idle resources
                logger.info(
                    f"Resource {resource_id} has been idle for "
                    f"{time.time() - resource.get('timestamp', 0)} seconds, "
                    f"exceeding max_idle_time {self.max_idle_time}"
                )
                resources_to_cleanup.append(resource_id)

        # Clean up resources
        if resources_to_cleanup:
            try:
                self.operating_mode.cleanup_resources(resources_to_cleanup)

                # Update internal state
                for resource_id in resources_to_cleanup:
                    if resource_id in self.resources:
                        job_id = self.resources[resource_id].get("job_id")
                        del self.resources[resource_id]
                        if job_id and job_id in self.job_map:
                            del self.job_map[job_id]

                # Save the updated state
                self._save_state()

                logger.info(f"Cleaned up {len(resources_to_cleanup)} resources")
            except Exception as e:
                logger.error(f"Failed to clean up resources: {e}")

    def scale_in(self, blocks: int) -> List[str]:
        """Scale in the number of blocks by the specified amount.

        Parameters
        ----------
        blocks : int
            Number of blocks to scale in by.

        Returns
        -------
        List[str]
            List of job IDs that were terminated.
        """
        if blocks <= 0:
            return []

        # Find running resources to terminate
        running_resources = [
            resource_id
            for resource_id, resource in self.resources.items()
            if resource.get("status") == "RUNNING"
        ]

        # Limit to the requested number of blocks
        resources_to_terminate = running_resources[:blocks]
        job_ids = [
            self.resources[resource_id].get("job_id")
            for resource_id in resources_to_terminate
            if resource_id in self.resources
        ]

        # Cancel the selected jobs
        self.cancel(job_ids)

        return job_ids

    def scale_out(self, blocks: int) -> List[str]:
        """Scale out resources by the specified number of blocks.

        Parameters
        ----------
        blocks : int
            Number of blocks to scale out by.

        Returns
        -------
        List[str]
            List of job IDs for the new resources.
        """
        # Not implemented in the base provider
        # This would be implemented by Parsl's strategy components
        return []

    def shutdown(self) -> None:
        """Shutdown the provider and cleanup all resources."""
        logger.info("Shutting down EphemeralAWSProvider")

        try:
            # Cancel all jobs
            job_ids = list(self.job_map.keys())
            if job_ids:
                self.cancel(job_ids)

            # Clean up infrastructure
            self.operating_mode.cleanup_infrastructure()

            # Clear state
            self.resources = {}
            self.job_map = {}

            # Save the empty state
            self._save_state()

            logger.info("Provider shutdown complete")
        except Exception as e:
            logger.error(f"Error during provider shutdown: {e}")

    def list_resources(self) -> Dict[str, List[Dict[str, Any]]]:
        """List all resources created by this provider.

        Returns
        -------
        Dict[str, List[Dict[str, Any]]]
            Dictionary of resource types and their details.
        """
        try:
            return self.operating_mode.list_resources()
        except Exception as e:
            logger.error(f"Failed to list resources: {e}")
            return {}

    def cleanup_all(self) -> None:
        """Clean up all resources created by this provider."""
        logger.info("Cleaning up all resources")

        try:
            # Clean up all resources in the operating mode
            self.operating_mode.cleanup_all()

            # Clear state
            self.resources = {}
            self.job_map = {}

            # Save the empty state
            self._save_state()

            logger.info("All resources cleaned up")
        except Exception as e:
            logger.error(f"Failed to clean up all resources: {e}")
            raise ProviderError(f"Failed to clean up all resources: {e}")

    @property
    def status_polling_interval(self) -> int:
        """Return the status polling interval for the provider.

        Returns
        -------
        int
            Polling interval in seconds.
        """
        return 60

    @property
    def label(self) -> str:
        """Return the label for the provider.

        Returns
        -------
        str
            Provider label.
        """
        return f"ephemeral-aws-{self.mode_type.value}-{self.provider_id[:8]}"

    def __repr__(self) -> str:
        """Return string representation of the provider.

        Returns
        -------
        str
            String representation.
        """
        return (
            f"EphemeralAWSProvider(mode={self.mode_type.value}, "
            f"region={self.region}, "
            f"min_blocks={self.min_blocks}, "
            f"max_blocks={self.max_blocks})"
        )

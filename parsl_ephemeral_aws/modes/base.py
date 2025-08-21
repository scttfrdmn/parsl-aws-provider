"""
Base operating mode interface for the EphemeralAWSProvider.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import abc
import logging
from typing import Any, Dict, List, Optional

import boto3

from parsl_ephemeral_aws.exceptions import OperatingModeError
from parsl_ephemeral_aws.state.base import StateStore


logger = logging.getLogger(__name__)


class OperatingMode(abc.ABC):
    """Abstract base class for provider operating modes.

    An operating mode defines how the provider interacts with AWS resources
    to execute jobs. Different modes have different trade-offs in terms of
    cost, performance, and capabilities.

    Attributes
    ----------
    provider_id : str
        Unique identifier for the provider instance
    session : boto3.Session
        AWS session for API calls
    state_store : StateStore
        Store for persisting state
    image_id : Optional[str]
        EC2 AMI ID to use for instances
    instance_type : str
        EC2 instance type for compute resources
    worker_init : str
        Script to execute during worker initialization
    vpc_id : Optional[str]
        Existing VPC ID to use
    subnet_id : Optional[str]
        Existing subnet ID to use
    security_group_id : Optional[str]
        Existing security group ID to use
    key_name : Optional[str]
        EC2 key pair name for SSH access
    use_spot : bool
        Whether to use spot instances
    spot_max_price : Optional[str]
        Maximum price for spot instances
    spot_allocation_strategy : str
        Allocation strategy for spot instances
    spot_interruption_handling : bool
        Whether to enable spot interruption handling
    checkpoint_bucket : Optional[str]
        S3 bucket name for storing task checkpoints
    checkpoint_prefix : str
        S3 key prefix for checkpoint data
    checkpoint_interval : int
        Interval between checkpoints in seconds
    additional_tags : Dict[str, str]
        Tags to apply to created resources
    auto_shutdown : bool
        Whether to automatically shut down idle resources
    max_idle_time : int
        Maximum idle time in seconds before shutdown
    create_vpc : bool
        Whether to create a new VPC if vpc_id is not provided
    use_public_ips : bool
        Whether to assign public IPs to instances
    custom_ami : bool
        Whether image_id refers to a custom AMI
    debug : bool
        Whether to enable debug logging
    """

    def __init__(
        self,
        provider_id: str,
        session: boto3.Session,
        state_store: StateStore,
        image_id: Optional[str] = None,
        instance_type: str = "t3.micro",
        worker_init: str = "",
        vpc_id: Optional[str] = None,
        subnet_id: Optional[str] = None,
        security_group_id: Optional[str] = None,
        key_name: Optional[str] = None,
        use_spot: bool = False,
        spot_max_price: Optional[str] = None,
        spot_allocation_strategy: str = "capacity-optimized",
        spot_interruption_handling: bool = False,
        checkpoint_bucket: Optional[str] = None,
        checkpoint_prefix: str = "parsl/checkpoints",
        checkpoint_interval: int = 60,
        additional_tags: Optional[Dict[str, str]] = None,
        auto_shutdown: bool = True,
        max_idle_time: int = 300,
        create_vpc: bool = True,
        use_public_ips: bool = True,
        custom_ami: bool = False,
        debug: bool = False,
        region: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the operating mode.

        Parameters
        ----------
        provider_id : str
            Unique identifier for the provider instance
        session : boto3.Session
            AWS session for API calls
        state_store : StateStore
            Store for persisting state
        image_id : Optional[str], optional
            EC2 AMI ID to use for instances, by default None
        instance_type : str, optional
            EC2 instance type for compute resources, by default "t3.micro"
        worker_init : str, optional
            Script to execute during worker initialization, by default ""
        vpc_id : Optional[str], optional
            Existing VPC ID to use, by default None
        subnet_id : Optional[str], optional
            Existing subnet ID to use, by default None
        security_group_id : Optional[str], optional
            Existing security group ID to use, by default None
        key_name : Optional[str], optional
            EC2 key pair name for SSH access, by default None
        use_spot : bool, optional
            Whether to use spot instances, by default False
        spot_max_price : Optional[str], optional
            Maximum price for spot instances, by default None
        spot_allocation_strategy : str, optional
            Allocation strategy for spot instances, by default "capacity-optimized"
        spot_interruption_handling : bool, optional
            Whether to enable spot interruption handling, by default False
        checkpoint_bucket : Optional[str], optional
            S3 bucket name for storing task checkpoints, by default None
        checkpoint_prefix : str, optional
            S3 key prefix for checkpoint data, by default "parsl/checkpoints"
        checkpoint_interval : int, optional
            Interval between checkpoints in seconds, by default 60
        additional_tags : Optional[Dict[str, str]], optional
            Tags to apply to created resources, by default None
        auto_shutdown : bool, optional
            Whether to automatically shut down idle resources, by default True
        max_idle_time : int, optional
            Maximum idle time in seconds before shutdown, by default 300
        create_vpc : bool, optional
            Whether to create a new VPC if vpc_id is not provided, by default True
        use_public_ips : bool, optional
            Whether to assign public IPs to instances, by default True
        custom_ami : bool, optional
            Whether image_id refers to a custom AMI, by default False
        debug : bool, optional
            Whether to enable debug logging, by default False
        """
        self.provider_id = provider_id
        self.session = session
        self.state_store = state_store
        self.image_id = image_id
        self.instance_type = instance_type
        self.worker_init = worker_init
        self.vpc_id = vpc_id
        self.subnet_id = subnet_id
        self.security_group_id = security_group_id
        self.key_name = key_name
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
        self.create_vpc = create_vpc
        self.use_public_ips = use_public_ips
        self.custom_ami = custom_ami
        self.debug = debug
        self.region = region or getattr(session, "region_name", "us-east-1")
        self.kwargs = kwargs

        # Set up logging
        if debug:
            logger.setLevel(logging.DEBUG)

        # Initialize state
        self.resources: Dict[str, Dict[str, Any]] = {}
        self.initialized = False

        logger.debug(f"Initialized {self.__class__.__name__}")

    @abc.abstractmethod
    def initialize(self) -> None:
        """Initialize mode-specific resources.

        This method should create any resources needed for the mode to operate,
        such as VPC, subnets, security groups, etc.

        Raises
        ------
        ResourceCreationError
            If resource creation fails
        """
        pass

    @abc.abstractmethod
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
        pass

    @abc.abstractmethod
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
        pass

    @abc.abstractmethod
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
        pass

    @abc.abstractmethod
    def cleanup_resources(self, resource_ids: List[str]) -> None:
        """Clean up resources.

        Parameters
        ----------
        resource_ids : List[str]
            List of resource IDs to clean up
        """
        pass

    @abc.abstractmethod
    def cleanup_infrastructure(self) -> None:
        """Clean up infrastructure created by this mode.

        This should clean up any VPC, subnets, security groups, etc. created
        by the mode.
        """
        pass

    @abc.abstractmethod
    def list_resources(self) -> Dict[str, List[Dict[str, Any]]]:
        """List all resources created by this mode.

        Returns
        -------
        Dict[str, List[Dict[str, Any]]]
            Dictionary of resource types and their details
        """
        pass

    @abc.abstractmethod
    def cleanup_all(self) -> None:
        """Clean up all resources created by this mode."""
        pass

    def ensure_initialized(self) -> None:
        """Ensure the mode is initialized.

        Raises
        ------
        OperatingModeError
            If initialization fails
        """
        if not self.initialized:
            try:
                self.initialize()
                self.initialized = True
            except Exception as e:
                logger.error(f"Initialization failed: {e}")
                raise OperatingModeError(f"Initialization failed: {e}") from e

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
        }

        try:
            self.state_store.save_state(state)
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
            state = self.state_store.load_state()
            if state and state.get("provider_id") == self.provider_id:
                self.resources = state.get("resources", {})
                self.vpc_id = state.get("vpc_id", self.vpc_id)
                self.subnet_id = state.get("subnet_id", self.subnet_id)
                self.security_group_id = state.get(
                    "security_group_id", self.security_group_id
                )
                self.initialized = state.get("initialized", False)
                logger.debug(f"Loaded state with {len(self.resources)} resources")
                return True
        except Exception as e:
            logger.error(f"Failed to load state: {e}")

        return False

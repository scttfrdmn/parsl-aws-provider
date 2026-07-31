"""
Base operating mode interface for the EphemeralAWSProvider.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import abc
import logging
from typing import Any, Dict, List, Optional

import boto3

from botocore.exceptions import ClientError

from parsl_ephemeral_aws.constants import DEFAULT_SPOT_ALLOCATION_STRATEGY
from parsl_ephemeral_aws.exceptions import OperatingModeError, ResourceNotFoundError
from parsl_ephemeral_aws.state.base import STATE_KEY_MODE, StateStore


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
        spot_allocation_strategy: str = DEFAULT_SPOT_ALLOCATION_STRATEGY,
        spot_interruption_handling: bool = False,
        checkpoint_bucket: Optional[str] = None,
        checkpoint_prefix: str = "parsl/checkpoints",
        checkpoint_interval: int = 60,
        additional_tags: Optional[Dict[str, str]] = None,
        auto_shutdown: bool = True,
        max_idle_time: int = 300,
        use_public_ips: bool = True,
        custom_ami: bool = False,
        debug: bool = False,
        region: Optional[str] = None,
        require_network_resources: bool = True,
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
            Allocation strategy for spot instances, in kebab-case, by default
            "price-capacity-optimized"
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
        use_public_ips : bool, optional
            Whether to assign public IPs to instances, by default True
        custom_ami : bool, optional
            Whether image_id refers to a custom AMI, by default False
        debug : bool, optional
            Whether to enable debug logging, by default False
        require_network_resources : bool, optional
            Whether vpc_id, subnet_id, and security_group_id are mandatory, by
            default True. Subclasses whose compute backend supplies its own
            networking (e.g. Lambda-only serverless mode) pass False.
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

        self.require_network_resources = require_network_resources

        if require_network_resources and (
            not self.vpc_id or not self.subnet_id or not self.security_group_id
        ):
            raise ValueError(
                "vpc_id, subnet_id, and security_group_id are required. "
                "Pre-provision network resources and pass their IDs."
            )

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
        """Save the current state under the mode's own state key.

        The provider writes ``STATE_KEY_PROVIDER`` separately; see
        ``EphemeralAWSProvider._save_state``.
        """
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
            self.state_store.save_state(STATE_KEY_MODE, state)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def delete_state(self) -> None:
        """Delete the state stored under the mode's own state key.

        Called on provider shutdown. The provider deletes its own key
        separately; leaving either behind strands a document that describes
        resources which no longer exist.
        """
        try:
            self.state_store.delete_state(STATE_KEY_MODE)
        except Exception as e:
            logger.error(f"Failed to delete state: {e}")

    #: EC2 error codes and describe call for each network ID, in the order they
    #: are checked. VPC first, so a wholly deleted VPC is reported as such
    #: rather than as three unrelated missing children.
    #:
    #: Both ``NotFound`` and ``Malformed`` are treated as "unusable ID". EC2
    #: returns the latter for a syntactically invalid ID — verified against real
    #: AWS, where ``sg-00000000000000000`` yields ``InvalidGroupId.Malformed``
    #: while the same shape of subnet or VPC ID yields ``NotFound``. To the
    #: caller both mean the same thing: the ID they supplied cannot be used.
    _NETWORK_RESOURCES = (
        (
            "vpc_id",
            "describe_vpcs",
            "VpcIds",
            ("InvalidVpcID.NotFound", "InvalidVpcID.Malformed"),
        ),
        (
            "subnet_id",
            "describe_subnets",
            "SubnetIds",
            ("InvalidSubnetID.NotFound", "InvalidSubnetId.Malformed"),
        ),
        (
            "security_group_id",
            "describe_security_groups",
            "GroupIds",
            ("InvalidGroup.NotFound", "InvalidGroupId.Malformed"),
        ),
    )

    def _verify_resources(self) -> None:
        """Confirm the caller-supplied network resources still exist.

        Raises
        ------
        ResourceNotFoundError
            If a configured VPC, subnet, or security group is missing or its ID
            is malformed.

        Notes
        -----
        Every mode used to null the attribute out here instead of raising, so
        that ``initialize()`` would create a replacement. Since #69 nothing
        creates one: the ``None`` propagates to ``run_instances`` and surfaces as
        an opaque ``InvalidParameterValue`` far from the missing resource, or —
        in serverless mode — re-entered a guard that read a ``create_vpc``
        attribute which no longer exists. Naming the resource here is the whole
        point of verifying it.
        """
        ec2 = self.session.client("ec2")

        for attribute, describe, id_param, bad_id_codes in self._NETWORK_RESOURCES:
            resource_id = getattr(self, attribute, None)
            if not resource_id:
                continue

            try:
                getattr(ec2, describe)(**{id_param: [resource_id]})
                logger.debug(f"Verified {attribute} {resource_id} exists")
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") in bad_id_codes:
                    raise ResourceNotFoundError(
                        f"{attribute} {resource_id} is not usable. It is "
                        "malformed, was deleted, or belongs to a different "
                        "region or account; pre-provision the network resources "
                        "and pass their IDs."
                    ) from e
                raise

    def _restore_network_ids(self, state: Dict[str, Any]) -> None:
        """Restore network IDs from *state*, never overwriting one with None.

        A state document from before these IDs became required can carry
        ``None`` for any of them. The constructor value was validated; a null
        from an old file has not been, and would surface later as an opaque
        boto3 ``InvalidParameterValue`` at launch.
        """
        for attribute in ("vpc_id", "subnet_id", "security_group_id"):
            saved = state.get(attribute)
            if saved:
                setattr(self, attribute, saved)
            elif getattr(self, attribute, None):
                logger.debug(
                    f"Keeping configured {attribute} — saved state has no value"
                )

    def load_state(self) -> bool:
        """Load state from the mode's own state key.

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
                logger.debug(f"Loaded state with {len(self.resources)} resources")
                return True
        except Exception as e:
            logger.error(f"Failed to load state: {e}")

        return False

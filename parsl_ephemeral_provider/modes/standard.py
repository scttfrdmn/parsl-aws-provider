"""
Standard operating mode for the EphemeralProvider.

The standard mode uses EC2 instances for computation with direct communication
between the client and worker nodes.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import logging
import time
import uuid
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

from parsl_ephemeral_provider.constants import (
    DEFAULT_RESOURCE_CREATION_TIMEOUT,
    DEFAULT_SPOT_ALLOCATION_STRATEGY,
    EC2_STATUS_MAPPING,
    IMDSV2_METADATA_OPTIONS,
    LAUNCH_TEMPLATE_NAME_PREFIX,
    RESOURCE_TYPE_EC2,
    RESOURCE_TYPE_SPOT_FLEET,
    STATUS_CANCELED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_UNKNOWN,
    STATUS_WARM,
)
from parsl_ephemeral_provider.error_handling import RetryConfig, poll_until
from parsl_ephemeral_provider.exceptions import (
    OperatingModeError,
    ResourceCreationError,
    SpotFleetError,
)
from parsl_ephemeral_provider.modes.base import OperatingMode
from parsl_ephemeral_provider.state.base import STATE_KEY_MODE
from parsl_ephemeral_provider.compute.spot_fleet import SpotFleetManager
from parsl_ephemeral_provider.compute.spot_interruption import SpotInterruptionMonitor
from parsl_ephemeral_provider.utils.aws import (
    architecture_for_instance_type,
    build_launch_template_data,
    create_launch_template,
    delete_launch_template,
    delete_ssm_instance_profile,
    get_default_ami,
    get_or_create_ssm_instance_profile,
    wait_for_resource,
)


logger = logging.getLogger(__name__)

# SendCommand is asynchronous: get_command_invocation returns InvocationDoesNotExist
# for a moment after it returns. Not a retry interval -- see _wait_for_worker_ready.
SSM_INVOCATION_SETTLE_SECONDS = 5


def _swallow_client_errors(exc: Exception) -> None:
    """Log an AWS error a poll should tolerate, and re-raise anything else.

    ``poll_until`` treats any exception as "not satisfied yet", which is right
    for the ``ClientError`` these polls expect -- an instance absent from SSM,
    an invocation that has not landed. It is wrong for everything else: a
    missing credential or a typo in a parameter name would otherwise be retried
    silently until the timeout expired, turning an immediate failure into a
    five-minute one. This narrows the tolerance back to what the hand-rolled
    loops had before #91.
    """
    if not isinstance(exc, ClientError):
        raise exc
    logger.debug(f"SSM poll tolerated: {exc}")


class StandardMode(OperatingMode):
    """Standard operating mode implementation.

    In standard mode, EC2 instances are created for computation with direct
    communication between the client and worker nodes.

    This mode supports regular EC2 instances, spot instances, and spot fleet
    requests for more reliable and cost-effective computation.
    """

    # Delay schedule for the SSM readiness polls (#91). These replaced flat
    # 10s/15s intervals: starting tighter finds an instance that registers
    # quickly sooner, the cap keeps a slow boot from being polled hundreds of
    # times, and the jitter stops N providers launched together from hitting
    # SSM in lockstep -- which was the concrete cost of hand-rolling these.
    _ssm_poll_config = RetryConfig(base_delay=5.0, max_delay=30.0)

    # The fleet-block poll is coarser: EC2 Fleet capacity is fulfilled on the
    # order of a minute, so polling it every few seconds only burns API quota.
    _fleet_poll_config = RetryConfig(base_delay=10.0, max_delay=60.0)

    def __init__(
        self,
        provider_id: str,
        session: boto3.Session,
        state_store: Any,
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
        additional_tags: Optional[Dict[str, str]] = None,
        auto_shutdown: bool = True,
        max_idle_time: int = 300,
        use_public_ips: bool = True,
        custom_ami: bool = False,
        debug: bool = False,
        use_spot_fleet: bool = False,
        instance_types: Optional[List[str]] = None,
        nodes_per_block: int = 1,
        spot_max_price_percentage: Optional[int] = None,
        warm_pool_size: int = 0,
        warm_pool_ttl: int = 600,
        iam_instance_profile_arn: Optional[str] = None,
        auto_create_instance_profile: bool = False,
        bake_ami: bool = False,
        baked_ami_id: Optional[str] = None,
        one_shot: bool = False,
        **kwargs: Any,
    ) -> None:
        """Initialize the standard mode.

        Parameters
        ----------
        provider_id : str
            Unique identifier for the provider instance
        session : boto3.Session
            AWS session for API calls
        state_store : Any
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
        additional_tags : Optional[Dict[str, str]], optional
            Tags to apply to created resources, by default None
        auto_shutdown : bool, optional
            Whether a worker terminates itself once its command finishes, by
            default True. Appends ``shutdown -h now`` to the worker's UserData.
        max_idle_time : int, optional
            Deprecated and ignored, by default 300. Use Parsl's own
            ``max_idletime`` to reclaim idle blocks (#194).
        use_public_ips : bool, optional
            Whether to assign public IPs to instances, by default True
        custom_ami : bool, optional
            Whether image_id refers to a custom AMI, by default False
        debug : bool, optional
            Whether to enable debug logging, by default False
        use_spot_fleet : bool, optional
            Whether to use Spot Fleet for spot instances, by default False
        instance_types : Optional[List[str]], optional
            List of instance types to use with Spot Fleet, by default None
        nodes_per_block : int, optional
            Number of nodes per block, by default 1
        spot_max_price_percentage : Optional[int], optional
            Maximum spot price as a percentage of on-demand price, by default None
        warm_pool_size : int, optional
            Number of idle instances to keep for reuse, by default 0 (disabled)
        warm_pool_ttl : int, optional
            Seconds a warm instance is kept before termination, by default 600
        iam_instance_profile_arn : Optional[str], optional
            Instance profile ARN granting SSM access, by default None
        auto_create_instance_profile : bool, optional
            Whether to create an instance profile with the
            ``AmazonSSMManagedInstanceCore`` policy when
            ``iam_instance_profile_arn`` is not supplied, by default False.
            SSM ``SendCommand`` dispatch cannot work without one of the two.
        bake_ami : bool, optional
            Whether to pre-install ``worker_init`` into a custom AMI, by default False
        baked_ami_id : Optional[str], optional
            Pre-baked AMI to use instead of baking one, by default None
        one_shot : bool, optional
            Whether to dispatch a single command per instance and terminate,
            by default False
        """
        # Call parent __init__ with standard params
        super().__init__(
            provider_id=provider_id,
            session=session,
            state_store=state_store,
            image_id=image_id,
            instance_type=instance_type,
            worker_init=worker_init,
            vpc_id=vpc_id,
            subnet_id=subnet_id,
            security_group_id=security_group_id,
            key_name=key_name,
            use_spot=use_spot,
            spot_max_price=spot_max_price,
            spot_allocation_strategy=spot_allocation_strategy,
            additional_tags=additional_tags,
            auto_shutdown=auto_shutdown,
            max_idle_time=max_idle_time,
            use_public_ips=use_public_ips,
            custom_ami=custom_ami,
            debug=debug,
            **kwargs,
        )

        # Standard mode specific attributes
        self.use_spot_fleet = use_spot_fleet
        self.instance_types = instance_types or []
        self.nodes_per_block = nodes_per_block
        self.spot_max_price_percentage = spot_max_price_percentage

        # Warm pool attributes
        self.warm_pool_size = warm_pool_size
        self.warm_pool_ttl = warm_pool_ttl
        self.iam_instance_profile_arn = iam_instance_profile_arn
        self.auto_create_instance_profile = auto_create_instance_profile
        # True when this mode auto-created the IAM role and instance profile, and
        # so is the one responsible for deleting them (#132). A caller-supplied
        # iam_instance_profile_arn is never ours to delete.
        self._owns_instance_profile: bool = False
        # List of instance IDs currently in the warm pool (ready for reuse, FIFO)
        self._warm_instances: List[str] = []

        # AMI baking attributes
        self.bake_ami = bake_ami
        self.baked_ami_id = baked_ami_id  # user-supplied pre-baked AMI
        self._baked_ami_id: Optional[str] = None  # resolved AMI ID (baked or supplied)
        self._owns_baked_ami: bool = False  # True if this provider created the AMI

        # One-shot mode: each instance runs exactly one command then terminates
        self.one_shot = one_shot

        # Launch template (#85). Built in initialize() so every launch path --
        # on-demand, spot, and fleet -- shares one definition carrying IMDSv2,
        # the shutdown behaviour, and the resolved instance profile.
        self._launch_template_id: Optional[str] = None
        self._launch_template_version: Optional[str] = None

        # Initialize SpotFleetManager if using spot fleet
        self.spot_fleet_manager = None
        self.spot_interruption_monitor = None

        # use_spot_fleet alone is enough, and use_spot alone is not: every
        # consumer of this manager gates on use_spot_fleet, so requiring
        # `use_spot and use_spot_fleet` left the manager None for a fleet caller
        # and silently degraded the launch to a single on-demand instance (#137).
        if self.use_spot_fleet:
            # Create a simplified provider object for the SpotFleetManager
            # The SpotFleetManager expects a provider object with certain attributes
            provider = type(
                "SimpleProvider",
                (),
                {
                    "workflow_id": self.provider_id,
                    # The mode's own session, so the manager inherits whatever
                    # the caller configured -- a profile, role credentials, or an
                    # endpoint_url. resolve_manager_session() prefers
                    # provider.session and only falls back to building one from
                    # ambient environment credentials when there is none, so
                    # omitting it sent every fleet to the default account for the
                    # region while the rest of the mode used the caller's (#159).
                    # #117 fixed this everywhere the provider is passed as self;
                    # this stand-in was built inline and so was missed.
                    "session": self.session,
                    "region": self.session.region_name,
                    "aws_profile": None,
                    "vpc_id": self.vpc_id,
                    "subnet_id": self.subnet_id,
                    "security_group_id": self.security_group_id,
                    "image_id": self.image_id,
                    "instance_type": self.instance_type,
                    "instance_types": self.instance_types,
                    "key_name": self.key_name,
                    "use_public_ips": self.use_public_ips,
                    "nodes_per_block": self.nodes_per_block,
                    "spot_max_price_percentage": self.spot_max_price_percentage,
                    "worker_init": self.worker_init,
                    "tags": self.additional_tags,
                    # Read by _build_launch_template_config (#85). Without it the
                    # manager's getattr fell through to None and every fleet
                    # instance launched with no instance profile, so SSM never
                    # came online. Still None here on the auto-create path --
                    # _resolve_instance_profile() overwrites it once resolved.
                    "iam_instance_profile_arn": self.iam_instance_profile_arn,
                },
            )

            logger.debug("Initializing SpotFleetManager for StandardMode")
            self.spot_fleet_manager = SpotFleetManager(provider)

        # Initialize spot interruption handling if enabled.
        # NOTE: start_monitoring() is called in initialize(), not here — so the
        # monitoring thread is only started after infrastructure is fully set up.
        # This prevents the monitor thread from leaking if __init__ succeeds but
        # a later call (e.g. initialize()) raises before cleanup can run.
        if (self.use_spot or self.use_spot_fleet) and self.spot_interruption_handling:
            logger.debug("Initializing SpotInterruptionMonitor")
            self.spot_interruption_monitor = SpotInterruptionMonitor(
                self.session, provider_id=self.provider_id
            )

    # ------------------------------------------------------------------
    # AMI baking helpers
    # ------------------------------------------------------------------

    def _bake_ami(self) -> str:
        """Bake worker_init into a custom AMI.

        Launches a builder instance with worker_init as UserData, waits for it
        to stop (via ``shutdown -h now`` at the end of UserData), creates an
        image snapshot, waits for the image to become available, terminates the
        builder, and returns the new AMI ID.

        Returns
        -------
        str
            ID of the newly created AMI.

        Raises
        ------
        ResourceCreationError
            If any step of the baking process fails.
        """
        ec2 = self.session.client("ec2")
        builder_id = self._launch_builder_instance()
        logger.info(f"Waiting for builder instance {builder_id} to stop...")
        try:
            wait_for_resource(
                builder_id,
                "instance_stopped",
                ec2,
                resource_name="AMI builder instance",
                delay=15,
                max_attempts=80,  # up to 20 minutes for slow UserData
            )
        except Exception as e:
            # Terminate the builder before re-raising so it doesn't linger
            try:
                ec2.terminate_instances(InstanceIds=[builder_id])
            except Exception:  # nosec B110
                pass
            raise ResourceCreationError(
                f"Builder instance {builder_id} did not stop: {e}"
            ) from e

        ami_name = f"parsl-baked-{self.provider_id[:8]}-{int(time.time())}"
        try:
            response = ec2.create_image(
                InstanceId=builder_id,
                Name=ami_name,
                NoReboot=True,
                TagSpecifications=[
                    {
                        "ResourceType": "image",
                        "Tags": [
                            {"Key": "ParslBakedAMI", "Value": "true"},
                            {"Key": "ProviderId", "Value": self.provider_id},
                            {
                                "Key": "CreatedBy",
                                "Value": "ParslEphemeralProvider",
                            },
                        ],
                    }
                ],
            )
        except Exception as e:
            try:
                ec2.terminate_instances(InstanceIds=[builder_id])
            except Exception:  # nosec B110
                pass
            raise ResourceCreationError(f"create_image failed: {e}") from e

        ami_id = response["ImageId"]
        logger.info(f"Created AMI {ami_id}, waiting for it to become available...")
        try:
            wait_for_resource(
                ami_id,
                "image_available",
                ec2,
                resource_name="baked AMI",
                delay=15,
                max_attempts=80,
            )
        except Exception as e:
            raise ResourceCreationError(
                f"AMI {ami_id} did not become available: {e}"
            ) from e
        finally:
            # Always terminate the builder, even on failure
            try:
                ec2.terminate_instances(InstanceIds=[builder_id])
                logger.debug(f"Terminated builder instance {builder_id}")
            except Exception as te:
                logger.warning(f"Failed to terminate builder {builder_id}: {te}")

        return ami_id

    def _launch_builder_instance(self) -> str:
        """Launch a builder EC2 instance that runs worker_init then shuts down.

        Returns
        -------
        str
            Instance ID of the launched builder.
        """
        ec2 = self.session.client("ec2")
        user_data = f"#!/bin/bash\n{self.worker_init}\nshutdown -h now\n"
        kwargs: Dict[str, Any] = {
            "ImageId": self.image_id,
            "InstanceType": self.instance_type,
            "MinCount": 1,
            "MaxCount": 1,
            "UserData": user_data,
            # IMDSv2 on the builder as well: whatever worker_init fetches from
            # the metadata service must work under the same rules the baked
            # image will boot under, or a script that works here breaks on
            # every worker launched from the resulting AMI (#85).
            "MetadataOptions": dict(IMDSV2_METADATA_OPTIONS),
            # Explicitly *stop*, not terminate. This is the one launch in the
            # mode that must not inherit the launch template's terminate
            # behaviour: the UserData ends in `shutdown -h now`, and create_image
            # needs the stopped instance to snapshot. The InstanceStopped waiter
            # names "terminated" as an explicit failure acceptor, so inheriting
            # terminate here would fail the bake rather than merely slow it --
            # which is why this path does not use the launch template.
            "InstanceInitiatedShutdownBehavior": "stop",
            "TagSpecifications": [
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {
                            "Key": "Name",
                            "Value": f"parsl-ami-builder-{self.provider_id[:8]}",
                        },
                        {"Key": "ParslAMIBuilder", "Value": "true"},
                        {"Key": "ProviderId", "Value": self.provider_id},
                        {
                            "Key": "CreatedBy",
                            "Value": "ParslEphemeralProvider",
                        },
                    ],
                }
            ],
        }
        if self.subnet_id:
            kwargs["SubnetId"] = self.subnet_id
        if self.security_group_id:
            kwargs["SecurityGroupIds"] = [self.security_group_id]
        if self.key_name:
            kwargs["KeyName"] = self.key_name
        if self.iam_instance_profile_arn:
            kwargs["IamInstanceProfile"] = {"Arn": self.iam_instance_profile_arn}

        response = ec2.run_instances(**kwargs)
        instance_id = response["Instances"][0]["InstanceId"]
        logger.info(f"Launched AMI builder instance {instance_id}")
        return instance_id

    def _deregister_baked_ami(self, ami_id: str) -> None:
        """Deregister a baked AMI and delete its backing EBS snapshots.

        Parameters
        ----------
        ami_id : str
            AMI ID to deregister.
        """
        ec2 = self.session.client("ec2")
        # Collect snapshot IDs before deregistering the image
        snapshot_ids: List[str] = []
        try:
            response = ec2.describe_images(ImageIds=[ami_id])
            images = response.get("Images", [])
            if images:
                for block_device in images[0].get("BlockDeviceMappings", []):
                    ebs = block_device.get("Ebs", {})
                    if "SnapshotId" in ebs:
                        snapshot_ids.append(ebs["SnapshotId"])
        except Exception as e:
            logger.warning(f"Could not describe AMI {ami_id} before deregistering: {e}")

        ec2.deregister_image(ImageId=ami_id)
        logger.debug(f"Deregistered AMI {ami_id}")

        for snapshot_id in snapshot_ids:
            try:
                ec2.delete_snapshot(SnapshotId=snapshot_id)
                logger.debug(f"Deleted snapshot {snapshot_id}")
            except Exception as e:
                logger.warning(f"Failed to delete snapshot {snapshot_id}: {e}")

    # ------------------------------------------------------------------
    # Launch template helpers (#85)
    # ------------------------------------------------------------------

    @property
    def launch_template_name(self) -> str:
        """Name of this mode's launch template, unique per provider."""
        return f"{LAUNCH_TEMPLATE_NAME_PREFIX}-{self.provider_id}"

    def _create_launch_template(self) -> None:
        """Create the launch template every launch path in this mode uses.

        Carries the settings that must not vary per launch: IMDSv2,
        ``InstanceInitiatedShutdownBehavior``, the network interface, the key
        pair, and the IAM instance profile resolved by
        :meth:`_resolve_instance_profile`. ``UserData`` and ``TagSpecifications``
        are deliberately left out -- they are per-job and are passed as
        overrides at launch.

        A failure here is not fatal. The launch paths fall back to raw
        ``RunInstances`` kwargs when no template ID is set, so an account
        without ``ec2:CreateLaunchTemplate`` keeps working, just without IMDSv2
        on the plain-spot path (``RequestSpotInstances`` has no
        ``MetadataOptions`` member at all, so a template is the only way to get
        IMDSv2 there).
        """
        ec2 = self.session.client("ec2")
        tags = [
            {"Key": "Name", "Value": self.launch_template_name},
            {"Key": "CreatedBy", "Value": "ParslEphemeralProvider"},
            {"Key": "ProviderId", "Value": self.provider_id},
        ]
        for key, value in self.additional_tags.items():
            tags.append({"Key": key, "Value": value})

        try:
            template_data = build_launch_template_data(
                image_id=self.image_id,
                instance_type=self.instance_type,
                subnet_id=self.subnet_id,
                security_group_id=self.security_group_id,
                associate_public_ip=self.use_public_ips,
                key_name=self.key_name,
                iam_instance_profile_arn=self.iam_instance_profile_arn,
                shutdown_behavior="terminate",
            )
            (
                self._launch_template_id,
                self._launch_template_version,
            ) = create_launch_template(
                ec2, self.launch_template_name, template_data, tags
            )
            logger.info(
                f"Created launch template {self._launch_template_id} "
                f"version {self._launch_template_version} with IMDSv2 required"
            )
        except Exception as e:
            logger.error(
                f"Failed to create launch template: {e}. Falling back to "
                "per-launch RunInstances parameters; IMDSv2 will not be "
                "enforced on spot instance requests."
            )
            self._launch_template_id = None
            self._launch_template_version = None

    def _delete_launch_template(self) -> None:
        """Delete this mode's launch template if it created one.

        Safe to call while instances launched from the template are still
        terminating -- deleting a template does not affect running instances.
        """
        if not self._launch_template_id:
            return

        try:
            delete_launch_template(self.session.client("ec2"), self._launch_template_id)
            logger.info(f"Deleted launch template {self._launch_template_id}")
        except Exception as e:
            logger.error(
                f"Failed to delete launch template {self._launch_template_id}: {e}"
            )
        finally:
            # Cleared either way: a template that could not be deleted must not
            # be referenced by a later launch, and cleanup is not retried.
            self._launch_template_id = None
            self._launch_template_version = None

    def _delete_instance_profile(self) -> None:
        """Delete the IAM role and instance profile, if this mode created them.

        ``_owns_instance_profile`` is the guard, and it is the whole point: a
        profile the caller supplied through ``iam_instance_profile_arn`` is shared
        infrastructure that other workloads may depend on, so deleting it would
        be a far worse bug than the leak this fixes (#132, same hazard class as
        the serverless security-group deletion in #100).

        Called during cleanup after the instances are confirmed terminated --
        IAM refuses to delete a profile still attached to a running instance.
        """
        if not self._owns_instance_profile:
            return

        try:
            delete_ssm_instance_profile(self.session, self.provider_id)
        except Exception as e:
            logger.error(f"Failed to delete IAM instance profile: {e}")
        finally:
            # Dropped either way: the ARN no longer names something usable, and
            # cleanup is not retried.
            self._owns_instance_profile = False
            self.iam_instance_profile_arn = None

    def _launch_template_reference(self) -> Optional[Dict[str, str]]:
        """Return the ``LaunchTemplate`` kwarg for a launch, or None.

        The version is pinned rather than sent as ``$Latest``: a template
        adopted from a previous run may carry several versions, and the launch
        has to use the one this mode built.
        """
        if not self._launch_template_id:
            return None
        return {
            "LaunchTemplateId": self._launch_template_id,
            "Version": self._launch_template_version or "$Latest",
        }

    def save_state(self) -> None:
        """Save the current state to the state store."""
        # Default state
        state = {
            "resources": self.resources,
            "provider_id": self.provider_id,
            "mode": self.__class__.__name__,
            "vpc_id": self.vpc_id,
            "subnet_id": self.subnet_id,
            "security_group_id": self.security_group_id,
            "initialized": self.initialized,
            "use_spot_fleet": self.use_spot_fleet,
            "spot_interruption_handling": self.spot_interruption_handling,
            "warm_instances": list(self._warm_instances),
            "baked_ami_id": self._baked_ami_id,
            "owns_baked_ami": self._owns_baked_ami,
            "launch_template_id": self._launch_template_id,
            "launch_template_version": self._launch_template_version,
            # Without this, a provider resumed from state would not know it owns
            # the IAM pair and would leave it behind on cleanup (#132).
            "owns_instance_profile": self._owns_instance_profile,
        }

        # Include spot fleet state if applicable
        if self.use_spot_fleet and self.spot_fleet_manager:
            spot_fleet_state = {
                "blocks": self.spot_fleet_manager.blocks,
                "fleet_requests": self.spot_fleet_manager.fleet_requests,
                "instances": self.spot_fleet_manager.instances,
                "enabled": True,
            }
            state["spot_fleet_state"] = spot_fleet_state

            try:
                self.state_store.save_state(STATE_KEY_MODE, state)
                logger.debug(
                    f"Saved state including SpotFleetManager with {len(self.spot_fleet_manager.blocks)} blocks"
                )
            except Exception as e:
                logger.error(f"Failed to save state: {e}")
        else:
            # Save directly (includes baked AMI fields not in the base class state)
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
                # Restore warm pool list; fall back to scanning resources for
                # STATUS_WARM entries in case the key is absent (older state files)
                if "warm_instances" in state:
                    self._warm_instances = state["warm_instances"]
                else:
                    self._warm_instances = [
                        rid
                        for rid, r in self.resources.items()
                        if r.get("warm_pool") and r.get("status") == STATUS_WARM
                    ]

                # Restore the launch template so a resumed provider launches
                # from the same definition instead of leaking it and building a
                # second one (#85).
                self._launch_template_id = state.get("launch_template_id")
                self._launch_template_version = state.get("launch_template_version")

                # Reclaim ownership of the IAM pair this provider created on an
                # earlier run, so cleanup can still delete it (#132).
                self._owns_instance_profile = state.get("owns_instance_profile", False)

                # Restore baked AMI state
                saved_baked_ami = state.get("baked_ami_id")
                if saved_baked_ami:
                    self._baked_ami_id = saved_baked_ami
                    self._owns_baked_ami = state.get("owns_baked_ami", False)
                    self.image_id = saved_baked_ami
                    logger.info(f"Restored baked AMI {saved_baked_ami} from state")

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
                                "Initializing SpotInterruptionMonitor after state load"
                            )
                            self.spot_interruption_monitor = SpotInterruptionMonitor(
                                self.session,
                                provider_id=self.provider_id,
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

                # Load SpotFleetManager state if available
                if (
                    self.use_spot_fleet
                    and self.spot_fleet_manager
                    and state.get("use_spot_fleet", False)
                    and state.get("spot_fleet_state")
                ):
                    spot_fleet_state = state.get("spot_fleet_state", {})

                    if spot_fleet_state.get("blocks"):
                        self.spot_fleet_manager.blocks = spot_fleet_state.get(
                            "blocks", {}
                        )
                    if spot_fleet_state.get("fleet_requests"):
                        self.spot_fleet_manager.fleet_requests = spot_fleet_state.get(
                            "fleet_requests", {}
                        )
                    if spot_fleet_state.get("instances"):
                        self.spot_fleet_manager.instances = spot_fleet_state.get(
                            "instances", {}
                        )

                    logger.debug(
                        f"Loaded SpotFleetManager state with {len(self.spot_fleet_manager.blocks)} blocks"
                    )

                    # Re-register fleets with the interruption monitor. Reads the
                    # block's "fleet_request_id", which is the key the manager
                    # actually writes; this used to iterate a "fleet_requests"
                    # list that nothing has ever produced, so a resumed workflow
                    # silently monitored none of its fleets.
                    if (
                        self.spot_interruption_handling
                        and self.spot_interruption_monitor
                    ):
                        for block_data in self.spot_fleet_manager.blocks.values():
                            fleet_id = block_data.get("fleet_request_id")
                            if not fleet_id:
                                continue
                            self.spot_interruption_monitor.register_fleet(
                                fleet_id,
                                self.handle_fleet_interruption,
                            )
                            logger.info(
                                f"Re-registered EC2 Fleet {fleet_id} for "
                                "interruption handling"
                            )

                logger.debug(f"Loaded state with {len(self.resources)} resources")
                return True
        except Exception as e:
            logger.error(f"Failed to load state: {e}")

        return False

    def initialize(self) -> None:
        """Initialize standard mode infrastructure.

        Raises
        ------
        ResourceCreationError
            If resource creation fails
        """
        # Idempotent: if already initialized, do nothing.
        if self.initialized:
            return

        # This span used to sit *outside* any teardown: the try/except that calls
        # cleanup_infrastructure() began below, after the instance profile had
        # been created and the network verified. So a failure in between left the
        # IAM role and instance profile standing with nothing tracking them --
        # the #132 pathology, reachable on nothing more exotic than a mistyped
        # subnet ID. #196 found it from the Globus side, where a config load that
        # ends in an error message is the normal debugging loop and every attempt
        # leaked another pair.
        #
        # The exception is re-raised unchanged rather than wrapped: #77 requires
        # that a bad network ID surface as a ResourceNotFoundError naming the ID,
        # and burying it in a ResourceCreationError would send the caller looking
        # for a provisioning problem instead of their own typo.
        try:
            # Resolve the SSM instance profile before either path below, so a
            # resumed provider gets an ARN too — it is not persisted in state.
            self._resolve_instance_profile()

            # Confirm the caller-supplied network resources exist. This runs on
            # both paths: verification used to sit inside the resume branch only,
            # so a first-run provider — the common case — never checked at all,
            # and a mistyped or cross-region ID surfaced much later as an opaque
            # InvalidParameterValue from inside run_instances.
            self._verify_resources()
        except Exception:
            self.cleanup_infrastructure()
            raise

        try:
            # Try to load state first
            if self.load_state():
                logger.debug("Loaded state, resources already verified")
                # A state document written before #85, or one whose template
                # creation failed, carries no template ID. Build one now rather
                # than leaving a resumed provider permanently on the fallback
                # path.
                if not self._launch_template_id:
                    self._create_launch_template()
                    self.save_state()
                # State written before this line existed -- or by any version that
                # persisted initialized=False -- would otherwise leave a resumed
                # mode rejecting every submit_job as uninitialized.
                self.initialized = True
                return

            logger.debug("Initializing standard mode infrastructure")

            # AMI baking: snapshot worker_init into a custom AMI
            if self.bake_ami and not self._baked_ami_id:
                ami_id = self._bake_ami()
                self._baked_ami_id = ami_id
                self._owns_baked_ami = True
                self.image_id = ami_id
                logger.info(f"Baked AMI {ami_id} from worker_init")
            elif self.baked_ami_id and not self._baked_ami_id:
                self._baked_ami_id = self.baked_ami_id
                self.image_id = self.baked_ami_id
                logger.info(f"Using pre-supplied baked AMI {self.baked_ami_id}")

            # After baking, so the template references the baked AMI rather than
            # the base one -- self.image_id is reassigned above (#85).
            self._create_launch_template()

            # Save state
            self.save_state()

            logger.info(
                f"Initialized standard mode infrastructure: "
                f"vpc_id={self.vpc_id}, subnet_id={self.subnet_id}, "
                f"security_group_id={self.security_group_id}"
            )

            # Start spot interruption monitoring here (not in __init__) so
            # the thread is only alive when infrastructure is fully ready.
            # The try/finally ensures we stop the thread if anything below
            # (or a subsequent call to initialize()) raises.
            if self.spot_interruption_monitor:
                try:
                    self.spot_interruption_monitor.start_monitoring()
                    logger.info("Started spot interruption monitoring")
                except Exception as monitor_err:
                    logger.error(
                        f"Failed to start spot interruption monitoring: {monitor_err}"
                    )
                    # Non-fatal — jobs can still run, just without interruption recovery
                    self.spot_interruption_monitor = None

            # Mark as initialized
            self.initialized = True
        except Exception as e:
            logger.error(f"Failed to initialize standard mode infrastructure: {e}")
            # Stop the monitor thread if it was started before the exception
            if self.spot_interruption_monitor:
                try:
                    self.spot_interruption_monitor.stop_monitoring()
                except Exception as stop_err:  # nosec B110
                    logger.debug(f"Error stopping monitor during cleanup: {stop_err}")
            # Try to clean up any resources we created
            self.cleanup_infrastructure()
            raise ResourceCreationError(
                f"Failed to initialize standard mode infrastructure: {e}"
            ) from e

    def _resolve_instance_profile(self) -> None:
        """Resolve ``iam_instance_profile_arn``, creating a profile if asked.

        Warm-pool and one-shot dispatch both go through SSM ``SendCommand``,
        which only works if the instance carries a profile holding the
        ``AmazonSSMManagedInstanceCore`` policy. With no ARN, ``_create_instance``
        attaches no profile, the SSM agent never registers, and every dispatch
        times out and silently falls back to UserData.

        A failure here is not fatal: dispatch degrades to UserData rather than
        the whole provider failing to start.
        """
        if self.iam_instance_profile_arn or not self.auto_create_instance_profile:
            return

        try:
            self.iam_instance_profile_arn = get_or_create_ssm_instance_profile(
                session=self.session,
                name_suffix=self.provider_id,
                auto_create=True,
            )
            # Ownership follows taking this branch, not create-vs-fetch (#132).
            # The names derive from provider_id, so a provider resumed from a
            # state file *fetches* the pair it made on its first run -- gating on
            # "did this call create it" would disown it on every restart and leak
            # the pair permanently.
            self._owns_instance_profile = True
            logger.info(
                f"Resolved IAM instance profile {self.iam_instance_profile_arn} "
                "for SSM command dispatch"
            )
            # The SpotFleetManager's provider stand-in was built in __init__,
            # before this ran, so its copy of the ARN is still None. Its launch
            # template reads that copy (#85), so without this every fleet
            # instance launches with no profile.
            if self.spot_fleet_manager:
                self.spot_fleet_manager.provider.iam_instance_profile_arn = (
                    self.iam_instance_profile_arn
                )
        except Exception as e:
            logger.error(
                f"Failed to create IAM instance profile: {e}. SSM command "
                "dispatch will not be available."
            )

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
            EC2 instance ID for tracking the job

        Raises
        ------
        OperatingModeError
            If job submission fails
        """
        # Check if the mode is initialized
        if not self.initialized:
            raise OperatingModeError(
                "StandardMode must be initialized before submitting jobs"
            )

        # Validate image_id
        if not self.image_id:
            # Architecture-matched and SSM-resolved (#84): an x86_64 AMI on a
            # Graviton instance type fails to launch.
            self.image_id = get_default_ami(
                self.session.region_name,
                architecture_for_instance_type(self.instance_type),
                session=self.session,
            )
            logger.info(
                f"Using default AMI {self.image_id} for region {self.session.region_name}"
            )

        logger.info(f"Submitting job {job_id} ({job_name if job_name else 'unnamed'})")

        try:
            # --- Warm pool fast path: reuse an idle instance ---
            if self.warm_pool_size > 0:
                warm_instance_id = self._get_warm_instance()
                if warm_instance_id is not None:
                    logger.info(
                        f"Reusing warm instance {warm_instance_id} for job {job_id}"
                    )
                    ssm_command_id = self._dispatch_ssm_command(
                        warm_instance_id, command, job_id
                    )
                    # Update resource record in-place for the new job
                    self.resources[warm_instance_id].update(
                        {
                            "job_id": job_id,
                            "job_name": job_name or "unnamed",
                            "status": STATUS_RUNNING,
                            "command": command,
                            "tasks_per_node": tasks_per_node,
                            "ssm_command_id": ssm_command_id,
                            "warm_since": None,
                            "created_at": time.time(),
                        }
                    )
                    self.save_state()
                    return warm_instance_id

            # --- Cold path: create a new EC2 instance ---
            init_script = self._prepare_init_script(command, job_id)
            instance_id = self._create_instance(init_script, job_id, job_name)

            # Track the resource
            resource_type = RESOURCE_TYPE_EC2
            fleet_request_id = None
            if (
                self.use_spot_fleet
                and self.spot_fleet_manager
                and instance_id in self.spot_fleet_manager.blocks
            ):
                resource_type = RESOURCE_TYPE_SPOT_FLEET
                fleet_request_id = self.spot_fleet_manager.blocks[instance_id].get(
                    "fleet_request_id"
                )

            resource_data: Dict[str, Any] = {
                "type": resource_type,
                "job_id": job_id,
                "job_name": job_name or "unnamed",
                "status": STATUS_PENDING,
                "created_at": time.time(),
                "command": command,
                "tasks_per_node": tasks_per_node,
            }

            if fleet_request_id:
                # The monitor registers the *fleet* ID, but this record is keyed
                # by block ID, so without this field handle_fleet_interruption
                # has no way back to the block it must mark (#137). The manager's
                # own blocks dict is not enough: it is not persisted in state, so
                # a resumed provider would lose the link.
                resource_data["fleet_request_id"] = fleet_request_id

            if self._uses_ssm_dispatch():
                # Cold start for the SSM paths: wait for SSM, then dispatch. The
                # UserData carries no command, so this is the only way the job runs.
                if self.warm_pool_size > 0:
                    resource_data["warm_pool"] = True
                else:
                    resource_data["one_shot"] = True
                self.resources[instance_id] = resource_data
                try:
                    self._wait_for_ssm_online(instance_id)
                    self._wait_for_worker_ready(instance_id)
                    ssm_command_id = self._dispatch_ssm_command(
                        instance_id, command, job_id
                    )
                    self.resources[instance_id]["ssm_command_id"] = ssm_command_id
                    self.resources[instance_id]["status"] = STATUS_RUNNING
                    self.resources[instance_id]["warm_since"] = None
                except Exception as ssm_err:
                    # The command is not in the UserData, so there is nothing to
                    # fall back to — the instance would idle until max_idle_time
                    # while reporting RUNNING. Terminate it and fail the submit.
                    logger.error(
                        f"SSM dispatch failed for instance {instance_id}: {ssm_err}. "
                        "Terminating it; the command was never delivered."
                    )
                    self._force_terminate(instance_id)
                    self.resources.pop(instance_id, None)
                    self.save_state()
                    raise
            else:
                self.resources[instance_id] = resource_data

            # Save state
            self.save_state()

            logger.info(f"Submitted job {job_id} as instance {instance_id}")
            return instance_id
        except Exception as e:
            logger.error(f"Failed to submit job {job_id}: {e}")
            raise OperatingModeError(f"Failed to submit job {job_id}: {e}") from e

    def _prepare_init_script(self, command: str, job_id: str) -> str:
        """Prepare the worker initialization script.

        Parameters
        ----------
        command : str
            Command to execute. Ignored when the command is dispatched over SSM
            instead — see :meth:`_uses_ssm_dispatch`.
        job_id : str
            Job ID

        Returns
        -------
        str
            Initialization script
        """
        # Start with base worker init script
        init_script = "#!/bin/bash\n"
        if self.worker_init:
            init_script += f"{self.worker_init}\n"

        if self._uses_ssm_dispatch():
            # UserData only runs worker_init and drops a ready marker. The command
            # is dispatched later via SSM SendCommand, which reports its exit code
            # — and for the warm pool, lets the instance serve several jobs without
            # re-running worker_init. No shutdown is appended: the provider
            # terminates the instance through cleanup_resources() once the command
            # reaches a terminal state.
            init_script += "mkdir -p /var/run/parsl\n"
            init_script += "touch /var/run/parsl_worker_ready\n"
            return init_script

        # Non-warm-pool path: embed command and optional shutdown in UserData
        init_script += "\n# Set environment variables\n"
        init_script += f"export PARSL_JOB_ID={job_id}\n"
        init_script += f"export PARSL_PROVIDER_ID={self.provider_id}\n"
        init_script += "export PARSL_WORKER_ID=$(hostname)\n"

        # Add command
        init_script += "\n# Execute Parsl worker command\n"
        init_script += f"{command}\n"

        # Add cleanup if auto shutdown is enabled or one_shot mode forces it
        if self.auto_shutdown or self.one_shot:
            init_script += "\n# Auto-shutdown\n"
            init_script += "shutdown -h now\n"

        return init_script

    # ------------------------------------------------------------------
    # SSM dispatch helpers (shared by the warm pool and one-shot mode)
    # ------------------------------------------------------------------

    def _uses_ssm_dispatch(self) -> bool:
        """Whether the command is delivered by SSM rather than embedded in UserData.

        Both the warm pool and one-shot mode need the command's exit code, which
        UserData cannot report — the instance state is identical whether the
        command succeeded or failed. SSM ``SendCommand`` reports it, so both
        launch with a marker-only UserData and dispatch afterwards.
        """
        return self.warm_pool_size > 0 or self.one_shot

    def _force_terminate(self, instance_id: str) -> None:
        """Terminate an instance best-effort, swallowing any error.

        Used on the SSM-dispatch failure path, where the caller is already
        raising and must not have that exception masked by a cleanup failure.
        The provider's ``cleanup_stray_instances`` safety net and the
        ``ProviderId`` tag both cover the case where this does not succeed.
        """
        try:
            self.session.client("ec2").terminate_instances(InstanceIds=[instance_id])
            logger.info(f"Terminated instance {instance_id} after failed dispatch")
        except Exception as e:
            logger.error(
                f"Could not terminate instance {instance_id} after failed "
                f"dispatch: {e}. It may still be running — check the "
                f"ProviderId={self.provider_id} tag."
            )

    def _get_warm_instance(self) -> Optional[str]:
        """Pop the oldest available warm instance from the pool (FIFO).

        Returns
        -------
        Optional[str]
            Instance ID if a warm instance is available, otherwise None.
            The returned instance's status is updated to STATUS_RUNNING.
        """
        if not self._warm_instances:
            return None
        instance_id = self._warm_instances.pop(0)
        if instance_id in self.resources:
            self.resources[instance_id]["status"] = STATUS_RUNNING
            self.resources[instance_id]["warm_since"] = None
        logger.debug(
            f"Popped warm instance {instance_id} from pool "
            f"({len(self._warm_instances)} remaining)"
        )
        return instance_id

    def _wait_for_ssm_online(self, instance_id: str, timeout: int = 300) -> None:
        """Wait for an EC2 instance to register with AWS Systems Manager.

        Parameters
        ----------
        instance_id : str
            EC2 instance ID to wait for.
        timeout : int, optional
            Maximum seconds to wait, by default 300.

        Raises
        ------
        OperatingModeError
            If the instance does not appear in SSM within *timeout* seconds.
        """
        ssm = self.session.client("ssm")

        def registered() -> bool:
            resp = ssm.describe_instance_information(
                Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
            )
            return bool(resp.get("InstanceInformationList"))

        try:
            poll_until(
                registered,
                timeout=timeout,
                description=f"instance {instance_id} to register with SSM",
                retry_config=self._ssm_poll_config,
                on_error=_swallow_client_errors,
            )
        except TimeoutError as e:
            raise OperatingModeError(
                f"Instance {instance_id} did not become available in SSM "
                f"within {timeout}s"
            ) from e
        logger.debug(f"Instance {instance_id} is online in SSM")

    def _wait_for_worker_ready(self, instance_id: str, timeout: int = 600) -> None:
        """Wait for the worker ready marker on an instance via SSM RunCommand.

        The init script (UserData) touches ``/var/run/parsl_worker_ready`` once
        worker_init completes.  This method polls until that file exists.

        Parameters
        ----------
        instance_id : str
            EC2 instance ID to check.
        timeout : int, optional
            Maximum seconds to wait, by default 600.

        Raises
        ------
        OperatingModeError
            If the ready marker is not found within *timeout* seconds.
        """
        ssm = self.session.client("ssm")

        def marker_present() -> bool:
            resp = ssm.send_command(
                InstanceIds=[instance_id],
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": ["test -f /var/run/parsl_worker_ready"]},
                Comment="Parsl worker ready check",
            )
            command_id = resp["Command"]["CommandId"]
            # Not a retry interval: SendCommand is asynchronous, so the
            # invocation does not exist to be read for a moment after it
            # returns. This pause stays inside the predicate and deliberately
            # stays flat -- backing it off would only delay the first read of a
            # command that always takes about the same time to land.
            time.sleep(SSM_INVOCATION_SETTLE_SECONDS)
            invocation = ssm.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id,
            )
            return bool(invocation.get("StatusDetails") == "Success")

        try:
            poll_until(
                marker_present,
                timeout=timeout,
                description=f"worker ready marker on {instance_id}",
                retry_config=self._ssm_poll_config,
                on_error=_swallow_client_errors,
            )
        except TimeoutError as e:
            raise OperatingModeError(
                f"Worker ready marker not found on {instance_id} within {timeout}s"
            ) from e
        logger.debug(f"Worker ready on {instance_id}")

    def _dispatch_ssm_command(self, instance_id: str, command: str, job_id: str) -> str:
        """Dispatch a shell command to an EC2 instance via SSM SendCommand.

        Parameters
        ----------
        instance_id : str
            Target EC2 instance ID.
        command : str
            Shell command to execute.
        job_id : str
            Parsl job ID (used for environment variable export and comment).

        Returns
        -------
        str
            SSM CommandId for later status polling via ``get_command_invocation``.

        Notes
        -----
        In one-shot mode a self-shutdown backstop is appended. The provider
        normally terminates the instance from ``_cleanup_resources()`` once the
        command reaches a terminal state, but that requires the driver process to
        still be alive to poll. The backstop bounds the cost of a driver that
        dies mid-job; it is scheduled after the command's exit status is captured
        so it cannot mask a non-zero exit.
        """
        ssm = self.session.client("ssm")
        env_setup = (
            f"export PARSL_JOB_ID={job_id}\n"
            f"export PARSL_PROVIDER_ID={self.provider_id}\n"
            "export PARSL_WORKER_ID=$(hostname)\n"
        )
        script = env_setup + command
        if self.one_shot:
            # Detach the shutdown so SSM sees the command finish and reports the
            # real exit code, then exit with that same code.
            script += (
                "\n_parsl_rc=$?\n"
                "nohup sh -c 'sleep 30; shutdown -h now' >/dev/null 2>&1 &\n"
                "exit $_parsl_rc\n"
            )
        response = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [script]},
            Comment=f"Parsl job {job_id[:16]}",
        )
        command_id = response["Command"]["CommandId"]
        logger.debug(
            f"Dispatched SSM command {command_id} to {instance_id} for job {job_id}"
        )
        return command_id

    def _create_instance(
        self, init_script: str, job_id: str, job_name: Optional[str] = None
    ) -> str:
        """Create an EC2 instance for the job.

        Parameters
        ----------
        init_script : str
            Initialization script
        job_id : str
            Job ID
        job_name : Optional[str], optional
            Job name, by default None

        Returns
        -------
        str
            EC2 instance ID

        Raises
        ------
        ResourceCreationError
            If instance creation fails
        """
        ec2 = self.session.client("ec2")

        # Prepare instance tags
        tags = [
            {"Key": "Name", "Value": f"parsl-worker-{job_id[:8]}"},
            {"Key": "CreatedBy", "Value": "ParslEphemeralProvider"},
            {"Key": "ProviderId", "Value": self.provider_id},
            {"Key": "JobId", "Value": job_id},
        ]

        if job_name:
            tags.append({"Key": "JobName", "Value": job_name})

        # Add additional tags
        for key, value in self.additional_tags.items():
            tags.append({"Key": key, "Value": value})

        # Prefer the launch template built in initialize(): it carries IMDSv2,
        # the shutdown behaviour, the network interface, the key pair, and the
        # IAM profile, leaving only the per-job UserData and tags here (#85).
        launch_template = self._launch_template_reference()
        run_args: Dict[str, Any]
        if launch_template:
            run_args = {
                "LaunchTemplate": launch_template,
                "MaxCount": 1,
                "MinCount": 1,
                "UserData": init_script,
                "TagSpecifications": [{"ResourceType": "instance", "Tags": tags}],
            }
        else:
            # Fallback for an account that cannot create launch templates.
            # Every setting the template would have carried has to be repeated
            # here, which is exactly the duplication #85 removes.
            network_interfaces = []
            if self.subnet_id:
                network_interface = {
                    "DeviceIndex": 0,
                    "SubnetId": self.subnet_id,
                    "AssociatePublicIpAddress": self.use_public_ips,
                }

                if self.security_group_id:
                    network_interface["Groups"] = [self.security_group_id]

                network_interfaces.append(network_interface)

            run_args = {
                "ImageId": self.image_id,
                "InstanceType": self.instance_type,
                "MaxCount": 1,
                "MinCount": 1,
                "UserData": init_script,
                "TagSpecifications": [{"ResourceType": "instance", "Tags": tags}],
                # The init script runs `shutdown -h now` when auto_shutdown or
                # one_shot is set, and EC2's default for an instance-initiated
                # shutdown is *stop*, not terminate — leaving a stopped instance
                # with a billed EBS volume. Worse, EC2_STATUS_MAPPING maps
                # "stopped" to COMPLETED, so the provider drops the tracking
                # record and the volume is orphaned as well as billed.
                # DetachedMode already sets this on all of its launch paths.
                "InstanceInitiatedShutdownBehavior": "terminate",
                "MetadataOptions": dict(IMDSV2_METADATA_OPTIONS),
            }

            # Add network configuration if available
            if network_interfaces:
                run_args["NetworkInterfaces"] = network_interfaces
            elif self.security_group_id:
                run_args["SecurityGroupIds"] = [self.security_group_id]

            # Add key pair if specified
            if self.key_name:
                run_args["KeyName"] = self.key_name

            # Attach the IAM instance profile whenever one is available. SSM
            # SendCommand needs it, and an unused profile costs nothing.
            if self.iam_instance_profile_arn:
                run_args["IamInstanceProfile"] = {"Arn": self.iam_instance_profile_arn}

        # Use spot instances if requested. use_spot_fleet implies spot: a fleet
        # request is only ever placed for spot capacity, and testing use_spot
        # alone here sent a use_spot_fleet=True caller down the on-demand path
        # so the fleet was never requested (#137).
        if self.use_spot or self.use_spot_fleet:
            return self._create_spot_instance(run_args)
        else:
            # Create on-demand instance
            try:
                response = ec2.run_instances(**run_args)
                instance_id = response["Instances"][0]["InstanceId"]

                # Wait for instance to be running
                wait_for_resource(
                    instance_id, "instance_running", ec2, resource_name="EC2 instance"
                )

                return instance_id
            except Exception as e:
                logger.error(f"Failed to create EC2 instance: {e}")
                raise ResourceCreationError(
                    f"Failed to create EC2 instance: {e}"
                ) from e

    def _create_spot_instance(self, run_args: Dict[str, Any]) -> str:
        """Create a spot instance.

        Parameters
        ----------
        run_args : Dict[str, Any]
            Arguments for EC2 instance creation

        Returns
        -------
        str
            EC2 instance ID or block ID for spot fleet

        Raises
        ------
        ResourceCreationError
            If spot instance creation fails
        SpotFleetError
            If spot fleet creation fails
        """
        # Check if using spot fleet
        if self.use_spot_fleet and self.spot_fleet_manager:
            return self._create_spot_fleet_instance(run_args)

        # With a launch template available, request spot through RunInstances
        # instead of the older RequestSpotInstances (#85). Verified against the
        # botocore service model: RequestSpotInstances accepts no LaunchTemplate,
        # and its LaunchSpecification shape has no MetadataOptions member at all,
        # so IMDSv2 cannot be set on that path by any means. RunInstances with
        # InstanceMarketOptions accepts both, and DryRun against real EC2
        # confirms the combination.
        if "LaunchTemplate" in run_args:
            return self._create_spot_instance_via_run_instances(run_args)

        # Traditional spot instance request
        ec2 = self.session.client("ec2")

        # Extract tags
        tags = run_args.pop("TagSpecifications", [{}])[0].get("Tags", [])

        # RunInstances-only keys that RequestSpotInstances rejects in a
        # LaunchSpecification. MinCount/MaxCount become InstanceCount, and
        # InstanceInitiatedShutdownBehavior has no spot equivalent — a spot
        # instance that shuts itself down is stopped, then reclaimed by EC2
        # when the one-time request is already closed, so the provider's own
        # terminate_instances in cleanup_resources() is what reclaims the EBS
        # volume here.
        for run_only_key in (
            "InstanceInitiatedShutdownBehavior",
            "MinCount",
            "MaxCount",
        ):
            run_args.pop(run_only_key, None)

        # Prepare spot request
        spot_args = {
            "InstanceCount": 1,
            "Type": "one-time",
            "LaunchSpecification": run_args,
        }

        # Add max price if specified
        if self.spot_max_price:
            spot_args["SpotPrice"] = self.spot_max_price

        try:
            # Request spot instance
            response = ec2.request_spot_instances(**spot_args)
            request_id = response["SpotInstanceRequests"][0]["SpotInstanceRequestId"]

            # Add tags to spot request
            if tags:
                tag_spec = {"Resources": [request_id], "Tags": tags}
                ec2.create_tags(**tag_spec)

            # Wait for spot request to be fulfilled
            logger.debug(f"Waiting for spot request {request_id} to be fulfilled")
            waiter = ec2.get_waiter("spot_instance_request_fulfilled")
            waiter.wait(
                SpotInstanceRequestIds=[request_id],
                WaiterConfig={"Delay": 5, "MaxAttempts": 60},
            )

            # Get instance ID
            response = ec2.describe_spot_instance_requests(
                SpotInstanceRequestIds=[request_id]
            )
            instance_id = response["SpotInstanceRequests"][0]["InstanceId"]

            # Wait for instance to be running
            wait_for_resource(
                instance_id, "instance_running", ec2, resource_name="EC2 spot instance"
            )

            # Register with spot interruption monitor if enabled
            self._register_spot_instance(instance_id)

            return instance_id
        except Exception as e:
            logger.error(f"Failed to create spot instance: {e}")
            raise ResourceCreationError(f"Failed to create spot instance: {e}") from e

    def _create_spot_instance_via_run_instances(self, run_args: Dict[str, Any]) -> str:
        """Request a spot instance through ``RunInstances`` (#85).

        The modern spelling: ``InstanceMarketOptions`` on ``RunInstances``
        instead of ``RequestSpotInstances``. Preferred whenever a launch
        template exists, because it is the only route that gets IMDSv2 onto a
        spot instance -- ``RequestSpotInstances`` has no ``MetadataOptions``
        member -- and because it returns the instance ID directly, with no
        intermediate request to poll or tag.

        Parameters
        ----------
        run_args : Dict[str, Any]
            ``RunInstances`` arguments, already carrying ``LaunchTemplate``.

        Returns
        -------
        str
            The EC2 instance ID.

        Raises
        ------
        ResourceCreationError
            If the request fails.
        """
        ec2 = self.session.client("ec2")
        spot_options: Dict[str, Any] = {}
        if self.spot_max_price:
            spot_options["MaxPrice"] = self.spot_max_price

        market_options: Dict[str, Any] = {"MarketType": "spot"}
        if spot_options:
            market_options["SpotOptions"] = spot_options

        # The template's InstanceInitiatedShutdownBehavior="terminate" is
        # deliberately left in place. RequestSpotInstances could not express it
        # -- its LaunchSpecification has no such member -- so the old path left
        # a self-shutting-down spot instance *stopped*, with a billed EBS volume
        # that EC2_STATUS_MAPPING then reported as COMPLETED, dropping the
        # tracking record. Verified against real EC2 that RunInstances accepts
        # terminate alongside InstanceMarketOptions: a one-time spot instance
        # launched this way reports InstanceLifecycle=spot and shutdown
        # behaviour terminate. So this path closes on spot the same leak #66
        # closed on demand.
        run_args = dict(run_args)
        run_args["InstanceMarketOptions"] = market_options

        try:
            response = ec2.run_instances(**run_args)
            instance_id = response["Instances"][0]["InstanceId"]

            wait_for_resource(
                instance_id, "instance_running", ec2, resource_name="EC2 spot instance"
            )
            self._register_spot_instance(instance_id)
            return instance_id
        except Exception as e:
            logger.error(f"Failed to create spot instance: {e}")
            raise ResourceCreationError(f"Failed to create spot instance: {e}") from e

    def _register_spot_instance(self, instance_id: str) -> None:
        """Register *instance_id* with the interruption monitor, if enabled."""
        if self.spot_interruption_handling and self.spot_interruption_monitor:
            self.spot_interruption_monitor.register_instance(
                instance_id,
                self.handle_instance_interruption,
            )
            logger.info(
                f"Registered spot instance {instance_id} for interruption handling"
            )

    def _create_spot_fleet_instance(self, run_args: Dict[str, Any]) -> str:
        """Create a spot fleet instance.

        Parameters
        ----------
        run_args : Dict[str, Any]
            Arguments for EC2 instance creation

        Returns
        -------
        str
            Block ID for the spot fleet

        Raises
        ------
        SpotFleetError
            If spot fleet creation fails
        """
        if not self.spot_fleet_manager:
            raise ResourceCreationError("SpotFleetManager not initialized")

        # Extract job ID from tags
        job_id = None
        if "TagSpecifications" in run_args and run_args["TagSpecifications"]:
            for tag in run_args["TagSpecifications"][0].get("Tags", []):
                if tag["Key"] == "JobId":
                    job_id = tag["Value"]
                    break

        if not job_id:
            job_id = str(uuid.uuid4())

        # Extract user data
        user_data = None
        if "UserData" in run_args:
            user_data = run_args["UserData"]
            self.spot_fleet_manager.provider.worker_init = user_data

        try:
            # Create the spot fleet
            blocks = self.spot_fleet_manager.create_blocks(1)

            if not blocks:
                logger.error(
                    "SpotFleetManager.create_blocks returned empty blocks dictionary"
                )
                raise ResourceCreationError("Failed to create Spot Fleet blocks")

            # Get the block ID (should be only one)
            block_id = next(iter(blocks.keys()))

            logger.info(f"Created EC2 Fleet block {block_id} for job {job_id}")

            # An instant fleet reports fulfilment synchronously, so a block with
            # no instances means EC2 could not fill the request at all -- fail
            # now rather than after a timeout (#86).
            if not blocks[block_id].get("instance_ids"):
                self._discard_failed_fleet_block(block_id)
                raise ResourceCreationError(
                    f"EC2 Fleet block {block_id} launched no instances; no spot "
                    "capacity was available for the requested instance types"
                )

            # The instances exist but have yet to reach "running". This wait now
            # covers only that transition -- fulfilment was settled synchronously
            # above -- so a timeout here no longer conflates "no spot capacity"
            # with "slow to boot".
            max_wait = DEFAULT_RESOURCE_CREATION_TIMEOUT
            # Bind the manager locally: the guard above narrowed it away from
            # None, but that narrowing does not reach inside a closure.
            fleet_manager = self.spot_fleet_manager

            def settled_status() -> Optional[str]:
                """Return the block status once it stops being provisional.

                Only RUNNING and the three terminal states are answers; anything
                else (PENDING, UNKNOWN) means keep waiting. Returning the status
                rather than a bool lets the terminal case be handled below --
                raising from inside a predicate would be read as "not yet"
                and swallowed by ``poll_until``.
                """
                status = fleet_manager.get_block_status(block_id)
                logger.debug(f"EC2 Fleet block {block_id} status: {status}")
                if status in (
                    STATUS_RUNNING,
                    STATUS_FAILED,
                    STATUS_CANCELED,
                    STATUS_COMPLETED,
                ):
                    return str(status)
                return None

            try:
                final_status = poll_until(
                    settled_status,
                    timeout=max_wait,
                    description=f"EC2 Fleet block {block_id} to reach RUNNING",
                    retry_config=self._fleet_poll_config,
                )
            except TimeoutError as e:
                logger.error(
                    f"Timeout waiting for EC2 Fleet block {block_id} to reach RUNNING"
                )
                self._discard_failed_fleet_block(block_id)
                raise ResourceCreationError(
                    f"EC2 Fleet block {block_id} did not reach RUNNING "
                    f"state within {max_wait}s"
                ) from e

            if final_status != STATUS_RUNNING:
                self._discard_failed_fleet_block(block_id)
                raise ResourceCreationError(
                    f"EC2 Fleet block failed with status {final_status}"
                )

            # Register the fleet with the spot interruption monitor if enabled.
            # The block records a single "fleet_request_id"; this used to read a
            # "fleet_requests" list that no code has ever written, so no fleet
            # was ever actually registered.
            if self.spot_interruption_handling and self.spot_interruption_monitor:
                fleet_id = self.spot_fleet_manager.blocks.get(block_id, {}).get(
                    "fleet_request_id"
                )
                if fleet_id:
                    self.spot_interruption_monitor.register_fleet(
                        fleet_id,
                        self.handle_fleet_interruption,
                    )
                    logger.info(
                        f"Registered EC2 Fleet {fleet_id} for interruption handling"
                    )

            return block_id

        except SpotFleetError as e:
            logger.error(f"Failed to create EC2 Fleet: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error creating EC2 Fleet: {e}")
            raise ResourceCreationError(f"Failed to create EC2 Fleet: {e}") from e

    def _discard_failed_fleet_block(self, block_id: str) -> None:
        """Tear down a fleet block that will never become usable.

        Deletes the fleet, terminating any instances it did launch, and drops the
        manager's record of the block so a later ``cleanup_all_resources`` does
        not report a fleet that is already gone. Never raises: the caller is
        already on its way to raising something more informative.

        Parameters
        ----------
        block_id : str
            Block to discard.
        """
        if not self.spot_fleet_manager:
            return
        try:
            self.spot_fleet_manager.terminate_block(block_id)
        except Exception as e:
            logger.error(f"Failed to clean up failed fleet block {block_id}: {e}")
        finally:
            self.spot_fleet_manager.blocks.pop(block_id, None)

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

        ec2 = self.session.client("ec2")
        status_map = {}

        # Group IDs by resource type / tracking method
        ec2_instances = []
        spot_fleet_blocks = []
        ssm_instances = []  # tracked via SSM command invocation

        for resource_id in resource_ids:
            resource = self.resources.get(resource_id)
            if not resource:
                status_map[resource_id] = STATUS_UNKNOWN
                continue

            # An interruption is sticky. Everything below re-derives status from
            # AWS, and a reclaimed instance goes to "shutting-down", which
            # EC2_STATUS_MAPPING renders COMPLETED -- so without this the marker
            # set by handle_instance_interruption is overwritten on the very next
            # poll and the reclaim is reported as success again (#137).
            if resource.get("status") == STATUS_INTERRUPTED:
                status_map[resource_id] = STATUS_INTERRUPTED
                continue

            # Any resource carrying an SSM command ID — warm pool or one-shot —
            # is tracked by the command's own status, which reports the exit code.
            # EC2 instance state cannot: it looks the same either way.
            if resource.get("ssm_command_id"):
                ssm_instances.append(resource_id)
            elif resource.get("type") == RESOURCE_TYPE_EC2:
                ec2_instances.append(resource_id)
            elif resource.get("type") == RESOURCE_TYPE_SPOT_FLEET:
                spot_fleet_blocks.append(resource_id)
            else:
                status_map[resource_id] = STATUS_UNKNOWN

        # --- SSM dispatch: poll command invocation status for the exit code ---
        if ssm_instances:
            ssm = self.session.client("ssm")
            for instance_id in ssm_instances:
                resource = self.resources[instance_id]
                command_id = resource["ssm_command_id"]
                try:
                    response = ssm.get_command_invocation(
                        CommandId=command_id,
                        InstanceId=instance_id,
                    )
                    ssm_status = response.get("Status", "Unknown")
                    if ssm_status == "Success":
                        status = STATUS_COMPLETED
                    elif ssm_status in (
                        "Failed",
                        "TimedOut",
                        "Cancelled",
                        "Undeliverable",
                        "DeliveryTimedOut",
                        "ExecutionTimedOut",
                    ):
                        status = STATUS_FAILED
                    else:
                        # InProgress, Pending, Delayed, etc.
                        status = STATUS_RUNNING
                    status_map[instance_id] = status
                    self.resources[instance_id]["status"] = status

                    # Record the exit code so a FAILED job is diagnosable
                    # without a second SSM round-trip.
                    exit_code = response.get("ResponseCode")
                    if exit_code is not None and exit_code >= 0:
                        self.resources[instance_id]["exit_code"] = exit_code
                    if status == STATUS_FAILED:
                        logger.error(
                            f"Command on {instance_id} reported {ssm_status} "
                            f"(exit code {exit_code}): "
                            f"{response.get('StandardErrorContent', '')[:500]}"
                        )
                except ClientError as e:
                    if "InvocationDoesNotExist" in str(e):
                        # Command not yet received by the instance
                        status_map[instance_id] = STATUS_RUNNING
                    else:
                        logger.error(
                            f"Failed to get SSM command status for {instance_id}: {e}"
                        )
                        status_map[instance_id] = STATUS_UNKNOWN

        # Check EC2 instance status
        if ec2_instances:
            try:
                response = ec2.describe_instances(InstanceIds=ec2_instances)

                # Process response
                for reservation in response.get("Reservations", []):
                    for instance in reservation.get("Instances", []):
                        instance_id = instance["InstanceId"]
                        instance_state = instance["State"]["Name"]

                        # Map EC2 state to our status
                        status = EC2_STATUS_MAPPING.get(instance_state, STATUS_UNKNOWN)
                        status_map[instance_id] = status

                        # Update resource state
                        if instance_id in self.resources:
                            self.resources[instance_id]["status"] = status
            except ClientError as e:
                logger.error(f"Failed to get EC2 instance status: {e}")
                # Handle case where instances don't exist anymore
                if "InvalidInstanceID.NotFound" in str(e):
                    for instance_id in ec2_instances:
                        status_map[instance_id] = STATUS_COMPLETED
                        if instance_id in self.resources:
                            self.resources[instance_id]["status"] = STATUS_COMPLETED
            except Exception as e:
                logger.error(f"Unexpected error getting EC2 instance status: {e}")
                for instance_id in ec2_instances:
                    status_map[instance_id] = STATUS_UNKNOWN

        # Check Spot Fleet status
        if spot_fleet_blocks and self.use_spot_fleet and self.spot_fleet_manager:
            for block_id in spot_fleet_blocks:
                try:
                    status = self.spot_fleet_manager.get_block_status(block_id)
                    status_map[block_id] = status

                    # Update resource state
                    if block_id in self.resources:
                        self.resources[block_id]["status"] = status
                except Exception as e:
                    logger.error(
                        f"Failed to get Spot Fleet block status for {block_id}: {e}"
                    )
                    status_map[block_id] = STATUS_UNKNOWN

        # Save state with updated status
        self.save_state()

        return status_map

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

        ec2 = self.session.client("ec2")
        cancel_map = {}

        # Group IDs by resource type
        ec2_instances = []
        spot_fleet_blocks = []

        for resource_id in resource_ids:
            resource = self.resources.get(resource_id)
            if not resource:
                cancel_map[resource_id] = STATUS_UNKNOWN
                continue

            if resource.get("type") == RESOURCE_TYPE_EC2:
                ec2_instances.append(resource_id)
            elif resource.get("type") == RESOURCE_TYPE_SPOT_FLEET:
                spot_fleet_blocks.append(resource_id)
            else:
                cancel_map[resource_id] = STATUS_UNKNOWN

        # Cancel EC2 instances
        if ec2_instances:
            try:
                ec2.terminate_instances(InstanceIds=ec2_instances)

                for instance_id in ec2_instances:
                    cancel_map[instance_id] = STATUS_CANCELED
                    if instance_id in self.resources:
                        self.resources[instance_id]["status"] = STATUS_CANCELED

                logger.info(f"Canceled {len(ec2_instances)} EC2 instances")
            except ClientError as e:
                logger.error(f"Failed to cancel EC2 instances: {e}")
                # Handle case where instances don't exist anymore
                if "InvalidInstanceID.NotFound" in str(e):
                    for instance_id in ec2_instances:
                        cancel_map[instance_id] = STATUS_COMPLETED
                        if instance_id in self.resources:
                            self.resources[instance_id]["status"] = STATUS_COMPLETED
                else:
                    for instance_id in ec2_instances:
                        cancel_map[instance_id] = STATUS_FAILED
            except Exception as e:
                logger.error(f"Unexpected error canceling EC2 instances: {e}")
                for instance_id in ec2_instances:
                    cancel_map[instance_id] = STATUS_FAILED

        # Cancel Spot Fleet blocks
        if spot_fleet_blocks and self.use_spot_fleet and self.spot_fleet_manager:
            for block_id in spot_fleet_blocks:
                try:
                    self.spot_fleet_manager.terminate_block(block_id)
                    cancel_map[block_id] = STATUS_CANCELED

                    # Update resource state
                    if block_id in self.resources:
                        self.resources[block_id]["status"] = STATUS_CANCELED

                    logger.info(f"Canceled Spot Fleet block {block_id}")
                except Exception as e:
                    logger.error(f"Failed to cancel Spot Fleet block {block_id}: {e}")
                    cancel_map[block_id] = STATUS_FAILED
                    if block_id in self.resources:
                        self.resources[block_id]["status"] = STATUS_FAILED

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

        ec2 = self.session.client("ec2")

        # Group IDs by resource type
        ec2_instances = []
        spot_fleet_blocks = []

        for resource_id in resource_ids:
            resource = self.resources.get(resource_id)
            if not resource:
                continue

            if resource.get("type") == RESOURCE_TYPE_EC2:
                ec2_instances.append(resource_id)
            elif resource.get("type") == RESOURCE_TYPE_SPOT_FLEET:
                spot_fleet_blocks.append(resource_id)

        # Terminate EC2 instances — only remove from tracking on confirmed success
        # or when the instance is already gone (InvalidInstanceID.NotFound).
        # On any other error, keep the entry so the next cleanup cycle can retry.
        if ec2_instances:
            try:
                ec2.terminate_instances(InstanceIds=ec2_instances)
                logger.info(f"Terminated {len(ec2_instances)} EC2 instances")
                for instance_id in ec2_instances:
                    self.resources.pop(instance_id, None)
            except ClientError as e:
                if "InvalidInstanceID.NotFound" in str(e):
                    # Instances already gone — safe to remove from tracking
                    for instance_id in ec2_instances:
                        self.resources.pop(instance_id, None)
                else:
                    logger.error(f"Failed to terminate EC2 instances: {e}")
                    # Do NOT remove — will be retried on next cleanup cycle
            except Exception as e:
                logger.error(f"Unexpected error terminating EC2 instances: {e}")
                # Do NOT remove — will be retried on next cleanup cycle

        # Terminate Spot Fleet blocks — same conservative removal policy
        if spot_fleet_blocks and self.use_spot_fleet and self.spot_fleet_manager:
            for block_id in spot_fleet_blocks:
                try:
                    self.spot_fleet_manager.terminate_block(block_id)
                    logger.info(f"Terminated Spot Fleet block {block_id}")
                    self.resources.pop(block_id, None)
                except Exception as e:
                    logger.error(
                        f"Failed to terminate Spot Fleet block {block_id}: {e}"
                    )
                    # Do NOT remove — will be retried on next cleanup cycle

        # Save state with updated resources
        self.save_state()

    def cleanup_infrastructure(self) -> None:
        """Clean up infrastructure created by this mode.

        This cleans up the VPC, subnet, and security group if they were created by the provider.
        """
        logger.info("Cleaning up infrastructure")

        # Collect EC2 instance IDs before cleanup so we can wait for termination.
        # cleanup_all() sends terminate requests but does not wait; instances remain
        # in "shutting-down" state and their ENIs keep references to the security
        # group, causing a DependencyViolation when we try to delete the SG.
        ec2_instance_ids = [
            rid
            for rid, r in self.resources.items()
            if r.get("type") == RESOURCE_TYPE_EC2
        ]

        # Delete all instances first
        if self.resources:
            self.cleanup_all()

        # Wait for all EC2 instances to reach terminated state so that their
        # network interfaces are released before we attempt SG/subnet deletion.
        if ec2_instance_ids:
            try:
                ec2 = self.session.client("ec2")
                waiter = ec2.get_waiter("instance_terminated")
                waiter.wait(
                    InstanceIds=ec2_instance_ids,
                    WaiterConfig={"Delay": 5, "MaxAttempts": 36},  # up to 3 min
                )
                logger.debug(
                    "All EC2 instances confirmed terminated: %s", ec2_instance_ids
                )
            except Exception as e:
                logger.warning(
                    "Timed out or error waiting for instance termination: %s — "
                    "proceeding with infrastructure cleanup (SG deletion may fail)",
                    e,
                )

        try:
            # Stop spot interruption monitoring if enabled
            if self.spot_interruption_monitor:
                try:
                    self.spot_interruption_monitor.stop_monitoring()
                    logger.info("Stopped spot interruption monitoring")
                except Exception as e:
                    logger.error(f"Failed to stop spot interruption monitoring: {e}")
                self.spot_interruption_monitor = None

            # Deregister baked AMI if this provider created it
            if self._baked_ami_id and getattr(self, "_owns_baked_ami", False):
                try:
                    self._deregister_baked_ami(self._baked_ami_id)
                    logger.info(f"Deregistered baked AMI {self._baked_ami_id}")
                    self._baked_ami_id = None
                except Exception as e:
                    logger.error(
                        f"Failed to deregister baked AMI {self._baked_ami_id}: {e}"
                    )

            # Delete the launch template (#85). After the AMI deregistration
            # above and before the state save below, so a failure to delete it
            # still leaves the ID cleared in the persisted state.
            self._delete_launch_template()

            # Delete the IAM role and instance profile, but only if we made them
            # (#132). Before the fix nothing deleted them, so every run left a
            # standing principal carrying AmazonSSMManagedInstanceCore behind and
            # the account walked toward IAM's 1,000-role quota.
            self._delete_instance_profile()

            # Clean up Spot Fleet resources if using spot fleet
            if self.use_spot_fleet and self.spot_fleet_manager:
                try:
                    self.spot_fleet_manager.cleanup_all_resources()
                    logger.info("Cleaned up all Spot Fleet resources")
                except Exception as e:
                    logger.error(f"Failed to clean up Spot Fleet resources: {e}")

            # Clear initialization flag
            self.initialized = False

            # Save state
            self.save_state()

            logger.info("Infrastructure cleanup complete")
        except Exception as e:
            logger.error(f"Failed to clean up infrastructure: {e}")
            # Still mark as not initialized
            self.initialized = False
            self.save_state()

    def list_resources(self) -> Dict[str, List[Dict[str, Any]]]:
        """List all resources created by this mode.

        Returns
        -------
        Dict[str, List[Dict[str, Any]]]
            Dictionary of resource types and their details
        """
        result: Dict[str, List[Dict[str, Any]]] = {
            "ec2_instances": [],
            "vpc": [],
            "subnet": [],
            "security_group": [],
            "spot_fleet": [],
        }

        # Add EC2 instances and Spot Fleet blocks
        for resource_id, resource in self.resources.items():
            if resource.get("type") == RESOURCE_TYPE_EC2:
                result["ec2_instances"].append(
                    {
                        "id": resource_id,
                        "job_id": resource.get("job_id"),
                        "job_name": resource.get("job_name"),
                        "status": resource.get("status"),
                        "created_at": resource.get("created_at"),
                    }
                )
            elif resource.get("type") == RESOURCE_TYPE_SPOT_FLEET:
                result["spot_fleet"].append(
                    {
                        "id": resource_id,
                        "job_id": resource.get("job_id"),
                        "job_name": resource.get("job_name"),
                        "status": resource.get("status"),
                        "created_at": resource.get("created_at"),
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

        # Add detailed Spot Fleet information if available
        if self.use_spot_fleet and self.spot_fleet_manager:
            seen_fleet_ids = {f["id"] for f in result["spot_fleet"]}
            for fleet_id, fleet in self.spot_fleet_manager.fleet_requests.items():
                block_id = fleet.get("block_id")
                if block_id:
                    fleet_details = {
                        "id": fleet_id,
                        "block_id": block_id,
                        "status": fleet.get("status"),
                        "created_at": fleet.get("created_at"),
                        "target_capacity": fleet.get("target_capacity"),
                    }

                    if fleet_id not in seen_fleet_ids:
                        result["spot_fleet"].append(fleet_details)
                        seen_fleet_ids.add(fleet_id)

        return result

    def cleanup_all(self) -> None:
        """Clean up all resources created by this mode."""
        logger.info("Cleaning up all resources")

        # Get all resource IDs
        resource_ids = list(self.resources.keys())

        if resource_ids:
            self.cleanup_resources(resource_ids)
            logger.info(f"Cleaned up {len(resource_ids)} resources")
        else:
            logger.debug("No resources to clean up")

        # Clean up Spot Fleet resources if using spot fleet
        if self.use_spot_fleet and self.spot_fleet_manager:
            try:
                self.spot_fleet_manager.cleanup_all_resources()
                logger.info("Cleaned up all Spot Fleet resources")
            except Exception as e:
                logger.error(f"Failed to clean up Spot Fleet resources: {e}")

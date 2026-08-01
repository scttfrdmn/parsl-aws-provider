"""Fleet-based compute resource implementation for Parsl Ephemeral AWS Provider.

Provides multi-pool spot instance management, which is more reliable than
individual spot requests: a fleet draws from several instance types at once, so
a single exhausted capacity pool does not fail the request.

Built on EC2 Fleet (``CreateFleet``) since #86. It previously used Spot Fleet
(``RequestSpotFleet``), which AWS describes as "a legacy API with no planned
investment", recommending EC2 Fleet or EC2 Auto Scaling instead. The class name
is unchanged to keep the ``use_spot_fleet`` provider kwarg and the persisted
state documents working across the upgrade.

Fleet type ``instant`` is used throughout: it returns the launched instance IDs
synchronously, so a block knows its instances without polling. See
:func:`parsl_ephemeral_aws.utils.aws.create_ec2_fleet` for the parameters this
fleet type rejects, and why capacity rebalancing is not among the options.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import logging
import uuid
import time
from typing import Dict, List, Optional, Any, Tuple

from botocore.exceptions import ClientError, NoCredentialsError

from ..exceptions import (
    ResourceCreationError,
    ResourceCleanupError,
    ResourceDeletionError,
    SpotFleetError,
    SpotFleetRequestError,
    SpotFleetThrottlingError,
)
from ..constants import (
    LAUNCH_TEMPLATE_NAME_PREFIX,
    TAG_PREFIX,
    TAG_MANAGED,
    TAG_WORKFLOW_ID,
    TAG_BLOCK_ID,
    RESOURCE_TYPE_SPOT_FLEET,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_CANCELLED,
    STATUS_UNKNOWN,
    DEFAULT_VPC_CIDR,
    DEFAULT_SPOT_ALLOCATION_STRATEGY,
)
from ..config import SecurityConfig
from ..utils.aws import (
    build_fleet_launch_template_configs,
    build_launch_template_data,
    create_ec2_fleet,
    create_launch_template,
    delete_ec2_fleet,
    delete_launch_template,
    describe_ec2_fleet,
    get_ec2_fleet_instance_ids,
    resolve_manager_session,
)
from ..security import (
    CredentialManager,
    CredentialConfiguration,
    SecurityEventType,
    SecurityEventSeverity,
    SecurityEvent,
)
from ..error_handling import (
    RobustErrorHandler,
    ErrorContext,
    retry_with_backoff,
    RetryConfig,
)


logger = logging.getLogger(__name__)


class SpotFleetManager:
    """Manager for AWS EC2 Fleet compute resources.

    Requests instances from several instance types at once, so an exhausted
    capacity pool degrades the fleet rather than failing it, and the allocation
    strategy can pick the pools least likely to be interrupted.

    Named for the legacy Spot Fleet API it was originally built on. It now calls
    ``CreateFleet`` (#86); the name is retained because it is reachable through
    the public ``use_spot_fleet`` provider kwarg and appears in persisted state.
    """

    def __init__(self, provider: Any) -> None:
        """Initialize the fleet manager.

        Parameters
        ----------
        provider : EphemeralAWSProvider
            The provider instance
        """
        self.provider = provider

        # Initialize error handling for spot fleet operations
        self.error_handler = RobustErrorHandler(
            retry_config=RetryConfig(
                max_attempts=6,  # Extra attempts for spot fleet due to market conditions
                base_delay=3.0,  # Longer delay for spot fleet operations
                exponential_backoff=True,
                jitter=True,
                max_delay=60.0,  # Cap at 1 minute for spot fleet
            )
        )
        logger.info("Error handler initialized for Spot Fleet operations")

        # Initialize security configuration and credential management
        self._setup_security_config()

        # Initialize audit logging
        self.audit_logger = self.security_config.get_audit_logger()
        if self.audit_logger:
            self.audit_logger.log_event(
                SecurityEvent(
                    event_type=SecurityEventType.CONFIG_CHANGE,
                    severity=SecurityEventSeverity.INFO,
                    message="SpotFleetManager initialized",
                    resource_type="spot_fleet_manager",
                    workflow_id=self.provider.workflow_id,
                    metadata={"provider_region": self.provider.region},
                )
            )
            logger.info("Audit logging enabled for Spot Fleet operations")

        # Initialize credential manager
        credential_config = self.security_config.get_credential_configuration()

        # Override credential config with provider-specific settings if provided
        if hasattr(provider, "aws_access_key_id") or hasattr(provider, "aws_profile"):
            # Legacy credential handling - create credential config from provider settings
            credential_config = self._create_credential_config_from_provider()

        try:
            self.credential_manager = CredentialManager(credential_config)
            logger.info("Spot Fleet credential manager initialized successfully")

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

        # Resolve the AWS session. The caller's own session takes precedence;
        # the credential manager is only a fallback for a provider that has
        # none. Going straight to the credential manager discarded an
        # explicitly configured session -- role credentials, a chosen profile,
        # a LocalStack endpoint -- in favour of ambient environment
        # credentials, so operations could land in a different account than the
        # caller selected (#117).
        try:
            self.aws_session = resolve_manager_session(
                self.provider, self.credential_manager
            )
        except NoCredentialsError as e:
            logger.error(f"No valid AWS credentials found: {e}")
            raise ResourceCreationError(f"AWS credential error: {e}")

        # Initialize clients
        self.ec2_client = self.aws_session.client("ec2")
        self.ec2_resource = self.aws_session.resource("ec2")

        # Track resources for cleanup
        self.vpc_id = None
        self.subnet_id = None
        self.security_group_id = None
        # Retained for state compatibility only. CreateFleet needs no service
        # role -- there is no IamFleetRole member on the request at all -- so
        # nothing sets this any more (#86). A resumed pre-#86 state document may
        # still carry a role ARN, and cleanup_all_resources() will delete the
        # role it names.
        self.iam_fleet_role_arn = None
        # fleet ID -> fleet record. Keyed by EC2 Fleet ID since #86; the
        # attribute name is unchanged because it is persisted in state.
        self.fleet_requests: Dict[str, Any] = {}
        self.instances: Dict[str, Any] = {}
        self.blocks: Dict[str, Any] = {}
        # block_id -> launch template ID, so each block's template is deleted
        # when the block is terminated (#85).
        self.launch_templates: Dict[str, str] = {}

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
            f"Spot Fleet Security configuration: environment={self.security_config.environment.value}, "
            f"strict_mode={self.security_config.strict_mode}"
        )

        # Analyze security posture
        analysis = self.security_config.analyze_security_posture()
        for warning in analysis.get("warnings", []):
            logger.warning(f"Spot Fleet Security warning: {warning}")
        for rec in analysis.get("recommendations", []):
            logger.info(f"Spot Fleet Security recommendation: {rec}")

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
            f"Spot Fleet Created credential config: role_arn={bool(role_arn)}, "
            f"profile={aws_profile}, use_env={use_env_vars}"
        )

        return config

    def _setup_network_resources(self) -> Dict[str, str]:
        """Resolve the caller-supplied VPC, subnet, and security group.

        Network resources are pre-provisioned by the caller and passed in via
        the provider; this manager never creates or deletes them.

        Returns
        -------
        Dict[str, str]
            Dictionary containing VPC ID, subnet ID, and security group ID

        Raises
        ------
        ResourceCreationError
            If the provider is missing any of the three required IDs
        """
        self.vpc_id = self.vpc_id or self.provider.vpc_id
        self.subnet_id = self.subnet_id or self.provider.subnet_id
        self.security_group_id = (
            self.security_group_id or self.provider.security_group_id
        )

        missing = [
            name
            for name, value in (
                ("vpc_id", self.vpc_id),
                ("subnet_id", self.subnet_id),
                ("security_group_id", self.security_group_id),
            )
            if not value
        ]
        if missing:
            raise ResourceCreationError(
                "EC2 Fleet requires pre-provisioned network resources; "
                f"missing: {', '.join(missing)}"
            )

        logger.info(
            f"Using network resources: vpc={self.vpc_id}, subnet={self.subnet_id}, "
            f"sg={self.security_group_id}"
        )

        return {
            "vpc_id": self.vpc_id,
            "subnet_id": self.subnet_id,
            "security_group_id": self.security_group_id,
        }

    def _generate_user_data(self) -> str:
        """Generate user data script for instance initialization.

        Returns
        -------
        str
            User data script
        """
        user_data = "#!/bin/bash\n"
        user_data += (
            f"echo 'Starting Parsl worker setup for {self.provider.workflow_id}'\n"
        )

        # Add worker initialization commands
        if self.provider.worker_init:
            user_data += f"\n# User-provided worker initialization\n{self.provider.worker_init}\n"

        # Add Parsl worker setup commands
        # In a real implementation, this would configure and start the Parsl worker

        return user_data

    def _translate_fleet_error(
        self, exc: ClientError, context: ErrorContext
    ) -> Exception:
        """Map a ``CreateFleet`` ``ClientError`` onto this package's exceptions.

        Returned rather than raised so the caller keeps its ``raise ... from``
        chain, which is what preserves the original botocore traceback.

        Parameters
        ----------
        exc : ClientError
            The error EC2 returned.
        context : ErrorContext
            Error context. Every error is recorded against it, whichever branch
            below classifies it.

        Returns
        -------
        Exception
            The exception the caller should raise.
        """
        # Record before classifying, so the recognized families land in
        # error_history too. Recording only the unrecognized fallthrough had it
        # backwards (#120): capacity shortages, quota rejections, and throttling
        # are precisely the failures worth counting, because a caller deciding
        # whether to back off, diversify instance types, or ask for a limit
        # increase reads them out of the history. An error nobody classified is
        # the least actionable of the lot.
        #
        # handle_error also attempts recovery for RETRY/FALLBACK actions, but
        # ErrorRecoveryHandler registers no strategy for "create_ec2_fleet", so
        # attempt_recovery returns False without doing anything. This stays a
        # recording call; retries are @retry_with_backoff on create_blocks.
        self.error_handler.handle_error(exc, context)

        error_code = exc.response["Error"]["Code"]

        if error_code in (
            "InvalidLaunchTemplateId.NotFound",
            "InvalidLaunchTemplateName.NotFoundException",
            "InvalidLaunchTemplateId.VersionNotFound",
        ):
            return SpotFleetRequestError(f"Launch template configuration error: {exc}")
        if error_code in (
            "InsufficientInstanceCapacity",
            "InsufficientReservedInstanceCapacity",
        ):
            return SpotFleetError(f"Insufficient instance capacity: {exc}")
        if error_code in (
            "InvalidParameter",
            "InvalidParameterValue",
            # CreateFleet's own configuration rejection, e.g. duplicate instance
            # pools in Overrides. The legacy API called this
            # InvalidSpotFleetRequestConfig, a code CreateFleet never returns.
            "InvalidFleetConfig",
        ):
            # Where a rejected allocation strategy, or a parameter the instant
            # fleet type does not accept, lands.
            return SpotFleetRequestError(f"Invalid fleet configuration: {exc}")
        if error_code in ("MaxSpotInstanceCountExceeded", "VcpuLimitExceeded"):
            # An account quota, not a transient shortage: retrying cannot help,
            # and the distinction from InsufficientInstanceCapacity is what tells
            # a caller to request a limit increase rather than back off. These are
            # the codes CreateFleet actually returns; the legacy
            # InstanceLimitExceeded belonged to RunInstances.
            return SpotFleetRequestError(f"Spot quota exceeded: {exc}")
        if error_code in ("Throttling", "RequestLimitExceeded"):
            retry_after = exc.response.get("ResponseMetadata", {}).get("RetryAfter", 60)
            return SpotFleetThrottlingError(
                message=f"AWS throttled the fleet request: {exc}",
                operation="create_fleet",
                retry_after=retry_after,
            )

        return SpotFleetError(f"Failed to create EC2 Fleet: {exc}")

    @retry_with_backoff()
    def create_blocks(self, count: int) -> Dict[str, Dict[str, Any]]:
        """Create compute blocks, one EC2 Fleet each.

        Parameters
        ----------
        count : int
            Number of blocks to create

        Returns
        -------
        Dict[str, Dict[str, Any]]
            Dictionary mapping block IDs to block information
        """
        blocks = {}

        try:
            # Ensure network resources exist
            network = self._setup_network_resources()

            # Create blocks. No IAM service role is fetched first: CreateFleet
            # has no IamFleetRole member, so the role the legacy API required is
            # simply not part of this path any more (#86).
            for _ in range(count):
                block_id = str(uuid.uuid4())

                # Create an EC2 Fleet for the block. Type "instant" returns its
                # instance IDs synchronously, so there is nothing to wait for.
                fleet_id, instance_ids = self._create_fleet(
                    block_id, network, self.provider.nodes_per_block
                )

                # Record block information
                self.blocks[block_id] = {
                    "id": block_id,
                    "fleet_request_id": fleet_id,
                    "status": STATUS_RUNNING if instance_ids else STATUS_PENDING,
                    "instance_ids": instance_ids,
                    "created_at": time.time(),
                }

                blocks[block_id] = self.blocks[block_id]

            return blocks

        except Exception as e:
            logger.error(f"Error creating blocks: {e}")

            # Clean up any partially created resources
            for block_id, block_info in blocks.items():
                try:
                    self.terminate_block(block_id)
                except Exception as cleanup_e:
                    logger.error(f"Error cleaning up block {block_id}: {cleanup_e}")

            raise ResourceCreationError(f"Failed to create blocks: {e}")

    def _build_launch_template_config(
        self,
        block_id: str,
        network: Dict[str, str],
        instance_types: List[str],
        instance_tags: List[Dict[str, str]],
    ) -> List[Dict[str, Any]]:
        """Build the ``LaunchTemplateConfigs`` for this block's fleet.

        One template per block, because the user data is per-block and the
        ``Overrides`` shape cannot carry it. The instance types become overrides
        so a single template still covers every pool the fleet may draw from.

        A template is mandatory rather than preferred: ``CreateFleet`` has no
        ``LaunchSpecifications`` member at all, so unlike the legacy
        ``RequestSpotFleet`` path there is nothing to fall back to (#86). That
        removes the old fallback's silent IMDSv2 downgrade -- failing to build
        the template now fails the block, which is the correct outcome.

        Parameters
        ----------
        block_id : str
            Block this template serves; also names the template.
        network : Dict[str, str]
            Resolved ``subnet_id`` and ``security_group_id``.
        instance_types : List[str]
            Types to emit as overrides.
        instance_tags : List[Dict[str, str]]
            Tags applied to launched instances.

        Returns
        -------
        List[Dict[str, Any]]
            A single-element ``LaunchTemplateConfigs`` list.

        Raises
        ------
        ResourceCreationError
            If the launch template cannot be created.
        """
        name = f"{LAUNCH_TEMPLATE_NAME_PREFIX}-fleet-{block_id}"
        template_data = build_launch_template_data(
            image_id=self.provider.image_id,
            instance_type=instance_types[0],
            subnet_id=network["subnet_id"],
            security_group_id=network["security_group_id"],
            associate_public_ip=getattr(self.provider, "use_public_ips", True),
            key_name=getattr(self.provider, "key_name", None),
            iam_instance_profile_arn=getattr(
                self.provider, "iam_instance_profile_arn", None
            ),
            # Fleet instances are reclaimed by deleting the fleet, which always
            # terminates them, so an instance that shuts itself down should
            # terminate too rather than linger as a billed volume.
            shutdown_behavior="terminate",
            user_data=self._generate_user_data(),
        )
        # Instances launched from the template carry the block tags; the
        # template resource itself carries them too, so a leaked template is
        # traceable to the workflow that made it.
        template_data["TagSpecifications"] = [
            {"ResourceType": "instance", "Tags": list(instance_tags)}
        ]

        template_id, version = create_launch_template(
            self.ec2_client, name, template_data, list(instance_tags)
        )
        self.launch_templates[block_id] = template_id
        logger.debug(
            f"Created launch template {template_id} for fleet block {block_id}"
        )
        return build_fleet_launch_template_configs(
            template_id, version, instance_types, network["subnet_id"]
        )

    def _delete_launch_template_for_block(self, block_id: str) -> None:
        """Delete the launch template created for *block_id*, if any.

        Deleting a template does not affect instances already launched from it,
        so this is safe as soon as the fleet is deleted.
        """
        template_id = self.launch_templates.pop(block_id, None)
        if not template_id:
            return
        try:
            delete_launch_template(self.ec2_client, template_id)
        except Exception as e:
            logger.warning(
                f"Failed to delete launch template {template_id} for block "
                f"{block_id}: {e}"
            )

    def _resolve_max_total_price(self, target_capacity: int) -> Optional[str]:
        """Translate ``spot_max_price_percentage`` into a fleet ``MaxTotalPrice``.

        Returns None when the provider sets no cap, which is the recommended
        configuration -- AWS: "We do not recommend using this parameter because
        it can lead to increased interruptions." A fleet with no maximum simply
        pays the prevailing spot price, which is already far below on-demand.

        The percentage is of on-demand, matching what the setting has always
        documented. There is no cheap API for an on-demand price, so the legacy
        3x-current-spot proxy is kept rather than adding a Pricing API call to
        the launch path. Unlike the legacy ``SpotPrice``, which was per instance
        hour, ``MaxTotalPrice`` covers the whole fleet, so it is multiplied by
        the capacity being requested.

        Parameters
        ----------
        target_capacity : int
            Instances this fleet will request.

        Returns
        -------
        Optional[str]
            The fleet-wide hourly maximum, or None for no cap.
        """
        percentage = getattr(self.provider, "spot_max_price_percentage", None)
        if not percentage:
            return None

        try:
            history = self.ec2_client.describe_spot_price_history(
                InstanceTypes=[self.provider.instance_type],
                ProductDescriptions=["Linux/UNIX"],
                MaxResults=1,
            ).get("SpotPriceHistory", [])
            current_spot = float(history[0]["SpotPrice"]) if history else 1.0
            # Use 3x current spot as a conservative on-demand proxy
            on_demand_price = current_spot * 3
        except Exception:
            on_demand_price = 1.0  # safe fallback

        per_instance = on_demand_price * (percentage / 100.0)
        return str(per_instance * target_capacity)

    def _create_fleet(
        self,
        block_id: str,
        network: Dict[str, str],
        target_capacity: int,
    ) -> Tuple[str, List[str]]:
        """Create an EC2 Fleet for *block_id* and return its ID and instances.

        Replaces the legacy ``RequestSpotFleet`` builder (#86). The differences
        that matter to callers: there is no IAM service role to supply, the
        launch template is mandatory rather than preferred, and the instance IDs
        come back from the create call itself instead of from a polling loop.

        Parameters
        ----------
        block_id : str
            ID of the block this fleet backs.
        network : Dict[str, str]
            Resolved ``subnet_id`` and ``security_group_id``.
        target_capacity : int
            Number of instances to request.

        Returns
        -------
        Tuple[str, List[str]]
            The fleet ID, and the instances it launched. The list can be short
            or empty if EC2 could not fill the request.

        Raises
        ------
        SpotFleetError
            Or a subclass, for any failure to create the fleet.
        """
        # Idempotency token, so a retried create does not double-launch.
        client_token = f"{self.provider.workflow_id}-{block_id}"

        # Use instance types from provider, falling back to the single primary type.
        # Do not attempt to synthesize alternatives from the instance type string
        # (e.g. slicing family/generation chars) — that approach breaks for
        # multi-char families (m5a, c6g) and produces invalid type names.
        instance_types = (
            self.provider.instance_types
            if (
                hasattr(self.provider, "instance_types")
                and self.provider.instance_types
            )
            else [self.provider.instance_type]
        )

        instance_tags = [
            {"Key": "Name", "Value": f"{TAG_PREFIX}-node-{block_id[:8]}"},
            {"Key": TAG_MANAGED, "Value": "true"},
            {"Key": TAG_WORKFLOW_ID, "Value": self.provider.workflow_id},
            {"Key": TAG_BLOCK_ID, "Value": block_id},
        ]
        for key, value in self.provider.tags.items():
            instance_tags.append({"Key": key, "Value": value})

        # The launch template is the only launch form CreateFleet accepts, and
        # the only way the per-block user data and IMDSv2 reach the instances:
        # the Overrides shape carries just InstanceType/SubnetId/price/priority.
        launch_template_configs = self._build_launch_template_config(
            block_id, network, instance_types, instance_tags
        )

        # The fleet resource carries its own Name so it is distinguishable from
        # the instances in the console, but shares the marker and workflow tags
        # so one sweep finds both.
        fleet_tags = [
            {"Key": "Name", "Value": f"{TAG_PREFIX}-fleet-{block_id[:8]}"},
            {"Key": TAG_MANAGED, "Value": "true"},
            {"Key": TAG_WORKFLOW_ID, "Value": self.provider.workflow_id},
            {"Key": TAG_BLOCK_ID, "Value": block_id},
        ]
        for key, value in self.provider.tags.items():
            fleet_tags.append({"Key": key, "Value": value})

        context = ErrorContext(
            operation="create_ec2_fleet",
            resource_type="ec2_fleet",
            resource_id=block_id,
            metadata={"instance_types": instance_types},
        )

        try:
            fleet_id, instance_ids = create_ec2_fleet(
                self.ec2_client,
                launch_template_configs=launch_template_configs,
                target_capacity=target_capacity,
                allocation_strategy=getattr(
                    self.provider,
                    "spot_allocation_strategy",
                    DEFAULT_SPOT_ALLOCATION_STRATEGY,
                ),
                client_token=client_token,
                tags=fleet_tags,
                max_total_price=self._resolve_max_total_price(target_capacity),
            )
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            error_msg = e.response["Error"]["Message"]
            logger.error(f"Error creating EC2 Fleet: {error_code} - {error_msg}")

            if self.audit_logger:
                self.audit_logger.log_resource_operation(
                    operation="create",
                    resource_type="spot_fleet",
                    resource_id=f"{block_id}-failed",
                    success=False,
                    workflow_id=self.provider.workflow_id,
                    block_id=block_id,
                    error=f"{error_code}: {error_msg}",
                )

            # The template is useless without the fleet it was built for, and
            # nothing else will reclaim it once this block is abandoned.
            self._delete_launch_template_for_block(block_id)
            raise self._translate_fleet_error(e, context) from e

        self.fleet_requests[fleet_id] = {
            "id": fleet_id,
            "block_id": block_id,
            "target_capacity": target_capacity,
            "status": STATUS_RUNNING if instance_ids else STATUS_PENDING,
            "instance_ids": instance_ids,
            "created_at": time.time(),
        }

        for instance_id in instance_ids:
            self.instances[instance_id] = {
                "id": instance_id,
                "block_id": block_id,
                "fleet_request_id": fleet_id,
                "type": RESOURCE_TYPE_SPOT_FLEET,
                "status": "running",
            }

        if self.audit_logger:
            self.audit_logger.log_resource_operation(
                operation="create",
                resource_type="spot_fleet",
                resource_id=fleet_id,
                success=True,
                workflow_id=self.provider.workflow_id,
                block_id=block_id,
                target_capacity=target_capacity,
                instance_types=instance_types,
                instance_ids=instance_ids,
            )

        return fleet_id, instance_ids

    def get_block_status(self, block_id: str) -> str:
        """Get the status of a block.

        An instant fleet does not maintain capacity, so its ``FleetState`` stays
        ``active`` for the life of the fleet regardless of what happened to the
        instances. The block's status therefore comes from the instances, with
        the fleet state consulted only for the terminal cases.

        Parameters
        ----------
        block_id : str
            ID of the block to check

        Returns
        -------
        str
            Block status
        """
        if block_id not in self.blocks:
            return STATUS_UNKNOWN

        fleet_id = self.blocks[block_id].get("fleet_request_id")
        if not fleet_id:
            return self.blocks[block_id].get("status", STATUS_UNKNOWN)

        try:
            fleet = describe_ec2_fleet(self.ec2_client, fleet_id)

            if fleet is None:
                # EC2 forgets a deleted fleet after roughly an hour, so an
                # unknown ID means the block is long gone rather than broken.
                self.blocks[block_id]["status"] = STATUS_COMPLETED
                return STATUS_COMPLETED

            fleet_state = fleet.get("FleetState")

            if fleet_state in ("deleted", "deleted_running", "deleted_terminating"):
                block_status = STATUS_CANCELLED
            elif fleet_state == "failed":
                block_status = STATUS_FAILED
            elif fleet_state in ("submitted", "modifying"):
                block_status = STATUS_PENDING
            else:
                block_status = self._status_from_instances(block_id, fleet_id)

            self.blocks[block_id]["status"] = block_status
            return block_status

        except Exception as e:
            logger.error(f"Error getting block status for {block_id}: {e}")
            return self.blocks[block_id].get("status", STATUS_UNKNOWN)

    def _status_from_instances(self, block_id: str, fleet_id: str) -> str:
        """Derive a block's status from the states of its instances.

        Parameters
        ----------
        block_id : str
            Block being examined; its ``instance_ids`` are refreshed.
        fleet_id : str
            Fleet backing the block, queried when the block has no instances
            recorded yet.

        Returns
        -------
        str
            One of the ``STATUS_*`` constants.
        """
        instance_ids = self.blocks[block_id].get("instance_ids", [])

        if not instance_ids:
            # A block restored from state, or one whose create call came back
            # unfilled, has nothing recorded; ask EC2 via the fleet-id tag.
            instance_ids = get_ec2_fleet_instance_ids(self.ec2_client, fleet_id)
            self.blocks[block_id]["instance_ids"] = instance_ids

        if not instance_ids:
            return STATUS_PENDING

        try:
            response = self.ec2_client.describe_instances(InstanceIds=instance_ids)
        except ClientError as e:
            if e.response["Error"]["Code"] == "InvalidInstanceID.NotFound":
                # EC2 drops terminated instances entirely after about an hour.
                return STATUS_COMPLETED
            logger.error(f"Error checking instance status for block {block_id}: {e}")
            return STATUS_RUNNING  # Assume running if we cannot check
        except Exception as e:
            logger.error(f"Error checking instance status for block {block_id}: {e}")
            return STATUS_RUNNING

        states: Dict[str, int] = {}
        for reservation in response.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                state = instance["State"]["Name"]
                states[state] = states.get(state, 0) + 1

        if states.get("running", 0) == len(instance_ids):
            return STATUS_RUNNING
        if states.get("terminated", 0) + states.get("shutting-down", 0) == len(
            instance_ids
        ):
            return STATUS_COMPLETED
        if "running" in states:
            return STATUS_RUNNING
        return STATUS_PENDING

    def terminate_block(self, block_id: str) -> None:
        """Terminate a compute block.

        Parameters
        ----------
        block_id : str
            ID of the block to terminate

        Raises
        ------
        SpotFleetThrottlingError
            If AWS throttled the deletion. Carries ``retry_after``, so a caller
            can wait the interval AWS named rather than retry blind.
        SpotFleetError
            If EC2 refused the deletion for any other reason.
        ResourceCleanupError
            For a failure that is not EC2 refusing the deletion.
        """
        if block_id not in self.blocks:
            logger.warning(f"Block {block_id} not found")
            return

        fleet_id = self.blocks[block_id].get("fleet_request_id")
        if not fleet_id:
            logger.warning(f"No fleet ID found for block {block_id}")
            # Still drop the template: it belongs to the block, not to the
            # fleet, and a block whose fleet never got recorded would otherwise
            # leak it until cleanup_all_resources runs (#85).
            self._delete_launch_template_for_block(block_id)
            return

        try:
            # Deleting the fleet terminates its instances; that is not optional
            # for an instant fleet (#86).
            delete_ec2_fleet(self.ec2_client, fleet_id)

            logger.info(f"Deleted EC2 Fleet {fleet_id} for block {block_id}")

            if fleet_id in self.fleet_requests:
                self.fleet_requests[fleet_id]["status"] = STATUS_CANCELLED

            self.blocks[block_id]["status"] = STATUS_CANCELLED

            for instance_id in self.blocks[block_id].get("instance_ids", []):
                if instance_id in self.instances:
                    self.instances[instance_id]["status"] = "terminated"

            # The block's launch template has no further use once the fleet is
            # deleted, and deleting it does not disturb instances still shutting
            # down (#85).
            self._delete_launch_template_for_block(block_id)

        except ResourceDeletionError as e:
            # ``delete_ec2_fleet`` wraps the ClientError, so the original is
            # reached through __cause__ rather than by catching ClientError here.
            # Catching ClientError instead left this whole branch dead: the
            # wrapper fell through to the ``except Exception`` below and every
            # refused deletion -- throttling included -- reported as a generic
            # ResourceCleanupError, discarding the retry_after AWS supplied.
            cause = e.__cause__
            error_code = ""
            error_msg = str(e)
            response: Dict[str, Any] = {}
            if isinstance(cause, ClientError):
                response = cause.response
                error_code = response["Error"]["Code"]
                error_msg = response["Error"]["Message"]

            logger.error(
                f"Error terminating block {block_id}: {error_code} - {error_msg}"
            )

            if error_code in ("Throttling", "RequestLimitExceeded"):
                retry_after = response.get("ResponseMetadata", {}).get("RetryAfter", 60)
                raise SpotFleetThrottlingError(
                    message=f"AWS throttled the fleet deletion: {error_msg}",
                    operation="delete_fleets",
                    retry_after=retry_after,
                ) from e
            raise SpotFleetError(
                f"Failed to terminate EC2 Fleet {fleet_id}: {error_code} - {error_msg}"
            ) from e
        except Exception as e:
            logger.error(f"Error terminating block {block_id}: {e}")
            raise ResourceCleanupError(
                f"Failed to terminate block {block_id}: {e}"
            ) from e

    def cleanup_all_resources(self) -> None:
        """Clean up all AWS resources created by this manager."""
        try:
            # Delete each fleet individually. ``delete_fleets`` accepts a list,
            # but reports per-fleet failures in UnsuccessfulFleetDeletions rather
            # than failing the call, so one loop iteration per fleet keeps a
            # single stuck fleet from hiding the others.
            for fleet_id in list(self.fleet_requests):
                try:
                    delete_ec2_fleet(self.ec2_client, fleet_id)
                    self.fleet_requests[fleet_id]["status"] = STATUS_CANCELLED
                    logger.info(f"Deleted EC2 Fleet {fleet_id}")
                except Exception as e:
                    logger.error(f"Error deleting EC2 Fleet {fleet_id}: {e}")

            # Delete every per-block launch template (#85). Iterated over a copy
            # of the keys because the helper pops from the dict as it goes.
            for block_id in list(self.launch_templates):
                self._delete_launch_template_for_block(block_id)

            # Clean up the legacy Spot Fleet service role, which only a state
            # document written before #86 can still name. CreateFleet has no
            # IamFleetRole, so nothing creates this any more -- but a workflow
            # resumed across the upgrade would otherwise leak the role.
            if self.iam_fleet_role_arn:
                role_name = self.iam_fleet_role_arn.split("/")[-1]
                try:
                    iam_client = self.aws_session.client("iam")

                    # Check if role exists
                    role_exists = True
                    try:
                        iam_client.get_role(RoleName=role_name)
                    except ClientError as e:
                        if e.response["Error"]["Code"] == "NoSuchEntity":
                            role_exists = False
                            logger.warning(
                                f"IAM role {role_name} not found, skipping cleanup"
                            )
                        else:
                            logger.error(
                                f"Error checking if role {role_name} exists: {e}"
                            )

                    if role_exists:
                        # Detach policies
                        try:
                            iam_client.detach_role_policy(
                                RoleName=role_name,
                                PolicyArn="arn:aws:iam::aws:policy/service-role/AmazonEC2SpotFleetTaggingRole",
                            )
                            logger.info(f"Detached policy from IAM role: {role_name}")
                        except ClientError as e:
                            error_code = e.response["Error"]["Code"]
                            error_msg = e.response["Error"]["Message"]
                            logger.error(
                                f"Error detaching policy from role {role_name}: {error_code} - {error_msg}"
                            )

                        # Wait briefly for policy detachment to propagate
                        time.sleep(2)

                        # Delete role
                        try:
                            iam_client.delete_role(RoleName=role_name)
                            logger.info(f"Deleted IAM role: {role_name}")
                        except ClientError as e:
                            error_code = e.response["Error"]["Code"]
                            error_msg = e.response["Error"]["Message"]

                            if error_code == "DeleteConflict":
                                logger.warning(
                                    f"Cannot delete role {role_name} as it still has attached entities"
                                )
                                # Try to list and detach all policies
                                try:
                                    attached_policies = (
                                        iam_client.list_attached_role_policies(
                                            RoleName=role_name
                                        )
                                    )
                                    for policy in attached_policies.get(
                                        "AttachedPolicies", []
                                    ):
                                        iam_client.detach_role_policy(
                                            RoleName=role_name,
                                            PolicyArn=policy["PolicyArn"],
                                        )
                                        logger.info(
                                            f"Detached policy {policy['PolicyName']} from role {role_name}"
                                        )

                                    # Try deletion again after detaching policies
                                    time.sleep(2)
                                    iam_client.delete_role(RoleName=role_name)
                                    logger.info(
                                        f"Deleted IAM role: {role_name} after detaching all policies"
                                    )
                                except Exception as policy_e:
                                    logger.error(
                                        f"Error detaching policies from role {role_name}: {policy_e}"
                                    )
                            elif error_code == "NoSuchEntity":
                                logger.info(f"IAM role {role_name} already deleted")
                            else:
                                logger.error(
                                    f"Error deleting IAM role {role_name}: {error_code} - {error_msg}"
                                )
                except Exception as e:
                    logger.error(f"Error cleaning up IAM role {role_name}: {e}")

            # The VPC, subnet, and security group are supplied by the caller and
            # are deliberately left untouched.

        except Exception as e:
            logger.error(f"Error cleaning up resources: {e}")
            raise ResourceCleanupError(f"Failed to clean up resources: {e}")

    def get_instance_public_ip(self, instance_id: str) -> Optional[str]:
        """Get the public IP address of an instance.

        Parameters
        ----------
        instance_id : str
            ID of the instance

        Returns
        -------
        Optional[str]
            Public IP address, or None if not available
        """
        try:
            response = self.ec2_client.describe_instances(InstanceIds=[instance_id])

            if response["Reservations"] and response["Reservations"][0]["Instances"]:
                instance = response["Reservations"][0]["Instances"][0]
                public_ip = instance.get("PublicIpAddress")

                # Update instance information
                if instance_id in self.instances:
                    self.instances[instance_id]["public_ip"] = public_ip
                    self.instances[instance_id]["private_ip"] = instance.get(
                        "PrivateIpAddress"
                    )
                    self.instances[instance_id]["status"] = instance["State"]["Name"]

                return public_ip

            return None

        except Exception as e:
            logger.error(f"Error getting public IP for instance {instance_id}: {e}")
            return None

    def get_instance_private_ip(self, instance_id: str) -> Optional[str]:
        """Get the private IP address of an instance.

        Parameters
        ----------
        instance_id : str
            ID of the instance

        Returns
        -------
        Optional[str]
            Private IP address, or None if not available
        """
        try:
            response = self.ec2_client.describe_instances(InstanceIds=[instance_id])

            if response["Reservations"] and response["Reservations"][0]["Instances"]:
                instance = response["Reservations"][0]["Instances"][0]
                private_ip = instance.get("PrivateIpAddress")

                # Update instance information
                if instance_id in self.instances:
                    self.instances[instance_id]["private_ip"] = private_ip
                    self.instances[instance_id]["public_ip"] = instance.get(
                        "PublicIpAddress"
                    )
                    self.instances[instance_id]["status"] = instance["State"]["Name"]

                return private_ip

            return None

        except Exception as e:
            logger.error(f"Error getting private IP for instance {instance_id}: {e}")
            return None

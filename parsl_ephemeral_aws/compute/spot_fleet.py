"""Spot Fleet compute resource implementation for Parsl Ephemeral AWS Provider.

This module provides enhanced spot instance management using AWS EC2 Spot Fleet,
offering better reliability and flexibility than individual spot requests.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import logging
import uuid
import time
import json
from typing import Dict, List, Optional, Any

from botocore.exceptions import ClientError, NoCredentialsError

from ..exceptions import (
    ResourceCreationError,
    ResourceCleanupError,
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
    build_launch_template_data,
    create_launch_template,
    delete_launch_template,
    encode_user_data,
    normalize_spot_fleet_allocation_strategy,
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
    """Manager for AWS EC2 Spot Fleet compute resources.

    This manager provides enhanced spot instance management using EC2 Spot Fleet,
    which offers better reliability and cost optimization compared to individual
    spot requests. Spot Fleet can automatically request instances from multiple
    instance types, availability zones, and purchase options to meet capacity
    requirements at the lowest possible cost.
    """

    def __init__(self, provider: Any) -> None:
        """Initialize the Spot Fleet manager.

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
        self.iam_fleet_role_arn = None
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
                "Spot Fleet requires pre-provisioned network resources; "
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

    def _get_iam_fleet_role(self) -> str:
        """Get or create an IAM role for the Spot Fleet.

        Returns
        -------
        str
            ARN of the IAM role
        """
        # Check if we already have a fleet role
        if self.iam_fleet_role_arn:
            return self.iam_fleet_role_arn

        iam_client = self.aws_session.client("iam")
        role_name = f"{TAG_PREFIX}-spot-fleet-role-{self.provider.workflow_id[:8]}"

        try:
            # Check if the role already exists
            try:
                response = iam_client.get_role(RoleName=role_name)
                self.iam_fleet_role_arn = response["Role"]["Arn"]
                logger.info(f"Using existing IAM role for Spot Fleet: {role_name}")
                return self.iam_fleet_role_arn
            except ClientError as e:
                if e.response["Error"]["Code"] != "NoSuchEntity":
                    raise

                # Role doesn't exist, create it
                trust_policy = {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "spotfleet.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                }

                response = iam_client.create_role(
                    RoleName=role_name,
                    AssumeRolePolicyDocument=json.dumps(trust_policy),
                    Description=f"Role for Parsl Spot Fleet ({self.provider.workflow_id})",
                )

                # Attach the required policy
                iam_client.attach_role_policy(
                    RoleName=role_name,
                    PolicyArn="arn:aws:iam::aws:policy/service-role/AmazonEC2SpotFleetTaggingRole",
                )

                # Get the role ARN
                self.iam_fleet_role_arn = response["Role"]["Arn"]
                logger.info(f"Created IAM role for Spot Fleet: {role_name}")

                # Wait for role to be ready (IAM changes can take time to propagate)
                time.sleep(10)

                return self.iam_fleet_role_arn

        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            error_msg = e.response["Error"]["Message"]
            logger.error(
                f"Error getting or creating IAM role for Spot Fleet: {error_code} - {error_msg}"
            )

            if error_code == "AccessDenied":
                raise SpotFleetError(
                    f"Access denied when creating IAM role for Spot Fleet. Ensure your AWS credentials "
                    f"have IAM permissions: {error_msg}"
                )
            elif error_code == "EntityAlreadyExists":
                # Role already exists but we failed to get it
                logger.warning(
                    f"IAM role {role_name} already exists but could not be retrieved"
                )
                try:
                    # Try to get the role again
                    response = iam_client.get_role(RoleName=role_name)
                    self.iam_fleet_role_arn = response["Role"]["Arn"]
                    logger.info(
                        f"Retrieved existing IAM role for Spot Fleet: {role_name}"
                    )
                    return self.iam_fleet_role_arn
                except Exception as retry_e:
                    logger.error(
                        f"Failed to retrieve existing IAM role {role_name}: {retry_e}"
                    )
                    raise SpotFleetError(
                        f"Failed to work with existing IAM role for Spot Fleet: {retry_e}"
                    )
            else:
                raise SpotFleetError(
                    f"Failed to create IAM role for Spot Fleet: {error_code} - {error_msg}"
                )
        except Exception as e:
            logger.error(f"Error getting or creating IAM role for Spot Fleet: {e}")
            raise SpotFleetError(
                f"Failed to get or create IAM role for Spot Fleet: {e}"
            )

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

    def _create_spot_fleet_with_retry(
        self, fleet_config: Dict[str, Any], context: ErrorContext
    ) -> Dict[str, Any]:
        """Create spot fleet request with error handling and retry logic.

        Parameters
        ----------
        fleet_config : Dict[str, Any]
            Spot fleet configuration
        context : ErrorContext
            Error context for tracking

        Returns
        -------
        Dict[str, Any]
            Spot fleet request response
        """
        try:
            response = self.ec2_client.request_spot_fleet(**fleet_config)
            return response
        except ClientError as e:
            error_code = e.response["Error"]["Code"]

            # Handle specific spot fleet errors
            if error_code in [
                "SpotFleetLaunchTemplateConfig.NotFound",
                "InvalidLaunchTemplateName.NotFound",
            ]:
                raise SpotFleetRequestError(f"Launch template configuration error: {e}")
            elif error_code in [
                "InsufficientInstanceCapacity",
                "InsufficientReservedInstanceCapacity",
            ]:
                raise SpotFleetError(f"Insufficient instance capacity: {e}")
            elif error_code in ["SpotFleetRequestConfig.InvalidLaunchSpecification"]:
                raise SpotFleetRequestError(f"Invalid launch specification: {e}")
            elif error_code in ["Throttling", "RequestLimitExceeded"]:
                raise SpotFleetThrottlingError(f"API throttling error: {e}")
            else:
                # Record error for analysis
                error_record = self.error_handler.handle_error(e, context)
                raise SpotFleetError(f"Failed to create spot fleet request: {e}")
        except Exception as e:
            error_record = self.error_handler.handle_error(e, context)
            raise SpotFleetError(f"Failed to create spot fleet request: {e}")

    @retry_with_backoff()
    def create_blocks(self, count: int) -> Dict[str, Dict[str, Any]]:
        """Create compute blocks using Spot Fleet.

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

            # Get IAM fleet role
            fleet_role_arn = self._get_iam_fleet_role()

            # Create blocks
            for _ in range(count):
                block_id = str(uuid.uuid4())

                # Create a Spot Fleet request for the block
                fleet_request_id = self._create_spot_fleet_request(
                    block_id, network, self.provider.nodes_per_block, fleet_role_arn
                )

                # Record block information
                self.blocks[block_id] = {
                    "id": block_id,
                    "fleet_request_id": fleet_request_id,
                    "status": STATUS_PENDING,
                    "created_at": time.time(),
                }

                blocks[block_id] = self.blocks[block_id]

                # Wait for instances to be created
                self._wait_for_fleet_instances(fleet_request_id, block_id)

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
    ) -> Optional[Dict[str, Any]]:
        """Build a ``LaunchTemplateConfig`` for this block, or None on failure.

        One template per block, because the user data is per-block and the
        ``Overrides`` shape cannot carry it -- it accepts only
        ``InstanceType``/``SubnetId``/price/priority. The instance types become
        overrides so a single template still covers every pool the fleet may
        draw from.

        Returns None if the template cannot be created, in which case the caller
        falls back to ``LaunchSpecifications``. That fallback launches without
        IMDSv2, which is unavoidable: ``SpotFleetLaunchSpecification`` has no
        ``MetadataOptions`` member.

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
        Optional[Dict[str, Any]]
            A ``LaunchTemplateConfig``, or None to use the legacy form.
        """
        name = f"{LAUNCH_TEMPLATE_NAME_PREFIX}-fleet-{block_id}"
        try:
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
                # Fleet instances are reclaimed by cancelling the fleet request
                # with TerminateInstances=True, so an instance that shuts itself
                # down should terminate rather than linger as a billed volume.
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
            return {
                "LaunchTemplateSpecification": {
                    "LaunchTemplateId": template_id,
                    "Version": version,
                },
                "Overrides": [
                    {
                        "InstanceType": instance_type,
                        "SubnetId": network["subnet_id"],
                    }
                    for instance_type in instance_types
                ],
            }
        except Exception as e:
            logger.warning(
                f"Could not create launch template for block {block_id}: {e}. "
                "Falling back to LaunchSpecifications; IMDSv2 will not be "
                "enforced on these instances."
            )
            return None

    def _delete_launch_template_for_block(self, block_id: str) -> None:
        """Delete the launch template created for *block_id*, if any.

        Deleting a template does not affect instances already launched from it,
        so this is safe as soon as the fleet request is cancelled.
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

    def _create_spot_fleet_request(
        self,
        block_id: str,
        network: Dict[str, str],
        target_capacity: int,
        fleet_role_arn: str,
    ) -> str:
        """Create a Spot Fleet request.

        Parameters
        ----------
        block_id : str
            ID of the block
        network : Dict[str, str]
            Network configuration
        target_capacity : int
            Number of instances to request

        Returns
        -------
        str
            Spot Fleet request ID
        """
        # Generate a unique client token
        client_token = f"{self.provider.workflow_id}-{block_id}"

        # Prepare launch specifications for multiple instance types
        launch_specifications = []

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

        # Prefer a launch template (#85). This is the only way to get IMDSv2 onto
        # Spot Fleet instances at all: SpotFleetLaunchSpecification has no
        # MetadataOptions member, and the LaunchTemplateConfig Overrides shape
        # carries only InstanceType/SubnetId/price/priority -- no UserData or
        # TagSpecifications -- so the per-block user data has to live in a
        # per-block template rather than in the overrides.
        launch_template_config = self._build_launch_template_config(
            block_id, network, instance_types, instance_tags
        )

        # Generate specs for each instance type
        for instance_type in instance_types:
            try:
                launch_spec = {
                    "ImageId": self.provider.image_id,
                    "InstanceType": instance_type,
                    "SubnetId": network["subnet_id"],
                    "SecurityGroups": [{"GroupId": network["security_group_id"]}],
                    "UserData": encode_user_data(self._generate_user_data()),
                    "TagSpecifications": [
                        {
                            "ResourceType": "instance",
                            "Tags": list(instance_tags),
                        }
                    ],
                }

                # Add key name if provided
                if self.provider.key_name:
                    launch_spec["KeyName"] = self.provider.key_name

                launch_specifications.append(launch_spec)
            except Exception as e:
                logger.warning(
                    f"Skipping instance type {instance_type} due to error: {e}"
                )

        # Prepare Spot Fleet request parameters
        request_params = {
            "SpotFleetRequestConfig": {
                "ClientToken": client_token,
                "TargetCapacity": target_capacity,
                "OnDemandTargetCapacity": 0,  # Use only spot instances
                "IamFleetRole": fleet_role_arn,
                "TerminateInstancesWithExpiration": True,
                "Type": "maintain",  # Maintain target capacity
                # Honour the provider's spot_allocation_strategy instead of
                # hardcoding lowestPrice, which ignored the documented setting
                # and chose the most interruption-prone pools (#84). Normalised
                # because RequestSpotFleet takes only the camelCase spelling.
                "AllocationStrategy": normalize_spot_fleet_allocation_strategy(
                    getattr(
                        self.provider,
                        "spot_allocation_strategy",
                        DEFAULT_SPOT_ALLOCATION_STRATEGY,
                    )
                ),
                "ReplaceUnhealthyInstances": True,
                "TagSpecifications": [
                    {
                        "ResourceType": "spot-fleet-request",
                        "Tags": [
                            {
                                "Key": "Name",
                                "Value": f"{TAG_PREFIX}-fleet-{block_id[:8]}",
                            },
                            {"Key": TAG_MANAGED, "Value": "true"},
                            {
                                "Key": TAG_WORKFLOW_ID,
                                "Value": self.provider.workflow_id,
                            },
                            {"Key": TAG_BLOCK_ID, "Value": block_id},
                        ],
                    }
                ],
            }
        }

        # Exactly one of the two launch forms. EC2 tolerates both keys being
        # present, but then the template silently loses to the specifications --
        # and with it IMDSv2 -- so only the preferred one is sent (#85).
        if launch_template_config:
            request_params["SpotFleetRequestConfig"]["LaunchTemplateConfigs"] = [
                launch_template_config
            ]
        else:
            request_params["SpotFleetRequestConfig"]["LaunchSpecifications"] = (
                launch_specifications
            )

        # Add provider tags to fleet request
        for key, value in self.provider.tags.items():
            request_params["SpotFleetRequestConfig"]["TagSpecifications"][0][
                "Tags"
            ].append({"Key": key, "Value": value})

        # Configure fleet to terminate instances when the request is cancelled
        request_params["SpotFleetRequestConfig"]["TerminateInstancesWithExpiration"] = (
            True
        )

        # Set a max price if specified
        if self.provider.spot_max_price_percentage:
            try:
                history = self.ec2_client.describe_spot_price_history(
                    InstanceTypes=[self.provider.instance_type],
                    ProductDescriptions=["Linux/UNIX"],
                    MaxResults=1,
                ).get("SpotPriceHistory", [])
                current_spot = float(history[0]["SpotPrice"]) if history else 1.0
                # Use 3× current spot as a conservative on-demand proxy
                on_demand_price = current_spot * 3
            except Exception:
                on_demand_price = 1.0  # safe fallback
            max_price = str(
                on_demand_price * (self.provider.spot_max_price_percentage / 100.0)
            )
            request_params["SpotFleetRequestConfig"]["SpotPrice"] = max_price

        try:
            # Create the Spot Fleet request
            response = self.ec2_client.request_spot_fleet(**request_params)

            fleet_request_id = response["SpotFleetRequestId"]
            logger.info(
                f"Created Spot Fleet request: {fleet_request_id} for block {block_id}"
            )

            # Record the request
            self.fleet_requests[fleet_request_id] = {
                "id": fleet_request_id,
                "block_id": block_id,
                "target_capacity": target_capacity,
                "status": "submitted",
                "instance_ids": [],
                "created_at": time.time(),
            }

            # Log successful spot fleet creation
            if self.audit_logger:
                self.audit_logger.log_resource_operation(
                    operation="create",
                    resource_type="spot_fleet",
                    resource_id=fleet_request_id,
                    success=True,
                    workflow_id=self.provider.workflow_id,
                    block_id=block_id,
                    target_capacity=target_capacity,
                    instance_types=instance_types,
                )

            return fleet_request_id

        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            error_msg = e.response["Error"]["Message"]
            logger.error(
                f"Error creating Spot Fleet request: {error_code} - {error_msg}"
            )

            # Log failed spot fleet creation
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

            # Handle specific error cases
            if error_code == "RequestLimitExceeded":
                # AWS is throttling our requests
                retry_after = e.response.get("ResponseMetadata", {}).get(
                    "RetryAfter", 60
                )
                raise SpotFleetThrottlingError(
                    message=f"AWS throttled Spot Fleet request: {error_msg}",
                    operation="request_spot_fleet",
                    retry_after=retry_after,
                )
            elif error_code == "InvalidIamInstanceProfileArn":
                # IAM role issue
                raise SpotFleetRequestError(
                    f"Invalid IAM role configuration: {error_msg}"
                )
            elif error_code == "InstanceLimitExceeded":
                # Instance limit reached
                raise SpotFleetRequestError(f"AWS instance limit exceeded: {error_msg}")
            elif "capacity-not-available" in error_msg.lower():
                # Not enough capacity for the request
                raise SpotFleetRequestError(
                    f"Insufficient capacity for Spot Fleet request: {error_msg}"
                )
            else:
                # General Spot Fleet error
                raise SpotFleetError(
                    f"Failed to create Spot Fleet request: {error_code} - {error_msg}"
                )

    def _wait_for_fleet_instances(
        self, fleet_request_id: str, block_id: str, max_wait: int = 300
    ) -> List[str]:
        """Wait for Spot Fleet instances to be created.

        Parameters
        ----------
        fleet_request_id : str
            Spot Fleet request ID
        block_id : str
            ID of the block
        max_wait : int, optional
            Maximum wait time in seconds, by default 300

        Returns
        -------
        List[str]
            List of instance IDs
        """
        start_time = time.time()
        instance_ids = []

        logger.info(f"Waiting for Spot Fleet instances for fleet {fleet_request_id}")

        while time.time() - start_time < max_wait:
            try:
                # Check fleet status
                response = self.ec2_client.describe_spot_fleet_requests(
                    SpotFleetRequestIds=[fleet_request_id]
                )

                if not response["SpotFleetRequestConfigs"]:
                    logger.warning(
                        f"No Spot Fleet request found with ID {fleet_request_id}"
                    )
                    break

                config = response["SpotFleetRequestConfigs"][0]
                status = config["SpotFleetRequestState"]
                activity_status = config.get("ActivityStatus")

                # Update fleet request info
                self.fleet_requests[fleet_request_id]["status"] = status

                if status == "active":
                    # Get instances in the fleet
                    instances_response = self.ec2_client.describe_spot_fleet_instances(
                        SpotFleetRequestId=fleet_request_id
                    )

                    # Extract instance IDs
                    instance_ids = [
                        instance["InstanceId"]
                        for instance in instances_response.get("ActiveInstances", [])
                    ]

                    # Update fleet request with instance IDs
                    self.fleet_requests[fleet_request_id]["instance_ids"] = instance_ids

                    # Update block status
                    self.blocks[block_id]["status"] = STATUS_RUNNING
                    self.blocks[block_id]["instance_ids"] = instance_ids

                    # Track instances
                    for instance_id in instance_ids:
                        if instance_id not in self.instances:
                            self.instances[instance_id] = {
                                "id": instance_id,
                                "block_id": block_id,
                                "fleet_request_id": fleet_request_id,
                                "type": RESOURCE_TYPE_SPOT_FLEET,
                                "status": "running",
                            }

                    logger.info(
                        f"Spot Fleet {fleet_request_id} is active with {len(instance_ids)} instances"
                    )
                    break

                elif status in [
                    "cancelled",
                    "cancelled_running",
                    "cancelled_terminating",
                    "error",
                ]:
                    logger.error(
                        f"Spot Fleet request {fleet_request_id} failed with status {status}"
                    )

                    # Update block status
                    self.blocks[block_id]["status"] = STATUS_FAILED

                    raise ResourceCreationError(
                        f"Spot Fleet request failed with status {status}"
                    )

                # Wait before checking again
                time.sleep(10)

            except ClientError as e:
                error_code = e.response["Error"]["Code"]
                error_msg = e.response["Error"]["Message"]
                logger.error(
                    f"Error checking Spot Fleet status: {error_code} - {error_msg}"
                )

                if error_code == "RequestLimitExceeded":
                    # AWS is throttling our requests, back off and retry
                    retry_after = e.response.get("ResponseMetadata", {}).get(
                        "RetryAfter", 10
                    )
                    logger.warning(
                        f"AWS throttled request, backing off for {retry_after} seconds"
                    )
                    time.sleep(retry_after)
                else:
                    # Other errors, wait a shorter time
                    time.sleep(10)

        # If we've waited too long without success
        if time.time() - start_time >= max_wait and not instance_ids:
            logger.error(
                f"Timeout waiting for Spot Fleet instances for fleet {fleet_request_id}"
            )

            # Update block status
            self.blocks[block_id]["status"] = STATUS_FAILED

            raise ResourceCreationError("Timeout waiting for Spot Fleet instances")

        return instance_ids

    def get_block_status(self, block_id: str) -> str:
        """Get the status of a block.

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

        # Get fleet request ID for this block
        fleet_request_id = self.blocks[block_id].get("fleet_request_id")
        if not fleet_request_id:
            return self.blocks[block_id].get("status", STATUS_UNKNOWN)

        try:
            # Check fleet status
            response = self.ec2_client.describe_spot_fleet_requests(
                SpotFleetRequestIds=[fleet_request_id]
            )

            if not response["SpotFleetRequestConfigs"]:
                # Fleet request not found
                self.blocks[block_id]["status"] = STATUS_COMPLETED
                return STATUS_COMPLETED

            config = response["SpotFleetRequestConfigs"][0]
            status = config["SpotFleetRequestState"]

            # Map EC2 status to our status
            if status == "active":
                # Check instance status
                instance_ids = self.blocks[block_id].get("instance_ids", [])

                if not instance_ids:
                    # No instances yet, check if any are associated with the fleet
                    try:
                        instances_response = (
                            self.ec2_client.describe_spot_fleet_instances(
                                SpotFleetRequestId=fleet_request_id
                            )
                        )

                        instance_ids = [
                            instance["InstanceId"]
                            for instance in instances_response.get(
                                "ActiveInstances", []
                            )
                        ]

                        # Update block with instance IDs
                        self.blocks[block_id]["instance_ids"] = instance_ids
                    except Exception as e:
                        logger.error(
                            f"Error getting instances for fleet {fleet_request_id}: {e}"
                        )

                if instance_ids:
                    # Check status of instances
                    try:
                        response = self.ec2_client.describe_instances(
                            InstanceIds=instance_ids
                        )

                        # Count instances by state
                        states = {}
                        for reservation in response.get("Reservations", []):
                            for instance in reservation.get("Instances", []):
                                state = instance["State"]["Name"]
                                states[state] = states.get(state, 0) + 1

                        # Determine overall status based on instance states
                        if states.get("running", 0) == len(instance_ids):
                            block_status = STATUS_RUNNING
                        elif states.get("terminated", 0) == len(instance_ids):
                            block_status = STATUS_COMPLETED
                        elif "running" in states:
                            block_status = STATUS_RUNNING
                        else:
                            block_status = STATUS_PENDING
                    except Exception as e:
                        logger.error(
                            f"Error checking instance status for block {block_id}: {e}"
                        )
                        block_status = (
                            STATUS_RUNNING  # Assume running if we can't check
                        )
                else:
                    block_status = STATUS_PENDING  # No instances yet
            elif status in ["submitted", "modifying"]:
                block_status = STATUS_PENDING
            elif status in ["cancelled", "cancelled_running", "cancelled_terminating"]:
                block_status = STATUS_CANCELLED
            elif status == "error":
                block_status = STATUS_FAILED
            else:
                block_status = STATUS_UNKNOWN

            # Update block status
            self.blocks[block_id]["status"] = block_status

            return block_status

        except Exception as e:
            logger.error(f"Error getting block status for {block_id}: {e}")
            return self.blocks[block_id].get("status", STATUS_UNKNOWN)

    def terminate_block(self, block_id: str) -> None:
        """Terminate a compute block.

        Parameters
        ----------
        block_id : str
            ID of the block to terminate
        """
        if block_id not in self.blocks:
            logger.warning(f"Block {block_id} not found")
            return

        # Get fleet request ID for this block
        fleet_request_id = self.blocks[block_id].get("fleet_request_id")
        if not fleet_request_id:
            logger.warning(f"No fleet request ID found for block {block_id}")
            # Still drop the template: it belongs to the block, not to the fleet
            # request, and a block whose request never got recorded would
            # otherwise leak it until cleanup_all_resources runs (#85).
            self._delete_launch_template_for_block(block_id)
            return

        try:
            # Cancel the Spot Fleet request
            self.ec2_client.cancel_spot_fleet_requests(
                SpotFleetRequestIds=[fleet_request_id],
                TerminateInstances=True,  # Terminate the instances as well
            )

            logger.info(
                f"Cancelled Spot Fleet request {fleet_request_id} for block {block_id}"
            )

            # Update fleet request status
            if fleet_request_id in self.fleet_requests:
                self.fleet_requests[fleet_request_id]["status"] = "cancelled"

            # Update block status
            self.blocks[block_id]["status"] = STATUS_CANCELLED

            # Update instance status
            instance_ids = self.blocks[block_id].get("instance_ids", [])
            for instance_id in instance_ids:
                if instance_id in self.instances:
                    self.instances[instance_id]["status"] = "terminated"

            # The block's launch template has no further use once the request is
            # cancelled, and deleting it does not disturb instances still
            # shutting down (#85).
            self._delete_launch_template_for_block(block_id)

        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            error_msg = e.response["Error"]["Message"]
            logger.error(
                f"Error terminating block {block_id}: {error_code} - {error_msg}"
            )

            if error_code == "RequestLimitExceeded":
                # AWS is throttling our requests
                retry_after = e.response.get("ResponseMetadata", {}).get(
                    "RetryAfter", 60
                )
                raise SpotFleetThrottlingError(
                    message=f"AWS throttled Spot Fleet termination request: {error_msg}",
                    operation="cancel_spot_fleet_requests",
                    retry_after=retry_after,
                )
            elif error_code == "InvalidSpotFleetRequestId.NotFound":
                # Fleet request ID not found, likely already terminated
                logger.warning(
                    f"Spot Fleet {fleet_request_id} not found, may already be terminated"
                )
                # Update our records anyway
                self.blocks[block_id]["status"] = STATUS_CANCELLED
                if fleet_request_id in self.fleet_requests:
                    self.fleet_requests[fleet_request_id]["status"] = "cancelled"
            else:
                # General error with termination
                raise SpotFleetError(
                    f"Failed to terminate Spot Fleet {fleet_request_id}: {error_code} - {error_msg}"
                )
        except Exception as e:
            logger.error(f"Error terminating block {block_id}: {e}")
            raise ResourceCleanupError(f"Failed to terminate block {block_id}: {e}")

    def cleanup_all_resources(self) -> None:
        """Clean up all AWS resources created by this manager."""
        try:
            # Cancel all Spot Fleet requests
            fleet_request_ids = list(self.fleet_requests.keys())
            if fleet_request_ids:
                try:
                    self.ec2_client.cancel_spot_fleet_requests(
                        SpotFleetRequestIds=fleet_request_ids, TerminateInstances=True
                    )
                    logger.info(
                        f"Cancelled {len(fleet_request_ids)} Spot Fleet requests"
                    )
                except ClientError as e:
                    error_code = e.response["Error"]["Code"]
                    error_msg = e.response["Error"]["Message"]
                    logger.error(
                        f"Error cancelling Spot Fleet requests: {error_code} - {error_msg}"
                    )

                    # If some fleet requests were not found, try to cancel the valid ones individually
                    if error_code == "InvalidSpotFleetRequestId.NotFound":
                        logger.warning(
                            "Some Spot Fleet requests not found, trying to cancel individually"
                        )
                        for fleet_id in fleet_request_ids:
                            try:
                                self.ec2_client.cancel_spot_fleet_requests(
                                    SpotFleetRequestIds=[fleet_id],
                                    TerminateInstances=True,
                                )
                                logger.info(f"Cancelled Spot Fleet request {fleet_id}")
                            except ClientError as individual_e:
                                ind_error_code = individual_e.response["Error"]["Code"]
                                logger.warning(
                                    f"Could not cancel Spot Fleet {fleet_id}: {ind_error_code}"
                                )
                except Exception as e:
                    logger.error(f"Error cancelling Spot Fleet requests: {e}")

            # Delete every per-block launch template (#85). Iterated over a copy
            # of the keys because the helper pops from the dict as it goes.
            for block_id in list(self.launch_templates):
                self._delete_launch_template_for_block(block_id)

            # Clean up IAM role if we created one
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

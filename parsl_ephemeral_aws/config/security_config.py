"""Security configuration schema for Parsl Ephemeral AWS Provider.

This module provides configuration classes for managing security settings
throughout the provider.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Union

from ..security import (
    NetworkSecurityPolicy,
    SecurityEnvironment,
    CredentialConfiguration,
    EncryptionConfiguration,
)
from ..constants import (
    DEFAULT_VPC_CIDR,
    DEFAULT_SECURITY_ENVIRONMENT,
    DEFAULT_ADMIN_CIDR_BLOCKS,
    DEFAULT_ALLOW_VPC_INTERNAL,
)

logger = logging.getLogger(__name__)


@dataclass
class SecurityConfig:
    """Security configuration for the Parsl Ephemeral AWS Provider.

    This class manages security settings and provides methods to create
    appropriate security policies for different environments.
    """

    # Environment type
    environment: Union[str, SecurityEnvironment] = DEFAULT_SECURITY_ENVIRONMENT

    # Network configuration
    vpc_cidr: str = DEFAULT_VPC_CIDR
    admin_cidr_blocks: List[str] = field(
        default_factory=lambda: DEFAULT_ADMIN_CIDR_BLOCKS.copy()
    )
    ssh_allowed_cidrs: Optional[List[str]] = None
    parsl_communication_cidrs: Optional[List[str]] = None

    # Security settings
    strict_mode: Optional[bool] = None  # Auto-determined from environment if None
    allow_vpc_internal: bool = DEFAULT_ALLOW_VPC_INTERNAL
    public_access_ports: List[int] = field(default_factory=list)

    # Security group templates
    use_security_templates: bool = True
    custom_security_rules: Optional[Dict[str, List[Dict[str, Any]]]] = None

    # Credential management settings
    credential_config: Optional[CredentialConfiguration] = None
    enable_credential_sanitization: bool = True
    
    # State encryption settings
    encryption_config: Optional[EncryptionConfiguration] = None
    enable_state_encryption: bool = True

    def __post_init__(self):
        """Validate and normalize configuration."""
        # Normalize environment
        if isinstance(self.environment, str):
            self.environment = SecurityEnvironment(self.environment.lower())

        # Set strict mode based on environment if not specified
        if self.strict_mode is None:
            self.strict_mode = self.environment == SecurityEnvironment.PRODUCTION

        # Validate configuration
        self._validate_config()

    def _validate_config(self) -> None:
        """Validate security configuration."""
        # Validate VPC CIDR
        from ..security.cidr_manager import CIDRManager

        cidr_manager = CIDRManager()

        if not cidr_manager.validate_cidr_block(self.vpc_cidr):
            raise ValueError(f"Invalid VPC CIDR: {self.vpc_cidr}")

        # Validate admin CIDRs
        for cidr in self.admin_cidr_blocks:
            if not cidr_manager.validate_cidr_block(cidr):
                raise ValueError(f"Invalid admin CIDR: {cidr}")

        # Production environment validation
        if self.environment == SecurityEnvironment.PRODUCTION:
            if not self.admin_cidr_blocks:
                raise ValueError("Admin CIDR blocks must be specified for production")

            if self.public_access_ports:
                logger.warning(
                    f"Public access ports {self.public_access_ports} configured "
                    f"for production environment"
                )

    def get_network_security_policy(self) -> NetworkSecurityPolicy:
        """Create a NetworkSecurityPolicy from this configuration.

        Returns
        -------
        NetworkSecurityPolicy
            Configured network security policy
        """
        return NetworkSecurityPolicy(
            environment=self.environment,
            vpc_cidr=self.vpc_cidr,
            admin_cidr_blocks=self.admin_cidr_blocks,
            ssh_allowed_cidrs=self.ssh_allowed_cidrs or [],
            parsl_communication_cidrs=self.parsl_communication_cidrs or [],
            public_access_ports=self.public_access_ports,
            allow_vpc_internal=self.allow_vpc_internal,
            strict_mode=self.strict_mode,
        )

    def get_security_group_rules(self, group_type: str) -> List[Dict[str, Any]]:
        """Get security group rules for a specific group type.

        Parameters
        ----------
        group_type : str
            Type of security group ("compute_worker", "bastion", "custom")

        Returns
        -------
        List[Dict[str, Any]]
            List of security group rules
        """
        if not self.use_security_templates:
            if self.custom_security_rules and group_type in self.custom_security_rules:
                return self.custom_security_rules[group_type]
            else:
                logger.warning(f"No custom rules defined for {group_type}")
                return []

        policy = self.get_network_security_policy()

        if group_type == "compute_worker":
            return policy.get_compute_worker_rules()
        elif group_type == "bastion":
            return policy.get_bastion_host_rules()
        elif group_type == "public_access":
            return policy.get_public_access_rules()
        else:
            logger.warning(f"Unknown security group type: {group_type}")
            return []

    def validate_security_rules(self, rules: List[Dict[str, Any]]) -> bool:
        """Validate security group rules against policy.

        Parameters
        ----------
        rules : List[Dict[str, Any]]
            Security group rules to validate

        Returns
        -------
        bool
            True if rules are valid, False otherwise
        """
        policy = self.get_network_security_policy()
        return policy.validate_security_group_rules(rules)

    def analyze_security_posture(self) -> Dict[str, Any]:
        """Analyze the current security configuration.

        Returns
        -------
        Dict[str, Any]
            Security analysis results
        """
        analysis = {
            "environment": self.environment.value,
            "strict_mode": self.strict_mode,
            "vpc_cidr": self.vpc_cidr,
            "admin_networks_count": len(self.admin_cidr_blocks),
            "public_ports_count": len(self.public_access_ports),
            "warnings": [],
            "recommendations": [],
        }

        # Environment-specific analysis
        if self.environment == SecurityEnvironment.PRODUCTION:
            if self.public_access_ports:
                analysis["warnings"].append(
                    f"Public access ports configured in production: {self.public_access_ports}"
                )

            if not self.strict_mode:
                analysis["warnings"].append("Strict mode disabled in production")

        elif self.environment == SecurityEnvironment.DEVELOPMENT:
            if self.strict_mode:
                analysis["recommendations"].append(
                    "Consider disabling strict mode for easier development"
                )

        # Admin network analysis
        if len(self.admin_cidr_blocks) == 1 and "0.0.0.0/0" in self.admin_cidr_blocks:
            analysis["warnings"].append("Admin access allows global internet access")

        # Credential management analysis
        analysis["credential_sanitization"] = self.enable_credential_sanitization
        analysis["has_credential_config"] = self.credential_config is not None

        if self.credential_config:
            if self.credential_config.role_arn:
                analysis["recommendations"].append(
                    "Using IAM role-based authentication (recommended)"
                )
            elif self.environment == SecurityEnvironment.PRODUCTION:
                analysis["warnings"].append(
                    "Consider using IAM role-based authentication for production"
                )
        elif self.environment == SecurityEnvironment.PRODUCTION:
            analysis["warnings"].append(
                "No credential configuration specified for production"
            )
        
        # State encryption analysis
        analysis["state_encryption"] = self.enable_state_encryption
        analysis["has_encryption_config"] = self.encryption_config is not None
        
        if self.enable_state_encryption:
            if not self.encryption_config:
                analysis["recommendations"].append(
                    "Consider configuring encryption settings for enhanced security"
                )
            elif self.encryption_config.master_key_source == "env":
                analysis["recommendations"].append(
                    "Environment variable key source configured - ensure key is properly secured"
                )
            elif self.encryption_config.master_key_source == "aws_kms":
                analysis["recommendations"].append(
                    "AWS KMS encryption configured (recommended for production)"
                )
        elif self.environment == SecurityEnvironment.PRODUCTION:
            analysis["warnings"].append(
                "State encryption disabled in production - consider enabling for sensitive data"
            )

        return analysis

    def get_credential_configuration(self) -> CredentialConfiguration:
        """Get credential configuration for this security profile.

        Returns
        -------
        CredentialConfiguration
            Credential configuration
        """
        if self.credential_config:
            return self.credential_config

        # Create default configuration based on environment
        config = CredentialConfiguration(
            enable_sanitization=self.enable_credential_sanitization,
            sanitize_logs=True,
            auto_refresh_tokens=True,
        )

        if self.environment == SecurityEnvironment.PRODUCTION:
            # Production: Prefer IAM roles, disable fallbacks
            config.use_environment_variables = False
            config.use_profile = None
            config.require_mfa = False  # Could be enabled based on requirements
        elif self.environment == SecurityEnvironment.DEVELOPMENT:
            # Development: Allow fallbacks for convenience
            config.use_environment_variables = True
            config.use_profile = "aws"

        return config

    def get_encryption_configuration(self) -> EncryptionConfiguration:
        """Get encryption configuration for this security profile.
        
        Returns
        -------
        EncryptionConfiguration
            Encryption configuration
        """
        if self.encryption_config:
            return self.encryption_config
        
        # Create default configuration based on environment
        if self.environment == SecurityEnvironment.PRODUCTION:
            # Production: Use AWS KMS if available, otherwise environment variables
            config = EncryptionConfiguration(
                algorithm="aes-gcm",
                master_key_source="env",  # Could be "aws_kms" if KMS key ID provided
                enable_key_rotation=True,
                key_rotation_days=30,  # More frequent rotation in production
                iterations=200000,  # Higher iterations for production
            )
        else:
            # Development: Use environment variables with reasonable defaults
            config = EncryptionConfiguration(
                algorithm="fernet",
                master_key_source="env",
                enable_key_rotation=False,  # Disabled for development convenience
                key_rotation_days=90,
                iterations=100000,
            )
        
        return config

    @classmethod
    def create_development_config(
        cls, vpc_cidr: str = DEFAULT_VPC_CIDR, role_arn: Optional[str] = None,
        enable_encryption: bool = True
    ) -> "SecurityConfig":
        """Create a development environment security configuration.

        Parameters
        ----------
        vpc_cidr : str
            VPC CIDR block
        role_arn : Optional[str]
            IAM role ARN for credential management
        enable_encryption : bool
            Whether to enable state encryption

        Returns
        -------
        SecurityConfig
            Development security configuration
        """
        # Create credential configuration for development
        credential_config = CredentialConfiguration(
            role_arn=role_arn,
            enable_sanitization=True,
            sanitize_logs=True,
            use_environment_variables=True,
            use_profile="aws",
            auto_refresh_tokens=True,
        )
        
        # Create encryption configuration for development
        encryption_config = None
        if enable_encryption:
            encryption_config = EncryptionConfiguration(
                algorithm="fernet",
                master_key_source="env",
                enable_key_rotation=False,
                key_rotation_days=90,
            )

        return cls(
            environment=SecurityEnvironment.DEVELOPMENT,
            vpc_cidr=vpc_cidr,
            admin_cidr_blocks=["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"],
            public_access_ports=[80, 443],  # Allowed in dev
            strict_mode=False,
            credential_config=credential_config,
            enable_credential_sanitization=True,
            encryption_config=encryption_config,
            enable_state_encryption=enable_encryption,
        )

    @classmethod
    def create_production_config(
        cls,
        vpc_cidr: str = DEFAULT_VPC_CIDR,
        admin_cidrs: List[str] = None,
        role_arn: Optional[str] = None,
        require_mfa: bool = False,
        kms_key_id: Optional[str] = None,
    ) -> "SecurityConfig":
        """Create a production environment security configuration.

        Parameters
        ----------
        vpc_cidr : str
            VPC CIDR block
        admin_cidrs : List[str]
            Administrative access CIDR blocks
        role_arn : Optional[str]
            IAM role ARN for credential management (recommended for production)
        require_mfa : bool
            Whether to require MFA for role assumption
        kms_key_id : Optional[str]
            AWS KMS key ID for encryption (recommended for production)

        Returns
        -------
        SecurityConfig
            Production security configuration
        """
        if admin_cidrs is None:
            raise ValueError("admin_cidrs must be specified for production")

        # Create credential configuration for production
        credential_config = CredentialConfiguration(
            role_arn=role_arn,
            enable_sanitization=True,
            sanitize_logs=True,
            use_environment_variables=False,  # Disable in production
            use_instance_profile=True,  # Allow instance profiles
            use_profile=None,  # No profile fallback
            auto_refresh_tokens=True,
            require_mfa=require_mfa,
            session_duration=3600,  # 1 hour sessions
        )
        
        # Create encryption configuration for production
        encryption_config = EncryptionConfiguration(
            algorithm="aes-gcm",
            master_key_source="aws_kms" if kms_key_id else "env",
            kms_key_id=kms_key_id,
            enable_key_rotation=True,
            key_rotation_days=30,  # More frequent rotation in production
            iterations=200000,  # Higher security
        )

        return cls(
            environment=SecurityEnvironment.PRODUCTION,
            vpc_cidr=vpc_cidr,
            admin_cidr_blocks=admin_cidrs,
            ssh_allowed_cidrs=admin_cidrs,
            public_access_ports=[],  # No public access by default
            strict_mode=True,
            credential_config=credential_config,
            enable_credential_sanitization=True,
            encryption_config=encryption_config,
            enable_state_encryption=True,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert configuration to dictionary.

        Returns
        -------
        Dict[str, Any]
            Configuration as dictionary
        """
        return {
            "environment": self.environment.value,
            "vpc_cidr": self.vpc_cidr,
            "admin_cidr_blocks": self.admin_cidr_blocks,
            "ssh_allowed_cidrs": self.ssh_allowed_cidrs,
            "parsl_communication_cidrs": self.parsl_communication_cidrs,
            "public_access_ports": self.public_access_ports,
            "strict_mode": self.strict_mode,
            "allow_vpc_internal": self.allow_vpc_internal,
            "use_security_templates": self.use_security_templates,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SecurityConfig":
        """Create configuration from dictionary.

        Parameters
        ----------
        data : Dict[str, Any]
            Configuration dictionary

        Returns
        -------
        SecurityConfig
            Security configuration
        """
        return cls(**data)

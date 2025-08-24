"""Credential management and sanitization for Parsl Ephemeral AWS Provider.

This module provides secure credential handling, sanitization, and IAM role-based
authentication to replace hardcoded credentials.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Any
from datetime import datetime, timedelta

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

logger = logging.getLogger(__name__)


@dataclass
class CredentialConfiguration:
    """Configuration for credential management."""

    # IAM role-based authentication
    role_arn: Optional[str] = None
    role_session_name: Optional[str] = None
    external_id: Optional[str] = None

    # STS token configuration
    session_duration: int = 3600  # 1 hour default
    auto_refresh_tokens: bool = True
    token_refresh_threshold: int = 300  # Refresh 5 minutes before expiry

    # Credential sanitization
    enable_sanitization: bool = True
    sanitize_logs: bool = True
    sanitize_memory_dumps: bool = True

    # Fallback credential sources
    use_instance_profile: bool = True
    use_environment_variables: bool = True
    use_profile: Optional[str] = None

    # Security settings
    require_mfa: bool = False
    mfa_serial_number: Optional[str] = None

    def __post_init__(self):
        """Validate configuration."""
        if self.require_mfa and not self.mfa_serial_number:
            raise ValueError("mfa_serial_number required when require_mfa is True")


class CredentialSanitizer:
    """Sanitizes credentials from logs and memory dumps."""

    # Patterns for credential detection
    CREDENTIAL_PATTERNS = [
        # AWS Access Key IDs
        re.compile(r"AKIA[0-9A-Z]{16}", re.IGNORECASE),
        # AWS Secret Access Keys
        re.compile(r"[A-Za-z0-9/+=]{40}"),
        # AWS Session Tokens (partial patterns)
        re.compile(r"[A-Za-z0-9/+=]{200,}"),
        # Generic API keys
        re.compile(
            r'["\']?[a-zA-Z0-9_-]*(?:api[_-]?key|secret|token)["\']?\s*[:=]\s*["\']?([A-Za-z0-9/+=_-]+)["\']?',
            re.IGNORECASE,
        ),
    ]

    # Replacement string for sanitized credentials
    SANITIZED_PLACEHOLDER = "***SANITIZED***"

    @classmethod
    def sanitize_string(cls, text: str) -> str:
        """Sanitize credentials from a string.

        Parameters
        ----------
        text : str
            Text to sanitize

        Returns
        -------
        str
            Sanitized text with credentials replaced
        """
        if not text:
            return text

        sanitized = text
        for pattern in cls.CREDENTIAL_PATTERNS:
            sanitized = pattern.sub(cls.SANITIZED_PLACEHOLDER, sanitized)

        return sanitized

    @classmethod
    def sanitize_dict(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        """Sanitize credentials from a dictionary.

        Parameters
        ----------
        data : Dict[str, Any]
            Dictionary to sanitize

        Returns
        -------
        Dict[str, Any]
            Sanitized dictionary
        """
        if not isinstance(data, dict):
            return data

        sanitized = {}
        sensitive_keys = {
            "access_key",
            "secret_key",
            "session_token",
            "password",
            "api_key",
            "token",
            "credential",
            "auth",
            "secret",
            "aws_access_key_id",
            "aws_secret_access_key",
            "aws_session_token",
        }

        for key, value in data.items():
            key_lower = key.lower()
            if any(sensitive in key_lower for sensitive in sensitive_keys):
                sanitized[key] = cls.SANITIZED_PLACEHOLDER
            elif isinstance(value, str):
                sanitized[key] = cls.sanitize_string(value)
            elif isinstance(value, dict):
                sanitized[key] = cls.sanitize_dict(value)
            elif isinstance(value, list):
                sanitized[key] = [
                    cls.sanitize_dict(item)
                    if isinstance(item, dict)
                    else cls.sanitize_string(item)
                    if isinstance(item, str)
                    else item
                    for item in value
                ]
            else:
                sanitized[key] = value

        return sanitized

    @classmethod
    def sanitize_logs(cls, record: logging.LogRecord) -> logging.LogRecord:
        """Sanitize credentials from log records.

        Parameters
        ----------
        record : logging.LogRecord
            Log record to sanitize

        Returns
        -------
        logging.LogRecord
            Sanitized log record
        """
        if hasattr(record, "msg") and isinstance(record.msg, str):
            record.msg = cls.sanitize_string(record.msg)

        if hasattr(record, "args") and record.args:
            sanitized_args = []
            for arg in record.args:
                if isinstance(arg, str):
                    sanitized_args.append(cls.sanitize_string(arg))
                elif isinstance(arg, dict):
                    sanitized_args.append(cls.sanitize_dict(arg))
                else:
                    sanitized_args.append(arg)
            record.args = tuple(sanitized_args)

        return record


class SanitizingLogHandler(logging.Handler):
    """Log handler that sanitizes credentials from log messages."""

    def __init__(self, handler: logging.Handler):
        """Initialize with wrapped handler.

        Parameters
        ----------
        handler : logging.Handler
            Handler to wrap with sanitization
        """
        super().__init__()
        self.handler = handler
        self.setLevel(handler.level)
        self.setFormatter(handler.formatter)

    def emit(self, record: logging.LogRecord) -> None:
        """Emit sanitized log record.

        Parameters
        ----------
        record : logging.LogRecord
            Log record to sanitize and emit
        """
        try:
            sanitized_record = CredentialSanitizer.sanitize_logs(record)
            self.handler.emit(sanitized_record)
        except Exception:
            self.handleError(record)


@dataclass
class CredentialInfo:
    """Information about current credentials."""

    access_key: str = field(repr=False)  # Don't include in repr for security
    secret_key: str = field(repr=False)
    session_token: Optional[str] = field(default=None, repr=False)
    expiry_time: Optional[datetime] = None
    source: str = "unknown"
    role_arn: Optional[str] = None

    def is_expired(self) -> bool:
        """Check if credentials are expired.

        Returns
        -------
        bool
            True if credentials are expired
        """
        if not self.expiry_time:
            return False
        return datetime.utcnow() >= self.expiry_time

    def needs_refresh(self, threshold_seconds: int = 300) -> bool:
        """Check if credentials need refresh.

        Parameters
        ----------
        threshold_seconds : int
            Seconds before expiry to trigger refresh

        Returns
        -------
        bool
            True if credentials need refresh
        """
        if not self.expiry_time:
            return False
        threshold_time = datetime.utcnow() + timedelta(seconds=threshold_seconds)
        return threshold_time >= self.expiry_time

    def to_boto3_session_kwargs(self) -> Dict[str, str]:
        """Convert to boto3 session parameters.

        Returns
        -------
        Dict[str, str]
            Session parameters for boto3
        """
        kwargs = {
            "aws_access_key_id": self.access_key,
            "aws_secret_access_key": self.secret_key,
        }
        if self.session_token:
            kwargs["aws_session_token"] = self.session_token
        return kwargs


class CredentialManager:
    """Manages AWS credentials with security best practices."""

    def __init__(self, config: CredentialConfiguration):
        """Initialize credential manager.

        Parameters
        ----------
        config : CredentialConfiguration
            Credential configuration
        """
        self.config = config
        self.current_credentials: Optional[CredentialInfo] = None
        self._sts_client = None

        # Set up credential sanitization
        if config.enable_sanitization and config.sanitize_logs:
            self._setup_log_sanitization()

    def _setup_log_sanitization(self) -> None:
        """Set up log sanitization for the root logger."""
        root_logger = logging.getLogger()

        # Wrap existing handlers with sanitizing handlers
        original_handlers = root_logger.handlers[:]
        for handler in original_handlers:
            if not isinstance(handler, SanitizingLogHandler):
                root_logger.removeHandler(handler)
                sanitizing_handler = SanitizingLogHandler(handler)
                root_logger.addHandler(sanitizing_handler)

    def get_credentials(self) -> CredentialInfo:
        """Get current valid credentials.

        Returns
        -------
        CredentialInfo
            Current valid credentials

        Raises
        ------
        NoCredentialsError
            If no valid credentials can be obtained
        """
        # Check if current credentials are still valid
        if (
            self.current_credentials
            and not self.current_credentials.is_expired()
            and not (
                self.config.auto_refresh_tokens
                and self.current_credentials.needs_refresh(
                    self.config.token_refresh_threshold
                )
            )
        ):
            return self.current_credentials

        # Try to obtain new credentials
        credentials = self._obtain_credentials()
        self.current_credentials = credentials

        logger.info(f"Obtained credentials from: {credentials.source}")
        if credentials.expiry_time:
            logger.info(f"Credentials expire at: {credentials.expiry_time}")

        return credentials

    def _obtain_credentials(self) -> CredentialInfo:
        """Obtain credentials from available sources.

        Returns
        -------
        CredentialInfo
            Obtained credentials

        Raises
        ------
        NoCredentialsError
            If no credentials can be obtained
        """
        # Try IAM role assumption first (most secure)
        if self.config.role_arn:
            try:
                return self._assume_role()
            except Exception as e:
                logger.warning(f"Failed to assume role {self.config.role_arn}: {e}")

        # Try instance profile (secure for EC2)
        if self.config.use_instance_profile:
            try:
                return self._get_instance_profile_credentials()
            except Exception as e:
                logger.debug(f"Instance profile not available: {e}")

        # Try environment variables (less secure, warn about it)
        if self.config.use_environment_variables:
            try:
                credentials = self._get_environment_credentials()
                logger.warning(
                    "Using environment variable credentials - consider IAM roles for better security"
                )
                return credentials
            except Exception as e:
                logger.debug(f"Environment credentials not available: {e}")

        # Try profile (less secure, warn about it)
        if self.config.use_profile:
            try:
                credentials = self._get_profile_credentials()
                logger.warning(
                    f"Using profile credentials ({self.config.use_profile}) - consider IAM roles for better security"
                )
                return credentials
            except Exception as e:
                logger.debug(f"Profile credentials not available: {e}")

        raise NoCredentialsError("No valid credentials found")

    def _assume_role(self) -> CredentialInfo:
        """Assume IAM role for credentials.

        Returns
        -------
        CredentialInfo
            Role credentials
        """
        if not self._sts_client:
            # Use default credential chain for STS client
            self._sts_client = boto3.client("sts")

        assume_role_kwargs = {
            "RoleArn": self.config.role_arn,
            "RoleSessionName": self.config.role_session_name
            or f"parsl-ephemeral-{int(time.time())}",
            "DurationSeconds": self.config.session_duration,
        }

        if self.config.external_id:
            assume_role_kwargs["ExternalId"] = self.config.external_id

        if self.config.require_mfa:
            # This would require interactive MFA token input
            # For now, we'll just document the requirement
            logger.info(
                "MFA required for role assumption - implement interactive MFA flow"
            )

        try:
            response = self._sts_client.assume_role(**assume_role_kwargs)
            credentials = response["Credentials"]

            return CredentialInfo(
                access_key=credentials["AccessKeyId"],
                secret_key=credentials["SecretAccessKey"],
                session_token=credentials["SessionToken"],
                expiry_time=credentials["Expiration"],
                source="iam_role",
                role_arn=self.config.role_arn,
            )
        except ClientError as e:
            logger.error(f"Failed to assume role: {e}")
            raise NoCredentialsError(f"Role assumption failed: {e}")

    def _get_instance_profile_credentials(self) -> CredentialInfo:
        """Get credentials from EC2 instance profile.

        Returns
        -------
        CredentialInfo
            Instance profile credentials
        """
        try:
            session = boto3.Session()
            credentials = session.get_credentials()

            if not credentials:
                raise NoCredentialsError("No instance profile credentials available")

            # Instance profile credentials are automatically refreshed by boto3
            return CredentialInfo(
                access_key=credentials.access_key,
                secret_key=credentials.secret_key,
                session_token=credentials.token,
                expiry_time=None,  # Auto-refreshed
                source="instance_profile",
            )
        except Exception as e:
            raise NoCredentialsError(f"Instance profile credentials not available: {e}")

    def _get_environment_credentials(self) -> CredentialInfo:
        """Get credentials from environment variables.

        Returns
        -------
        CredentialInfo
            Environment credentials
        """
        import os

        access_key = os.environ.get("AWS_ACCESS_KEY_ID")
        secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
        session_token = os.environ.get("AWS_SESSION_TOKEN")

        if not access_key or not secret_key:
            raise NoCredentialsError("Environment credentials not available")

        return CredentialInfo(
            access_key=access_key,
            secret_key=secret_key,
            session_token=session_token,
            expiry_time=None,
            source="environment",
        )

    def _get_profile_credentials(self) -> CredentialInfo:
        """Get credentials from AWS profile.

        Returns
        -------
        CredentialInfo
            Profile credentials
        """
        try:
            session = boto3.Session(profile_name=self.config.use_profile)
            credentials = session.get_credentials()

            if not credentials:
                raise NoCredentialsError(
                    f"Profile {self.config.use_profile} not available"
                )

            return CredentialInfo(
                access_key=credentials.access_key,
                secret_key=credentials.secret_key,
                session_token=credentials.token,
                expiry_time=None,
                source=f"profile_{self.config.use_profile}",
            )
        except Exception as e:
            raise NoCredentialsError(f"Profile credentials not available: {e}")

    def create_boto3_session(self, region: str = "us-east-1") -> boto3.Session:
        """Create a boto3 session with current credentials.

        Parameters
        ----------
        region : str
            AWS region

        Returns
        -------
        boto3.Session
            Configured session
        """
        credentials = self.get_credentials()
        session_kwargs = credentials.to_boto3_session_kwargs()
        session_kwargs["region_name"] = region

        return boto3.Session(**session_kwargs)

    def refresh_credentials(self) -> CredentialInfo:
        """Force refresh of credentials.

        Returns
        -------
        CredentialInfo
            Refreshed credentials
        """
        self.current_credentials = None
        return self.get_credentials()

    def get_credential_info(self) -> Dict[str, Any]:
        """Get sanitized information about current credentials.

        Returns
        -------
        Dict[str, Any]
            Sanitized credential information
        """
        if not self.current_credentials:
            return {"status": "no_credentials"}

        info = {
            "source": self.current_credentials.source,
            "has_session_token": bool(self.current_credentials.session_token),
            "role_arn": self.current_credentials.role_arn,
            "expired": self.current_credentials.is_expired(),
            "needs_refresh": self.current_credentials.needs_refresh(
                self.config.token_refresh_threshold
            ),
        }

        if self.current_credentials.expiry_time:
            info["expires_at"] = self.current_credentials.expiry_time.isoformat()
            info["expires_in_seconds"] = int(
                (
                    self.current_credentials.expiry_time - datetime.utcnow()
                ).total_seconds()
            )

        return info

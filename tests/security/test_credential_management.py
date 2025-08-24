"""Tests for credential management framework.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import pytest
import logging
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timedelta
from botocore.exceptions import ClientError, NoCredentialsError

from parsl_ephemeral_aws.security.credential_manager import (
    CredentialConfiguration,
    CredentialSanitizer,
    SanitizingLogHandler,
    CredentialInfo,
    CredentialManager
)


class TestCredentialConfiguration:
    """Tests for credential configuration."""

    def test_default_configuration(self):
        """Test default credential configuration values."""
        config = CredentialConfiguration()
        
        assert config.role_arn is None
        assert config.session_duration == 3600
        assert config.auto_refresh_tokens is True
        assert config.enable_sanitization is True
        assert config.use_instance_profile is True
        assert config.require_mfa is False

    def test_mfa_validation(self):
        """Test MFA configuration validation."""
        # Should raise error when MFA required but no serial number
        with pytest.raises(ValueError, match="mfa_serial_number required"):
            CredentialConfiguration(require_mfa=True)

        # Should not raise error when MFA serial number provided
        config = CredentialConfiguration(
            require_mfa=True,
            mfa_serial_number="arn:aws:iam::123456789012:mfa/user"
        )
        assert config.require_mfa is True
        assert config.mfa_serial_number is not None


class TestCredentialSanitizer:
    """Tests for credential sanitization."""

    def test_sanitize_aws_access_key(self):
        """Test sanitization of AWS access keys."""
        text = "Access Key: AKIAIOSFODNN7EXAMPLE"
        sanitized = CredentialSanitizer.sanitize_string(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in sanitized
        assert "***SANITIZED***" in sanitized

    def test_sanitize_aws_secret_key(self):
        """Test sanitization of AWS secret keys."""
        text = "Secret: wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        sanitized = CredentialSanitizer.sanitize_string(text)
        assert "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" not in sanitized
        assert "***SANITIZED***" in sanitized

    def test_sanitize_session_token(self):
        """Test sanitization of AWS session tokens."""
        # Long token-like string
        token = "FwoGZXIvYXdzEAcaDG" + "A" * 200 + "=="
        text = f"Session Token: {token}"
        sanitized = CredentialSanitizer.sanitize_string(text)
        assert token not in sanitized
        assert "***SANITIZED***" in sanitized

    def test_sanitize_dict_sensitive_keys(self):
        """Test sanitization of dictionary with sensitive keys."""
        data = {
            "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
            "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "aws_session_token": "FwoGZXIvYXdzEAcaDG" + "A" * 200,
            "regular_key": "regular_value",
            "nested": {
                "secret": "my_secret",
                "normal": "normal_value"
            }
        }

        sanitized = CredentialSanitizer.sanitize_dict(data)
        
        assert sanitized["aws_access_key_id"] == "***SANITIZED***"
        assert sanitized["aws_secret_access_key"] == "***SANITIZED***"
        assert sanitized["aws_session_token"] == "***SANITIZED***"
        assert sanitized["regular_key"] == "regular_value"
        assert sanitized["nested"]["secret"] == "***SANITIZED***"
        assert sanitized["nested"]["normal"] == "normal_value"

    def test_sanitize_empty_data(self):
        """Test sanitization of empty or None data."""
        assert CredentialSanitizer.sanitize_string(None) is None
        assert CredentialSanitizer.sanitize_string("") == ""
        assert CredentialSanitizer.sanitize_dict({}) == {}

    def test_sanitize_log_record(self):
        """Test log record sanitization."""
        # Create a log record with credentials
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=1,
            msg="Access key: AKIAIOSFODNN7EXAMPLE",
            args=(),
            exc_info=None
        )

        sanitized = CredentialSanitizer.sanitize_logs(record)
        assert "AKIAIOSFODNN7EXAMPLE" not in sanitized.msg
        assert "***SANITIZED***" in sanitized.msg

    def test_sanitize_log_record_with_args(self):
        """Test log record sanitization with args."""
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=1,
            msg="Credentials: %s, %s",
            args=("AKIAIOSFODNN7EXAMPLE", {"secret": "my_secret"}),
            exc_info=None
        )

        sanitized = CredentialSanitizer.sanitize_logs(record)
        assert "AKIAIOSFODNN7EXAMPLE" not in str(sanitized.args)
        assert "my_secret" not in str(sanitized.args)
        assert "***SANITIZED***" in str(sanitized.args)


class TestSanitizingLogHandler:
    """Tests for sanitizing log handler."""

    def test_handler_initialization(self):
        """Test handler initialization with wrapped handler."""
        mock_handler = Mock(spec=logging.Handler)
        mock_handler.level = logging.INFO
        mock_handler.formatter = None

        sanitizer = SanitizingLogHandler(mock_handler)
        
        assert sanitizer.handler == mock_handler
        assert sanitizer.level == logging.INFO

    def test_emit_sanitizes_record(self):
        """Test that emit method sanitizes records."""
        mock_handler = Mock(spec=logging.Handler)
        mock_handler.level = logging.INFO
        mock_handler.formatter = None

        sanitizer = SanitizingLogHandler(mock_handler)

        # Create record with credentials
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=1,
            msg="Access key: AKIAIOSFODNN7EXAMPLE",
            args=(),
            exc_info=None
        )

        sanitizer.emit(record)

        # Verify the handler was called
        mock_handler.emit.assert_called_once()
        called_record = mock_handler.emit.call_args[0][0]
        assert "AKIAIOSFODNN7EXAMPLE" not in called_record.msg
        assert "***SANITIZED***" in called_record.msg


class TestCredentialInfo:
    """Tests for credential information."""

    def test_credential_info_creation(self):
        """Test credential info creation."""
        expiry = datetime.utcnow() + timedelta(hours=1)
        creds = CredentialInfo(
            access_key="AKIAIOSFODNN7EXAMPLE",
            secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            session_token="session_token",
            expiry_time=expiry,
            source="test",
            role_arn="arn:aws:iam::123456789012:role/test"
        )

        assert creds.access_key == "AKIAIOSFODNN7EXAMPLE"
        assert creds.expiry_time == expiry
        assert creds.source == "test"
        assert creds.role_arn == "arn:aws:iam::123456789012:role/test"

    def test_credential_info_repr_security(self):
        """Test that sensitive fields are not in repr."""
        creds = CredentialInfo(
            access_key="AKIAIOSFODNN7EXAMPLE",
            secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            session_token="session_token"
        )

        repr_str = repr(creds)
        assert "AKIAIOSFODNN7EXAMPLE" not in repr_str
        assert "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" not in repr_str
        assert "session_token" not in repr_str

    def test_is_expired(self):
        """Test credential expiry check."""
        # Not expired
        future_expiry = datetime.utcnow() + timedelta(hours=1)
        creds = CredentialInfo("key", "secret", expiry_time=future_expiry)
        assert not creds.is_expired()

        # Expired
        past_expiry = datetime.utcnow() - timedelta(hours=1)
        creds = CredentialInfo("key", "secret", expiry_time=past_expiry)
        assert creds.is_expired()

        # No expiry time
        creds = CredentialInfo("key", "secret")
        assert not creds.is_expired()

    def test_needs_refresh(self):
        """Test credential refresh check."""
        # Needs refresh (expires in 2 minutes, threshold is 5 minutes)
        soon_expiry = datetime.utcnow() + timedelta(minutes=2)
        creds = CredentialInfo("key", "secret", expiry_time=soon_expiry)
        assert creds.needs_refresh(threshold_seconds=300)  # 5 minutes

        # Doesn't need refresh (expires in 10 minutes)
        far_expiry = datetime.utcnow() + timedelta(minutes=10)
        creds = CredentialInfo("key", "secret", expiry_time=far_expiry)
        assert not creds.needs_refresh(threshold_seconds=300)

        # No expiry time
        creds = CredentialInfo("key", "secret")
        assert not creds.needs_refresh()

    def test_to_boto3_session_kwargs(self):
        """Test conversion to boto3 session kwargs."""
        creds = CredentialInfo(
            access_key="AKIAIOSFODNN7EXAMPLE",
            secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            session_token="session_token"
        )

        kwargs = creds.to_boto3_session_kwargs()
        expected = {
            'aws_access_key_id': 'AKIAIOSFODNN7EXAMPLE',
            'aws_secret_access_key': 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
            'aws_session_token': 'session_token'
        }
        assert kwargs == expected

        # Test without session token
        creds = CredentialInfo(
            access_key="AKIAIOSFODNN7EXAMPLE",
            secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        )
        kwargs = creds.to_boto3_session_kwargs()
        assert 'aws_session_token' not in kwargs


class TestCredentialManager:
    """Tests for credential manager."""

    def test_initialization(self):
        """Test credential manager initialization."""
        config = CredentialConfiguration()
        manager = CredentialManager(config)
        
        assert manager.config == config
        assert manager.current_credentials is None

    @patch('parsl_ephemeral_aws.security.credential_manager.logging.getLogger')
    def test_log_sanitization_setup(self, mock_get_logger):
        """Test log sanitization setup."""
        # Mock root logger and handler
        mock_root_logger = Mock()
        mock_handler = Mock(spec=logging.Handler)
        mock_root_logger.handlers = [mock_handler]
        mock_get_logger.return_value = mock_root_logger

        config = CredentialConfiguration(enable_sanitization=True, sanitize_logs=True)
        CredentialManager(config)

        # Verify handlers were wrapped
        mock_root_logger.removeHandler.assert_called_once_with(mock_handler)
        mock_root_logger.addHandler.assert_called_once()

    @patch.dict('os.environ', {
        'AWS_ACCESS_KEY_ID': 'AKIAIOSFODNN7EXAMPLE',
        'AWS_SECRET_ACCESS_KEY': 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
        'AWS_SESSION_TOKEN': 'session_token'
    })
    def test_get_environment_credentials(self):
        """Test getting credentials from environment variables."""
        config = CredentialConfiguration(use_environment_variables=True)
        manager = CredentialManager(config)
        
        creds = manager._get_environment_credentials()
        
        assert creds.access_key == 'AKIAIOSFODNN7EXAMPLE'
        assert creds.secret_key == 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'
        assert creds.session_token == 'session_token'
        assert creds.source == 'environment'

    @patch.dict('os.environ', {}, clear=True)
    def test_get_environment_credentials_missing(self):
        """Test error when environment credentials are missing."""
        config = CredentialConfiguration()
        manager = CredentialManager(config)
        
        with pytest.raises(NoCredentialsError, match="Environment credentials not available"):
            manager._get_environment_credentials()

    @patch('parsl_ephemeral_aws.security.credential_manager.boto3.Session')
    def test_get_instance_profile_credentials(self, mock_session_class):
        """Test getting credentials from instance profile."""
        # Mock session and credentials
        mock_credentials = Mock()
        mock_credentials.access_key = 'AKIAIOSFODNN7EXAMPLE'
        mock_credentials.secret_key = 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'
        mock_credentials.token = 'session_token'
        
        mock_session = Mock()
        mock_session.get_credentials.return_value = mock_credentials
        mock_session_class.return_value = mock_session

        config = CredentialConfiguration()
        manager = CredentialManager(config)
        
        creds = manager._get_instance_profile_credentials()
        
        assert creds.access_key == 'AKIAIOSFODNN7EXAMPLE'
        assert creds.source == 'instance_profile'

    @patch('parsl_ephemeral_aws.security.credential_manager.boto3.Session')
    def test_get_instance_profile_credentials_missing(self, mock_session_class):
        """Test error when instance profile credentials are missing."""
        mock_session = Mock()
        mock_session.get_credentials.return_value = None
        mock_session_class.return_value = mock_session

        config = CredentialConfiguration()
        manager = CredentialManager(config)
        
        with pytest.raises(NoCredentialsError, match="No instance profile credentials available"):
            manager._get_instance_profile_credentials()

    @patch('parsl_ephemeral_aws.security.credential_manager.boto3.client')
    def test_assume_role(self, mock_boto3_client):
        """Test IAM role assumption."""
        # Mock STS client response
        mock_sts_client = Mock()
        mock_response = {
            'Credentials': {
                'AccessKeyId': 'ASIAIOSFODNN7EXAMPLE',
                'SecretAccessKey': 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
                'SessionToken': 'session_token',
                'Expiration': datetime.utcnow() + timedelta(hours=1)
            }
        }
        mock_sts_client.assume_role.return_value = mock_response
        mock_boto3_client.return_value = mock_sts_client

        config = CredentialConfiguration(
            role_arn='arn:aws:iam::123456789012:role/test',
            role_session_name='test-session'
        )
        manager = CredentialManager(config)
        
        creds = manager._assume_role()
        
        assert creds.access_key == 'ASIAIOSFODNN7EXAMPLE'
        assert creds.source == 'iam_role'
        assert creds.role_arn == 'arn:aws:iam::123456789012:role/test'
        
        # Verify assume_role was called with correct parameters
        mock_sts_client.assume_role.assert_called_once_with(
            RoleArn='arn:aws:iam::123456789012:role/test',
            RoleSessionName='test-session',
            DurationSeconds=3600
        )

    @patch('parsl_ephemeral_aws.security.credential_manager.boto3.client')
    def test_assume_role_failure(self, mock_boto3_client):
        """Test IAM role assumption failure."""
        mock_sts_client = Mock()
        mock_sts_client.assume_role.side_effect = ClientError(
            {'Error': {'Code': 'AccessDenied', 'Message': 'Access denied'}},
            'AssumeRole'
        )
        mock_boto3_client.return_value = mock_sts_client

        config = CredentialConfiguration(role_arn='arn:aws:iam::123456789012:role/test')
        manager = CredentialManager(config)
        
        with pytest.raises(NoCredentialsError, match="Role assumption failed"):
            manager._assume_role()

    def test_get_credentials_with_cached_valid(self):
        """Test getting credentials with valid cached credentials."""
        config = CredentialConfiguration()
        manager = CredentialManager(config)
        
        # Set up cached credentials that are still valid
        future_expiry = datetime.utcnow() + timedelta(hours=1)
        cached_creds = CredentialInfo(
            access_key="AKIAIOSFODNN7EXAMPLE",
            secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            expiry_time=future_expiry,
            source="cached"
        )
        manager.current_credentials = cached_creds
        
        creds = manager.get_credentials()
        
        # Should return the cached credentials
        assert creds == cached_creds

    @patch('parsl_ephemeral_aws.security.credential_manager.CredentialManager._obtain_credentials')
    def test_get_credentials_refresh_needed(self, mock_obtain):
        """Test getting credentials when refresh is needed."""
        config = CredentialConfiguration(auto_refresh_tokens=True)
        manager = CredentialManager(config)
        
        # Set up cached credentials that need refresh
        soon_expiry = datetime.utcnow() + timedelta(minutes=2)  # Less than 5 min threshold
        cached_creds = CredentialInfo(
            access_key="AKIAIOSFODNN7EXAMPLE",
            secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            expiry_time=soon_expiry,
            source="cached"
        )
        manager.current_credentials = cached_creds
        
        # Mock new credentials
        new_creds = CredentialInfo(
            access_key="ASIAIOSFODNN7EXAMPLE",
            secret_key="newSecret",
            source="refreshed"
        )
        mock_obtain.return_value = new_creds
        
        creds = manager.get_credentials()
        
        # Should return new credentials
        assert creds == new_creds
        assert manager.current_credentials == new_creds
        mock_obtain.assert_called_once()

    @patch('parsl_ephemeral_aws.security.credential_manager.boto3.Session')
    def test_create_boto3_session(self, mock_session_class):
        """Test creating boto3 session with credentials."""
        config = CredentialConfiguration()
        manager = CredentialManager(config)
        
        # Mock credentials
        creds = CredentialInfo(
            access_key="AKIAIOSFODNN7EXAMPLE",
            secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            session_token="session_token"
        )
        manager.current_credentials = creds
        
        session = manager.create_boto3_session(region='us-west-2')
        
        # Verify session was created with correct parameters
        mock_session_class.assert_called_once_with(
            aws_access_key_id='AKIAIOSFODNN7EXAMPLE',
            aws_secret_access_key='wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
            aws_session_token='session_token',
            region_name='us-west-2'
        )

    def test_get_credential_info_no_credentials(self):
        """Test getting credential info with no credentials."""
        config = CredentialConfiguration()
        manager = CredentialManager(config)
        
        info = manager.get_credential_info()
        assert info == {"status": "no_credentials"}

    def test_get_credential_info_with_credentials(self):
        """Test getting credential info with credentials."""
        config = CredentialConfiguration()
        manager = CredentialManager(config)
        
        # Set up credentials
        expiry = datetime.utcnow() + timedelta(hours=1)
        creds = CredentialInfo(
            access_key="AKIAIOSFODNN7EXAMPLE",
            secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            session_token="session_token",
            expiry_time=expiry,
            source="test",
            role_arn="arn:aws:iam::123456789012:role/test"
        )
        manager.current_credentials = creds
        
        info = manager.get_credential_info()
        
        assert info["source"] == "test"
        assert info["has_session_token"] is True
        assert info["role_arn"] == "arn:aws:iam::123456789012:role/test"
        assert info["expired"] is False
        assert "expires_at" in info
        assert "expires_in_seconds" in info

    @patch('parsl_ephemeral_aws.security.credential_manager.CredentialManager._obtain_credentials')
    def test_refresh_credentials(self, mock_obtain):
        """Test forcing credential refresh."""
        config = CredentialConfiguration()
        manager = CredentialManager(config)
        
        # Set up existing credentials
        old_creds = CredentialInfo("old_key", "old_secret", source="old")
        manager.current_credentials = old_creds
        
        # Mock new credentials
        new_creds = CredentialInfo("new_key", "new_secret", source="new")
        mock_obtain.return_value = new_creds
        
        result = manager.refresh_credentials()
        
        assert result == new_creds
        assert manager.current_credentials == new_creds
        mock_obtain.assert_called_once()
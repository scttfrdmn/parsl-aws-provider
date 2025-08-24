"""Tests for state encryption framework.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import pytest
import os
import json
import tempfile
from unittest.mock import Mock, patch
from parsl_ephemeral_aws.security.encryption import (
    EncryptionConfiguration,
    EncryptionKeyManager,
    StateEncryptor,
    SecureStateManager
)


class TestEncryptionConfiguration:
    """Tests for encryption configuration."""

    def test_default_configuration(self):
        """Test default encryption configuration."""
        config = EncryptionConfiguration()
        
        assert config.algorithm == "fernet"
        assert config.key_derivation == "pbkdf2"
        assert config.master_key_source == "env"
        assert config.enable_key_rotation is True
        assert config.salt_length == 32
        assert config.iterations == 100000

    def test_invalid_algorithm(self):
        """Test validation of invalid algorithm."""
        with pytest.raises(ValueError, match="Unsupported encryption algorithm"):
            EncryptionConfiguration(algorithm="invalid")

    def test_invalid_key_source(self):
        """Test validation of invalid key source."""
        with pytest.raises(ValueError, match="Unsupported master key source"):
            EncryptionConfiguration(master_key_source="invalid")

    def test_file_key_source_validation(self):
        """Test validation when using file key source."""
        with pytest.raises(ValueError, match="master_key_file_path required"):
            EncryptionConfiguration(
                master_key_source="file",
                master_key_file_path=None
            )
        
        # Should not raise with file path
        config = EncryptionConfiguration(
            master_key_source="file",
            master_key_file_path="/path/to/key"
        )
        assert config.master_key_file_path == "/path/to/key"


class TestEncryptionKeyManager:
    """Tests for encryption key manager."""

    def test_initialization(self):
        """Test key manager initialization."""
        config = EncryptionConfiguration()
        manager = EncryptionKeyManager(config)
        
        assert manager.config == config
        assert manager._master_key is None
        assert manager._derived_keys == {}

    @patch.dict('os.environ', {'TEST_MASTER_KEY': 'dGVzdC1rZXktMTIzNDU2Nzg='})  # base64 encoded
    def test_load_master_key_from_env_base64(self):
        """Test loading master key from environment variable (base64)."""
        config = EncryptionConfiguration(master_key_env_var="TEST_MASTER_KEY")
        manager = EncryptionKeyManager(config)
        
        key = manager._load_master_key()
        assert key == b'test-key-12345678'  # decoded from base64

    @patch.dict('os.environ', {'TEST_MASTER_KEY': 'test-key-string'})
    def test_load_master_key_from_env_string(self):
        """Test loading master key from environment variable (string)."""
        config = EncryptionConfiguration(master_key_env_var="TEST_MASTER_KEY")
        manager = EncryptionKeyManager(config)
        
        key = manager._load_master_key()
        assert key == b'test-key-string'

    @patch.dict('os.environ', {}, clear=True)
    def test_load_master_key_missing_env(self):
        """Test error when environment variable is missing."""
        config = EncryptionConfiguration(master_key_env_var="MISSING_KEY")
        manager = EncryptionKeyManager(config)
        
        with pytest.raises(ValueError, match="Master key not found in environment variable"):
            manager._load_master_key()

    def test_load_master_key_from_file(self):
        """Test loading master key from file."""
        with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
            test_key = b'file-test-key-123456'
            tmp_file.write(test_key)
            tmp_file.flush()
            
            config = EncryptionConfiguration(
                master_key_source="file",
                master_key_file_path=tmp_file.name
            )
            manager = EncryptionKeyManager(config)
            
            key = manager._load_master_key()
            assert key == test_key
            
            os.unlink(tmp_file.name)

    def test_load_master_key_file_not_found(self):
        """Test error when key file is not found."""
        config = EncryptionConfiguration(
            master_key_source="file",
            master_key_file_path="/nonexistent/key/file"
        )
        manager = EncryptionKeyManager(config)
        
        with pytest.raises(ValueError, match="Master key file not found"):
            manager._load_master_key()

    @patch('boto3.client')
    def test_load_kms_key(self, mock_boto3_client):
        """Test loading key from AWS KMS."""
        mock_kms = Mock()
        mock_kms.generate_data_key.return_value = {
            'Plaintext': b'kms-generated-key-123'
        }
        mock_boto3_client.return_value = mock_kms
        
        config = EncryptionConfiguration(
            master_key_source="aws_kms",
            kms_key_id="arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789012"
        )
        manager = EncryptionKeyManager(config)
        
        key = manager._load_kms_key()
        assert key == b'kms-generated-key-123'
        
        mock_kms.generate_data_key.assert_called_once_with(
            KeyId=config.kms_key_id,
            KeySpec='AES_256'
        )

    @patch.dict('os.environ', {'TEST_MASTER_KEY': 'dGVzdC1rZXktMTIzNDU2Nzg='})
    def test_derive_key(self):
        """Test key derivation."""
        config = EncryptionConfiguration(master_key_env_var="TEST_MASTER_KEY")
        manager = EncryptionKeyManager(config)
        
        # Test key derivation with context
        key1, salt1 = manager.derive_key("test_context")
        assert len(key1) == 32  # 256-bit key
        assert len(salt1) == config.salt_length
        
        # Same context and salt should produce same key
        key2, salt2 = manager.derive_key("test_context", salt=salt1)
        assert key1 == key2
        assert salt1 == salt2
        
        # Different context should produce different key
        key3, salt3 = manager.derive_key("different_context", salt=salt1)
        assert key1 != key3

    def test_generate_master_key(self):
        """Test master key generation."""
        config = EncryptionConfiguration()
        manager = EncryptionKeyManager(config)
        
        key = manager.generate_master_key()
        assert isinstance(key, str)
        assert len(key) > 0
        
        # Should be valid base64
        import base64
        decoded = base64.b64decode(key)
        assert len(decoded) == 32  # 256-bit key


class TestStateEncryptor:
    """Tests for state encryptor."""

    @patch.dict('os.environ', {'TEST_MASTER_KEY': 'dGVzdC1rZXktMTIzNDU2Nzg='})
    def test_encrypt_decrypt_state_fernet(self):
        """Test state encryption and decryption with Fernet."""
        config = EncryptionConfiguration(
            algorithm="fernet",
            master_key_env_var="TEST_MASTER_KEY"
        )
        encryptor = StateEncryptor(config)
        
        test_state = {
            "instances": ["i-123", "i-456"],
            "vpc_id": "vpc-123",
            "security_groups": {"sg-123": "parsl-sg"}
        }
        
        # Encrypt state
        encrypted_state = encryptor.encrypt_state(test_state)
        
        assert "encrypted_data" in encrypted_state
        assert "encryption_metadata" in encrypted_state
        assert encrypted_state["encryption_metadata"]["algorithm"] == "fernet"
        assert encrypted_state["encryption_metadata"]["version"] == "1.0"
        
        # Decrypt state
        decrypted_state = encryptor.decrypt_state(encrypted_state)
        assert decrypted_state == test_state

    @patch.dict('os.environ', {'TEST_MASTER_KEY': 'dGVzdC1rZXktMTIzNDU2Nzg='})
    def test_encrypt_decrypt_state_aes_gcm(self):
        """Test state encryption and decryption with AES-GCM."""
        config = EncryptionConfiguration(
            algorithm="aes-gcm",
            master_key_env_var="TEST_MASTER_KEY"
        )
        encryptor = StateEncryptor(config)
        
        test_state = {
            "tasks": ["task-1", "task-2"],
            "cluster_name": "test-cluster"
        }
        
        # Encrypt state
        encrypted_state = encryptor.encrypt_state(test_state)
        
        assert "encrypted_data" in encrypted_state
        assert "encryption_metadata" in encrypted_state
        assert encrypted_state["encryption_metadata"]["algorithm"] == "aes-gcm"
        
        # Decrypt state
        decrypted_state = encryptor.decrypt_state(encrypted_state)
        assert decrypted_state == test_state

    @patch.dict('os.environ', {'TEST_MASTER_KEY': 'dGVzdC1rZXktMTIzNDU2Nzg='})
    def test_encrypt_decrypt_sensitive_field(self):
        """Test sensitive field encryption and decryption."""
        config = EncryptionConfiguration(master_key_env_var="TEST_MASTER_KEY")
        encryptor = StateEncryptor(config)
        
        sensitive_value = "sensitive-password-123"
        
        # Encrypt field
        encrypted_field = encryptor.encrypt_sensitive_field(sensitive_value, "password")
        assert isinstance(encrypted_field, str)
        assert sensitive_value not in encrypted_field
        
        # Decrypt field
        decrypted_value = encryptor.decrypt_sensitive_field(encrypted_field, "password")
        assert decrypted_value == sensitive_value

    def test_unsupported_algorithm_error(self):
        """Test error with unsupported algorithm."""
        # This should be caught by configuration validation
        with pytest.raises(ValueError):
            EncryptionConfiguration(algorithm="unsupported")


class TestSecureStateManager:
    """Tests for secure state manager."""

    @patch.dict('os.environ', {'TEST_MASTER_KEY': 'dGVzdC1rZXktMTIzNDU2Nzg='})
    def test_save_load_secure_state(self):
        """Test saving and loading secure state."""
        config = EncryptionConfiguration(master_key_env_var="TEST_MASTER_KEY")
        manager = SecureStateManager(config)
        
        test_state = {
            "workflow_id": "test-workflow",
            "resources": {
                "instances": ["i-123", "i-456"],
                "vpc_id": "vpc-123"
            }
        }
        
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as tmp_file:
            storage_path = tmp_file.name
        
        try:
            # Save state
            manager.save_secure_state(test_state, storage_path)
            
            # Verify file exists and has secure permissions
            assert os.path.exists(storage_path)
            file_stats = os.stat(storage_path)
            assert oct(file_stats.st_mode)[-3:] == '600'  # Owner read/write only
            
            # Load state
            loaded_state = manager.load_secure_state(storage_path)
            assert loaded_state == test_state
            
        finally:
            if os.path.exists(storage_path):
                os.unlink(storage_path)

    @patch.dict('os.environ', {'TEST_MASTER_KEY': 'dGVzdC1rZXktMTIzNDU2Nzg='})
    def test_load_nonexistent_state(self):
        """Test loading state from nonexistent file."""
        config = EncryptionConfiguration(master_key_env_var="TEST_MASTER_KEY")
        manager = SecureStateManager(config)
        
        # Should return empty dict for nonexistent file
        loaded_state = manager.load_secure_state("/nonexistent/file")
        assert loaded_state == {}

    @patch.dict('os.environ', {'TEST_MASTER_KEY': 'dGVzdC1rZXktMTIzNDU2Nzg='})
    def test_verify_state_integrity(self):
        """Test state integrity verification."""
        config = EncryptionConfiguration(master_key_env_var="TEST_MASTER_KEY")
        manager = SecureStateManager(config)
        
        test_state = {"test": "data"}
        
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as tmp_file:
            storage_path = tmp_file.name
        
        try:
            # Save valid state
            manager.save_secure_state(test_state, storage_path)
            
            # Verify integrity of valid state
            assert manager.verify_state_integrity(storage_path) is True
            
            # Corrupt the file
            with open(storage_path, 'w') as f:
                f.write("invalid json content")
            
            # Verify integrity of corrupted state
            assert manager.verify_state_integrity(storage_path) is False
            
        finally:
            if os.path.exists(storage_path):
                os.unlink(storage_path)

    def test_verify_nonexistent_state_integrity(self):
        """Test integrity verification of nonexistent state."""
        config = EncryptionConfiguration()
        manager = SecureStateManager(config)
        
        # Should return False for nonexistent file
        assert manager.verify_state_integrity("/nonexistent/file") is False
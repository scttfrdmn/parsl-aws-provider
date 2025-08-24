"""State encryption and key management for Parsl Ephemeral AWS Provider.

This module provides secure encryption capabilities for provider state data,
including encryption at rest and key management following security best practices.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import logging
import os
import base64
import json
from typing import Dict, Optional, Any, Union
from dataclasses import dataclass, field
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import secrets

logger = logging.getLogger(__name__)


@dataclass
class EncryptionConfiguration:
    """Configuration for state encryption."""
    
    # Encryption settings
    algorithm: str = "fernet"  # "fernet" or "aes-gcm"
    key_derivation: str = "pbkdf2"  # Key derivation function
    
    # Key management
    master_key_source: str = "env"  # "env", "aws_kms", "file"
    master_key_env_var: str = "PARSL_EPHEMERAL_MASTER_KEY"
    master_key_file_path: Optional[str] = None
    kms_key_id: Optional[str] = None
    
    # Key rotation
    enable_key_rotation: bool = True
    key_rotation_days: int = 90
    
    # Security settings
    salt_length: int = 32
    iterations: int = 100000  # PBKDF2 iterations
    
    def __post_init__(self):
        """Validate configuration."""
        if self.algorithm not in ["fernet", "aes-gcm"]:
            raise ValueError(f"Unsupported encryption algorithm: {self.algorithm}")
        
        if self.master_key_source not in ["env", "aws_kms", "file"]:
            raise ValueError(f"Unsupported master key source: {self.master_key_source}")
        
        if self.master_key_source == "file" and not self.master_key_file_path:
            raise ValueError("master_key_file_path required when using file key source")


class EncryptionKeyManager:
    """Manages encryption keys and key rotation."""
    
    def __init__(self, config: EncryptionConfiguration):
        """Initialize key manager.
        
        Parameters
        ----------
        config : EncryptionConfiguration
            Encryption configuration
        """
        self.config = config
        self._master_key: Optional[bytes] = None
        self._derived_keys: Dict[str, bytes] = {}
    
    def get_master_key(self) -> bytes:
        """Get or derive the master encryption key.
        
        Returns
        -------
        bytes
            Master encryption key
        """
        if self._master_key is None:
            self._master_key = self._load_master_key()
        
        return self._master_key
    
    def _load_master_key(self) -> bytes:
        """Load master key from configured source.
        
        Returns
        -------
        bytes
            Master key
            
        Raises
        ------
        ValueError
            If key cannot be loaded or is invalid
        """
        if self.config.master_key_source == "env":
            key_str = os.environ.get(self.config.master_key_env_var)
            if not key_str:
                raise ValueError(
                    f"Master key not found in environment variable: {self.config.master_key_env_var}"
                )
            
            try:
                # Try to decode as base64 first
                return base64.b64decode(key_str)
            except Exception:
                # Fall back to using the string directly as UTF-8 bytes
                return key_str.encode('utf-8')
        
        elif self.config.master_key_source == "file":
            try:
                with open(self.config.master_key_file_path, 'rb') as f:
                    return f.read()
            except FileNotFoundError:
                raise ValueError(f"Master key file not found: {self.config.master_key_file_path}")
        
        elif self.config.master_key_source == "aws_kms":
            return self._load_kms_key()
        
        else:
            raise ValueError(f"Unsupported master key source: {self.config.master_key_source}")
    
    def _load_kms_key(self) -> bytes:
        """Load key from AWS KMS.
        
        Returns
        -------
        bytes
            Decrypted key from KMS
        """
        try:
            import boto3
            from botocore.exceptions import ClientError
            
            kms = boto3.client('kms')
            
            # For simplicity, we'll use KMS to decrypt a data key
            # In practice, you'd store an encrypted data key and decrypt it with KMS
            response = kms.generate_data_key(
                KeyId=self.config.kms_key_id,
                KeySpec='AES_256'
            )
            
            # Return the plaintext key (KMS automatically handles decryption)
            return response['Plaintext']
            
        except ImportError:
            raise ValueError("boto3 required for AWS KMS key management")
        except ClientError as e:
            raise ValueError(f"Failed to load key from KMS: {e}")
    
    def derive_key(self, context: str, salt: Optional[bytes] = None) -> bytes:
        """Derive a key for specific context.
        
        Parameters
        ----------
        context : str
            Context for key derivation (e.g., "state", "logs")
        salt : Optional[bytes]
            Salt for key derivation. If None, generates new salt
            
        Returns
        -------
        bytes
            Derived key
        """
        if salt is None:
            salt = secrets.token_bytes(self.config.salt_length)
        
        cache_key = f"{context}:{base64.b64encode(salt).decode()}"
        
        if cache_key not in self._derived_keys:
            master_key = self.get_master_key()
            
            # Use PBKDF2 for key derivation
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,  # 256-bit key
                salt=salt,
                iterations=self.config.iterations,
            )
            
            # Include context in the key derivation
            context_bytes = context.encode('utf-8')
            derived_key = kdf.derive(master_key + context_bytes)
            
            self._derived_keys[cache_key] = derived_key
        
        return self._derived_keys[cache_key], salt
    
    def generate_master_key(self) -> str:
        """Generate a new master key for initial setup.
        
        Returns
        -------
        str
            Base64-encoded master key
        """
        key = secrets.token_bytes(32)  # 256-bit key
        return base64.b64encode(key).decode('utf-8')


class StateEncryptor:
    """Encrypts and decrypts provider state data."""
    
    def __init__(self, config: EncryptionConfiguration):
        """Initialize state encryptor.
        
        Parameters
        ----------
        config : EncryptionConfiguration
            Encryption configuration
        """
        self.config = config
        self.key_manager = EncryptionKeyManager(config)
    
    def encrypt_state(self, state_data: Dict[str, Any]) -> Dict[str, Any]:
        """Encrypt provider state data.
        
        Parameters
        ----------
        state_data : Dict[str, Any]
            State data to encrypt
            
        Returns
        -------
        Dict[str, Any]
            Encrypted state with metadata
        """
        # Serialize state data
        plaintext = json.dumps(state_data, sort_keys=True).encode('utf-8')
        
        # Derive encryption key
        encryption_key, salt = self.key_manager.derive_key("state")
        
        # Encrypt based on algorithm
        if self.config.algorithm == "fernet":
            encrypted_data = self._encrypt_fernet(plaintext, encryption_key)
        elif self.config.algorithm == "aes-gcm":
            encrypted_data = self._encrypt_aes_gcm(plaintext, encryption_key)
        else:
            raise ValueError(f"Unsupported encryption algorithm: {self.config.algorithm}")
        
        # Return encrypted state with metadata
        return {
            "encrypted_data": base64.b64encode(encrypted_data).decode('utf-8'),
            "encryption_metadata": {
                "algorithm": self.config.algorithm,
                "key_derivation": self.config.key_derivation,
                "salt": base64.b64encode(salt).decode('utf-8'),
                "iterations": self.config.iterations,
                "version": "1.0"
            }
        }
    
    def decrypt_state(self, encrypted_state: Dict[str, Any]) -> Dict[str, Any]:
        """Decrypt provider state data.
        
        Parameters
        ----------
        encrypted_state : Dict[str, Any]
            Encrypted state with metadata
            
        Returns
        -------
        Dict[str, Any]
            Decrypted state data
        """
        # Extract metadata
        metadata = encrypted_state["encryption_metadata"]
        encrypted_data = base64.b64decode(encrypted_state["encrypted_data"])
        salt = base64.b64decode(metadata["salt"])
        
        # Derive decryption key
        decryption_key, _ = self.key_manager.derive_key("state", salt=salt)
        
        # Decrypt based on algorithm
        if metadata["algorithm"] == "fernet":
            plaintext = self._decrypt_fernet(encrypted_data, decryption_key)
        elif metadata["algorithm"] == "aes-gcm":
            plaintext = self._decrypt_aes_gcm(encrypted_data, decryption_key)
        else:
            raise ValueError(f"Unsupported encryption algorithm: {metadata['algorithm']}")
        
        # Deserialize and return state data
        return json.loads(plaintext.decode('utf-8'))
    
    def _encrypt_fernet(self, plaintext: bytes, key: bytes) -> bytes:
        """Encrypt using Fernet algorithm.
        
        Parameters
        ----------
        plaintext : bytes
            Data to encrypt
        key : bytes
            Encryption key
            
        Returns
        -------
        bytes
            Encrypted data
        """
        # Fernet requires a 32-byte key encoded as base64
        fernet_key = base64.urlsafe_b64encode(key)
        fernet = Fernet(fernet_key)
        return fernet.encrypt(plaintext)
    
    def _decrypt_fernet(self, ciphertext: bytes, key: bytes) -> bytes:
        """Decrypt using Fernet algorithm.
        
        Parameters
        ----------
        ciphertext : bytes
            Encrypted data
        key : bytes
            Decryption key
            
        Returns
        -------
        bytes
            Decrypted data
        """
        fernet_key = base64.urlsafe_b64encode(key)
        fernet = Fernet(fernet_key)
        return fernet.decrypt(ciphertext)
    
    def _encrypt_aes_gcm(self, plaintext: bytes, key: bytes) -> bytes:
        """Encrypt using AES-GCM algorithm.
        
        Parameters
        ----------
        plaintext : bytes
            Data to encrypt
        key : bytes
            Encryption key
            
        Returns
        -------
        bytes
            Encrypted data with nonce and tag
        """
        aesgcm = AESGCM(key)
        nonce = secrets.token_bytes(12)  # 96-bit nonce for GCM
        ciphertext = aesgcm.encrypt(nonce, plaintext, None)
        
        # Prepend nonce to ciphertext for storage
        return nonce + ciphertext
    
    def _decrypt_aes_gcm(self, encrypted_data: bytes, key: bytes) -> bytes:
        """Decrypt using AES-GCM algorithm.
        
        Parameters
        ----------
        encrypted_data : bytes
            Encrypted data with nonce and tag
        key : bytes
            Decryption key
            
        Returns
        -------
        bytes
            Decrypted data
        """
        # Extract nonce and ciphertext
        nonce = encrypted_data[:12]
        ciphertext = encrypted_data[12:]
        
        aesgcm = AESGCM(key)
        return aesgcm.decrypt(nonce, ciphertext, None)
    
    def encrypt_sensitive_field(self, value: str, context: str = "field") -> str:
        """Encrypt a single sensitive field.
        
        Parameters
        ----------
        value : str
            Value to encrypt
        context : str
            Context for key derivation
            
        Returns
        -------
        str
            Base64-encoded encrypted value
        """
        plaintext = value.encode('utf-8')
        encryption_key, salt = self.key_manager.derive_key(context)
        
        if self.config.algorithm == "fernet":
            encrypted_data = self._encrypt_fernet(plaintext, encryption_key)
        else:  # AES-GCM
            encrypted_data = self._encrypt_aes_gcm(plaintext, encryption_key)
        
        # Include salt in encrypted field for decryption
        field_data = {
            "data": base64.b64encode(encrypted_data).decode('utf-8'),
            "salt": base64.b64encode(salt).decode('utf-8')
        }
        
        return base64.b64encode(json.dumps(field_data).encode('utf-8')).decode('utf-8')
    
    def decrypt_sensitive_field(self, encrypted_value: str, context: str = "field") -> str:
        """Decrypt a single sensitive field.
        
        Parameters
        ----------
        encrypted_value : str
            Base64-encoded encrypted value
        context : str
            Context for key derivation
            
        Returns
        -------
        str
            Decrypted value
        """
        # Decode field data
        field_json = base64.b64decode(encrypted_value).decode('utf-8')
        field_data = json.loads(field_json)
        
        encrypted_data = base64.b64decode(field_data["data"])
        salt = base64.b64decode(field_data["salt"])
        
        # Derive decryption key
        decryption_key, _ = self.key_manager.derive_key(context, salt=salt)
        
        if self.config.algorithm == "fernet":
            plaintext = self._decrypt_fernet(encrypted_data, decryption_key)
        else:  # AES-GCM
            plaintext = self._decrypt_aes_gcm(encrypted_data, decryption_key)
        
        return plaintext.decode('utf-8')


class SecureStateManager:
    """Manages secure state persistence with encryption."""
    
    def __init__(self, encryption_config: EncryptionConfiguration):
        """Initialize secure state manager.
        
        Parameters
        ----------
        encryption_config : EncryptionConfiguration
            Encryption configuration
        """
        self.encryption_config = encryption_config
        self.encryptor = StateEncryptor(encryption_config)
    
    def save_secure_state(self, state_data: Dict[str, Any], storage_path: str) -> None:
        """Save encrypted state to storage.
        
        Parameters
        ----------
        state_data : Dict[str, Any]
            State data to encrypt and save
        storage_path : str
            Path to save encrypted state
        """
        try:
            # Encrypt state data
            encrypted_state = self.encryptor.encrypt_state(state_data)
            
            # Save to file with secure permissions
            with open(storage_path, 'w') as f:
                json.dump(encrypted_state, f, indent=2)
            
            # Set secure file permissions (owner read/write only)
            os.chmod(storage_path, 0o600)
            
            logger.info(f"Secure state saved to: {storage_path}")
            
        except Exception as e:
            logger.error(f"Failed to save secure state: {e}")
            raise
    
    def load_secure_state(self, storage_path: str) -> Dict[str, Any]:
        """Load and decrypt state from storage.
        
        Parameters
        ----------
        storage_path : str
            Path to encrypted state file
            
        Returns
        -------
        Dict[str, Any]
            Decrypted state data
        """
        try:
            # Load encrypted state
            with open(storage_path, 'r') as f:
                encrypted_state = json.load(f)
            
            # Decrypt and return state data
            state_data = self.encryptor.decrypt_state(encrypted_state)
            
            logger.info(f"Secure state loaded from: {storage_path}")
            return state_data
            
        except FileNotFoundError:
            logger.warning(f"State file not found: {storage_path}")
            return {}
        except Exception as e:
            logger.error(f"Failed to load secure state: {e}")
            raise
    
    def verify_state_integrity(self, storage_path: str) -> bool:
        """Verify the integrity of encrypted state.
        
        Parameters
        ----------
        storage_path : str
            Path to encrypted state file
            
        Returns
        -------
        bool
            True if state integrity is verified
        """
        try:
            # Try to load and decrypt the state
            self.load_secure_state(storage_path)
            return True
        except Exception as e:
            logger.error(f"State integrity verification failed: {e}")
            return False
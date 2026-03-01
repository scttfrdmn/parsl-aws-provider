"""
File-based state store for the EphemeralAWSProvider.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import json
import logging
import os
from typing import Any, Dict, Optional

try:
    import fcntl

    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

from parsl_ephemeral_aws.exceptions import (
    StateDeserializationError,
    StateSerializationError,
    StateStoreError,
)
from parsl_ephemeral_aws.state.base import StateStore


logger = logging.getLogger(__name__)


class FileStateStore(StateStore):
    """File-based state store implementation.

    Stores provider state in a local JSON file.

    Attributes
    ----------
    file_path : str
        Path to the state file
    provider_id : str
        Unique identifier for the provider instance
    """

    def __init__(self, file_path: str, provider_id: str) -> None:
        """Initialize the file state store.

        Parameters
        ----------
        file_path : str
            Path to the state file
        provider_id : str
            Unique identifier for the provider instance
        """
        super().__init__(provider_id)
        self.file_path = file_path
        logger.debug(f"Initialized FileStateStore with file_path={file_path}")

    def save_state(self, state: Dict[str, Any]) -> None:
        """Save the provider state to a file.

        Parameters
        ----------
        state : Dict[str, Any]
            Provider state to save

        Raises
        ------
        StateSerializationError
            If serializing state fails
        StateStoreError
            If saving state fails
        """
        try:
            # Create directory if it doesn't exist
            os.makedirs(os.path.dirname(os.path.abspath(self.file_path)), exist_ok=True)

            with open(self.file_path, "w") as f:
                if _HAS_FCNTL:
                    fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    json.dump(state, f, indent=2)
                finally:
                    if _HAS_FCNTL:
                        fcntl.flock(f, fcntl.LOCK_UN)

            logger.debug(f"Saved state to {self.file_path}")
        except json.JSONDecodeError as e:
            logger.error(f"Failed to serialize state: {e}")
            raise StateSerializationError(f"Failed to serialize state: {e}") from e
        except OSError as e:
            logger.error(f"Failed to write state file {self.file_path}: {e}")
            raise StateStoreError(
                f"Failed to write state file {self.file_path}: {e}"
            ) from e
        except Exception as e:
            logger.error(f"Unexpected error saving state to {self.file_path}: {e}")
            raise StateStoreError(
                f"Unexpected error saving state to {self.file_path}: {e}"
            ) from e

    def load_state(self) -> Optional[Dict[str, Any]]:
        """Load the provider state from a file.

        Returns
        -------
        Optional[Dict[str, Any]]
            Provider state if the file exists, None otherwise

        Raises
        ------
        StateDeserializationError
            If deserializing state fails
        StateStoreError
            If loading state fails
        """
        if not os.path.exists(self.file_path):
            logger.debug(f"State file {self.file_path} does not exist")
            return None

        try:
            with open(self.file_path, "r") as f:
                if _HAS_FCNTL:
                    fcntl.flock(f, fcntl.LOCK_SH)
                try:
                    state = json.load(f)
                finally:
                    if _HAS_FCNTL:
                        fcntl.flock(f, fcntl.LOCK_UN)

            logger.debug(f"Loaded state from {self.file_path}")
            return state
        except json.JSONDecodeError as e:
            logger.error(f"Failed to deserialize state from {self.file_path}: {e}")
            raise StateDeserializationError(
                f"Failed to deserialize state from {self.file_path}: {e}"
            ) from e
        except OSError as e:
            logger.error(f"Failed to read state file {self.file_path}: {e}")
            raise StateStoreError(
                f"Failed to read state file {self.file_path}: {e}"
            ) from e
        except Exception as e:
            logger.error(f"Unexpected error loading state from {self.file_path}: {e}")
            raise StateStoreError(
                f"Unexpected error loading state from {self.file_path}: {e}"
            ) from e

    def delete_state(self) -> None:
        """Delete the provider state file.

        Raises
        ------
        StateStoreError
            If deleting state fails
        """
        if not os.path.exists(self.file_path):
            logger.debug(
                f"State file {self.file_path} does not exist, nothing to delete"
            )
            return

        try:
            os.remove(self.file_path)
            logger.debug(f"Deleted state file {self.file_path}")
        except OSError as e:
            logger.error(f"Failed to delete state file {self.file_path}: {e}")
            raise StateStoreError(
                f"Failed to delete state file {self.file_path}: {e}"
            ) from e
        except Exception as e:
            logger.error(f"Unexpected error deleting state file {self.file_path}: {e}")
            raise StateStoreError(
                f"Unexpected error deleting state file {self.file_path}: {e}"
            ) from e

"""
File-based state store for the EphemeralProvider.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
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

from parsl_ephemeral_provider.exceptions import (
    StateDeserializationError,
    StateSerializationError,
    StateStoreError,
)
from parsl_ephemeral_provider.state.base import (
    STATE_KEY_MODE,
    STATE_KEY_PROVIDER,
    StateStore,
)


logger = logging.getLogger(__name__)

#: Top-level key holding the per-state-key sub-documents. Its presence is what
#: distinguishes a keyed document from a flat pre-v0.7.0 one.
_DOCUMENTS_KEY = "_states"

#: Written alongside the sub-documents so a reader can tell which layout it has
#: without relying on key-name heuristics.
_VERSION_KEY = "_version"
_VERSION = 2


class FileStateStore(StateStore):
    """File-based state store implementation.

    Stores state in a single local JSON file. Each ``state_key`` is a top-level
    sub-document under ``_states``, so writing one key preserves the others:

    .. code-block:: text

        {"_version": 2, "_states": {"provider": {...}, "mode": {...}}}

    Files written before v0.7.0 hold a single flat document with no ``_states``
    wrapper. Those are read back under every key — the provider and the mode each
    take the fields they recognise — and the first write upgrades the file to the
    keyed layout, seeding both keys from the flat document so the writer's
    counterpart can still find its fields afterwards.

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

    def _read_document(self) -> Dict[str, Any]:
        """Read and parse the whole state file.

        Returns
        -------
        Dict[str, Any]
            The parsed file contents, or an empty dict if the file is absent.

        Raises
        ------
        StateDeserializationError
            If the file exists but is not valid JSON
        StateStoreError
            If the file cannot be read
        """
        if not os.path.exists(self.file_path):
            return {}

        try:
            with open(self.file_path, "r") as f:
                if _HAS_FCNTL:
                    fcntl.flock(f, fcntl.LOCK_SH)
                try:
                    document = json.load(f)
                finally:
                    if _HAS_FCNTL:
                        fcntl.flock(f, fcntl.LOCK_UN)
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

        if not isinstance(document, dict):
            raise StateDeserializationError(
                f"State file {self.file_path} does not contain a JSON object"
            )
        return document

    def _write_document(self, document: Dict[str, Any]) -> None:
        """Serialize and write the whole state file.

        Raises
        ------
        StateSerializationError
            If the document cannot be serialized
        StateStoreError
            If the file cannot be written
        """
        try:
            # Serialize before truncating the file, so a document that cannot be
            # encoded leaves the previous state intact rather than emptying it.
            payload = json.dumps(document, indent=2)
        except (TypeError, ValueError) as e:
            logger.error(f"Failed to serialize state: {e}")
            raise StateSerializationError(f"Failed to serialize state: {e}") from e

        try:
            directory = os.path.dirname(os.path.abspath(self.file_path))
            os.makedirs(directory, exist_ok=True)

            with open(self.file_path, "w") as f:
                if _HAS_FCNTL:
                    fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    f.write(payload)
                finally:
                    if _HAS_FCNTL:
                        fcntl.flock(f, fcntl.LOCK_UN)
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

    def save_state(self, state_key: str, state_data: Dict[str, Any]) -> None:
        """Save a state document under *state_key*.

        Read-modify-write: the other keys in the file are preserved.

        Parameters
        ----------
        state_key : str
            Key to store the document under
        state_data : Dict[str, Any]
            State document to save

        Raises
        ------
        StateSerializationError
            If serializing state fails
        StateStoreError
            If saving state fails
        """
        try:
            existing = self._read_document()
        except StateDeserializationError:
            # An unreadable file must not block the write; the current state is
            # more valuable than a corrupt document we cannot merge into.
            logger.warning(
                f"State file {self.file_path} is not readable JSON; overwriting it"
            )
            existing = {}

        documents = existing.get(_DOCUMENTS_KEY)
        if not isinstance(documents, dict):
            # Upgrading a flat pre-v0.7.0 document. Seed both well-known keys
            # from it: whichever of the provider and the mode writes first would
            # otherwise erase the fields the other has not read yet.
            documents = {}
            if existing:
                documents[STATE_KEY_PROVIDER] = existing
                documents[STATE_KEY_MODE] = existing
                logger.info(
                    f"Upgrading {self.file_path} to keyed state; the previous "
                    "document is retained under both the provider and mode keys"
                )

        documents[state_key] = state_data
        self._write_document({_VERSION_KEY: _VERSION, _DOCUMENTS_KEY: documents})

        logger.debug(f"Saved state key '{state_key}' to {self.file_path}")

    def load_state(self, state_key: str) -> Optional[Dict[str, Any]]:
        """Load the state document stored under *state_key*.

        Parameters
        ----------
        state_key : str
            Key to load the document from

        Returns
        -------
        Optional[Dict[str, Any]]
            State document if present, None otherwise. A flat pre-v0.7.0 file is
            returned whole for any key.

        Raises
        ------
        StateDeserializationError
            If deserializing state fails
        StateStoreError
            If loading state fails
        """
        document = self._read_document()
        if not document:
            logger.debug(f"State file {self.file_path} does not exist or is empty")
            return None

        documents = document.get(_DOCUMENTS_KEY)
        if isinstance(documents, dict):
            state = documents.get(state_key)
            if state is None:
                logger.debug(f"State key '{state_key}' not present in {self.file_path}")
                return None
            logger.debug(f"Loaded state key '{state_key}' from {self.file_path}")
            return state

        # Flat pre-v0.7.0 document: the provider and the mode wrote to the same
        # slot, so hand it to whichever asks and let each pick out its own fields.
        logger.debug(
            f"State file {self.file_path} predates state keys; "
            f"returning the flat document for '{state_key}'"
        )
        return document

    def delete_state(self, state_key: str) -> None:
        """Delete the state document stored under *state_key*.

        The file itself is removed once the last key is gone.

        Parameters
        ----------
        state_key : str
            Key to delete the document for

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
            existing = self._read_document()
        except StateDeserializationError:
            # Unparseable: there is no sub-document to remove selectively, so
            # dropping the file is the only meaningful interpretation.
            existing = {}

        documents = existing.get(_DOCUMENTS_KEY)
        if isinstance(documents, dict):
            documents.pop(state_key, None)
            if documents:
                self._write_document(
                    {_VERSION_KEY: _VERSION, _DOCUMENTS_KEY: documents}
                )
                logger.debug(f"Deleted state key '{state_key}' from {self.file_path}")
                return

        self._remove_file()

    def _remove_file(self) -> None:
        """Delete the state file, tolerating its absence."""
        try:
            os.remove(self.file_path)
            logger.debug(f"Deleted state file {self.file_path}")
        except FileNotFoundError:
            logger.debug(f"State file {self.file_path} already gone")
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

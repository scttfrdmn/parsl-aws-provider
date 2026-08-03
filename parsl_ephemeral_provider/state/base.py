"""
Base state store interface for the EphemeralProvider.

State documents are addressed by a **state key**. The provider and its operating
mode persist different, partially overlapping sets of fields, so each owns its
own key: without that separation the two full-document writes overwrite each
other and mode-only fields (the baked AMI ID, the warm-pool list) or
provider-only fields (``job_map``) are silently destroyed (#78).

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import abc
import logging
from typing import Any, Dict, Optional

import boto3


logger = logging.getLogger(__name__)

#: State key owned by ``EphemeralProvider`` (``resources``, ``job_map``, ...)
STATE_KEY_PROVIDER = "provider"

#: State key owned by the ``OperatingMode`` (network IDs, baked AMI, warm pool, ...)
STATE_KEY_MODE = "mode"


def resolve_session(provider: Any) -> boto3.Session:
    """Return a boto3 session for talking to a provider's account.

    Prefers the session the provider already built — it resolved credentials
    once, correctly, via ``utils.aws.create_session()``. Only when there is none
    does this fall back to assembling a session from whatever credential
    attributes the object happens to carry.

    Every attribute is read with ``getattr`` because this is called with either
    an ``EphemeralProvider`` or an ``OperatingMode``, and neither defines the
    full set. Reading them directly is what made the AWS state stores raise
    ``AttributeError`` on construction (#77).

    Parameters
    ----------
    provider : Any
        An ``EphemeralProvider`` or ``OperatingMode``

    Returns
    -------
    boto3.Session
        A session bound to the provider's region
    """
    existing = getattr(provider, "session", None)
    if isinstance(existing, boto3.Session):
        return existing

    region = getattr(provider, "region", None)

    session_kwargs: Dict[str, Any] = {}
    access_key = getattr(provider, "aws_access_key_id", None)
    secret_key = getattr(provider, "aws_secret_access_key", None)
    if access_key and secret_key:
        session_kwargs["aws_access_key_id"] = access_key
        session_kwargs["aws_secret_access_key"] = secret_key

    session_token = getattr(provider, "aws_session_token", None)
    if session_token:
        session_kwargs["aws_session_token"] = session_token

    # An OperatingMode has no aws_profile; the provider names the field
    # profile_name. Accept either.
    profile = getattr(provider, "aws_profile", None) or getattr(
        provider, "profile_name", None
    )
    if profile:
        session_kwargs["profile_name"] = profile

    return boto3.Session(region_name=region, **session_kwargs)


def get_workflow_id(provider: Any) -> str:
    """Return a provider's workflow identifier, for tagging stored state.

    ``workflow_id`` is the convention the compute managers use; the provider
    itself only guarantees ``provider_id``. Falls back to ``"unknown"`` so a
    missing identifier degrades a tag rather than failing the save.
    """
    return str(
        getattr(provider, "workflow_id", None)
        or getattr(provider, "provider_id", None)
        or "unknown"
    )


def get_provider_id(provider: Any) -> str:
    """Return a provider's own identifier."""
    return str(
        getattr(provider, "provider_id", None)
        or getattr(provider, "workflow_id", None)
        or "unknown"
    )


class StateStore(abc.ABC):
    """Abstract base class for provider state stores.

    A state store persists and retrieves state documents, each addressed by a
    ``state_key``. Different implementations store them in different places —
    local files, AWS Parameter Store, or S3.

    Attributes
    ----------
    provider_id : str
        Unique identifier for the provider instance
    """

    def __init__(self, provider_id: str) -> None:
        """Initialize the state store.

        Parameters
        ----------
        provider_id : str
            Unique identifier for the provider instance
        """
        self.provider_id = provider_id
        logger.debug(f"Initialized {self.__class__.__name__}")

    @abc.abstractmethod
    def save_state(self, state_key: str, state_data: Dict[str, Any]) -> None:
        """Save a state document.

        Writing one key must leave the other keys in the store untouched.

        Parameters
        ----------
        state_key : str
            Key to store the document under
        state_data : Dict[str, Any]
            State document to save

        Raises
        ------
        StateStoreError
            If saving state fails
        """
        pass

    @abc.abstractmethod
    def load_state(self, state_key: str) -> Optional[Dict[str, Any]]:
        """Load a state document.

        Parameters
        ----------
        state_key : str
            Key to load the document from

        Returns
        -------
        Optional[Dict[str, Any]]
            State document if it exists, None otherwise

        Raises
        ------
        StateStoreError
            If loading state fails
        """
        pass

    @abc.abstractmethod
    def delete_state(self, state_key: str) -> None:
        """Delete a state document.

        Deleting a key that does not exist is not an error.

        Parameters
        ----------
        state_key : str
            Key to delete the document for

        Raises
        ------
        StateStoreError
            If deleting state fails
        """
        pass

"""
Base state store interface for the EphemeralAWSProvider.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import abc
import logging
from typing import Any, Dict, Optional


logger = logging.getLogger(__name__)


class StateStore(abc.ABC):
    """Abstract base class for provider state stores.
    
    A state store is responsible for persisting and retrieving the provider's state.
    Different implementations store state in different locations, such as local files,
    AWS Parameter Store, or S3.
    
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
    def save_state(self, state: Dict[str, Any]) -> None:
        """Save the provider state.
        
        Parameters
        ----------
        state : Dict[str, Any]
            Provider state to save
            
        Raises
        ------
        StateStoreError
            If saving state fails
        """
        pass

    @abc.abstractmethod
    def load_state(self) -> Optional[Dict[str, Any]]:
        """Load the provider state.
        
        Returns
        -------
        Optional[Dict[str, Any]]
            Provider state if it exists, None otherwise
            
        Raises
        ------
        StateStoreError
            If loading state fails
        """
        pass

    @abc.abstractmethod
    def delete_state(self) -> None:
        """Delete the provider state.
        
        Raises
        ------
        StateStoreError
            If deleting state fails
        """
        pass
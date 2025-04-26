"""Base state implementation for Parsl Ephemeral AWS Provider.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional


class StateStore(ABC):
    """Base class for state persistence mechanisms."""
    
    @abstractmethod
    def save_state(self, state_key: str, state_data: Dict[str, Any]) -> None:
        """Save provider state.
        
        Parameters
        ----------
        state_key : str
            Key to store the state under
        state_data : Dict[str, Any]
            State data to save
        """
        pass
    
    @abstractmethod
    def load_state(self, state_key: str) -> Optional[Dict[str, Any]]:
        """Load provider state.
        
        Parameters
        ----------
        state_key : str
            Key to load the state from
            
        Returns
        -------
        Optional[Dict[str, Any]]
            Loaded state data, or None if not found
        """
        pass
    
    @abstractmethod
    def delete_state(self, state_key: str) -> None:
        """Delete provider state.
        
        Parameters
        ----------
        state_key : str
            Key to delete the state for
        """
        pass
    
    @abstractmethod
    def list_states(self, prefix: str) -> Dict[str, Dict[str, Any]]:
        """List all states with a given prefix.
        
        Parameters
        ----------
        prefix : str
            Prefix to list states for
            
        Returns
        -------
        Dict[str, Dict[str, Any]]
            Dictionary mapping state keys to state data
        """
        pass
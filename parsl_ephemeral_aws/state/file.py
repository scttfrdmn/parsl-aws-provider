"""File-based state implementation for Parsl Ephemeral AWS Provider.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import json
import logging
import os
import pathlib
import glob
from typing import Dict, Any, Optional, List

from ..exceptions import StateError
from .base import StateStore


logger = logging.getLogger(__name__)


class FileState(StateStore):
    """File-based implementation of state persistence."""
    
    def __init__(self, 
                provider: Any,
                directory: str,
                file_extension: str = '.json') -> None:
        """Initialize file state.
        
        Parameters
        ----------
        provider : EphemeralAWSProvider
            The provider instance
        directory : str
            Directory to store state files in
        file_extension : str, optional
            File extension for state files, by default '.json'
        """
        self.provider = provider
        self.directory = os.path.expanduser(directory)
        self.file_extension = file_extension if file_extension.startswith('.') else f'.{file_extension}'
        
        # Ensure directory exists
        pathlib.Path(self.directory).mkdir(parents=True, exist_ok=True)
    
    def _get_state_path(self, state_key: str) -> str:
        """Get the full path for a state file.
        
        Parameters
        ----------
        state_key : str
            State key
            
        Returns
        -------
        str
            Full path to the state file
        """
        # Convert state key to a safe filename
        safe_key = state_key.replace('/', '_').replace('\\', '_')
        
        # Ensure unique filenames by prefixing with workflow ID
        filename = f"{self.provider.workflow_id}_{safe_key}{self.file_extension}"
        
        return os.path.join(self.directory, filename)
    
    def save_state(self, state_key: str, state_data: Dict[str, Any]) -> None:
        """Save provider state to a file.
        
        Parameters
        ----------
        state_key : str
            Key to store the state under
        state_data : Dict[str, Any]
            State data to save
        """
        try:
            state_path = self._get_state_path(state_key)
            
            # Convert state data to JSON
            state_json = json.dumps(state_data, indent=2)
            
            # Write to file
            with open(state_path, 'w') as f:
                f.write(state_json)
            
            logger.debug(f"Saved state to file: {state_path}")
            
        except Exception as e:
            logger.error(f"Error saving state to file: {e}")
            raise StateError(f"Failed to save state: {e}")
    
    def load_state(self, state_key: str) -> Optional[Dict[str, Any]]:
        """Load provider state from a file.
        
        Parameters
        ----------
        state_key : str
            Key to load the state from
            
        Returns
        -------
        Optional[Dict[str, Any]]
            Loaded state data, or None if not found
        """
        try:
            state_path = self._get_state_path(state_key)
            
            # Check if file exists
            if not os.path.exists(state_path):
                logger.debug(f"State file not found: {state_path}")
                return None
            
            # Read and parse JSON
            with open(state_path, 'r') as f:
                state_json = f.read()
                
            state_data = json.loads(state_json)
            
            logger.debug(f"Loaded state from file: {state_path}")
            
            return state_data
            
        except Exception as e:
            logger.error(f"Error loading state from file: {e}")
            raise StateError(f"Failed to load state: {e}")
    
    def delete_state(self, state_key: str) -> None:
        """Delete provider state file.
        
        Parameters
        ----------
        state_key : str
            Key to delete the state for
        """
        try:
            state_path = self._get_state_path(state_key)
            
            # Check if file exists
            if not os.path.exists(state_path):
                logger.debug(f"State file not found for deletion: {state_path}")
                return
            
            # Delete file
            os.remove(state_path)
            
            logger.debug(f"Deleted state file: {state_path}")
            
        except Exception as e:
            logger.error(f"Error deleting state file: {e}")
            raise StateError(f"Failed to delete state: {e}")
    
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
        try:
            # Convert prefix to a safe filename pattern
            safe_prefix = prefix.replace('/', '_').replace('\\', '_')
            
            # Create a glob pattern to match files
            pattern = os.path.join(self.directory, f"{self.provider.workflow_id}_{safe_prefix}*{self.file_extension}")
            
            # Find matching files
            matched_files = glob.glob(pattern)
            
            # Load state data
            states = {}
            for file_path in matched_files:
                # Extract the state key from the filename
                filename = os.path.basename(file_path)
                # Remove workflow ID prefix and file extension
                state_key = filename[len(self.provider.workflow_id) + 1:-len(self.file_extension)]
                # Convert back to original format (replace underscores with slashes)
                # Note: This is a simplification and may not perfectly reconstruct the original key
                
                # Read and parse the file
                try:
                    with open(file_path, 'r') as f:
                        state_json = f.read()
                        
                    state_data = json.loads(state_json)
                    states[state_key] = state_data
                    
                except Exception as e:
                    logger.warning(f"Failed to load state data from {file_path}: {e}")
            
            return states
            
        except Exception as e:
            logger.error(f"Error listing states from files: {e}")
            raise StateError(f"Failed to list states: {e}")
    
    def cleanup_workflow_states(self) -> None:
        """Clean up all states for the current workflow."""
        try:
            # Create a glob pattern to match workflow files
            pattern = os.path.join(self.directory, f"{self.provider.workflow_id}_*{self.file_extension}")
            
            # Find matching files
            matched_files = glob.glob(pattern)
            
            # Delete files
            for file_path in matched_files:
                try:
                    os.remove(file_path)
                    logger.debug(f"Deleted state file: {file_path}")
                except Exception as e:
                    logger.warning(f"Failed to delete state file {file_path}: {e}")
            
            logger.info(f"Cleaned up {len(matched_files)} workflow state files")
            
        except Exception as e:
            logger.error(f"Error cleaning up workflow state files: {e}")
            raise StateError(f"Failed to clean up workflow states: {e}")
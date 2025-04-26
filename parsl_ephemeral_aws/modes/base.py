"""Base mode implementation for Parsl Ephemeral AWS Provider.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any


class BaseMode(ABC):
    """Base class for all operating modes.
    
    This abstract class defines the interface that all operating modes must implement.
    """
    
    def __init__(self, provider: Any) -> None:
        """Initialize the mode handler.
        
        Parameters
        ----------
        provider : EphemeralAWSProvider
            The provider instance
        """
        self.provider = provider
    
    @abstractmethod
    def submit(self, command: str, tasks_per_node: int, job_name: str = "") -> Dict[str, Any]:
        """Submit a job for execution.
        
        Parameters
        ----------
        command : str
            Command to execute
        tasks_per_node : int
            Number of tasks per node
        job_name : str, optional
            Name for the job, by default ""
            
        Returns
        -------
        Dict[str, Any]
            Dictionary containing job ID
        """
        pass
    
    @abstractmethod
    def status(self, job_ids: List[Any]) -> List[Dict[str, Any]]:
        """Get the status of jobs.
        
        Parameters
        ----------
        job_ids : List[Any]
            List of job IDs to check
            
        Returns
        -------
        List[Dict[str, Any]]
            List of job status dictionaries
        """
        pass
    
    @abstractmethod
    def cancel(self, job_ids: List[Any]) -> List[Dict[str, Any]]:
        """Cancel jobs.
        
        Parameters
        ----------
        job_ids : List[Any]
            List of job IDs to cancel
            
        Returns
        -------
        List[Dict[str, Any]]
            List of job status dictionaries
        """
        pass
    
    @abstractmethod
    def scale_out(self, blocks: int) -> List[str]:
        """Scale out the infrastructure by the specified number of blocks.
        
        Parameters
        ----------
        blocks : int
            Number of blocks to add
            
        Returns
        -------
        List[str]
            List of block IDs
        """
        pass
    
    @abstractmethod
    def scale_in(self, blocks: Optional[int] = None, block_ids: Optional[List[str]] = None) -> List[str]:
        """Scale in the infrastructure.
        
        Parameters
        ----------
        blocks : Optional[int], optional
            Number of blocks to remove, by default None
        block_ids : Optional[List[str]], optional
            Specific block IDs to remove, by default None
            
        Returns
        -------
        List[str]
            List of block IDs removed
        """
        pass
    
    @abstractmethod
    def shutdown(self) -> None:
        """Shutdown the mode handler, releasing all resources."""
        pass
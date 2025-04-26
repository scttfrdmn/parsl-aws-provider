"""Standard mode implementation for Parsl Ephemeral AWS Provider.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import uuid
import logging
from typing import Dict, List, Optional, Any

from ..exceptions import JobSubmissionError
from ..constants import STATUS_PENDING
from .base import BaseMode
from ..compute.ec2 import EC2Manager


logger = logging.getLogger(__name__)


class StandardMode(BaseMode):
    """Standard mode for the EphemeralAWSProvider.
    
    In this mode, the client directly communicates with worker nodes.
    This is suitable for development or smaller workflows where the client
    has a stable internet connection.
    """
    
    def __init__(self, provider: Any) -> None:
        """Initialize the standard mode handler.
        
        Parameters
        ----------
        provider : EphemeralAWSProvider
            The provider instance
        """
        super().__init__(provider)
        self.jobs = {}
        self.blocks = {}
        
        # Initialize compute manager based on worker type
        if self.provider.worker_type == "ec2":
            self.compute_manager = EC2Manager(self.provider)
        else:
            # In standard mode, we only support EC2 instances for now
            # Other worker types will be supported in the future
            logger.warning(f"Worker type '{self.provider.worker_type}' not fully supported in standard mode. Falling back to EC2.")
            self.compute_manager = EC2Manager(self.provider)
    
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
        # Generate a unique ID for this job
        job_id = str(uuid.uuid4())
        
        try:
            # Ensure we have enough resources to run the job
            # If we don't have enough blocks, scale out
            active_blocks = len([b for b in self.blocks.values() if b.get("status") == "RUNNING"])
            
            # Calculate how many blocks we need based on tasks_per_node
            # For simplicity, we'll assume 1 block = 1 node * nodes_per_block
            blocks_needed = max(1, (tasks_per_node + self.provider.nodes_per_block - 1) // self.provider.nodes_per_block)
            
            # If we don't have enough blocks and we're below max_blocks, scale out
            if active_blocks < blocks_needed and len(self.blocks) < self.provider.max_blocks:
                blocks_to_add = min(blocks_needed - active_blocks, self.provider.max_blocks - len(self.blocks))
                if blocks_to_add > 0:
                    self.scale_out(blocks_to_add)
            
            # Create job record
            self.jobs[job_id] = {
                "id": job_id,
                "command": command,
                "tasks_per_node": tasks_per_node,
                "status": STATUS_PENDING,
                "block_id": None  # This will be assigned when the job is scheduled
            }
            
            # Return job information
            return {"job_id": job_id}
        
        except Exception as e:
            logger.error(f"Error submitting job: {e}")
            raise JobSubmissionError(f"Failed to submit job: {e}")
    
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
        results = []
        
        for job_id in job_ids:
            if job_id in self.jobs:
                # Get job information
                job = self.jobs[job_id]
                
                # Check if job is assigned to a block and update status if needed
                if job["block_id"] is not None and job["block_id"] in self.blocks:
                    # If block status changes, update job status
                    block_status = self.blocks[job["block_id"]].get("status")
                    if block_status in ["TERMINATED", "FAILED"]:
                        job["status"] = "FAILED"
                
                # Return job status
                results.append({
                    "job_id": job_id,
                    "status": job["status"]
                })
            else:
                # Job not found
                results.append({
                    "job_id": job_id,
                    "status": "UNKNOWN"
                })
        
        return results
    
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
        results = []
        
        for job_id in job_ids:
            if job_id in self.jobs:
                # Get job information
                job = self.jobs[job_id]
                
                # Update job status
                job["status"] = "CANCELLED"
                
                # Return updated status
                results.append({
                    "job_id": job_id,
                    "status": job["status"]
                })
            else:
                # Job not found
                results.append({
                    "job_id": job_id,
                    "status": "UNKNOWN"
                })
        
        return results
    
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
        block_ids = []
        
        # Ensure we don't exceed max_blocks
        current_blocks = len(self.blocks)
        if current_blocks + blocks > self.provider.max_blocks:
            blocks = max(0, self.provider.max_blocks - current_blocks)
            
        if blocks <= 0:
            return []
            
        try:
            # Create blocks using compute manager
            new_blocks = self.compute_manager.create_blocks(blocks)
            
            # Add blocks to our records
            for block_id, block_info in new_blocks.items():
                self.blocks[block_id] = block_info
                block_ids.append(block_id)
            
            return block_ids
            
        except Exception as e:
            logger.error(f"Error scaling out: {e}")
            
            # Attempt to clean up any partially created resources
            for block_id in block_ids:
                try:
                    self.compute_manager.terminate_block(block_id)
                except Exception as cleanup_e:
                    logger.error(f"Error cleaning up block {block_id}: {cleanup_e}")
            
            # Re-raise the exception
            raise
    
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
        removed_block_ids = []
        
        try:
            # If specific block IDs are provided, remove those
            if block_ids is not None:
                for block_id in block_ids:
                    if block_id in self.blocks:
                        # Terminate the block
                        self.compute_manager.terminate_block(block_id)
                        
                        # Update block status
                        self.blocks[block_id]["status"] = "TERMINATED"
                        
                        # Add to removed list
                        removed_block_ids.append(block_id)
            
            # If number of blocks is provided, remove that many blocks
            elif blocks is not None:
                # Get list of active blocks sorted by creation time (oldest first)
                active_blocks = sorted(
                    [(block_id, block_info) for block_id, block_info in self.blocks.items() 
                     if block_info.get("status") == "RUNNING"],
                    key=lambda x: x[1].get("created_at", 0)
                )
                
                # Don't go below min_blocks
                if len(active_blocks) - blocks < self.provider.min_blocks:
                    blocks = max(0, len(active_blocks) - self.provider.min_blocks)
                
                # Terminate blocks
                for i in range(min(blocks, len(active_blocks))):
                    block_id, _ = active_blocks[i]
                    
                    # Terminate the block
                    self.compute_manager.terminate_block(block_id)
                    
                    # Update block status
                    self.blocks[block_id]["status"] = "TERMINATED"
                    
                    # Add to removed list
                    removed_block_ids.append(block_id)
            
            return removed_block_ids
            
        except Exception as e:
            logger.error(f"Error scaling in: {e}")
            raise
    
    def shutdown(self) -> None:
        """Shutdown the mode handler, releasing all resources."""
        try:
            # Terminate all blocks
            for block_id in list(self.blocks.keys()):
                try:
                    self.compute_manager.terminate_block(block_id)
                    self.blocks[block_id]["status"] = "TERMINATED"
                except Exception as e:
                    logger.error(f"Error terminating block {block_id}: {e}")
            
            # Clean up other resources
            self.compute_manager.cleanup_all_resources()
            
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")
            raise
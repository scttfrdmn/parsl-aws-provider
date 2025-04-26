"""Detached mode implementation for Parsl Ephemeral AWS Provider.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import uuid
import logging
import time
from typing import Dict, List, Optional, Any

from ..exceptions import JobSubmissionError
from ..constants import STATUS_PENDING
from .base import BaseMode
from ..compute.ec2 import EC2Manager


logger = logging.getLogger(__name__)


class DetachedMode(BaseMode):
    """Detached mode for the EphemeralAWSProvider.
    
    In this mode, a bastion/coordinator instance is launched in AWS that manages worker nodes.
    This allows the client to disconnect while computation continues, making it suitable for
    long-running workflows or situations where the client is behind a NAT or has an unstable
    connection.
    """
    
    def __init__(self, provider: Any) -> None:
        """Initialize the detached mode handler.
        
        Parameters
        ----------
        provider : EphemeralAWSProvider
            The provider instance
        """
        super().__init__(provider)
        self.jobs = {}
        self.blocks = {}
        self.bastion_id = None
        
        # Initialize compute manager
        self.compute_manager = EC2Manager(self.provider)
        
        # Launch bastion host
        self._launch_bastion()
    
    def _launch_bastion(self) -> None:
        """Launch the bastion/coordinator host."""
        logger.info("Launching bastion host")
        
        # Create bastion host
        bastion_config = {
            "instance_type": self.provider.bastion_instance_type,
            "is_bastion": True,
            "idle_timeout": self.provider.bastion_idle_timeout,
            "auto_shutdown": self.provider.auto_shutdown
        }
        
        try:
            bastion_info = self.compute_manager.create_bastion(bastion_config)
            self.bastion_id = bastion_info["id"]
            
            # Wait for bastion to be ready
            max_wait = 300  # 5 minutes
            start_time = time.time()
            
            while time.time() - start_time < max_wait:
                status = self.compute_manager.get_instance_status(self.bastion_id)
                if status == "running":
                    logger.info(f"Bastion host {self.bastion_id} is ready")
                    break
                logger.debug(f"Waiting for bastion host {self.bastion_id} to be ready (status: {status})")
                time.sleep(5)
            else:
                raise TimeoutError(f"Timed out waiting for bastion host {self.bastion_id} to be ready")
            
        except Exception as e:
            logger.error(f"Error launching bastion host: {e}")
            
            # Clean up any resources
            if self.bastion_id:
                try:
                    self.compute_manager.terminate_instance(self.bastion_id)
                except Exception as cleanup_e:
                    logger.error(f"Error cleaning up bastion host {self.bastion_id}: {cleanup_e}")
            
            # Re-raise the exception
            raise
    
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
            # Ensure bastion host is running
            if not self.bastion_id:
                raise JobSubmissionError("Bastion host not available")
                
            bastion_status = self.compute_manager.get_instance_status(self.bastion_id)
            if bastion_status != "running":
                raise JobSubmissionError(f"Bastion host not ready (status: {bastion_status})")
            
            # Submit job to the bastion host
            # In a real implementation, this would use SSM/SSH to submit the job
            # For now, we'll just simulate the submission
            
            # Calculate required blocks for the job
            blocks_needed = max(1, (tasks_per_node + self.provider.nodes_per_block - 1) // self.provider.nodes_per_block)
            
            # Create job record
            self.jobs[job_id] = {
                "id": job_id,
                "command": command,
                "tasks_per_node": tasks_per_node,
                "status": STATUS_PENDING,
                "blocks_needed": blocks_needed,
                "block_ids": []  # Will be filled when blocks are assigned
            }
            
            # In a real implementation, the bastion would handle scaling
            # For now, we'll initiate scaling from the client if needed
            active_blocks = len([b for b in self.blocks.values() if b.get("status") == "RUNNING"])
            
            if active_blocks < blocks_needed and len(self.blocks) < self.provider.max_blocks:
                blocks_to_add = min(blocks_needed - active_blocks, self.provider.max_blocks - len(self.blocks))
                if blocks_to_add > 0:
                    self.scale_out(blocks_to_add)
            
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
        
        # Ensure bastion host is running
        if not self.bastion_id:
            # If bastion is not available, mark all jobs as unknown
            return [{"job_id": job_id, "status": "UNKNOWN"} for job_id in job_ids]
            
        bastion_status = self.compute_manager.get_instance_status(self.bastion_id)
        if bastion_status != "running":
            # If bastion is not running, mark all jobs as unknown
            return [{"job_id": job_id, "status": "UNKNOWN"} for job_id in job_ids]
        
        # In a real implementation, this would query the bastion host for job status
        # For now, we'll just return the local status
        for job_id in job_ids:
            if job_id in self.jobs:
                # Get job information
                job = self.jobs[job_id]
                
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
        
        # Ensure bastion host is running
        if not self.bastion_id:
            # If bastion is not available, mark all jobs as unknown
            return [{"job_id": job_id, "status": "UNKNOWN"} for job_id in job_ids]
            
        bastion_status = self.compute_manager.get_instance_status(self.bastion_id)
        if bastion_status != "running":
            # If bastion is not running, mark all jobs as unknown
            return [{"job_id": job_id, "status": "UNKNOWN"} for job_id in job_ids]
        
        # In a real implementation, this would send cancel commands to the bastion host
        # For now, we'll just update the local status
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
            # In a real implementation, this would instruct the bastion to create blocks
            # For now, we'll create blocks directly
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
            # In a real implementation, this would instruct the bastion to terminate blocks
            # For now, we'll terminate blocks directly
            
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
            # In a real implementation, this would instruct the bastion to clean up all resources
            # and then terminate itself
            
            # Terminate all blocks
            for block_id in list(self.blocks.keys()):
                try:
                    self.compute_manager.terminate_block(block_id)
                    self.blocks[block_id]["status"] = "TERMINATED"
                except Exception as e:
                    logger.error(f"Error terminating block {block_id}: {e}")
            
            # Terminate bastion host
            if self.bastion_id:
                try:
                    self.compute_manager.terminate_instance(self.bastion_id)
                except Exception as e:
                    logger.error(f"Error terminating bastion host {self.bastion_id}: {e}")
            
            # Clean up other resources
            self.compute_manager.cleanup_all_resources()
            
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")
            raise
"""Serverless mode implementation for Parsl Ephemeral AWS Provider.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import uuid
import logging
from typing import Dict, List, Optional, Any, Union

from ..exceptions import JobSubmissionError, ConfigurationError
from ..constants import (
    STATUS_PENDING,
    WORKER_TYPE_LAMBDA,
    WORKER_TYPE_ECS,
    WORKER_TYPE_AUTO
)
from .base import BaseMode
from ..compute.lambda_func import LambdaManager
from ..compute.ecs import ECSManager


logger = logging.getLogger(__name__)


class ServerlessMode(BaseMode):
    """Serverless mode for the EphemeralAWSProvider.
    
    In this mode, AWS Lambda and/or ECS/Fargate are used to execute tasks without
    any EC2 instances. This is best for event-driven or sporadic workloads with
    short-running tasks.
    """
    
    def __init__(self, provider: Any) -> None:
        """Initialize the serverless mode handler.
        
        Parameters
        ----------
        provider : EphemeralAWSProvider
            The provider instance
        """
        super().__init__(provider)
        self.jobs = {}
        self.worker_instances = {}
        
        # Validate worker type
        if self.provider.worker_type not in [WORKER_TYPE_LAMBDA, WORKER_TYPE_ECS, WORKER_TYPE_AUTO]:
            raise ConfigurationError(
                f"Serverless mode requires worker_type to be '{WORKER_TYPE_LAMBDA}', '{WORKER_TYPE_ECS}', or '{WORKER_TYPE_AUTO}'"
            )
        
        # Initialize compute managers based on worker type
        self.lambda_manager = None
        self.ecs_manager = None
        
        if self.provider.worker_type in [WORKER_TYPE_LAMBDA, WORKER_TYPE_AUTO]:
            self.lambda_manager = LambdaManager(self.provider)
            
        if self.provider.worker_type in [WORKER_TYPE_ECS, WORKER_TYPE_AUTO]:
            self.ecs_manager = ECSManager(self.provider)
    
    def _select_worker_type(self, command: str, tasks_per_node: int) -> str:
        """Select the appropriate worker type for a job.
        
        Parameters
        ----------
        command : str
            Command to execute
        tasks_per_node : int
            Number of tasks per node
            
        Returns
        -------
        str
            Worker type to use (lambda or ecs)
        """
        # If worker type is not auto, use the configured type
        if self.provider.worker_type != WORKER_TYPE_AUTO:
            return self.provider.worker_type
        
        # For auto mode, select based on job characteristics
        
        # Use Lambda for short, simple jobs
        if len(command) < 5000 and tasks_per_node <= 1:
            return WORKER_TYPE_LAMBDA
        
        # Otherwise use ECS
        return WORKER_TYPE_ECS
    
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
            # Select worker type
            worker_type = self._select_worker_type(command, tasks_per_node)
            
            # Submit job to the appropriate service
            if worker_type == WORKER_TYPE_LAMBDA:
                if not self.lambda_manager:
                    raise JobSubmissionError("Lambda manager not available")
                
                # Submit to Lambda
                lambda_job_info = self.lambda_manager.submit_job(job_id, command)
                
                # Create job record
                self.jobs[job_id] = {
                    "id": job_id,
                    "command": command,
                    "tasks_per_node": tasks_per_node,
                    "status": STATUS_PENDING,
                    "worker_type": WORKER_TYPE_LAMBDA,
                    "lambda_function_name": lambda_job_info["function_name"],
                    "lambda_request_id": lambda_job_info["request_id"]
                }
                
            elif worker_type == WORKER_TYPE_ECS:
                if not self.ecs_manager:
                    raise JobSubmissionError("ECS manager not available")
                
                # Submit to ECS
                ecs_job_info = self.ecs_manager.submit_job(job_id, command, tasks_per_node)
                
                # Create job record
                self.jobs[job_id] = {
                    "id": job_id,
                    "command": command,
                    "tasks_per_node": tasks_per_node,
                    "status": STATUS_PENDING,
                    "worker_type": WORKER_TYPE_ECS,
                    "ecs_cluster": ecs_job_info["cluster"],
                    "ecs_task_id": ecs_job_info["task_id"]
                }
                
            else:
                raise JobSubmissionError(f"Unsupported worker type: {worker_type}")
            
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
                
                # Update status based on worker type
                if job["worker_type"] == WORKER_TYPE_LAMBDA:
                    if self.lambda_manager:
                        lambda_status = self.lambda_manager.get_job_status(
                            job["lambda_function_name"], job["lambda_request_id"]
                        )
                        job["status"] = lambda_status
                        
                elif job["worker_type"] == WORKER_TYPE_ECS:
                    if self.ecs_manager:
                        ecs_status = self.ecs_manager.get_job_status(
                            job["ecs_cluster"], job["ecs_task_id"]
                        )
                        job["status"] = ecs_status
                
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
                
                # Cancel job based on worker type
                if job["worker_type"] == WORKER_TYPE_LAMBDA:
                    if self.lambda_manager:
                        # Lambda invocations can't be cancelled once started
                        # Update status locally
                        job["status"] = "CANCELLED"
                        
                elif job["worker_type"] == WORKER_TYPE_ECS:
                    if self.ecs_manager:
                        # Stop the ECS task
                        self.ecs_manager.cancel_job(job["ecs_cluster"], job["ecs_task_id"])
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
        
        In serverless mode, this is a no-op as resources are provisioned on-demand.
        
        Parameters
        ----------
        blocks : int
            Number of blocks to add
            
        Returns
        -------
        List[str]
            Empty list (no blocks in serverless mode)
        """
        # In serverless mode, there are no blocks to scale out
        # Resources are provisioned on-demand
        logger.info("Scale out operation ignored in serverless mode (resources are provisioned on-demand)")
        return []
    
    def scale_in(self, blocks: Optional[int] = None, block_ids: Optional[List[str]] = None) -> List[str]:
        """Scale in the infrastructure.
        
        In serverless mode, this is a no-op as resources are provisioned on-demand.
        
        Parameters
        ----------
        blocks : Optional[int], optional
            Number of blocks to remove, by default None
        block_ids : Optional[List[str]], optional
            Specific block IDs to remove, by default None
            
        Returns
        -------
        List[str]
            Empty list (no blocks in serverless mode)
        """
        # In serverless mode, there are no blocks to scale in
        # Resources are provisioned on-demand
        logger.info("Scale in operation ignored in serverless mode (resources are provisioned on-demand)")
        return []
    
    def shutdown(self) -> None:
        """Shutdown the mode handler, releasing all resources."""
        try:
            # Clean up Lambda resources
            if self.lambda_manager:
                self.lambda_manager.cleanup_all_resources()
            
            # Clean up ECS resources
            if self.ecs_manager:
                self.ecs_manager.cleanup_all_resources()
            
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")
            raise
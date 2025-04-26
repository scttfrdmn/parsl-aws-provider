"""EphemeralAWSProvider implementation.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

from typing import Dict, List, Optional, Union, Any
import uuid
import logging

from parsl.providers.base import ExecutionProvider
from parsl.launchers import Launcher

from .constants import (
    DEFAULT_INSTANCE_TYPE, 
    DEFAULT_REGION,
    MODE_STANDARD,
    MODE_DETACHED,
    MODE_SERVERLESS,
    STATE_STORE_PARAMETER,
    STATE_STORE_S3,
    STATE_STORE_FILE,
    STATE_STORE_NONE,
    WORKER_TYPE_EC2,
    WORKER_TYPE_LAMBDA,
    WORKER_TYPE_ECS,
    WORKER_TYPE_AUTO
)
from .modes.standard import StandardMode
from .modes.detached import DetachedMode
from .modes.serverless import ServerlessMode


logger = logging.getLogger(__name__)


class EphemeralAWSProvider(ExecutionProvider):
    """Ephemeral AWS Provider for Parsl.
    
    This provider creates ephemeral AWS resources for executing Parsl workflows.
    All resources (including VPC, security groups, etc.) are cleaned up automatically
    when no longer needed.
    
    Parameters
    ----------
    image_id : str
        AWS AMI ID to use for instances
    instance_type : str, optional
        EC2 instance type to use, by default 't3.medium'
    region : str, optional
        AWS region to use, by default 'us-east-1'
    init_blocks : int, optional
        Initial number of blocks to provision, by default 1
    min_blocks : int, optional
        Minimum number of blocks to maintain, by default 0
    max_blocks : int, optional
        Maximum number of blocks to provision, by default 10
    nodes_per_block : int, optional
        Number of nodes per block, by default 1
    use_spot_instances : bool, optional
        Whether to use spot instances, by default False
    spot_max_price_percentage : int, optional
        Maximum spot price as percentage of on-demand, by default 80
    spot_interruption_behavior : str, optional
        What to do if spot instance is interrupted, by default 'terminate'
    mode : str, optional
        Operating mode: 'standard', 'detached', or 'serverless', by default 'standard'
    state_store : str, optional
        State persistence mechanism, by default 'parameter_store'
    state_prefix : str, optional
        Prefix for state storage keys, by default '/parsl/workflows'
    use_public_ips : bool, optional
        Whether to assign public IPs to instances, by default True
    worker_init : str, optional
        Commands to run on worker startup, by default ''
    worker_type : str, optional
        Type of worker to use, by default 'ec2'
    launcher : Launcher, optional
        Parsl launcher to use
    bastion_instance_type : str, optional
        EC2 instance type for bastion host, by default 't3.micro'
    bastion_idle_timeout : int, optional
        Minutes to wait before shutting down idle bastion, by default 30
    auto_shutdown : bool, optional
        Whether to automatically shut down when idle, by default True
    lambda_memory : int, optional
        Memory in MB for Lambda functions, by default 1024
    lambda_timeout : int, optional
        Timeout in seconds for Lambda functions, by default 900
    ecs_task_cpu : int, optional
        CPU units for ECS tasks, by default 1024
    ecs_task_memory : int, optional
        Memory in MB for ECS tasks, by default 2048
    ecs_container_image : str, optional
        Container image for ECS tasks, by default None
    use_ec2_fleet : bool, optional
        Whether to use EC2 Fleet for instance provisioning, by default False
    instance_types : List[Dict], optional
        List of instance types to use with EC2 Fleet, by default None
    workflow_id : str, optional
        Unique ID for the workflow, by default None (auto-generated)
    aws_access_key_id : str, optional
        AWS access key ID, by default None (uses environment or instance profile)
    aws_secret_access_key : str, optional
        AWS secret access key, by default None (uses environment or instance profile)
    aws_session_token : str, optional
        AWS session token, by default None (uses environment or instance profile)
    aws_profile : str, optional
        AWS profile name, by default None (uses default profile)
    tags : Dict[str, str], optional
        Additional tags to apply to AWS resources, by default None
    """
    
    def __init__(
        self,
        image_id: str,
        instance_type: str = DEFAULT_INSTANCE_TYPE,
        region: str = DEFAULT_REGION,
        init_blocks: int = 1,
        min_blocks: int = 0,
        max_blocks: int = 10,
        nodes_per_block: int = 1,
        use_spot_instances: bool = False,
        spot_max_price_percentage: int = 80,
        spot_interruption_behavior: str = 'terminate',
        mode: str = MODE_STANDARD,
        state_store: str = STATE_STORE_PARAMETER,
        state_prefix: str = '/parsl/workflows',
        use_public_ips: bool = True,
        worker_init: str = '',
        worker_type: str = WORKER_TYPE_EC2,
        launcher: Optional[Launcher] = None,
        bastion_instance_type: str = 't3.micro',
        bastion_idle_timeout: int = 30,
        auto_shutdown: bool = True,
        lambda_memory: int = 1024,
        lambda_timeout: int = 900,
        ecs_task_cpu: int = 1024,
        ecs_task_memory: int = 2048,
        ecs_container_image: Optional[str] = None,
        use_ec2_fleet: bool = False,
        instance_types: Optional[List[Dict[str, Any]]] = None,
        workflow_id: Optional[str] = None,
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
        aws_session_token: Optional[str] = None,
        aws_profile: Optional[str] = None,
        tags: Optional[Dict[str, str]] = None,
    ) -> None:
        """Initialize the Ephemeral AWS Provider."""
        super().__init__()
        
        # Generate a unique ID for this workflow if not provided
        self.workflow_id = workflow_id or f"parsl-{str(uuid.uuid4())[:8]}"
        
        # Store configuration
        self.image_id = image_id
        self.instance_type = instance_type
        self.region = region
        self.init_blocks = init_blocks
        self.min_blocks = min_blocks
        self.max_blocks = max_blocks
        self.nodes_per_block = nodes_per_block
        self.use_spot_instances = use_spot_instances
        self.spot_max_price_percentage = spot_max_price_percentage
        self.spot_interruption_behavior = spot_interruption_behavior
        self.mode = mode.lower()
        self.state_store = state_store
        self.state_prefix = state_prefix
        self.use_public_ips = use_public_ips
        self.worker_init = worker_init
        self.worker_type = worker_type
        self.launcher = launcher
        self.bastion_instance_type = bastion_instance_type
        self.bastion_idle_timeout = bastion_idle_timeout
        self.auto_shutdown = auto_shutdown
        self.lambda_memory = lambda_memory
        self.lambda_timeout = lambda_timeout
        self.ecs_task_cpu = ecs_task_cpu
        self.ecs_task_memory = ecs_task_memory
        self.ecs_container_image = ecs_container_image
        self.use_ec2_fleet = use_ec2_fleet
        self.instance_types = instance_types or []
        
        # AWS authentication
        self.aws_access_key_id = aws_access_key_id
        self.aws_secret_access_key = aws_secret_access_key
        self.aws_session_token = aws_session_token
        self.aws_profile = aws_profile
        
        # Additional tags
        self.tags = tags or {}
        
        # Initialize state
        self.resources = {}
        self.blocks = {}
        
        # Validate configuration
        self._validate_configuration()
        
        # Initialize mode handler
        if self.mode == MODE_STANDARD:
            self.mode_handler = StandardMode(self)
        elif self.mode == MODE_DETACHED:
            self.mode_handler = DetachedMode(self)
        elif self.mode == MODE_SERVERLESS:
            self.mode_handler = ServerlessMode(self)
        else:
            raise ValueError(f"Invalid mode: {mode}. Must be '{MODE_STANDARD}', '{MODE_DETACHED}', or '{MODE_SERVERLESS}'.")
        
        # Set up initial blocks if requested
        if self.init_blocks > 0:
            logger.info(f"Initializing {self.init_blocks} blocks")
            self.scale_out(self.init_blocks)
        
    def _validate_configuration(self) -> None:
        """Validate the provider configuration."""
        
        # Validate mode
        if self.mode not in [MODE_STANDARD, MODE_DETACHED, MODE_SERVERLESS]:
            raise ValueError(f"Invalid mode: {self.mode}. Must be '{MODE_STANDARD}', '{MODE_DETACHED}', or '{MODE_SERVERLESS}'.")
        
        # Validate worker type
        if self.worker_type not in [WORKER_TYPE_EC2, WORKER_TYPE_LAMBDA, WORKER_TYPE_ECS, WORKER_TYPE_AUTO]:
            raise ValueError(f"Invalid worker type: {self.worker_type}. Must be '{WORKER_TYPE_EC2}', '{WORKER_TYPE_LAMBDA}', '{WORKER_TYPE_ECS}', or '{WORKER_TYPE_AUTO}'.")
        
        # Validate state store
        if self.state_store not in [STATE_STORE_PARAMETER, STATE_STORE_S3, STATE_STORE_FILE, STATE_STORE_NONE]:
            raise ValueError(f"Invalid state store: {self.state_store}. Must be '{STATE_STORE_PARAMETER}', '{STATE_STORE_S3}', '{STATE_STORE_FILE}', or '{STATE_STORE_NONE}'.")
        
        # Validate block counts
        if self.max_blocks < self.min_blocks:
            raise ValueError(f"max_blocks ({self.max_blocks}) cannot be less than min_blocks ({self.min_blocks}).")
            
        # Serverless mode requires lambda or ecs worker type
        if self.mode == MODE_SERVERLESS and self.worker_type == WORKER_TYPE_EC2:
            raise ValueError(f"Serverless mode requires worker_type to be '{WORKER_TYPE_LAMBDA}', '{WORKER_TYPE_ECS}', or '{WORKER_TYPE_AUTO}'.")
            
        # EC2 fleet requires instance types
        if self.use_ec2_fleet and not self.instance_types:
            raise ValueError("EC2 Fleet requires instance_types to be specified.")
            
        # Lambda worker type validation
        if self.worker_type == WORKER_TYPE_LAMBDA:
            if self.lambda_memory < 128 or self.lambda_memory > 10240:
                raise ValueError(f"Lambda memory must be between 128 and 10240 MB, got {self.lambda_memory} MB.")
            if self.lambda_timeout < 1 or self.lambda_timeout > 900:
                raise ValueError(f"Lambda timeout must be between 1 and 900 seconds, got {self.lambda_timeout} seconds.")
                
        # ECS worker type validation
        if self.worker_type == WORKER_TYPE_ECS:
            if self.ecs_task_cpu < 256:
                raise ValueError(f"ECS task CPU must be at least 256 units, got {self.ecs_task_cpu} units.")
            if self.ecs_task_memory < 512:
                raise ValueError(f"ECS task memory must be at least 512 MB, got {self.ecs_task_memory} MB.")
        
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
        logger.info(f"Submitting job: {command[:50]}{'...' if len(command) > 50 else ''}")
        return self.mode_handler.submit(command, tasks_per_node, job_name)
    
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
        logger.debug(f"Checking status of {len(job_ids)} jobs")
        return self.mode_handler.status(job_ids)
    
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
        logger.info(f"Cancelling {len(job_ids)} jobs")
        return self.mode_handler.cancel(job_ids)
    
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
        logger.info(f"Scaling out by {blocks} blocks")
        return self.mode_handler.scale_out(blocks)
    
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
        if blocks is not None:
            logger.info(f"Scaling in by {blocks} blocks")
        elif block_ids is not None:
            logger.info(f"Scaling in blocks: {block_ids}")
        return self.mode_handler.scale_in(blocks, block_ids)
    
    def shutdown(self) -> None:
        """Shutdown the provider, releasing all resources."""
        logger.info("Shutting down provider")
        return self.mode_handler.shutdown()
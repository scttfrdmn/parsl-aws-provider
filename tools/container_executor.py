#!/usr/bin/env python3
"""
Container-enabled HighThroughputExecutor for Parsl.

Based on Globus Compute's proven container implementation approach.
"""

import logging
import shlex
from typing import Optional, Literal

from parsl.executors.high_throughput.executor import HighThroughputExecutor

logger = logging.getLogger(__name__)

# Container command templates (copied from Globus Compute)
DOCKER_CMD_TEMPLATE = "{cmd} run {options} -v {rundir}:{rundir} -t {image} {command}"
APPTAINER_CMD_TEMPLATE = "{cmd} run {options} {image} {command}"

# Valid container types (from Globus Compute)
_DOCKER_TYPES = ("docker", "podman", "podman-hpc")
_APPTAINER_TYPES = ("apptainer", "singularity")
VALID_CONTAINER_TYPES = _DOCKER_TYPES + _APPTAINER_TYPES + ("custom", None)


class ContainerHighThroughputExecutor(HighThroughputExecutor):
    """HighThroughputExecutor with native container support.
    
    This executor extends Parsl's HighThroughputExecutor to support containerized 
    task execution using the same proven approach as Globus Compute's GlobusComputeEngine.
    
    The key insight is to modify the executor's launch_cmd BEFORE starting execution,
    rather than trying to wrap commands during runtime.
    """
    
    def __init__(
        self,
        container_image: Optional[str] = None,
        container_runtime: Literal[VALID_CONTAINER_TYPES] = "docker",  # type: ignore
        container_options: Optional[str] = None,
        **kwargs
    ):
        """Initialize ContainerHighThroughputExecutor.
        
        Parameters
        ----------
        container_image : str, optional
            Container image to use for task execution (e.g., "python:3.10-slim")
            If None, tasks run directly on the host
            
        container_runtime : str
            Container runtime to use ("docker", "podman", "singularity", etc.)
            Default: "docker"
            
        container_options : str, optional  
            Additional options to pass to container runtime
            Default: "--rm --network host" for Docker
            
        **kwargs
            All other arguments passed to HighThroughputExecutor
        """
        super().__init__(**kwargs)
        
        self.container_image = container_image
        self.container_runtime = container_runtime
        
        # Set default container options based on runtime
        if container_options is None:
            if container_runtime in _DOCKER_TYPES:
                container_options = "--rm --network host"
            else:
                container_options = ""
        
        self.container_options = container_options
        
        # Validate container configuration
        if container_image and container_runtime not in VALID_CONTAINER_TYPES:
            raise ValueError(f"Invalid container_runtime: {container_runtime}. "
                           f"Must be one of: {VALID_CONTAINER_TYPES}")
        
        logger.info(f"ContainerHighThroughputExecutor initialized")
        if container_image:
            logger.info(f"  Container image: {container_image}")
            logger.info(f"  Container runtime: {container_runtime}")
            logger.info(f"  Container options: {container_options}")
        else:
            logger.info("  No container - running tasks directly on host")
    
    def containerized_launch_cmd(self) -> str:
        """Create containerized version of launch_cmd.
        
        This method applies the same approach used by Globus Compute:
        wrap the original launch_cmd with container runtime.
        """
        launch_cmd = self.launch_cmd
        assert launch_cmd, "launch_cmd must be set"
        
        # Handle worker script availability in container using Globus approach
        if "process_worker_pool.py" in launch_cmd:
            # Use Globus Compute's exact pattern: replace script with python module execution
            import re
            container_launch_cmd = re.sub(
                r"process_worker_pool\.py",
                "python3 -m parsl.executors.high_throughput.process_worker_pool",
                launch_cmd,
            )
            
            # For SSH over SSM tunnels: replace 127.0.0.1 with Docker bridge IP for container access
            container_launch_cmd = re.sub(
                r"-a [^-]*127\.0\.0\.1[^-]*",
                lambda m: m.group(0).replace("127.0.0.1", "172.17.0.1"),
                container_launch_cmd
            )
            
            # Install Parsl in container at startup
            container_launch_cmd = f"bash -c 'pip install --no-cache-dir parsl && exec {container_launch_cmd}'"
        else:
            container_launch_cmd = launch_cmd
        
        if self.container_runtime in _DOCKER_TYPES:
            # Use Globus Compute's exact approach: same paths in container as host
            # This avoids path remapping issues that cause container worker to exit
            
            # Add volume mounts and environment for container execution
            extra_options = f"-v /tmp:/tmp -e PYTHONUNBUFFERED=1"
            all_options = f"{extra_options} {self.container_options or ''}".strip()
            
            # Use sudo docker for AWS instances where ubuntu user needs sudo for Docker
            docker_cmd = f"sudo {self.container_runtime}" if self.container_runtime == "docker" else self.container_runtime
            
            # Use Globus's exact template pattern
            containerized_cmd = DOCKER_CMD_TEMPLATE.format(
                cmd=docker_cmd,
                image=self.container_image,
                rundir=self.run_dir,  # Use SAME path in container as host
                command=container_launch_cmd,
                options=all_options,
            )
        elif self.container_runtime in _APPTAINER_TYPES:
            # Use Globus's Apptainer template  
            containerized_cmd = APPTAINER_CMD_TEMPLATE.format(
                cmd=self.container_runtime,
                image=self.container_image,
                command=container_launch_cmd,
                options=self.container_options or "",
            )
        elif self.container_runtime == "custom":
            # Custom container command (user-defined template)
            assert self.container_options, "container_options required for custom container_runtime"
            template = self.container_options.replace("{EXECUTOR_RUNDIR}", str(self.run_dir))
            containerized_cmd = template.replace("{EXECUTOR_LAUNCH_CMD}", container_launch_cmd)
        else:
            raise ValueError(f"Unsupported container_runtime: {self.container_runtime}")
        
        # Clean up extra whitespace (from Globus code)
        containerized_cmd = " ".join(shlex.split(containerized_cmd))
        
        return containerized_cmd
    
    def start(self):
        """Start the executor with container support.
        
        This is the KEY METHOD where we apply Globus's approach:
        modify launch_cmd AFTER super().start() completes formatting
        """
        # First, let the parent initialize_scaling() format the base command
        super().start()
        
        # NOW apply containerization to the fully formatted launch_cmd  
        if self.container_image:
            original_cmd = self.launch_cmd
            self.launch_cmd = self.containerized_launch_cmd()
            
            logger.info("=" * 60)
            logger.info("CONTAINER EXECUTION ENABLED")
            logger.info(f"Formatted command: {original_cmd}")
            logger.info(f"Container command: {self.launch_cmd}")
            logger.info("=" * 60)
            
            logger.info(f"✅ ContainerHighThroughputExecutor started with {self.container_image}")
    
    def _get_launch_command(self, block_id: str) -> str:
        """Override to ensure container commands are used for block launching."""
        logger.info(f"🔍 _get_launch_command called for block {block_id}, container_image: {self.container_image}")
        
        # Return the already containerized launch_cmd (set in start() method)
        # The containerization was already applied in start(), so we just substitute block_id
        command = self.launch_cmd.replace("{block_id}", str(block_id))
        logger.info(f"🐳 Final command with block_id: {command}")
        return command
    
    def scale_out(self, blocks: int = 1) -> list:
        """Scale out with container support.""" 
        result = super().scale_out(blocks)
        if self.container_image:
            logger.info(f"Scaled out {blocks} containerized blocks using {self.container_image}")
        return result
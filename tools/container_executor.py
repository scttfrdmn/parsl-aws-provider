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
        **kwargs,
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
            raise ValueError(
                f"Invalid container_runtime: {container_runtime}. "
                f"Must be one of: {VALID_CONTAINER_TYPES}"
            )

        logger.info("ContainerHighThroughputExecutor initialized")
        if container_image:
            logger.info(f"  Container image: {container_image}")
            logger.info(f"  Container runtime: {container_runtime}")
            logger.info(f"  Container options: {container_options}")
        else:
            logger.info("  No container - running tasks directly on host")

    def containerized_launch_cmd(self) -> str:
        """Create containerized version of launch_cmd using Globus Compute's exact approach."""
        launch_cmd = self.launch_cmd
        assert launch_cmd, "launch_cmd must be set"

        # Handle script availability in container - use module execution
        if "process_worker_pool.py" in launch_cmd:
            import re
            launch_cmd = re.sub(
                r"process_worker_pool\.py",
                "python3 -m parsl.executors.high_throughput.process_worker_pool",
                launch_cmd,
            )

        # Use Globus Compute's exact approach: simple template substitution
        if self.container_runtime in _DOCKER_TYPES:
            # Add essential volume mounts and options for container execution
            # CRITICAL: Use host networking to preserve SSH tunnels (Globus pattern)
            extra_options = "-v /tmp:/tmp -e PYTHONUNBUFFERED=1 --network host"
            all_options = f"{extra_options} {self.container_options or ''}".strip()

            # Use sudo docker for AWS instances where ubuntu user needs sudo for Docker
            docker_cmd = (
                f"sudo {self.container_runtime}"
                if self.container_runtime == "docker"
                else self.container_runtime
            )

            # CRITICAL: Ensure Parsl is available in container before running worker
            # Install specific Parsl version to match interchange
            import parsl
            parsl_version = parsl.__version__
            
            # CRITICAL FIX: Proper quoting for bash -c to prevent shell parsing errors
            # Need to escape the entire bash -c command for shell execution
            escaped_launch_cmd = launch_cmd.replace("'", "'\"'\"'")
            bash_command = f"pip install --no-cache-dir parsl=={parsl_version} && exec {escaped_launch_cmd}"
            # Double quote the entire bash -c command to survive shell parsing
            parsl_install_cmd = f'bash -c "{bash_command}"'
            
            containerized_cmd = f"{docker_cmd} run {all_options} -w /tmp {self.container_image} {parsl_install_cmd}"
            
            # DEBUG: Log the exact command being generated
            logger.info(f"🔍 EXACT CONTAINERIZED COMMAND: {containerized_cmd}")
            logger.info(f"🔍 BASH COMMAND PART: {parsl_install_cmd}")
        elif self.container_runtime in _APPTAINER_TYPES:
            # Use Globus's Apptainer template
            containerized_cmd = APPTAINER_CMD_TEMPLATE.format(
                cmd=self.container_runtime,
                image=self.container_image,
                command=launch_cmd,
                options=self.container_options or "",
            )
        elif self.container_runtime == "custom":
            # Custom container command (user-defined template)
            assert (
                self.container_options
            ), "container_options required for custom container_runtime"
            template = self.container_options.replace(
                "{EXECUTOR_RUNDIR}", str(self.run_dir)
            )
            containerized_cmd = template.replace(
                "{EXECUTOR_LAUNCH_CMD}", launch_cmd
            )
        else:
            raise ValueError(f"Unsupported container_runtime: {self.container_runtime}")

        # Return the containerized command as-is (preserve quoting)
        return containerized_cmd

    def start(self):
        """Start the executor with container support - Globus Compute's exact approach."""
        logger.info("ContainerHighThroughputExecutor.start() called")
        
        # First, let the parent start normally
        super().start()
        logger.info(f"After super().start(), launch_cmd: {self.launch_cmd}")

        # Then apply containerization exactly like Globus Compute
        if self.container_image:
            logger.info(f"Applying containerization for image: {self.container_image}")
            self.launch_cmd = self.containerized_launch_cmd()
            logger.info(f"Containerized launch cmd: {self.launch_cmd}")
        else:
            logger.info("No container image specified - using host execution")

    def _get_launch_command(self, block_id: str) -> str:
        """Return containerized launch command with block_id substituted."""
        # Simple substitution like Globus Compute
        return self.launch_cmd.replace("{block_id}", str(block_id))

    def scale_out(self, blocks: int = 1) -> list:
        """Scale out with container support."""
        result = super().scale_out(blocks)
        if self.container_image:
            logger.info(
                f"Scaled out {blocks} containerized blocks using {self.container_image}"
            )
        return result

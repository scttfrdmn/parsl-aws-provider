#!/usr/bin/env python3
"""
SSM Tunneling Support for Phase 1.5 Enhanced AWS Provider.

Provides NAT/firewall traversal capabilities using AWS SSM port forwarding,
enabling Parsl workers to run in private subnets without internet access.
"""

import asyncio
import json
import logging
import re
import socket
import subprocess
import threading
import time
from typing import Dict, Optional, Tuple
from dataclasses import dataclass

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class SSMTunnelError(Exception):
    """Base exception for SSM tunneling issues."""

    pass


class TunnelCreationError(SSMTunnelError):
    """Failed to create SSM tunnel."""

    pass


class SSMAgentTimeoutError(SSMTunnelError):
    """SSM agent not ready within timeout."""

    pass


class TunnelHealthError(SSMTunnelError):
    """Tunnel became unhealthy during operation."""

    pass


class PortExhaustionError(SSMTunnelError):
    """No ports available in the configured range."""

    pass


@dataclass
class TunnelConfig:
    """Configuration for SSM tunnel."""

    instance_id: str
    local_port: int
    remote_port: int
    job_id: str


class PortAllocator:
    """Thread-safe port allocation for SSM tunnels."""

    def __init__(self, port_range: Tuple[int, int] = (50000, 60000)):
        """Initialize port allocator with given range."""
        self.min_port, self.max_port = port_range
        self.allocated_ports = set()
        self.lock = threading.Lock()

        logger.info(
            f"PortAllocator initialized with range {self.min_port}-{self.max_port}"
        )

    def allocate_port(self) -> int:
        """Allocate next available port."""
        with self.lock:
            for port in range(self.min_port, self.max_port + 1):
                if port not in self.allocated_ports and self._is_port_free(port):
                    self.allocated_ports.add(port)
                    logger.debug(f"Allocated port {port}")
                    return port

            raise PortExhaustionError(
                f"No free ports in range {self.min_port}-{self.max_port}. "
                f"Allocated: {len(self.allocated_ports)}"
            )

    def release_port(self, port: int) -> None:
        """Release allocated port."""
        with self.lock:
            if port in self.allocated_ports:
                self.allocated_ports.discard(port)
                logger.debug(f"Released port {port}")

    def _is_port_free(self, port: int) -> bool:
        """Check if port is actually free on the system."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", port))
                return True
        except OSError:
            return False


class TunnelSession:
    """Manages individual SSM tunnel lifecycle."""

    def __init__(self, config: TunnelConfig, session: boto3.Session):
        """Initialize tunnel session."""
        self.config = config
        self.session = session
        self.process: Optional[subprocess.Popen] = None
        self.is_ready = False
        self.start_time: Optional[float] = None
        self._health_check_thread: Optional[threading.Thread] = None
        self._stop_health_check = threading.Event()

        logger.info(
            f"TunnelSession created for job {config.job_id}: "
            f"{config.local_port} -> {config.instance_id}:{config.remote_port}"
        )

    def start(self) -> bool:
        """Start SSM tunnel process."""
        try:
            cmd = [
                "aws",
                "ssm",
                "start-session",
                "--target",
                self.config.instance_id,
                "--document-name",
                "AWS-StartPortForwardingSession",
                "--parameters",
                json.dumps(
                    {
                        "portNumber": [str(self.config.remote_port)],
                        "localPortNumber": [str(self.config.local_port)],
                    }
                ),
                "--region",
                self.session.region_name,
            ]

            logger.info(f"Starting SSM tunnel: {' '.join(cmd)}")

            # Ensure Session Manager Plugin is in PATH
            import os

            env = os.environ.copy()
            home_bin = os.path.expanduser("~/.local/bin")
            if home_bin not in env.get("PATH", ""):
                env["PATH"] = f"{home_bin}:{env.get('PATH', '')}"

            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE,
                env=env,
            )

            self.start_time = time.time()

            # Start health check thread
            self._health_check_thread = threading.Thread(
                target=self._health_check_loop, daemon=True
            )
            self._health_check_thread.start()

            logger.info(f"SSM tunnel process started with PID {self.process.pid}")
            return True

        except Exception as e:
            logger.error(f"Failed to start SSM tunnel: {e}")
            self.cleanup()
            raise TunnelCreationError(f"Failed to start tunnel: {e}")

    def wait_for_ready(self, timeout: int = 60) -> bool:
        """Wait for tunnel to be operational."""
        logger.info(f"Waiting up to {timeout}s for tunnel to be ready...")

        start_time = time.time()

        while time.time() - start_time < timeout:
            if self.is_healthy():
                self.is_ready = True
                ready_time = time.time() - self.start_time if self.start_time else 0
                logger.info(f"Tunnel ready after {ready_time:.1f}s")
                return True

            if self.process and self.process.poll() is not None:
                # Process has exited
                returncode = self.process.returncode
                stdout, stderr = self.process.communicate(timeout=5)
                logger.error(f"SSM tunnel process exited with code {returncode}")
                logger.error(f"Stdout: {stdout.decode()}")
                logger.error(f"Stderr: {stderr.decode()}")
                raise TunnelCreationError(f"SSM tunnel failed with code {returncode}")

            time.sleep(2)

        logger.error(f"Tunnel not ready after {timeout}s")
        return False

    def is_healthy(self) -> bool:
        """Check if tunnel is still working."""
        if not self.process or self.process.poll() is not None:
            return False

        # Test local port connectivity
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(2)
                result = sock.connect_ex(("127.0.0.1", self.config.local_port))
                return result == 0
        except Exception:
            return False

    def _health_check_loop(self):
        """Continuous health monitoring."""
        logger.debug(f"Health check thread started for tunnel {self.config.job_id}")

        while not self._stop_health_check.wait(30):  # Check every 30 seconds
            if not self.is_healthy():
                logger.warning(f"Tunnel {self.config.job_id} became unhealthy")
                break

        logger.debug(f"Health check thread stopping for tunnel {self.config.job_id}")

    def cleanup(self) -> None:
        """Clean up tunnel process and resources."""
        logger.info(f"Cleaning up tunnel for job {self.config.job_id}")

        # Stop health check
        self._stop_health_check.set()
        if self._health_check_thread and self._health_check_thread.is_alive():
            self._health_check_thread.join(timeout=5)

        # Terminate process
        if self.process:
            try:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    logger.warning("Tunnel process didn't terminate, killing...")
                    self.process.kill()
                    self.process.wait(timeout=5)
            except Exception as e:
                logger.error(f"Error terminating tunnel process: {e}")

        self.is_ready = False
        logger.info(f"Tunnel cleanup complete for job {self.config.job_id}")


class ParslWorkerCommandParser:
    """Parses and modifies Parsl worker commands for tunneling."""

    @staticmethod
    def parse_addresses_and_port(command: str) -> Dict[str, Optional[str]]:
        """Extract addresses and port from worker command."""
        # Example: "process_worker_pool.py -a 127.0.0.1,47.157.77.146 --port=54304 ..."

        # Extract addresses (can be -a or --addresses)
        addr_match = re.search(r"(?:--addresses|-a)\s+([^\s]+)", command)
        addresses = addr_match.group(1) if addr_match else None

        # Extract port (can be --port=X, --port X, or -p X)
        port_match = re.search(r"--port[=\s]+(\d+)", command)
        if not port_match:
            # Try -p format
            port_match = re.search(r"-p\s+(\d+)", command)
        port = port_match.group(1) if port_match else None

        # Also try to find port in the format --port={worker_port} (Parsl template)
        if not port:
            port_match = re.search(r"--port=\{worker_port\}", command)
            if port_match:
                port = "template"

        logger.debug(f"Parsed command - addresses: {addresses}, port: {port}")

        return {"addresses": addresses, "port": port, "original_command": command}

    @staticmethod
    def modify_for_tunnel(command: str, tunnel_port: int) -> str:
        """Replace controller addresses and port with localhost tunnel."""

        parsed = ParslWorkerCommandParser.parse_addresses_and_port(command)

        if not parsed["addresses"] or not parsed["port"]:
            raise ValueError(f"Could not parse Parsl worker command: {command}")

        modified = command

        # Replace addresses with localhost (handle both -a and --addresses)
        if f"--addresses {parsed['addresses']}" in modified:
            modified = modified.replace(
                f"--addresses {parsed['addresses']}", "--addresses 127.0.0.1"
            )
        elif f"-a {parsed['addresses']}" in modified:
            modified = modified.replace(f"-a {parsed['addresses']}", "-a 127.0.0.1")

        # Replace the port with the tunnel port (handle --port=X, --port X, and -p X)
        original_port = parsed["port"]
        if f"--port={original_port}" in modified:
            modified = modified.replace(
                f"--port={original_port}", f"--port={tunnel_port}"
            )
        elif f"--port {original_port}" in modified:
            modified = modified.replace(
                f"--port {original_port}", f"--port {tunnel_port}"
            )
        elif f"-p {original_port}" in modified:
            modified = modified.replace(f"-p {original_port}", f"-p {tunnel_port}")

        # Fix problematic parameters that cause worker failures
        # Keep --cert_dir None to disable certificate authentication
        # (Certificate files are not available on remote instances via SSM tunneling)

        # Fix --logdir with local paths -> --logdir /tmp/parsl_logs
        import re

        modified = re.sub(
            r"--logdir=[^\s]*Users[^\s]*", "--logdir=/tmp/parsl_logs", modified
        )
        modified = re.sub(
            r"--logdir [^\s]*Users[^\s]*", "--logdir /tmp/parsl_logs", modified
        )

        logger.info(f"Modified worker command for tunnel on port {tunnel_port}")
        logger.debug(
            f"Original: --addresses {parsed['addresses']}, --port {original_port}"
        )
        logger.debug(f"Modified: --addresses 127.0.0.1, --port {tunnel_port}")
        logger.debug(f"Final command: {modified}")

        return modified


class SSMTunnelManager:
    """Manages SSM port forwarding tunnels for Parsl workers."""

    def __init__(
        self, session: boto3.Session, port_range: Tuple[int, int] = (50000, 60000)
    ):
        """Initialize tunnel manager."""
        self.session = session
        self.port_allocator = PortAllocator(port_range)
        self.active_tunnels: Dict[str, TunnelSession] = {}  # job_id -> TunnelSession
        self.ssm_client = session.client("ssm")

        logger.info(f"SSMTunnelManager initialized for region {session.region_name}")

    async def create_tunnel_for_job(
        self, instance_id: str, job_id: str, controller_port: int
    ) -> TunnelSession:
        """Create SSM tunnel for specific job."""
        logger.info(f"Creating tunnel for job {job_id} to instance {instance_id}")

        # Wait for SSM agent to be ready
        await self._wait_for_ssm_agent(instance_id, timeout=180)

        # Allocate local port
        local_port = self.port_allocator.allocate_port()

        try:
            # Create tunnel configuration
            config = TunnelConfig(
                instance_id=instance_id,
                local_port=local_port,
                remote_port=controller_port,
                job_id=job_id,
            )

            # Create and start tunnel session
            tunnel = TunnelSession(config, self.session)
            tunnel.start()

            # Wait for tunnel to be ready
            if not tunnel.wait_for_ready(timeout=60):
                raise TunnelCreationError(
                    f"Tunnel for job {job_id} failed to become ready"
                )

            # Store active tunnel
            self.active_tunnels[job_id] = tunnel

            logger.info(
                f"Tunnel created successfully for job {job_id}: "
                f"localhost:{local_port} -> {instance_id}:{controller_port}"
            )

            return tunnel

        except Exception as e:
            # Clean up on failure
            self.port_allocator.release_port(local_port)
            logger.error(f"Failed to create tunnel for job {job_id}: {e}")
            raise

    def cleanup_job_tunnels(self, job_id: str) -> None:
        """Clean up all tunnels for a job."""
        if job_id in self.active_tunnels:
            tunnel = self.active_tunnels[job_id]

            # Release port
            self.port_allocator.release_port(tunnel.config.local_port)

            # Cleanup tunnel
            tunnel.cleanup()

            # Remove from active tunnels
            del self.active_tunnels[job_id]

            logger.info(f"Cleaned up tunnel for job {job_id}")

    def cleanup_all_tunnels(self) -> None:
        """Clean up all active tunnels."""
        logger.info(f"Cleaning up {len(self.active_tunnels)} active tunnels")

        for job_id in list(self.active_tunnels.keys()):
            self.cleanup_job_tunnels(job_id)

    def modify_worker_command(self, original_command: str, job_id: str) -> str:
        """Replace controller addresses with localhost tunnel."""
        if job_id not in self.active_tunnels:
            raise ValueError(f"No active tunnel for job {job_id}")

        tunnel = self.active_tunnels[job_id]
        return ParslWorkerCommandParser.modify_for_tunnel(
            original_command, tunnel.config.local_port
        )

    async def _wait_for_ssm_agent(self, instance_id: str, timeout: int = 300):
        """Wait for SSM agent to be ready on instance with exponential backoff."""
        logger.info(
            f"Waiting for SSM agent on instance {instance_id} (timeout: {timeout}s)..."
        )

        start_time = time.time()
        check_count = 0
        base_delay = 5
        max_delay = 30

        while time.time() - start_time < timeout:
            check_count += 1
            elapsed = time.time() - start_time

            try:
                # Check instance state first
                ec2_client = self.session.client("ec2")
                instance_response = ec2_client.describe_instances(
                    InstanceIds=[instance_id]
                )
                instance = instance_response["Reservations"][0]["Instances"][0]
                instance_state = instance["State"]["Name"]

                if instance_state != "running":
                    logger.debug(
                        f"Check {check_count}: Instance {instance_id} state: {instance_state} (waiting for running)"
                    )
                    await asyncio.sleep(min(base_delay, max_delay))
                    continue

                # Instance is running, check SSM agent
                response = self.ssm_client.describe_instance_information(
                    Filters=[
                        {"Key": "InstanceIds", "Values": [instance_id]},
                        {"Key": "PingStatus", "Values": ["Online"]},
                    ]
                )

                if response["InstanceInformationList"]:
                    instance_info = response["InstanceInformationList"][0]
                    ping_status = instance_info["PingStatus"]
                    last_ping = instance_info.get("LastPingDateTime", "Unknown")

                    if ping_status == "Online":
                        ready_time = time.time() - start_time
                        logger.info(
                            f"✅ SSM agent ready on {instance_id} after {ready_time:.1f}s ({check_count} checks)"
                        )
                        logger.debug(f"   Last ping: {last_ping}")
                        logger.debug(
                            f"   Platform: {instance_info.get('PlatformType', 'Unknown')}"
                        )
                        return
                    else:
                        logger.debug(
                            f"Check {check_count}: SSM agent status: {ping_status}"
                        )
                else:
                    logger.debug(f"Check {check_count}: SSM agent not registered yet")

            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code", "Unknown")
                if error_code in ["InvalidInstanceID.NotFound", "InvalidInstanceId"]:
                    logger.debug(
                        f"Check {check_count}: Instance {instance_id} not found yet"
                    )
                else:
                    logger.debug(f"Check {check_count}: SSM check failed: {error_code}")
            except Exception as e:
                logger.debug(f"Check {check_count}: Unexpected error: {e}")

            # Exponential backoff with jitter
            delay = min(base_delay * (1.2 ** (check_count // 5)), max_delay)
            jitter = delay * 0.1 * (0.5 + __import__("random").random() * 0.5)
            actual_delay = delay + jitter

            logger.debug(
                f"Check {check_count}: Waiting {actual_delay:.1f}s (elapsed: {elapsed:.1f}s)"
            )
            await asyncio.sleep(actual_delay)

        raise SSMAgentTimeoutError(
            f"SSM agent not ready on {instance_id} within {timeout}s after {check_count} checks"
        )

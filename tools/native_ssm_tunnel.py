#!/usr/bin/env python3
"""
Native AWS SSM Port Forwarding for Parsl worker connectivity.
Uses AWS's built-in port forwarding capabilities instead of SSH tunnels.
"""

import subprocess
import time
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class NativeSSMTunnel:
    """Native AWS SSM port forwarding for reliable bidirectional connectivity."""

    def __init__(self, session, region: str):
        """Initialize native SSM tunnel manager."""
        self.session = session
        self.region = region
        self.active_tunnels: Dict[str, Dict[str, Any]] = {}

    def create_port_forward_tunnel(
        self,
        instance_id: str,
        remote_port: int,
        local_port: int,
        aws_profile: str = "aws",
    ) -> Optional[subprocess.Popen]:
        """Create native AWS SSM port forwarding tunnel."""
        try:
            # Use AWS CLI's native port forwarding
            # This creates: localhost:local_port -> instance:remote_port
            ssm_cmd = [
                "aws",
                "ssm",
                "start-session",
                "--target",
                instance_id,
                "--document-name",
                "AWS-StartPortForwardingSession",
                "--parameters",
                f"portNumber={remote_port},localPortNumber={local_port}",
                "--region",
                self.region,
                "--profile",
                aws_profile,
            ]

            logger.info(
                f"Creating native SSM port forward: localhost:{local_port} -> {instance_id}:{remote_port}"
            )
            logger.debug(f"SSM command: {' '.join(ssm_cmd)}")

            # Start port forwarding session
            proc = subprocess.Popen(
                ssm_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )

            # Give it time to establish
            time.sleep(5)

            # Check if tunnel is working
            if proc.poll() is None:
                # Still running, likely successful
                self.active_tunnels[instance_id] = {
                    "process": proc,
                    "local_port": local_port,
                    "remote_port": remote_port,
                    "type": "native_ssm",
                }
                logger.info(
                    f"✅ Native SSM tunnel established: localhost:{local_port} -> {instance_id}:{remote_port}"
                )
                return proc
            else:
                # Process exited, tunnel failed
                stdout, stderr = proc.communicate()
                logger.error(f"❌ Native SSM tunnel failed for {instance_id}: {stderr}")
                return None

        except Exception as e:
            logger.error(f"Error creating native SSM tunnel for {instance_id}: {e}")
            return None

    def cleanup_tunnel(self, instance_id: str):
        """Clean up port forwarding tunnel for instance."""
        if instance_id in self.active_tunnels:
            tunnel = self.active_tunnels[instance_id]
            try:
                proc = tunnel["process"]
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=10)
                logger.info(f"Cleaned up native SSM tunnel for {instance_id}")
            except Exception as e:
                logger.error(f"Error cleaning up tunnel for {instance_id}: {e}")

            del self.active_tunnels[instance_id]

    def cleanup_all_tunnels(self):
        """Clean up all active port forwarding tunnels."""
        instance_ids = list(self.active_tunnels.keys())
        for instance_id in instance_ids:
            self.cleanup_tunnel(instance_id)

#!/usr/bin/env python3
"""
SSH Reverse Tunneling over SSM for Parsl worker connectivity.
Enables AWS workers to connect back to local Parsl interchange.
"""

import subprocess
import time
import logging
import os
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class SSMSSHTunnel:
    """SSH tunnel over SSM with reverse port forwarding capability."""

    def __init__(self, session, region: str):
        """Initialize SSH tunnel manager."""
        self.session = session
        self.region = region
        self.active_tunnels: Dict[str, Dict[str, Any]] = {}

    def setup_ssh_config(self):
        """Set up SSH config for SSM ProxyCommand."""
        ssh_config_content = """
# SSH over Session Manager for Parsl
Host i-* mi-*
    ProxyCommand sh -c "aws ssm start-session --target %h --document-name AWS-StartSSHSession --parameters 'portNumber=%p' --region {region} --profile aws"
    User ubuntu
    IdentityFile ~/.ssh/parsl_ssm_rsa
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel ERROR
    ServerAliveInterval 60
    ServerAliveCountMax 3
""".format(region=self.region)

        ssh_config_path = Path.home() / ".ssh" / "config"
        ssh_config_path.parent.mkdir(exist_ok=True)

        # Backup existing config if it exists
        if ssh_config_path.exists():
            backup_path = ssh_config_path.with_suffix(".config.backup")
            if not backup_path.exists():
                ssh_config_path.rename(backup_path)
                logger.info(f"Backed up SSH config to {backup_path}")

        # Write new config
        with open(ssh_config_path, "w") as f:
            f.write(ssh_config_content)

        logger.info(f"SSH config updated for SSM tunneling: {ssh_config_path}")
        return ssh_config_path

    def generate_ssh_key(self, key_name: str = "parsl_ssm") -> tuple[str, str]:
        """Generate SSH key pair for tunnel authentication."""
        ssh_dir = Path.home() / ".ssh"
        ssh_dir.mkdir(exist_ok=True)

        private_key_path = ssh_dir / f"{key_name}_rsa"
        public_key_path = ssh_dir / f"{key_name}_rsa.pub"

        # Generate key pair if it doesn't exist
        if not private_key_path.exists():
            cmd = [
                "ssh-keygen",
                "-t",
                "rsa",
                "-b",
                "2048",
                "-f",
                str(private_key_path),
                "-N",
                "",  # No passphrase
                "-C",
                f"parsl-aws-provider-{int(time.time())}",
            ]

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise Exception(f"Failed to generate SSH key: {result.stderr}")

            # Set proper permissions
            os.chmod(private_key_path, 0o600)
            logger.info(f"Generated SSH key pair: {private_key_path}")

        return str(private_key_path), str(public_key_path)

    def _wait_for_ssm_agent(self, instance_id: str, max_attempts: int = 24) -> bool:
        """Wait for SSM agent to be ready with robust retry logic."""
        ssm_client = self.session.client("ssm")
        logger.info(f"⏳ Waiting for SSM agent on {instance_id} (up to {max_attempts * 10} seconds)...")

        for attempt in range(max_attempts):
            try:
                # Check if instance is managed by SSM
                response = ssm_client.describe_instance_information(
                    Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
                )

                if response["InstanceInformationList"]:
                    instance_info = response["InstanceInformationList"][0]
                    ping_status = instance_info.get("PingStatus", "Unknown")

                    if ping_status == "Online":
                        logger.info(f"✅ SSM agent ready on {instance_id} after {(attempt + 1) * 10} seconds")
                        
                        # Additional verification: test actual session connectivity
                        try:
                            test_response = ssm_client.start_session(
                                Target=instance_id,
                                DocumentName="AWS-StartSSHSession",
                                Parameters={"portNumber": ["22"]}
                            )
                            # Immediately terminate the test session
                            ssm_client.terminate_session(SessionId=test_response['SessionId'])
                            logger.info(f"✅ SSM session connectivity verified for {instance_id}")
                            return True
                        except Exception as e:
                            logger.warning(f"⚠️ SSM agent online but session test failed: {e}, retrying...")
                            time.sleep(5)
                            continue
                    else:
                        if attempt % 6 == 0:  # Log every minute
                            logger.info(f"⏳ SSM agent status: {ping_status} (attempt {attempt + 1}/{max_attempts})")
                else:
                    if attempt % 6 == 0:  # Log every minute  
                        logger.info(f"⏳ Instance {instance_id} not yet managed by SSM (attempt {attempt + 1}/{max_attempts})")

            except Exception as e:
                if attempt % 6 == 0:  # Log every minute
                    logger.info(f"⏳ SSM check error (attempt {attempt + 1}/{max_attempts}): {e}")

            time.sleep(10)  # Wait 10 seconds between checks

        logger.error(f"❌ SSM agent not ready after {max_attempts * 10} seconds")
        return False

    def install_ssh_key_on_instance(
        self, instance_id: str, public_key_path: str
    ) -> bool:
        """Install SSH public key on AWS instance for authentication."""
        try:
            # Read public key
            with open(public_key_path, "r") as f:
                public_key = f.read().strip()

            # Wait for SSM agent to be ready first
            logger.info(f"Waiting for SSM agent to be ready on {instance_id}...")
            if not self._wait_for_ssm_agent(instance_id):
                logger.error(f"SSM agent not ready on {instance_id}")
                return False

            # Install key via SSM with more comprehensive setup
            ssm_client = self.session.client("ssm")
            response = ssm_client.send_command(
                InstanceIds=[instance_id],
                DocumentName="AWS-RunShellScript",
                Parameters={
                    "commands": [
                        "#!/bin/bash",
                        "set -e",
                        # Determine the correct user (ec2-user on Amazon Linux, ubuntu on Ubuntu, etc.)
                        "if id ec2-user >/dev/null 2>&1; then",
                        "  TARGET_USER=ec2-user",
                        "  USER_HOME=/home/ec2-user",
                        "elif id ubuntu >/dev/null 2>&1; then",
                        "  TARGET_USER=ubuntu",
                        "  USER_HOME=/home/ubuntu",
                        "else",
                        "  TARGET_USER=$(whoami)",
                        "  USER_HOME=$HOME",
                        "fi",
                        'echo "Setting up SSH key for user: $TARGET_USER in $USER_HOME"',
                        # Create SSH directory with proper permissions
                        "mkdir -p $USER_HOME/.ssh",
                        "chmod 700 $USER_HOME/.ssh",
                        # Install the public key (create new file, don't append)
                        f'echo "{public_key}" > $USER_HOME/.ssh/authorized_keys',
                        "chmod 600 $USER_HOME/.ssh/authorized_keys",
                        "chown -R $TARGET_USER:$TARGET_USER $USER_HOME/.ssh",
                        # Verify key installation
                        "ls -la $USER_HOME/.ssh/",
                        'echo "SSH key installed successfully for $TARGET_USER"',
                    ]
                },
                TimeoutSeconds=60,  # Longer timeout
            )

            command_id = response["Command"]["CommandId"]

            # Wait for command to complete with longer timeout
            for attempt in range(20):  # Increased attempts
                time.sleep(5)  # Longer delay between checks
                result = ssm_client.get_command_invocation(
                    CommandId=command_id, InstanceId=instance_id
                )
                if result["Status"] in ["Success", "Failed"]:
                    break
                logger.debug(
                    f"SSH key installation attempt {attempt + 1}/20, status: {result['Status']}"
                )

            if result["Status"] == "Success":
                logger.info(f"SSH key installed on {instance_id}")
                logger.debug(
                    f"SSH key installation output: {result.get('StandardOutputContent', '')}"
                )
                return True
            else:
                stdout = result.get("StandardOutputContent", "")
                stderr = result.get("StandardErrorContent", "")
                logger.error(f"Failed to install SSH key on {instance_id}")
                logger.error(f"Status: {result['Status']}")
                logger.error(f"stdout: {stdout}")
                logger.error(f"stderr: {stderr}")
                return False

        except Exception as e:
            logger.error(f"Error installing SSH key on {instance_id}: {e}")
            return False

    def create_reverse_tunnel(
        self, instance_id: str, local_port: int, remote_port: int, private_key_path: str
    ) -> Optional[subprocess.Popen]:
        """Create reverse SSH tunnel from AWS instance back to local machine."""

        try:
            # SSH command for reverse tunnel
            # -R remote_port:localhost:local_port creates reverse tunnel
            ssh_cmd = [
                "ssh",
                "-i",
                private_key_path,
                "-R",
                f"{remote_port}:localhost:{local_port}",
                "-N",  # Don't execute command, just forward ports
                "-f",  # Background mode
                "-o",
                "ExitOnForwardFailure=yes",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                "-o",
                "LogLevel=ERROR",
                instance_id,
            ]

            logger.info(
                f"Creating reverse tunnel: {instance_id}:{remote_port} -> localhost:{local_port}"
            )
            logger.debug(f"SSH command: {' '.join(ssh_cmd)}")

            # Start reverse tunnel
            proc = subprocess.Popen(
                ssh_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )

            # Give it time to establish
            time.sleep(3)

            # Check if tunnel is working
            if proc.poll() is None:
                # Still running, likely successful
                self.active_tunnels[instance_id] = {
                    "process": proc,
                    "local_port": local_port,
                    "remote_port": remote_port,
                    "type": "reverse",
                }
                logger.info(
                    f"✅ Reverse tunnel established: {instance_id}:{remote_port} -> localhost:{local_port}"
                )
                return proc
            else:
                # Process exited, tunnel failed
                stdout, stderr = proc.communicate()
                logger.error(f"❌ Reverse tunnel failed for {instance_id}: {stderr}")
                return None

        except Exception as e:
            logger.error(f"Error creating reverse tunnel for {instance_id}: {e}")
            return None

    def cleanup_tunnel(self, instance_id: str):
        """Clean up reverse tunnel for instance."""
        if instance_id in self.active_tunnels:
            tunnel = self.active_tunnels[instance_id]
            try:
                proc = tunnel["process"]
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=10)
                logger.info(f"Cleaned up reverse tunnel for {instance_id}")
            except Exception as e:
                logger.error(f"Error cleaning up tunnel for {instance_id}: {e}")

            del self.active_tunnels[instance_id]

    def cleanup_all_tunnels(self):
        """Clean up all active reverse tunnels."""
        instance_ids = list(self.active_tunnels.keys())
        for instance_id in instance_ids:
            self.cleanup_tunnel(instance_id)

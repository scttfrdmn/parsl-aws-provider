#!/usr/bin/env python3
"""Container runtime management for Parsl AWS Provider Phase 2."""

import asyncio
import json
import logging
import re
import subprocess
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class ContainerRuntimeError(Exception):
    """Exception raised for container runtime errors."""
    pass


class DockerRuntimeManager:
    """Manages Docker container execution on EC2 instances for Parsl workers."""
    
    def __init__(self, provider):
        """Initialize Docker runtime manager.
        
        Args:
            provider: AWSProvider instance for EC2 communication
        """
        self.provider = provider
        self.installation_cache = {}  # Track Docker installation status per instance
        self.container_cache = {}     # Track running containers per instance
        
    async def ensure_docker_installed(self, instance_id: str) -> bool:
        """Ensure Docker is installed and configured on EC2 instance.
        
        Args:
            instance_id: EC2 instance ID
            
        Returns:
            bool: True if Docker is ready, False if installation failed
        """
        if instance_id in self.installation_cache:
            return self.installation_cache[instance_id]
            
        logger.info(f"Installing Docker on instance {instance_id}")
        
        try:
            # Docker installation commands for Ubuntu
            install_commands = [
                # Update system packages
                "sudo apt-get update -qq",
                
                # Install Docker from Ubuntu repository (fastest method)
                "sudo apt-get install -y docker.io",
                
                # Start and enable Docker service
                "sudo systemctl start docker",
                "sudo systemctl enable docker",
                
                # Add ubuntu user to docker group for passwordless access
                "sudo usermod -a -G docker ubuntu",
                
                # Verify Docker installation
                "docker --version",
                "sudo docker run hello-world"
            ]
            
            for cmd in install_commands:
                result = await self.provider.execute_remote_command(instance_id, cmd, timeout=180)
                if result.returncode != 0:
                    logger.error(f"Command failed: {cmd}")
                    logger.error(f"Error output: {result.stderr}")
                    raise ContainerRuntimeError(f"Docker installation failed: {result.stderr}")
                    
            logger.info(f"Docker successfully installed on {instance_id}")
            self.installation_cache[instance_id] = True
            return True
            
        except Exception as e:
            logger.error(f"Docker installation failed on {instance_id}: {e}")
            self.installation_cache[instance_id] = False
            return False
    
    def wrap_worker_command_for_container(self, worker_command: str, container_config: Dict) -> str:
        """Wrap Parsl worker command for Docker container execution.
        
        Args:
            worker_command: Original Parsl worker command
            container_config: Container configuration dictionary
            
        Returns:
            str: Docker-wrapped command for container execution
        """
        try:
            # Parse the original worker command
            worker_args = self._parse_worker_command(worker_command)
            
            # Build Docker run command with better networking for SSH tunnels
            docker_cmd = [
                'docker', 'run',
                '--rm',                           # Auto-cleanup when worker exits
                '--network', 'host',              # Use host networking for SSH tunnels
                '--pid', 'host',                  # Share PID namespace for better process visibility
                '--volume', '/tmp:/tmp',          # Mount temp directory for Parsl
                '--volume', '/var/log:/var/log',  # Mount logs for debugging
                '--volume', '/home/ubuntu:/home/ubuntu',  # Mount home directory
                '--env', 'PYTHONPATH=/tmp',       # Ensure Parsl modules are found
                '--env', 'PYTHONUNBUFFERED=1',    # Immediate output for debugging
                '--env', 'HOME=/home/ubuntu',     # Set proper home directory
                '--workdir', '/home/ubuntu'       # Set working directory
            ]
            
            # Add resource limits if specified
            if 'memory_limit' in container_config:
                docker_cmd.extend(['--memory', container_config['memory_limit']])
            if 'cpu_limit' in container_config:
                docker_cmd.extend(['--cpus', str(container_config['cpu_limit'])])
                
            # Add container image
            docker_cmd.append(container_config['image'])
            
            # Add the original worker command as container entrypoint
            container_worker_cmd = self._build_container_worker_command(worker_args)
            docker_cmd.extend(['bash', '-c', container_worker_cmd])
            
            return ' '.join(f'"{arg}"' if ' ' in arg else arg for arg in docker_cmd)
            
        except Exception as e:
            logger.error(f"Failed to wrap worker command for container: {e}")
            raise ContainerRuntimeError(f"Command wrapping failed: {e}")
    
    def _parse_worker_command(self, command: str) -> Dict:
        """Parse Parsl worker command to extract components."""
        
        # Extract worker script path (flexible matching)
        worker_script_match = re.search(r'((?:/usr/local/bin/)?process_worker_pool\.py)', command)
        if not worker_script_match:
            raise ContainerRuntimeError("Could not find worker script in command")
            
        worker_script = "/usr/local/bin/process_worker_pool.py"  # Always use full path
        
        # Extract worker arguments
        args_part = command[worker_script_match.end():].strip()
        
        # Extract key parameters
        address_match = re.search(r'-a\s+(\S+)', args_part)
        port_match = re.search(r'--port[=\s]+(\d+)', args_part)
        if not port_match:
            port_match = re.search(r'-p\s+(\d+)', args_part)
            
        max_workers_match = re.search(r'--max_workers_per_node[=\s]+(\d+)', args_part)
        
        return {
            'worker_script': worker_script,
            'address': address_match.group(1) if address_match else '127.0.0.1',
            'port': port_match.group(1) if port_match else '50000',
            'max_workers': max_workers_match.group(1) if max_workers_match else '1',
            'full_args': args_part
        }
    
    def _build_container_worker_command(self, worker_args: Dict) -> str:
        """Build worker command for execution inside container."""
        
        # Use Python module execution instead of script path
        container_cmd = f"""
# Create log directory for container execution
mkdir -p /tmp/parsl_logs

# Execute Parsl worker using Python module (works regardless of installation path)
python3 -m parsl.executors.high_throughput.process_worker_pool {worker_args['full_args']}
"""
        
        return container_cmd
    
    async def get_container_status(self, instance_id: str) -> Dict:
        """Get status of containers running on instance."""
        
        try:
            # Get container information
            result = await self.provider.execute_remote_command(
                instance_id, 
                "docker ps --format 'table {{.ID}}\\t{{.Image}}\\t{{.Status}}\\t{{.Names}}'"
            )
            
            if result.returncode == 0:
                return {
                    'docker_available': True,
                    'containers': result.stdout.strip(),
                    'container_count': len(result.stdout.strip().split('\n')) - 1  # Exclude header
                }
            else:
                return {'docker_available': False, 'error': result.stderr}
                
        except Exception as e:
            logger.error(f"Failed to get container status on {instance_id}: {e}")
            return {'docker_available': False, 'error': str(e)}
    
    async def cleanup_containers(self, instance_id: str):
        """Clean up all Parsl-related containers on instance."""
        
        try:
            # Stop and remove all containers with Parsl-related images
            cleanup_cmd = """
docker ps -aq --filter "ancestor=parsl-*" | xargs -r docker stop
docker ps -aq --filter "ancestor=parsl-*" | xargs -r docker rm
docker images --filter "reference=parsl-*" --format "{{.ID}}" | xargs -r docker rmi
"""
            
            result = await self.provider.execute_remote_command(instance_id, cleanup_cmd)
            
            if result.returncode == 0:
                logger.info(f"Container cleanup completed on {instance_id}")
            else:
                logger.warning(f"Container cleanup had issues on {instance_id}: {result.stderr}")
                
        except Exception as e:
            logger.error(f"Container cleanup failed on {instance_id}: {e}")


class ScientificContainerBuilder:
    """Builds optimized containers for scientific computing workloads."""
    
    # Predefined scientific software stacks
    SCIENTIFIC_STACKS = {
        'basic': {
            'base_image': 'python:3.10-slim',
            'system_packages': ['build-essential', 'gfortran', 'libopenblas-dev'],
            'python_packages': ['numpy>=1.24.0', 'scipy>=1.11.0', 'pandas>=2.0.0']
        },
        'ml': {
            'base_image': 'python:3.10-slim',
            'system_packages': ['build-essential', 'gfortran', 'libopenblas-dev', 'liblapack-dev'],
            'python_packages': [
                'numpy>=1.24.0', 'scipy>=1.11.0', 'pandas>=2.0.0',
                'scikit-learn>=1.3.0', 'torch>=2.0.0', 'tensorflow>=2.13.0'
            ]
        },
        'bio': {
            'base_image': 'python:3.10-slim', 
            'system_packages': ['build-essential', 'gfortran', 'libopenblas-dev'],
            'python_packages': [
                'numpy>=1.24.0', 'scipy>=1.11.0', 'pandas>=2.0.0',
                'biopython>=1.81', 'rdkit>=2023.3.1', 'matplotlib>=3.7.0'
            ]
        }
    }
    
    def generate_dockerfile(self, stack_name: str, custom_packages: Optional[List[str]] = None) -> str:
        """Generate optimized Dockerfile for scientific computing stack.
        
        Args:
            stack_name: Name of predefined scientific stack
            custom_packages: Additional Python packages to include
            
        Returns:
            str: Complete Dockerfile content
        """
        if stack_name not in self.SCIENTIFIC_STACKS:
            raise ValueError(f"Unknown scientific stack: {stack_name}")
            
        stack_config = self.SCIENTIFIC_STACKS[stack_name]
        packages = stack_config['python_packages'].copy()
        
        if custom_packages:
            packages.extend(custom_packages)
            
        dockerfile = f"""
# Optimized scientific computing container for Parsl AWS Provider
FROM {stack_config['base_image']}

# Install system dependencies for scientific computing
RUN apt-get update && apt-get install -y \\
    {' '.join(stack_config['system_packages'])} \\
    curl \\
    wget \\
    && rm -rf /var/lib/apt/lists/*

# Install Python packages in single layer for efficiency
# Use --no-cache-dir to reduce image size
RUN pip install --no-cache-dir --compile \\
    parsl>=2024.8.25 \\
    {' '.join(packages)}

# Create ubuntu user for Parsl execution
RUN useradd -m -u 1000 -s /bin/bash ubuntu && \\
    mkdir -p /home/ubuntu/.parsl && \\
    chown -R ubuntu:ubuntu /home/ubuntu

# Configure Python environment for optimal performance
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH="/tmp:/home/ubuntu"

# Set working directory and user
USER ubuntu
WORKDIR /home/ubuntu

# Verify installations and create marker file
RUN python3 -c "import parsl, numpy, scipy; print('Scientific stack verified')" && \\
    echo "Stack: {stack_name}" > /home/ubuntu/.container-info

# Default command (overridden by Parsl)
CMD ["python3", "-c", "print('Scientific computing container ready')"]
"""
        
        return dockerfile
    
    def build_container_image(self, stack_name: str, custom_packages: Optional[List[str]] = None) -> str:
        """Build scientific computing container image.
        
        Args:
            stack_name: Scientific stack to build
            custom_packages: Additional packages to include
            
        Returns:  
            str: Built container image tag
        """
        dockerfile = self.generate_dockerfile(stack_name, custom_packages)
        image_tag = f"parsl-{stack_name}:latest"
        
        try:
            # Create temporary directory for build context
            import tempfile
            with tempfile.TemporaryDirectory() as build_dir:
                dockerfile_path = os.path.join(build_dir, 'Dockerfile')
                
                with open(dockerfile_path, 'w') as f:
                    f.write(dockerfile)
                    
                # Build image
                build_cmd = [
                    'docker', 'build',
                    '-t', image_tag,
                    '--progress', 'plain',
                    build_dir
                ]
                
                logger.info(f"Building container image: {image_tag}")
                result = subprocess.run(
                    build_cmd, 
                    capture_output=True, 
                    text=True, 
                    timeout=600  # 10 minute timeout for builds
                )
                
                if result.returncode != 0:
                    raise ContainerRuntimeError(f"Container build failed: {result.stderr}")
                    
                logger.info(f"Successfully built container image: {image_tag}")
                return image_tag
                
        except Exception as e:
            logger.error(f"Container image build failed: {e}")
            raise ContainerRuntimeError(f"Build failed: {e}")


class ContainerConfigurationManager:
    """Manages container configurations for different scientific workloads."""
    
    def __init__(self):
        self.configurations = {}
        
    def get_configuration(self, stack_name: str, performance_tier: str = 'standard') -> Dict:
        """Get container configuration for scientific stack.
        
        Args:
            stack_name: Scientific software stack
            performance_tier: Performance configuration (standard, optimized, gpu)
            
        Returns:
            Dict: Container configuration
        """
        base_config = {
            'image': f'parsl-{stack_name}:latest',
            'network_mode': 'host',           # Required for SSH tunnels
            'auto_remove': True,              # Cleanup on exit
            'detach': True,                   # Background execution
            'environment': {
                'PYTHONUNBUFFERED': '1',
                'PYTHONDONTWRITEBYTECODE': '1',
                'PYTHONPATH': '/tmp:/home/ubuntu'
            },
            'volumes': {
                '/tmp': {'bind': '/tmp', 'mode': 'rw'},
                '/var/log': {'bind': '/var/log', 'mode': 'rw'}
            },
            'user': 'ubuntu',
            'working_dir': '/home/ubuntu'
        }
        
        # Performance tier optimizations
        if performance_tier == 'optimized':
            base_config.update({
                'cpu_count': 0,               # Use all available CPUs
                'mem_limit': '90%',           # Leave 10% for system
                'tmpfs': {'/tmp': 'size=2g,exec'},  # Fast temp storage
                'ulimits': [
                    {'name': 'memlock', 'soft': -1, 'hard': -1},  # Unlimited locked memory
                    {'name': 'stack', 'soft': 67108864, 'hard': 67108864}  # 64MB stack
                ]
            })
        elif performance_tier == 'gpu':
            base_config.update({
                'runtime': 'nvidia',          # GPU runtime
                'environment': {
                    **base_config['environment'],
                    'CUDA_VISIBLE_DEVICES': 'all'
                }
            })
            
        return base_config
    
    def validate_container_config(self, config: Dict) -> bool:
        """Validate container configuration for Parsl compatibility."""
        
        required_fields = ['image', 'network_mode']
        for field in required_fields:
            if field not in config:
                logger.error(f"Missing required container config field: {field}")
                return False
                
        # Validate network mode for SSH tunnel compatibility
        if config['network_mode'] != 'host':
            logger.warning("Non-host networking may break SSH tunnel connectivity")
            
        return True


class ContainerPerformanceMonitor:
    """Monitors container performance for optimization."""
    
    def __init__(self):
        self.metrics = {}
        
    async def benchmark_container_overhead(self, instance_id: str, container_config: Dict) -> Dict:
        """Benchmark container performance overhead vs native execution.
        
        Args:
            instance_id: EC2 instance to test on
            container_config: Container configuration to benchmark
            
        Returns:
            Dict: Performance comparison metrics
        """
        logger.info(f"Running container performance benchmark on {instance_id}")
        
        # Define benchmark workload
        benchmark_code = """
import time
import math

def cpu_benchmark(iterations=100000):
    start = time.time()
    result = 0
    for i in range(iterations):
        result += math.sqrt(i * 2.5) * math.sin(i / 1000.0)
    return time.time() - start

def memory_benchmark(size_mb=100):
    start = time.time() 
    data = [i for i in range(size_mb * 1000)]  # Allocate memory
    total = sum(data)
    return time.time() - start

# Run benchmarks
cpu_time = cpu_benchmark()
memory_time = memory_benchmark()

print(f"CPU_BENCHMARK:{cpu_time:.4f}")
print(f"MEMORY_BENCHMARK:{memory_time:.4f}")
"""
        
        try:
            # 1. Native execution benchmark
            native_cmd = f'python3 -c "{benchmark_code}"'
            native_result = await self.provider.execute_remote_command(
                instance_id, native_cmd, timeout=60
            )
            
            if native_result.returncode != 0:
                raise ContainerRuntimeError(f"Native benchmark failed: {native_result.stderr}")
                
            native_metrics = self._parse_benchmark_output(native_result.stdout)
            
            # 2. Container execution benchmark  
            container_cmd = f"""
docker run --rm --network host {container_config['image']} \\
python3 -c "{benchmark_code}"
"""
            
            container_result = await self.provider.execute_remote_command(
                instance_id, container_cmd, timeout=120
            )
            
            if container_result.returncode != 0:
                raise ContainerRuntimeError(f"Container benchmark failed: {container_result.stderr}")
                
            container_metrics = self._parse_benchmark_output(container_result.stdout)
            
            # Calculate overhead
            overhead = {}
            for metric in native_metrics:
                native_time = native_metrics[metric]
                container_time = container_metrics[metric]
                overhead[metric] = {
                    'native_seconds': native_time,
                    'container_seconds': container_time,
                    'overhead_percent': ((container_time - native_time) / native_time) * 100
                }
                
            return {
                'instance_id': instance_id,
                'container_image': container_config['image'],
                'benchmark_results': overhead,
                'overall_overhead': sum(o['overhead_percent'] for o in overhead.values()) / len(overhead)
            }
            
        except Exception as e:
            logger.error(f"Performance benchmark failed: {e}")
            raise ContainerRuntimeError(f"Benchmark failed: {e}")
    
    def _parse_benchmark_output(self, output: str) -> Dict[str, float]:
        """Parse benchmark output to extract timing metrics."""
        
        metrics = {}
        for line in output.strip().split('\n'):
            if ':' in line:
                metric_name, metric_value = line.split(':', 1)
                try:
                    metrics[metric_name.lower()] = float(metric_value)
                except ValueError:
                    continue
                    
        return metrics


class ECRIntegration:
    """Manages container images in AWS Elastic Container Registry."""
    
    def __init__(self, provider):
        self.provider = provider
        self.ecr_client = provider.session.client('ecr')
        
    async def setup_ecr_repository(self, repository_name: str) -> str:
        """Create ECR repository for scientific container images.
        
        Args:
            repository_name: Name for ECR repository
            
        Returns:
            str: ECR repository URI
        """
        try:
            # Check if repository exists
            response = self.ecr_client.describe_repositories(
                repositoryNames=[repository_name]
            )
            repository_uri = response['repositories'][0]['repositoryUri']
            logger.info(f"Using existing ECR repository: {repository_uri}")
            
        except self.ecr_client.exceptions.RepositoryNotFoundException:
            # Create new repository
            response = self.ecr_client.create_repository(
                repositoryName=repository_name,
                imageScanningConfiguration={'scanOnPush': True}
            )
            repository_uri = response['repository']['repositoryUri']
            logger.info(f"Created new ECR repository: {repository_uri}")
            
        return repository_uri
    
    async def push_container_image(self, local_tag: str, repository_uri: str) -> str:
        """Push local container image to ECR.
        
        Args:
            local_tag: Local Docker image tag
            repository_uri: ECR repository URI
            
        Returns:
            str: Full ECR image URI
        """
        try:
            # Get ECR login token
            auth_response = self.ecr_client.get_authorization_token()
            auth_data = auth_response['authorizationData'][0]
            
            # Extract login credentials
            import base64
            username, password = base64.b64decode(auth_data['authorizationToken']).decode().split(':')
            registry_url = auth_data['proxyEndpoint']
            
            # Login to ECR
            login_cmd = f'echo "{password}" | docker login --username {username} --password-stdin {registry_url}'
            login_result = subprocess.run(login_cmd, shell=True, capture_output=True, text=True)
            
            if login_result.returncode != 0:
                raise ContainerRuntimeError(f"ECR login failed: {login_result.stderr}")
                
            # Tag for ECR
            ecr_tag = f"{repository_uri}:latest"
            tag_cmd = ['docker', 'tag', local_tag, ecr_tag]
            tag_result = subprocess.run(tag_cmd, capture_output=True, text=True)
            
            if tag_result.returncode != 0:
                raise ContainerRuntimeError(f"Image tagging failed: {tag_result.stderr}")
                
            # Push to ECR
            push_cmd = ['docker', 'push', ecr_tag]
            push_result = subprocess.run(push_cmd, capture_output=True, text=True, timeout=900)
            
            if push_result.returncode != 0:
                raise ContainerRuntimeError(f"Image push failed: {push_result.stderr}")
                
            logger.info(f"Successfully pushed {local_tag} to {ecr_tag}")
            return ecr_tag
            
        except Exception as e:
            logger.error(f"ECR push failed: {e}")
            raise ContainerRuntimeError(f"ECR operation failed: {e}")


# Test and validation functions
async def test_container_integration():
    """Test container integration with Phase 1.5 provider."""
    
    # This will be expanded as we implement the enhanced provider
    logger.info("Container integration testing not yet implemented")
    logger.info("Will be added in enhanced provider integration")


if __name__ == "__main__":
    # Basic validation of container building capability
    builder = ScientificContainerBuilder()
    
    print("Available scientific stacks:")
    for stack in builder.SCIENTIFIC_STACKS:
        print(f"  - {stack}: {builder.SCIENTIFIC_STACKS[stack]['python_packages']}")
        
    print("\nSample Dockerfile for 'basic' stack:")
    print(builder.generate_dockerfile('basic'))
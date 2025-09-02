# Phase 2 Container Strategy Research

## Executive Summary

Based on comprehensive research into Docker containers for scientific computing, containerization is the optimal approach for Phase 2 dependency management. Key findings:

- **Performance Overhead**: Negligible (<5%) for compute-intensive scientific workloads
- **Parsl Support**: Native container execution since v0.5.0, production-ready
- **Scientific Adoption**: Widely adopted for reproducible research workflows
- **Ecosystem**: Mature container registry ecosystem (BioContainers, etc.)

## Docker Performance Analysis for Scientific Computing

### Performance Characteristics

**Research Results**:
- **CPU Overhead**: Negligible for compute-intensive tasks (typical in scientific computing)
- **Memory Overhead**: <200MB base overhead (acceptable for c5.large+ instances)
- **I/O Performance**: Near-native performance for file operations
- **Network Performance**: Host networking preserves SSH tunnel performance

**Key Finding**: "Container virtualization has negligible overhead on pipeline performance when composed of medium/long running tasks, which is the most common scenario in computational genomic pipelines."

### Performance Validation Requirements for Phase 2
```python
# Performance benchmark targets
PERFORMANCE_TARGETS = {
    'startup_overhead': '<30 seconds',      # Container + dependency load
    'cpu_overhead': '<5%',                  # vs native execution  
    'memory_overhead': '<200MB',            # Base container footprint
    'network_overhead': '<10ms',            # Additional latency through tunnels
}
```

## Container Strategy for Scientific Computing

### Approach 1: Scientific Base Images
**Strategy**: Use established scientific computing base images
**Advantages**: 
- Pre-installed scientific software stacks
- Optimized for research workloads  
- Community-maintained and validated

```python
SCIENTIFIC_BASE_IMAGES = {
    'basic': 'continuumio/miniconda3:latest',
    'scipy': 'jupyter/scipy-notebook:latest', 
    'ml': 'tensorflow/tensorflow:latest-gpu',
    'bio': 'biocontainers/base:latest',
    'r': 'rocker/r-ver:4.3.0'
}
```

### Approach 2: Custom Optimized Images  
**Strategy**: Build custom images optimized for our SSH tunneling architecture
**Advantages**:
- Minimal overhead for our specific use case
- Optimized for AWS EC2 + SSH tunnel environment
- Full control over software stack

```dockerfile
# Base image for our provider
FROM python:3.10-slim-bookworm

# System dependencies for scientific computing
RUN apt-get update && apt-get install -y \
    build-essential \
    gfortran \
    libopenblas-dev \
    liblapack-dev \
    && rm -rf /var/lib/apt/lists/*

# Scientific Python stack
RUN pip install --no-cache-dir \
    numpy==1.24.3 \
    scipy==1.11.1 \
    pandas==2.0.3 \
    scikit-learn==1.3.0

# Configure for Parsl execution
RUN useradd -m -s /bin/bash ubuntu
USER ubuntu
WORKDIR /home/ubuntu

# Ensure SSH tunnel compatibility
EXPOSE 50000-60000
```

### Recommended Strategy: Hybrid Approach
**Phase 2.1**: Start with established scientific base images for rapid development
**Phase 2.2**: Optimize with custom images once performance requirements are validated

## Parsl Container Integration Architecture

### Current Parsl Container Support
Parsl supports container execution where apps are executed on workers launched within containers. Key capabilities:
- **Multiple Runtimes**: Docker, Singularity, Shifter support
- **Automatic Scaling**: Container-based workers scale with workload
- **Resource Management**: CPU/memory limits through container configuration

### Integration with Our Provider
```python
# Enhanced provider with container support
class AWSProvider(ExecutionProvider):
    def __init__(self, **config):
        # Phase 1.5 SSH tunneling (preserve)
        super().__init__(**config)
        
        # Phase 2 container support
        self.container_runtime = config.get('container_runtime', 'docker')
        self.container_image = config.get('container_image')
        self.container_config = config.get('container_config', {})
        
    def submit(self, command, tasks_per_node, job_name=""):
        """Enhanced submit with container support."""
        if self.container_runtime:
            # Wrap command in container execution
            container_cmd = self._wrap_command_in_container(command)
            return super().submit(container_cmd, tasks_per_node, job_name)
        else:
            # Phase 1.5 native execution
            return super().submit(command, tasks_per_node, job_name)
    
    def _wrap_command_in_container(self, original_command):
        """Wrap Parsl worker command for container execution."""
        # Extract worker command components
        worker_script = self._extract_worker_script(original_command)
        tunnel_ports = self._extract_tunnel_ports(original_command)
        
        # Build Docker run command
        docker_cmd = [
            'docker', 'run',
            '--rm',                    # Auto-cleanup
            '--network', 'host',       # Preserve SSH tunnels  
            '-v', '/tmp:/tmp',         # Mount temp directory
            '-e', f'PYTHONPATH=/tmp',  # Python path for Parsl
            self.container_image,
            'python3', worker_script, *tunnel_ports
        ]
        
        return ' '.join(docker_cmd)
```

## Implementation Plan Details

### Week 1: Foundation Research & Design

#### Day 1: Container Runtime Evaluation
**Research Focus**: Docker vs Singularity for our architecture

**Docker Advantages**:
- Native AWS support and integration
- Extensive scientific computing ecosystem
- Simple networking with host mode for SSH tunnels
- Mature tooling and documentation

**Singularity Considerations**:
- Popular in HPC environments  
- Better security model for shared systems
- More complex installation on Ubuntu EC2

**Decision**: Proceed with Docker for Phase 2, evaluate Singularity for Phase 3 HPC integration

#### Day 2: Scientific Container Ecosystem Analysis
**Research Areas**:
- **BioContainers**: Pre-built bioinformatics tools
- **Jupyter Stack**: Scientific notebook environments
- **TensorFlow/PyTorch**: ML/AI optimized containers  
- **Conda-based**: Comprehensive scientific Python stacks

#### Day 3-4: Container Performance Benchmarking
```python
# Benchmark plan for container performance
def benchmark_container_performance():
    """Compare native vs containerized execution."""
    
    # Test cases
    benchmarks = [
        ('cpu_intensive', cpu_benchmark_function),
        ('memory_intensive', memory_benchmark_function), 
        ('io_intensive', io_benchmark_function),
        ('network_intensive', network_benchmark_function)
    ]
    
    results = {}
    for test_name, test_func in benchmarks:
        # Native execution
        native_time = time_execution(test_func, execution_type='native')
        
        # Container execution  
        container_time = time_execution(test_func, execution_type='container')
        
        results[test_name] = {
            'native_time': native_time,
            'container_time': container_time, 
            'overhead_percent': ((container_time - native_time) / native_time) * 100
        }
    
    return results
```

#### Day 5: SSH Tunnel + Container Integration Design
**Critical Research**: Ensure SSH reverse tunnels work properly with Docker host networking

**Validation Test**:
```python
def test_ssh_tunnel_container_compatibility():
    """Verify SSH tunnels work with Docker host networking."""
    
    # 1. Establish SSH reverse tunnel to EC2 instance
    tunnel = create_reverse_tunnel(instance_id, local_port=54321, remote_port=54321)
    
    # 2. Launch container with host networking
    container_cmd = [
        'docker', 'run', '--rm', '--network', 'host',
        'python:3.10-slim',
        'python3', '-c', 
        'import socket; s=socket.socket(); s.connect(("127.0.0.1", 54321)); print("SUCCESS")'
    ]
    
    # 3. Verify container can connect through tunnel
    result = execute_on_instance(instance_id, container_cmd)
    assert 'SUCCESS' in result.stdout
```

### Week 2: Core Container Implementation

#### Docker Runtime Manager Implementation
```python
# File: container_runtime.py
class DockerRuntimeManager:
    """Manages Docker container execution on EC2 instances."""
    
    def __init__(self, provider):
        self.provider = provider
        self.installation_cache = {}  # Cache Docker installation status
    
    async def ensure_docker_installed(self, instance_id):
        """Ensure Docker is installed and running on instance."""
        if instance_id in self.installation_cache:
            return True
            
        install_commands = [
            # Update system
            "sudo apt-get update",
            
            # Install Docker
            "sudo apt-get install -y docker.io", 
            
            # Configure Docker
            "sudo systemctl start docker",
            "sudo systemctl enable docker",
            "sudo usermod -a -G docker ubuntu",
            
            # Verify installation
            "docker --version"
        ]
        
        try:
            for cmd in install_commands:
                result = await self.provider.execute_remote_command(instance_id, cmd)
                if result.returncode != 0:
                    raise Exception(f"Docker installation failed: {result.stderr}")
            
            self.installation_cache[instance_id] = True
            return True
            
        except Exception as e:
            logger.error(f"Docker installation failed on {instance_id}: {e}")
            return False
    
    def wrap_worker_command_for_container(self, worker_command, container_config):
        """Wrap Parsl worker command for container execution."""
        
        # Parse original worker command
        original_args = self._parse_worker_command(worker_command)
        
        # Build Docker command
        docker_cmd = [
            'docker', 'run',
            '--rm',                           # Auto-cleanup
            '--detach',                       # Run in background
            '--network', 'host',              # Preserve SSH tunnels
            '--volume', '/tmp:/tmp',          # Mount temp directory
            '--volume', '/var/log:/var/log',  # Mount log directory
            '--env', 'PYTHONPATH=/tmp'        # Python path for Parsl
        ]
        
        # Add container image
        docker_cmd.append(container_config['image'])
        
        # Add original worker command
        docker_cmd.extend(['python3', '-c', f'''
import sys
sys.path.insert(0, "/tmp")
exec(open("{original_args['worker_script']}").read())
'''])
        
        # Add worker arguments
        docker_cmd.extend(original_args['worker_args'])
        
        return ' '.join(docker_cmd)
```

#### Container Image Management
```python
# File: container_images.py
class ScientificContainerImages:
    """Manages scientific computing container images."""
    
    # Pre-defined scientific stacks
    STACKS = {
        'basic': {
            'base_image': 'python:3.10-slim',
            'packages': ['numpy', 'scipy', 'pandas']
        },
        'ml': {
            'base_image': 'python:3.10-slim', 
            'packages': ['numpy', 'scipy', 'scikit-learn', 'torch', 'tensorflow']
        },
        'bio': {
            'base_image': 'python:3.10-slim',
            'packages': ['numpy', 'scipy', 'biopython', 'rdkit-pypi']  
        }
    }
    
    def build_scientific_image(self, stack_name, custom_packages=None):
        """Build optimized scientific computing image."""
        
        if stack_name not in self.STACKS:
            raise ValueError(f"Unknown stack: {stack_name}")
            
        stack_config = self.STACKS[stack_name]
        packages = stack_config['packages'].copy()
        
        if custom_packages:
            packages.extend(custom_packages)
            
        dockerfile = self._generate_dockerfile(stack_config['base_image'], packages)
        return self._build_and_tag_image(dockerfile, f"parsl-{stack_name}:latest")
    
    def _generate_dockerfile(self, base_image, packages):
        """Generate optimized Dockerfile for scientific computing."""
        return f"""
FROM {base_image}

# Install system dependencies for scientific computing
RUN apt-get update && apt-get install -y \\
    build-essential \\
    gfortran \\
    libopenblas-dev \\
    liblapack-dev \\
    pkg-config \\
    && rm -rf /var/lib/apt/lists/*

# Install Python packages in single layer for efficiency
RUN pip install --no-cache-dir --compile \\
    {' '.join(packages)}

# Configure user environment
RUN useradd -m -u 1000 ubuntu
USER ubuntu
WORKDIR /home/ubuntu

# Configure Python environment
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
"""
```

### Week 3: Dependency Caching System

#### Multi-Level Cache Architecture
```python
# File: dependency_cache.py
class MultiLevelDependencyCache:
    """Intelligent dependency caching across memory, EBS, and S3."""
    
    def __init__(self, cache_config):
        self.s3_bucket = cache_config.get('s3_bucket', 'parsl-dependency-cache')
        self.ebs_mount = cache_config.get('ebs_mount', '/opt/parsl-cache')
        self.memory_cache = {}
        
    async def get_cached_environment(self, env_hash):
        """Retrieve cached environment from fastest available source."""
        
        # 1. Check memory cache (fastest)
        if env_hash in self.memory_cache:
            return self.memory_cache[env_hash]
            
        # 2. Check EBS volume cache (fast)
        ebs_path = f"{self.ebs_mount}/{env_hash}"
        if os.path.exists(ebs_path):
            env_data = self._load_from_ebs(ebs_path)
            self.memory_cache[env_hash] = env_data  # Promote to memory
            return env_data
            
        # 3. Check S3 cache (slower but persistent)
        try:
            env_data = await self._download_from_s3(env_hash)
            if env_data:
                # Promote to higher cache levels
                self._store_to_ebs(ebs_path, env_data)
                self.memory_cache[env_hash] = env_data
                return env_data
        except Exception as e:
            logger.warning(f"S3 cache miss for {env_hash}: {e}")
            
        return None  # Cache miss at all levels
    
    async def cache_environment(self, env_hash, environment_data):
        """Store environment in all cache levels."""
        
        # Store in memory
        self.memory_cache[env_hash] = environment_data
        
        # Store on EBS
        ebs_path = f"{self.ebs_mount}/{env_hash}"
        self._store_to_ebs(ebs_path, environment_data)
        
        # Store in S3  
        await self._upload_to_s3(env_hash, environment_data)
```

#### Dependency Resolution Algorithm
```python
class DependencyResolver:
    """Intelligent dependency resolution with conflict management."""
    
    def resolve_requirements(self, requirements_list):
        """Resolve dependencies and detect conflicts."""
        
        # Parse requirements
        parsed_reqs = []
        for req in requirements_list:
            parsed_reqs.append(self._parse_requirement(req))
            
        # Check for version conflicts
        conflicts = self._detect_conflicts(parsed_reqs)
        if conflicts:
            resolved = self._resolve_conflicts(conflicts, parsed_reqs)
            return resolved
            
        return parsed_reqs
    
    def _generate_environment_hash(self, requirements, python_version, os_version):
        """Generate hash for environment caching."""
        env_string = f"{python_version}:{os_version}:{':'.join(sorted(requirements))}"
        return hashlib.sha256(env_string.encode()).hexdigest()[:16]
```

### Week 4: Enhanced Provider Integration

#### Enhanced Provider with Full Container Support
```python
# Enhanced phase15_enhanced.py with Phase 2 features
class AWSProvider(ExecutionProvider):
    def __init__(self, **config):
        # Initialize Phase 1.5 features
        super().__init__(**config)
        
        # Phase 2 container configuration
        self.container_runtime = config.get('container_runtime')
        self.container_image = config.get('container_image')
        self.scientific_stack = config.get('scientific_stack')
        self.worker_init = config.get('worker_init', [])
        
        # Phase 2 dependency configuration
        self.dependency_cache = config.get('dependency_cache', True)
        self.cache_backend = config.get('cache_backend', 's3')
        self.custom_packages = config.get('custom_packages', [])
        
        # Initialize Phase 2 managers
        self.container_manager = None
        self.dependency_cache_manager = None
        self.scientific_image_builder = None
        
        if self.container_runtime:
            self.container_manager = DockerRuntimeManager(self)
            self.dependency_cache_manager = MultiLevelDependencyCache(config)
            self.scientific_image_builder = ScientificContainerImages()
    
    async def _configure_worker_environment(self, instance_id):
        """Configure worker environment with container/dependency support."""
        
        if self.container_runtime:
            # Container-based execution
            await self.container_manager.ensure_docker_installed(instance_id)
            
            if self.scientific_stack:
                # Use pre-built scientific stack
                container_image = await self._get_scientific_container(self.scientific_stack)
            else:
                container_image = self.container_image
                
            return {
                'execution_mode': 'container',
                'container_image': container_image,
                'container_config': self.container_config
            }
        else:
            # Native execution with dependency management
            if self.custom_packages:
                env_hash = self._generate_environment_hash()
                cached_env = await self.dependency_cache_manager.get_cached_environment(env_hash)
                
                if not cached_env:
                    # Install dependencies and cache
                    installed_env = await self._install_dependencies(instance_id)
                    await self.dependency_cache_manager.cache_environment(env_hash, installed_env)
                    
            return {
                'execution_mode': 'native',
                'environment_path': '/home/ubuntu/.parsl-env'
            }
```

### Week 5: Globus Compute Integration Enhancement

#### Enhanced Globus Endpoint Configuration
```python
# File: globus_enhanced_integration.py
class EnhancedGlobusIntegration:
    """Enhanced Globus Compute integration with Phase 2 features."""
    
    def generate_endpoint_config(self, provider_config, endpoint_name):
        """Generate advanced Globus endpoint configuration."""
        
        base_config = {
            'display_name': f"AWS Enhanced {endpoint_name}",
            'engine': {
                'type': 'GlobusComputeEngine',
                'provider': {
                    'type': 'AWSProvider',
                    'region': provider_config['region'],
                    'instance_type': provider_config.get('instance_type', 'c5.large'),
                    'python_version': '3.10'
                },
                'max_retries_on_system_failure': 3,
                'encrypted': True
            }
        }
        
        # Add Phase 2 container support
        if provider_config.get('scientific_stack'):
            base_config['engine']['container_type'] = 'docker'
            base_config['engine']['container_uri'] = f"parsl-{provider_config['scientific_stack']}:latest"
            
        # Add custom dependency support  
        if provider_config.get('custom_packages'):
            base_config['engine']['worker_init'] = [
                f"pip install {' '.join(provider_config['custom_packages'])}"
            ]
            
        return base_config
    
    def create_scientific_endpoints(self, institution_config):
        """Create multiple endpoints for different scientific domains."""
        
        endpoints = {}
        
        # Biology/Bioinformatics endpoint
        endpoints['biology'] = self.generate_endpoint_config({
            'region': 'us-east-1',
            'instance_type': 'c5.2xlarge', 
            'scientific_stack': 'bio'
        }, 'Biology Research')
        
        # Machine Learning endpoint  
        endpoints['ml'] = self.generate_endpoint_config({
            'region': 'us-east-1',
            'instance_type': 'g4dn.xlarge',  # GPU instance
            'scientific_stack': 'ml'
        }, 'Machine Learning')
        
        # General Scientific Computing endpoint
        endpoints['general'] = self.generate_endpoint_config({
            'region': 'us-east-1', 
            'instance_type': 'c5.large',
            'scientific_stack': 'basic'
        }, 'General Scientific Computing')
        
        return endpoints
```

## Phase 2 Success Metrics

### Technical Performance Metrics
- **Container Cold Start**: <90 seconds (from instance launch to ready worker)
- **Dependency Cache Hit**: >80% for common scientific packages
- **Runtime Overhead**: <10% performance penalty vs Phase 1.5 native
- **Memory Efficiency**: <500MB total overhead (container + dependencies)

### Functional Validation Metrics  
- **Scientific Package Support**: NumPy, SciPy, pandas, scikit-learn, TensorFlow
- **Globus Integration**: Successful function execution through enhanced endpoints
- **Multi-Container**: Multiple scientific stacks available simultaneously
- **Dependency Isolation**: No conflicts between different package requirements

### User Experience Metrics
- **Setup Complexity**: Single configuration parameter to enable containers
- **Documentation Quality**: Complete examples for major scientific domains  
- **Error Messages**: Clear, actionable error messages for common issues
- **Migration Path**: Seamless upgrade from Phase 1.5 without breaking changes

## Risk Assessment and Mitigation

### High-Risk Areas
1. **SSH Tunnel + Container Networking**: Complex interaction between host networking and tunnels
   - *Mitigation*: Extensive testing of networking configurations
2. **Container Image Size**: Large scientific images may slow startup significantly  
   - *Mitigation*: Multi-stage builds and aggressive image optimization
3. **Dependency Cache Complexity**: Multi-level caching may introduce bugs
   - *Mitigation*: Start simple with S3 caching, add layers incrementally

### Medium-Risk Areas  
1. **Globus Compatibility**: Container support may conflict with existing Globus patterns
   - *Mitigation*: Test against known Globus Compute configurations
2. **AWS Service Limits**: ECR and container usage may hit service quotas
   - *Mitigation*: Monitor service usage and implement quota management

## Phase 2 Deliverables Summary

### Core Implementation Files
- `container_runtime.py` - Docker runtime management
- `dependency_cache.py` - Multi-level dependency caching
- `container_images.py` - Scientific container building
- `globus_enhanced_integration.py` - Advanced Globus support

### Enhanced Provider
- Updated `phase15_enhanced.py` with container and dependency features
- Backward compatibility with Phase 1.5 configurations
- New configuration options for scientific computing

### Documentation Updates
- Container usage examples in `USAGE_GUIDE.md`  
- Globus integration patterns in `GLOBUS_COMPUTE_INTEGRATION.md`
- Performance benchmarks and optimization guide
- Migration guide for Phase 1.5 users

### Testing and Validation
- Container performance benchmark suite
- Scientific workflow validation tests  
- Globus Compute integration test suite
- End-to-end production deployment validation

**Phase 2 Mission**: Transform universal connectivity foundation into comprehensive scientific computing platform with full dependency management and enterprise-grade container support.
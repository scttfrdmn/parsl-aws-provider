# Phase 2: Dependency & Container Management - Implementation Plan

## Executive Summary

**Goal**: Transform our Phase 1.5 universal connectivity foundation into a full-featured scientific computing platform supporting external dependencies, containerized workloads, and enhanced Globus Compute integration.

**Timeline**: 4-6 weeks
**Key Innovation**: Container-based execution with intelligent dependency caching
**Success Metric**: NumPy/SciPy workloads executing reliably with <90 second cold start times

## Phase 2 Architecture Overview

```
Local Environment          AWS Infrastructure              Execution Environment
┌─────────────────┐        ┌─────────────────────┐        ┌─────────────────────┐
│ Researcher      │        │ SSH Reverse Tunnel  │        │ Containerized       │
│ Submits:        │───────▶│ + Enhanced Provider │───────▶│ Worker              │
│ - Code          │        │ + Dependency Mgmt   │        │ - Scientific Stack  │
│ - Dependencies  │        │ + Container Support │        │ - User Environment  │
│ - Data          │        └─────────────────────┘        │ - Cached Packages   │
└─────────────────┘                                       └─────────────────────┘
         │                                                          │
         │                  Globus Integration                      │
         └──────────────────┐                 ┌───────────────────┘
                            ▼                 ▼
                    ┌─────────────────────────────────┐
                    │ Globus Compute Endpoint         │
                    │ - FaaS Interface               │
                    │ - Multi-Site Orchestration     │
                    │ - Enterprise Integration       │
                    └─────────────────────────────────┘
```

## Core Components to Implement

### 2.1 Container Runtime Manager

**Purpose**: Enable execution of scientific workloads with complex dependency requirements through containerization.

**Key Features**:
- Docker container support on EC2 instances
- Custom container image building and management
- Runtime switching between native and containerized execution
- Container registry integration (ECR)

**Implementation Plan**:

```python
# New class: ContainerRuntimeManager
class ContainerRuntimeManager:
    def __init__(self, provider_config):
        self.runtime_type = provider_config.get('container_runtime', 'docker')
        self.base_image = provider_config.get('container_image')
        self.ecr_client = boto3.client('ecr')
    
    def setup_container_runtime(self, instance_id):
        """Install and configure container runtime on instance."""
        pass
    
    def build_custom_image(self, dockerfile_path, dependencies):
        """Build custom container image with dependencies.""" 
        pass
    
    def run_containerized_worker(self, container_config, worker_command):
        """Execute Parsl worker inside container."""
        pass
```

### 2.2 Dependency Resolution System

**Purpose**: Intelligently manage Python packages, system dependencies, and scientific software stacks.

**Key Features**:
- Multi-level caching (instance, EBS volumes, S3)
- Support for pip, conda, and system packages
- Virtual environment isolation
- Dependency conflict resolution

**Implementation Plan**:

```python
# New class: DependencyManager
class DependencyManager:
    def __init__(self, cache_config):
        self.cache_levels = ['memory', 'ebs', 's3']
        self.package_managers = ['pip', 'conda', 'apt']
    
    def resolve_dependencies(self, requirements_spec):
        """Resolve and cache dependencies efficiently."""
        pass
    
    def install_dependencies(self, instance_id, dependency_list):
        """Install dependencies with caching optimization."""
        pass
    
    def create_isolated_environment(self, env_spec):
        """Create isolated Python environment."""
        pass
```

### 2.3 Custom AMI Builder

**Purpose**: Create optimized AMIs with pre-installed scientific software for faster startup times.

**Key Features**:
- Automated AMI building pipeline
- Multi-language support (Python, R, Julia)
- Scientific software stack templates
- Version management and tagging

**Implementation Plan**:

```python
# New class: ScientificAMIBuilder
class ScientificAMIBuilder:
    def __init__(self, build_config):
        self.base_os = build_config.get('base_os', 'ubuntu22')
        self.software_stacks = build_config.get('stacks', [])
    
    def build_scientific_ami(self, stack_definition):
        """Build AMI with scientific software stack."""
        pass
    
    def install_python_stack(self, python_version, packages):
        """Install comprehensive Python scientific stack."""
        pass
    
    def install_system_dependencies(self, package_list):
        """Install system-level dependencies.""" 
        pass
```

### 2.4 Enhanced Globus Compute Integration

**Purpose**: Provide first-class Globus Compute endpoint support with advanced container and dependency features.

**Implementation Plan**:

```python
# Enhanced provider configuration for Globus
class GlobusComputeConfig:
    def __init__(self):
        self.endpoint_configs = {}
    
    def generate_endpoint_config(self, provider_instance, endpoint_name):
        """Generate Globus endpoint config with our provider."""
        return {
            'display_name': f"AWS {endpoint_name}",
            'engine': {
                'type': 'GlobusComputeEngine',
                'provider': provider_instance,
                'container_type': provider_instance.container_runtime,
                'container_uri': provider_instance.container_image
            }
        }
```

## Detailed Implementation Roadmap

### Week 1: Foundation and Research
**Goals**: Establish Phase 2 architecture and research optimal approaches

#### Day 1-2: Container Strategy Research
- **Docker vs. Singularity**: Performance comparison on EC2
- **Container Registry**: ECR setup and image management patterns  
- **Security**: Container security best practices for scientific computing
- **Performance**: Overhead analysis (target: <10% vs native)

#### Day 3-4: Dependency Management Architecture
- **Caching Strategy**: Multi-level cache design (memory/EBS/S3)
- **Package Managers**: pip vs conda vs system package integration
- **Environment Isolation**: Virtual environment vs container isolation
- **Conflict Resolution**: Dependency version conflict handling

#### Day 5: Enhanced Provider Design
- **Interface Extensions**: New provider parameters for Phase 2 features
- **Backward Compatibility**: Ensure Phase 1.5 workflows continue working
- **Configuration Management**: Enhanced config system for complex scenarios

**Deliverables**:
- `PHASE2_ARCHITECTURE.md` - Technical architecture document
- `container_strategy_research.md` - Container approach analysis
- `dependency_management_design.md` - Caching and installation strategy

### Week 2: Core Container Implementation
**Goals**: Implement Docker container support with basic dependency management

#### Container Runtime Implementation
```python
# File: container_runtime.py
class DockerRuntimeManager:
    def __init__(self, provider):
        self.provider = provider
        self.ecr_client = provider.session.client('ecr')
    
    def setup_docker_on_instance(self, instance_id):
        """Install and configure Docker on EC2 instance."""
        commands = [
            "sudo apt-get update",
            "sudo apt-get install -y docker.io",
            "sudo systemctl start docker",
            "sudo systemctl enable docker", 
            "sudo usermod -a -G docker ubuntu"
        ]
        return self.provider.execute_remote_commands(instance_id, commands)
    
    def run_containerized_worker(self, instance_id, container_config, worker_cmd):
        """Launch Parsl worker inside container."""
        docker_cmd = [
            "docker", "run", "-d",
            "--network", "host",  # Use host networking for tunnels
            "-v", "/tmp:/tmp",    # Mount temp directory
            container_config['image'],
            "bash", "-c", worker_cmd
        ]
        return self.provider.execute_remote_command(instance_id, docker_cmd)
```

#### Enhanced Provider Integration
```python
# Enhanced phase15_enhanced.py
class AWSProvider(ExecutionProvider):
    def __init__(self, **config):
        # Phase 1.5 compatibility
        super().__init__(**config)
        
        # Phase 2 features
        self.container_runtime = config.get('container_runtime')
        self.container_image = config.get('container_image')
        self.worker_init = config.get('worker_init', [])
        self.dependency_cache = config.get('dependency_cache', True)
        
        # Initialize new managers
        if self.container_runtime:
            self.container_manager = DockerRuntimeManager(self)
        self.dependency_manager = DependencyManager(config)
```

**Week 2 Deliverables**:
- Basic Docker container support
- Enhanced provider with container parameters
- Container image building capabilities
- Integration tests for containerized workers

### Week 3: Dependency Management System  
**Goals**: Implement intelligent dependency caching and installation

#### Multi-Level Caching Implementation
```python
# File: dependency_cache.py
class DependencyCache:
    def __init__(self, cache_config):
        self.levels = {
            'memory': MemoryCache(),
            'ebs': EBSVolumeCache(),
            's3': S3ArtifactCache()
        }
    
    def get_cached_environment(self, env_hash):
        """Retrieve cached environment from fastest available level."""
        for level in ['memory', 'ebs', 's3']:
            if env := self.levels[level].get(env_hash):
                return env
        return None
    
    def cache_environment(self, env_hash, environment_data):
        """Store environment in all cache levels."""
        for level in self.levels.values():
            level.store(env_hash, environment_data)
```

#### Package Installation Pipeline
```python
@python_app
def install_scientific_stack(packages, python_version="3.10"):
    """Install scientific computing packages with caching."""
    import subprocess
    import time
    
    start_time = time.time()
    
    # Create isolated environment
    subprocess.run([f"python{python_version}", "-m", "venv", "/tmp/sci_env"])
    
    # Install packages
    for package in packages:
        result = subprocess.run([
            "/tmp/sci_env/bin/pip", "install", package
        ], capture_output=True, text=True)
        
        if result.returncode != 0:
            raise Exception(f"Failed to install {package}: {result.stderr}")
    
    return {
        'packages': packages,
        'install_time': time.time() - start_time,
        'environment_path': '/tmp/sci_env'
    }
```

**Week 3 Deliverables**:
- Multi-level dependency caching system
- Package installation pipeline with error handling
- Environment isolation and conflict resolution
- Performance benchmarks for cached vs fresh installs

### Week 4: Advanced Container Features
**Goals**: Implement production-ready container features and optimization

#### Custom Container Building
```python
# File: ami_container_builder.py  
class ScientificContainerBuilder:
    def __init__(self, build_config):
        self.base_images = {
            'python': 'python:3.10-slim',
            'scientific': 'continuumio/miniconda3:latest', 
            'gpu': 'nvidia/cuda:12.0-devel-ubuntu22.04'
        }
    
    def build_scientific_container(self, stack_name, requirements):
        """Build container with scientific software stack."""
        dockerfile = self.generate_scientific_dockerfile(stack_name, requirements)
        image_tag = f"scientific-computing:{stack_name}"
        
        # Build and push to ECR
        return self.build_and_push(dockerfile, image_tag)
    
    def generate_scientific_dockerfile(self, stack_name, requirements):
        """Generate optimized Dockerfile for scientific computing."""
        base_image = self.base_images.get(stack_name, self.base_images['scientific'])
        
        dockerfile = f"""
FROM {base_image}

# Install system dependencies
RUN apt-get update && apt-get install -y \\
    build-essential \\
    libopenblas-dev \\
    liblapack-dev \\
    gfortran \\
    && rm -rf /var/lib/apt/lists/*

# Install Python packages
RUN pip install --no-cache-dir {' '.join(requirements)}

# Configure for Parsl execution
RUN useradd -m -s /bin/bash parsl
USER parsl
WORKDIR /home/parsl
"""
        return dockerfile
```

#### Performance Optimization
```python
# Container performance optimizations
class ContainerOptimizer:
    def optimize_for_compute(self, container_config):
        """Optimize container for scientific computing."""
        optimizations = {
            # CPU optimizations
            'cpu_limit': None,  # No CPU limiting
            'memory_limit': '90%',  # Leave 10% for system
            
            # Network optimizations  
            'network_mode': 'host',  # Use host networking for SSH tunnels
            
            # Storage optimizations
            'tmpfs': {'/tmp': 'size=2g'},  # Fast temp storage
            'volumes': {'/data': {'bind': '/data', 'mode': 'rw'}}
        }
        return optimizations
```

**Week 4 Deliverables**:
- Custom scientific container building pipeline
- Performance optimization for compute workloads  
- Integration with ECR for container registry
- Automated testing of containerized workflows

### Week 5: Globus Compute Enhancement
**Goals**: First-class Globus Compute integration with full dependency support

#### Globus Endpoint Manager
```python
# File: globus_integration.py
class GlobusEndpointManager:
    def __init__(self, aws_provider):
        self.provider = aws_provider
        self.gc_client = globus_compute_sdk.Client()
    
    def create_endpoint_config(self, endpoint_name, container_image=None):
        """Generate Globus endpoint configuration."""
        config = {
            'display_name': f"AWS {endpoint_name}",
            'engine': {
                'type': 'GlobusComputeEngine',
                'provider': self.provider,
                'max_retries_on_system_failure': 3,
                'encrypted': True
            }
        }
        
        if container_image:
            config['engine']['container_type'] = 'docker'
            config['engine']['container_uri'] = container_image
            
        return config
    
    def deploy_endpoint(self, config):
        """Deploy and start Globus endpoint with our provider."""
        pass
    
    def register_scientific_functions(self, function_library):
        """Register common scientific functions for reuse."""
        pass
```

#### Multi-Stack Container Support
```python
# Predefined scientific computing stacks
SCIENTIFIC_STACKS = {
    'basic': ['numpy', 'scipy', 'pandas'],
    'ml': ['numpy', 'scipy', 'scikit-learn', 'torch', 'tensorflow'],
    'bio': ['numpy', 'scipy', 'biopython', 'rdkit', 'mdanalysis'],
    'astro': ['numpy', 'scipy', 'astropy', 'healpy', 'photutils'],
    'geo': ['numpy', 'scipy', 'geopandas', 'rasterio', 'folium']
}

class ScientificStackBuilder:
    def build_stack_container(self, stack_name):
        """Build container with predefined scientific stack."""
        if stack_name not in SCIENTIFIC_STACKS:
            raise ValueError(f"Unknown stack: {stack_name}")
            
        requirements = SCIENTIFIC_STACKS[stack_name]
        return self.container_builder.build_scientific_container(stack_name, requirements)
```

**Week 5 Deliverables**:
- Enhanced Globus Compute endpoint creation
- Pre-built scientific computing container stacks
- Function library for common computational patterns
- Multi-site workflow examples

### Week 6: Production Readiness and Optimization  
**Goals**: Performance optimization, production features, comprehensive testing

#### Performance Optimization
- **Container Startup**: Target <30 seconds for cached containers
- **Dependency Installation**: Target <60 seconds for complex stacks
- **Runtime Overhead**: Maintain <10% overhead vs native execution
- **Memory Efficiency**: Optimize container memory usage patterns

#### Production Features
```python
# Enhanced provider with full Phase 2 capabilities
provider = AWSProvider(
    # Phase 1.5 features
    region="us-east-1",
    python_version="3.10",
    
    # Phase 2 features
    container_runtime="docker",
    container_image="scientific-computing:ml-stack-v1.0",
    dependency_cache=True,
    cache_backend="s3",
    
    # Scientific computing optimizations
    instance_type="c5.xlarge",
    ebs_optimized=True,
    placement_group="compute-cluster",
    
    # Globus integration
    enable_globus_endpoint=True,
    endpoint_name="institutional-aws-compute"
)
```

## Implementation Priorities

### Priority 1: Container Support (Critical Path)
Container support unlocks external dependencies and is foundational for all other Phase 2 features.

**Implementation Order**:
1. Docker runtime setup on EC2 instances
2. Basic container execution for Parsl workers  
3. Custom scientific container building
4. ECR integration for container registry

### Priority 2: Dependency Caching (Performance Critical)
Intelligent caching dramatically improves user experience by reducing cold start times.

**Implementation Order**:
1. S3-based artifact caching  
2. EBS volume dependency storage
3. Memory-based package caching
4. Cache invalidation and update mechanisms

### Priority 3: Globus Enhancement (Strategic)
Enhanced Globus integration positions our provider as the enterprise solution for distributed scientific computing.

**Implementation Order**:
1. Endpoint configuration generation
2. Container integration with GlobusComputeEngine
3. Multi-site workflow patterns
4. Enterprise deployment templates

### Priority 4: Scientific Stack Templates (User Experience)
Pre-built scientific computing environments dramatically reduce setup complexity for researchers.

**Implementation Order**:
1. Define common scientific computing stacks
2. Build and validate container images
3. Performance optimization for each stack
4. Documentation and usage examples

## Technical Challenges and Solutions

### Challenge 1: Container Networking with SSH Tunnels
**Problem**: Docker containers need to communicate through SSH reverse tunnels
**Solution**: Use host networking mode to preserve tunnel connectivity
**Implementation**: Configure containers with `--network host` to inherit SSH tunnels

### Challenge 2: Dependency Cache Invalidation
**Problem**: Cached dependencies may become outdated or incompatible
**Solution**: Content-based hashing with dependency version tracking
**Implementation**: SHA-256 hash of requirements.txt + Python version + OS version

### Challenge 3: Container Image Size Optimization
**Problem**: Large scientific containers increase startup times
**Solution**: Multi-stage builds with layer optimization
**Implementation**: Separate base layer (OS + system deps) from package layer (Python packages)

### Challenge 4: Globus Endpoint State Management
**Problem**: Globus endpoints need persistent state across provider restarts
**Solution**: State persistence through AWS Parameter Store
**Implementation**: Store endpoint configuration and registration data in Parameter Store

## Success Metrics and Validation

### Performance Targets
- **Container Cold Start**: <90 seconds for complex scientific stacks
- **Cached Dependency Load**: <30 seconds for pre-cached environments
- **Runtime Overhead**: <10% performance penalty vs native execution
- **Memory Efficiency**: <200MB base container overhead

### Functionality Validation
- **NumPy/SciPy Workflows**: Matrix operations, linear algebra, statistical analysis
- **Machine Learning**: scikit-learn, TensorFlow, PyTorch model training
- **Bioinformatics**: Biopython, sequence analysis, protein structure prediction
- **Geospatial**: GeoPandas, rasterio, satellite imagery processing

### Integration Testing
- **Globus Compute Endpoints**: Multi-endpoint function execution
- **Data Movement**: S3 integration with Globus Transfer
- **Multi-Site Workflows**: AWS + HPC hybrid execution patterns
- **Enterprise Deployment**: Corporate network deployment scenarios

## Risk Mitigation

### Technical Risks
1. **Container Performance**: Docker overhead may be too high
   - *Mitigation*: Benchmark against native, optimize runtime configuration
2. **Dependency Conflicts**: Package version incompatibilities
   - *Mitigation*: Isolated environments, comprehensive testing matrix
3. **Cache Complexity**: Multi-level caching may be overly complex
   - *Mitigation*: Start with simple S3 caching, add layers incrementally

### Integration Risks
1. **Globus Compatibility**: Changes may break existing Globus workflows
   - *Mitigation*: Extensive testing with existing Globus Compute patterns
2. **Parsl Version Compatibility**: New features may require newer Parsl versions
   - *Mitigation*: Maintain compatibility matrix, graceful feature degradation

## Phase 2 Validation Plan

### Week 2 Validation: Basic Container Support
```python
# Test: Basic containerized execution
@parsl.python_app  
def test_container_execution():
    import numpy as np  # Should work with scientific container
    return np.version.version

provider = AWSProvider(
    container_image="scientific-computing:basic-stack",
    container_runtime="docker"
)

result = test_container_execution().result()
assert result is not None  # NumPy available in container
```

### Week 4 Validation: Complex Dependencies
```python
# Test: Machine learning workflow
@parsl.python_app
def train_ml_model(dataset_size):
    import numpy as np
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.datasets import make_classification
    
    # Generate dataset
    X, y = make_classification(n_samples=dataset_size)
    
    # Train model
    model = RandomForestClassifier(n_estimators=100)
    model.fit(X, y)
    
    return {
        'accuracy': model.score(X, y),
        'dataset_size': dataset_size,
        'model_complexity': model.n_estimators
    }

provider = AWSProvider(
    container_image="scientific-computing:ml-stack",
    dependency_cache=True
)

result = train_ml_model(10000).result()
assert result['accuracy'] > 0.8  # Model trained successfully
```

### Week 6 Validation: End-to-End Globus Integration
```python
# Test: Complete Globus Compute workflow
def scientific_analysis_function(data_params):
    """Complex scientific analysis with external dependencies."""
    import numpy as np
    import scipy.stats as stats
    from sklearn.decomposition import PCA
    
    # Generate synthetic research data
    data = np.random.multivariate_normal([0, 0], [[1, 0.5], [0.5, 1]], data_params['n_samples'])
    
    # Principal component analysis
    pca = PCA(n_components=2)
    transformed = pca.fit_transform(data)
    
    # Statistical analysis
    correlation = stats.pearsonr(transformed[:, 0], transformed[:, 1])
    
    return {
        'pca_explained_variance': pca.explained_variance_ratio_.tolist(),
        'correlation_coefficient': correlation[0],
        'p_value': correlation[1],
        'n_samples': data_params['n_samples']
    }

# Submit to Globus endpoint using our enhanced provider
gc = Client()
endpoint_id = "aws-enhanced-endpoint" 

task_id = gc.run(scientific_analysis_function, 
                endpoint_id=endpoint_id,
                data_params={'n_samples': 10000})

result = gc.get_result(task_id)
assert 'pca_explained_variance' in result  # Analysis completed successfully
```

## Continuous Blog Post Updates

As we implement Phase 2 features, the blog post will be continuously updated to include:

### Week 2 Update: Container Support
- Real-world container examples with NumPy/SciPy
- Performance comparison: native vs containerized execution
- Updated case studies with external dependency requirements

### Week 4 Update: Advanced Dependencies  
- Machine learning workflow examples
- Bioinformatics pipeline demonstrations
- Dependency caching performance results

### Week 6 Update: Production Deployment
- Complete enterprise deployment guides
- Advanced Globus Compute patterns
- Performance benchmarks and optimization results

## Next Steps

**Immediate Actions** (This Week):
1. Create Phase 2 architecture document
2. Research container runtime options and performance characteristics  
3. Design dependency management system architecture
4. Begin Docker runtime implementation

**Success Criteria for Phase 2**:
- External Python packages (NumPy, SciPy, etc.) work reliably
- Container startup times <90 seconds
- Globus Compute endpoints support full dependency stacks
- Production-ready deployment for enterprise environments

Phase 2 will transform our universal connectivity foundation into a comprehensive scientific computing platform, enabling researchers to focus on discovery rather than infrastructure management.
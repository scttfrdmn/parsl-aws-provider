# Examples and Tutorials

This document provides concrete examples and tutorials for using the Parsl Ephemeral AWS Provider in various scenarios.

## Basic Usage Examples

### Standard Mode with EC2

```python
import parsl
from parsl.config import Config
from parsl_ephemeral_aws import EphemeralAWSProvider

# Configure the provider
provider = EphemeralAWSProvider(
    image_id='ami-0123456789abcdef0',  # Amazon Linux 2 AMI
    instance_type='t3.micro',
    region='us-east-1',
    mode='standard',
    min_blocks=0,
    max_blocks=10,
    worker_init="""
        #!/bin/bash
        pip install parsl
    """
)

# Configure Parsl
config = Config(
    executors=[
        parsl.executors.HighThroughputExecutor(
            label='aws_executor',
            provider=provider,
        )
    ]
)

# Initialize Parsl with the configuration
parsl.load(config)

# Define a simple Parsl app
@parsl.python_app
def hello(name):
    import platform
    return f"Hello {name} from {platform.node()}"

# Execute the app
future = hello("World")
print(future.result())

# Clean up
parsl.clear()
```

### Detached Mode with Bastion Host

```python
import parsl
from parsl.config import Config
from parsl_ephemeral_aws import EphemeralAWSProvider

# Configure the provider
provider = EphemeralAWSProvider(
    image_id='ami-0123456789abcdef0',
    instance_type='m5.large',
    region='us-east-1',
    mode='detached',
    bastion_instance_type='t3.micro',
    min_blocks=0,
    max_blocks=10,
    worker_init="""
        #!/bin/bash
        pip install parsl
        pip install numpy scipy
    """
)

# Configure Parsl
config = Config(
    executors=[
        parsl.executors.HighThroughputExecutor(
            label='aws_detached_executor',
            provider=provider,
            max_workers=4,
        )
    ]
)

# Initialize Parsl with the configuration
parsl.load(config)

# Define a computation app
@parsl.python_app
def compute_stats(data):
    import numpy as np
    return {
        'mean': np.mean(data),
        'std': np.std(data),
        'min': np.min(data),
        'max': np.max(data)
    }

# Generate some data and compute stats
import numpy as np
data = np.random.normal(size=1000)
future = compute_stats(data.tolist())
print(future.result())

# Clean up
parsl.clear()
```

### Serverless Mode with Lambda

```python
import parsl
from parsl.config import Config
from parsl_ephemeral_aws import EphemeralAWSProvider

# Configure the provider
provider = EphemeralAWSProvider(
    region='us-east-1',
    mode='serverless',
    compute_type='lambda',
    memory_size=1024,  # Memory in MB
    timeout=300,       # Timeout in seconds
    max_blocks=50,     # Max concurrent Lambda invocations
    worker_init="""
        import numpy
        import scipy
    """
)

# Configure Parsl
config = Config(
    executors=[
        parsl.executors.HighThroughputExecutor(
            label='aws_lambda_executor',
            provider=provider,
        )
    ]
)

# Initialize Parsl with the configuration
parsl.load(config)

# Define a simple Lambda-compatible app
@parsl.python_app
def process_data(x):
    # Lambda functions work best with smaller workloads
    import numpy as np
    result = np.square(x) + np.sqrt(abs(x))
    return float(result)

# Execute multiple Lambda functions in parallel
futures = [process_data(i) for i in range(-10, 11)]
results = [f.result() for f in futures]
print(results)

# Clean up
parsl.clear()
```

## Advanced Examples

### Using Spot Instances

```python
import parsl
from parsl.config import Config
from parsl_ephemeral_aws import EphemeralAWSProvider

# Configure the provider with spot instances
provider = EphemeralAWSProvider(
    image_id='ami-0123456789abcdef0',
    instance_type='m5.large',
    region='us-east-1',
    mode='standard',
    min_blocks=0,
    max_blocks=20,
    use_spot=True,
    spot_max_price='0.05',  # Maximum hourly price in USD
    spot_allocation_strategy='capacity-optimized',
    spot_timeout=600,  # Timeout in seconds for spot request
    worker_init="""
        #!/bin/bash
        pip install parsl
        pip install tensorflow
    """
)

# Configure Parsl with retry for spot interruptions
config = Config(
    executors=[
        parsl.executors.HighThroughputExecutor(
            label='aws_spot_executor',
            provider=provider,
            max_workers=4,
        )
    ],
    retries=3,  # Retry tasks in case of spot interruption
)

# Initialize Parsl with the configuration
parsl.load(config)

# Define a machine learning app
@parsl.python_app
def train_model(epochs=10):
    import tensorflow as tf
    import numpy as np

    # Generate synthetic data
    x_train = np.random.random((1000, 10))
    y_train = np.random.random((1000, 1))

    # Create a simple model
    model = tf.keras.models.Sequential([
        tf.keras.layers.Dense(64, activation='relu', input_shape=(10,)),
        tf.keras.layers.Dense(32, activation='relu'),
        tf.keras.layers.Dense(1)
    ])

    model.compile(optimizer='adam', loss='mse')

    # Train the model
    history = model.fit(x_train, y_train, epochs=epochs, verbose=0)

    return {"final_loss": float(history.history['loss'][-1])}

# Train multiple models in parallel
futures = [train_model(epochs=15) for _ in range(5)]
results = [f.result() for f in futures]

for i, result in enumerate(results):
    print(f"Model {i+1} final loss: {result['final_loss']}")

# Clean up
parsl.clear()
```

### MPI-Enabled Workflow

```python
import parsl
from parsl.config import Config
from parsl_ephemeral_aws import EphemeralAWSProvider

# Configure the provider for MPI workloads
provider = EphemeralAWSProvider(
    image_id='ami-0123456789abcdef0',  # AMI with MPI installed
    instance_type='c5.2xlarge',
    region='us-east-1',
    mode='detached',
    min_blocks=1,
    max_blocks=1,  # Single block for MPI
    nodes_per_block=4,  # 4 nodes in the MPI cluster
    worker_init="""
        #!/bin/bash
        pip install parsl
        # Ensure MPI is properly configured
        apt-get update && apt-get install -y mpich
    """
)

# Configure Parsl
config = Config(
    executors=[
        parsl.executors.MPIExecutor(
            label='mpi_executor',
            provider=provider,
            mpi_mode='openmpi',
        )
    ]
)

# Initialize Parsl with the configuration
parsl.load(config)

# Define an MPI app
@parsl.bash_app
def mpi_hello(stdout='mpi_hello.stdout', stderr='mpi_hello.stderr'):
    return """
    mpirun -np 16 -ppn 4 python -c "
from mpi4py import MPI
import socket
import time

comm = MPI.COMM_WORLD
rank = comm.Get_rank()
size = comm.Get_size()

print(f'Hello from rank {rank} of {size} on {socket.gethostname()}')
comm.Barrier()

if rank == 0:
    print(f'MPI World size: {size}')
    print(f'MPI Information: {MPI.Get_vendor()}')
    "
    """

# Run MPI job
future = mpi_hello()
future.result()

# Display output
with open('mpi_hello.stdout', 'r') as f:
    print(f.read())

# Clean up
parsl.clear()
```

### Custom VPC and Network Configuration

```python
import parsl
from parsl.config import Config
from parsl_ephemeral_aws import EphemeralAWSProvider

# Configure the provider with custom networking
provider = EphemeralAWSProvider(
    image_id='ami-0123456789abcdef0',
    instance_type='m5.large',
    region='us-east-1',
    mode='standard',
    min_blocks=0,
    max_blocks=10,

    # Custom VPC configuration
    vpc_id='vpc-0123456789abcdef0',  # Optional: Use existing VPC
    subnet_id='subnet-0123456789abcdef0',  # Optional: Use existing subnet

    # Alternative: Create new VPC with custom CIDR
    create_vpc=True,  # Create a new VPC if vpc_id not provided
    vpc_cidr='10.0.0.0/16',

    # Security group configuration
    security_group_id='sg-0123456789abcdef0',  # Optional: Use existing security group
    create_security_group=True,  # Create a new security group if security_group_id not provided

    # Additional ingress rules for security group
    additional_ingress_rules=[
        {
            'IpProtocol': 'tcp',
            'FromPort': 8888,
            'ToPort': 8888,
            'IpRanges': [{'CidrIp': '0.0.0.0/0'}]
        }
    ],

    worker_init="""
        #!/bin/bash
        pip install parsl jupyter
        jupyter notebook --ip=0.0.0.0 --port=8888 --no-browser --NotebookApp.token='' --NotebookApp.password=''
    """
)

# Configure Parsl
config = Config(
    executors=[
        parsl.executors.HighThroughputExecutor(
            label='aws_custom_net_executor',
            provider=provider,
        )
    ]
)

# Initialize Parsl with the configuration
parsl.load(config)

# Define a simple app
@parsl.python_app
def get_instance_info():
    import socket
    import requests

    hostname = socket.gethostname()

    # Get instance metadata
    try:
        r = requests.get('http://169.254.169.254/latest/meta-data/instance-id', timeout=2)
        instance_id = r.text
        r = requests.get('http://169.254.169.254/latest/meta-data/local-ipv4', timeout=2)
        private_ip = r.text
        r = requests.get('http://169.254.169.254/latest/meta-data/public-ipv4', timeout=2)
        public_ip = r.text
    except:
        instance_id = "unknown"
        private_ip = "unknown"
        public_ip = "unknown"

    return {
        'hostname': hostname,
        'instance_id': instance_id,
        'private_ip': private_ip,
        'public_ip': public_ip
    }

# Run the app
future = get_instance_info()
print(future.result())

# Clean up
parsl.clear()
```

### Data Transfer with S3

```python
import parsl
from parsl.config import Config
from parsl.data_provider.files import File
from parsl_ephemeral_aws import EphemeralAWSProvider

# Configure the provider
provider = EphemeralAWSProvider(
    image_id='ami-0123456789abcdef0',
    instance_type='m5.large',
    region='us-east-1',
    mode='standard',
    min_blocks=0,
    max_blocks=5,

    # S3 bucket for data staging
    s3_bucket='my-parsl-data-bucket',

    worker_init="""
        #!/bin/bash
        pip install parsl boto3 pandas
    """
)

# Configure Parsl
config = Config(
    executors=[
        parsl.executors.HighThroughputExecutor(
            label='aws_data_executor',
            provider=provider,
            storage_access=parsl.data_provider.S3Storage(
                bucket='my-parsl-data-bucket',
                region='us-east-1'
            )
        )
    ]
)

# Initialize Parsl with the configuration
parsl.load(config)

# Define a file processing app
@parsl.python_app
def process_csv(input_file, outputs=[]):
    import pandas as pd

    # Read the input CSV
    df = pd.read_csv(input_file)

    # Process the data
    result = df.groupby('category').agg({'value': ['mean', 'std', 'count']})

    # Write the result to the output file
    result.to_csv(outputs[0])

    return f"Processed {len(df)} rows into {len(result)} categories"

# Create input and output file objects
input_file = File('s3://my-parsl-data-bucket/input.csv')
output_file = File('s3://my-parsl-data-bucket/output.csv')

# Run the processing app
future = process_csv(input_file, outputs=[output_file])
print(future.result())

# Clean up
parsl.clear()
```

## Real-World Use Cases

### Machine Learning Hyperparameter Tuning

```python
import parsl
from parsl.config import Config
from parsl_ephemeral_aws import EphemeralAWSProvider
import numpy as np

# Configure the provider
provider = EphemeralAWSProvider(
    image_id='ami-0123456789abcdef0',
    instance_type='p3.2xlarge',  # GPU instance for ML
    region='us-east-1',
    mode='detached',
    min_blocks=0,
    max_blocks=10,
    use_spot=True,  # Use spot instances for cost savings
    worker_init="""
        #!/bin/bash
        pip install parsl scikit-learn torch
    """
)

# Configure Parsl
config = Config(
    executors=[
        parsl.executors.HighThroughputExecutor(
            label='ml_executor',
            provider=provider,
            max_workers=1,  # One worker per GPU
        )
    ]
)

# Initialize Parsl with the configuration
parsl.load(config)

# Define a hyperparameter tuning app
@parsl.python_app
def train_and_evaluate(learning_rate, hidden_units, dropout_rate):
    import torch
    import torch.nn as nn
    import torch.optim as optim
    from torch.utils.data import DataLoader, TensorDataset
    import numpy as np

    # Generate synthetic data
    np.random.seed(42)
    X = np.random.rand(10000, 20).astype(np.float32)
    y = (X[:, 0] + X[:, 1]**2 + np.sin(X[:, 2]) > 1.0).astype(np.float32)

    # Split data
    split = int(0.8 * len(X))
    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]

    # Convert to PyTorch tensors
    X_train_tensor = torch.tensor(X_train)
    y_train_tensor = torch.tensor(y_train).view(-1, 1)
    X_test_tensor = torch.tensor(X_test)
    y_test_tensor = torch.tensor(y_test).view(-1, 1)

    # Create data loaders
    train_dataset = TensorDataset(X_train_tensor, y_train_tensor)
    train_loader = DataLoader(train_dataset, batch_size=64, shuffle=True)

    # Define model
    class MLP(nn.Module):
        def __init__(self, input_dim, hidden_dim, dropout_rate):
            super(MLP, self).__init__()
            self.fc1 = nn.Linear(input_dim, hidden_dim)
            self.fc2 = nn.Linear(hidden_dim, hidden_dim // 2)
            self.fc3 = nn.Linear(hidden_dim // 2, 1)
            self.dropout = nn.Dropout(dropout_rate)
            self.relu = nn.ReLU()
            self.sigmoid = nn.Sigmoid()

        def forward(self, x):
            x = self.relu(self.fc1(x))
            x = self.dropout(x)
            x = self.relu(self.fc2(x))
            x = self.dropout(x)
            x = self.sigmoid(self.fc3(x))
            return x

    # Initialize model
    model = MLP(input_dim=20, hidden_dim=hidden_units, dropout_rate=dropout_rate)

    # Check if GPU is available
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    model.to(device)

    # Define loss function and optimizer
    criterion = nn.BCELoss()
    optimizer = optim.Adam(model.parameters(), lr=learning_rate)

    # Train model
    epochs = 50
    for epoch in range(epochs):
        model.train()
        for inputs, targets in train_loader:
            inputs, targets = inputs.to(device), targets.to(device)

            optimizer.zero_grad()
            outputs = model(inputs)
            loss = criterion(outputs, targets)
            loss.backward()
            optimizer.step()

    # Evaluate model
    model.eval()
    with torch.no_grad():
        X_test_tensor = X_test_tensor.to(device)
        y_test_tensor = y_test_tensor.to(device)

        test_outputs = model(X_test_tensor)
        test_loss = criterion(test_outputs, y_test_tensor).item()

        predictions = (test_outputs > 0.5).float()
        accuracy = (predictions == y_test_tensor).float().mean().item()

    return {
        'hyperparameters': {
            'learning_rate': learning_rate,
            'hidden_units': hidden_units,
            'dropout_rate': dropout_rate
        },
        'test_loss': test_loss,
        'accuracy': accuracy,
        'device': str(device)
    }

# Define hyperparameter grid
learning_rates = [0.001, 0.01, 0.1]
hidden_units = [64, 128, 256]
dropout_rates = [0.2, 0.5]

# Launch hyperparameter tuning jobs
futures = []
for lr in learning_rates:
    for hu in hidden_units:
        for dr in dropout_rates:
            future = train_and_evaluate(lr, hu, dr)
            futures.append(future)

# Collect and analyze results
results = [f.result() for f in futures]
results.sort(key=lambda x: x['accuracy'], reverse=True)

print("Top 3 hyperparameter configurations:")
for i in range(min(3, len(results))):
    res = results[i]
    print(f"Rank {i+1}:")
    print(f"  Learning Rate: {res['hyperparameters']['learning_rate']}")
    print(f"  Hidden Units: {res['hyperparameters']['hidden_units']}")
    print(f"  Dropout Rate: {res['hyperparameters']['dropout_rate']}")
    print(f"  Accuracy: {res['accuracy']:.4f}")
    print(f"  Test Loss: {res['test_loss']:.4f}")
    print(f"  Device Used: {res['device']}")
    print()

# Clean up
parsl.clear()
```

### Genomic Data Processing

```python
import parsl
from parsl.config import Config
from parsl.data_provider.files import File
from parsl_ephemeral_aws import EphemeralAWSProvider

# Configure the provider for genomic processing
provider = EphemeralAWSProvider(
    image_id='ami-0123456789abcdef0',  # AMI with bioinformatics tools
    instance_type='r5.2xlarge',        # Memory-optimized instance
    region='us-east-1',
    mode='detached',
    min_blocks=0,
    max_blocks=20,
    s3_bucket='my-genomics-bucket',
    worker_init="""
        #!/bin/bash
        # Install bioinformatics tools
        apt-get update
        apt-get install -y samtools bwa bedtools
        pip install parsl biopython pysam
    """
)

# Configure Parsl
config = Config(
    executors=[
        parsl.executors.HighThroughputExecutor(
            label='genomics_executor',
            provider=provider,
            storage_access=parsl.data_provider.S3Storage(
                bucket='my-genomics-bucket',
                region='us-east-1'
            )
        )
    ]
)

# Initialize Parsl with the configuration
parsl.load(config)

# Define apps for genomic data processing

# App to align reads to a reference genome
@parsl.bash_app
def align_reads(reference, reads, output, cores=8,
                stdout=parsl.AUTO_LOGNAME, stderr=parsl.AUTO_LOGNAME):
    return f"""
    # Index the reference genome if index doesn't exist
    if [ ! -f {reference}.bwt ]; then
        bwa index {reference}
    fi

    # Align reads to the reference
    bwa mem -t {cores} {reference} {reads} > {output}
    """

# App to convert SAM to BAM and sort
@parsl.bash_app
def sam_to_sorted_bam(sam_file, bam_file, cores=8,
                      stdout=parsl.AUTO_LOGNAME, stderr=parsl.AUTO_LOGNAME):
    return f"""
    # Convert SAM to BAM and sort
    samtools view -b -@ {cores} {sam_file} | samtools sort -@ {cores} -o {bam_file}

    # Index the BAM file
    samtools index {bam_file}
    """

# App to call variants
@parsl.bash_app
def call_variants(reference, bam_file, vcf_file, cores=8,
                 stdout=parsl.AUTO_LOGNAME, stderr=parsl.AUTO_LOGNAME):
    return f"""
    # Call variants
    samtools mpileup -uf {reference} {bam_file} | bcftools call -mv -o {vcf_file}
    """

# App to analyze variants
@parsl.python_app
def analyze_variants(vcf_file):
    import pysam

    # Open the VCF file
    vcf = pysam.VariantFile(vcf_file)

    # Count variants by type
    variant_types = {
        'SNP': 0,
        'INDEL': 0,
        'OTHER': 0
    }

    # Analyze variants
    for record in vcf.fetch():
        # Determine variant type
        if len(record.ref) == 1 and all(len(alt) == 1 for alt in record.alts):
            variant_types['SNP'] += 1
        elif len(record.ref) != len(record.alts[0]):
            variant_types['INDEL'] += 1
        else:
            variant_types['OTHER'] += 1

    return {
        'vcf_file': vcf_file,
        'total_variants': sum(variant_types.values()),
        'variant_types': variant_types
    }

# Create file objects for input and output
reference = File('s3://my-genomics-bucket/reference/hg38.fa')
reads = File('s3://my-genomics-bucket/samples/sample1.fastq')
sam_output = File('s3://my-genomics-bucket/results/sample1.sam')
bam_output = File('s3://my-genomics-bucket/results/sample1.sorted.bam')
vcf_output = File('s3://my-genomics-bucket/results/sample1.vcf')

# Run the workflow
aligned = align_reads(reference, reads, sam_output)
sorted_bam = sam_to_sorted_bam(sam_output, bam_output, inputs=[aligned])
variants = call_variants(reference, bam_output, vcf_output, inputs=[sorted_bam])
analysis = analyze_variants(vcf_output, inputs=[variants])

# Wait for the final result and display
result = analysis.result()
print(f"Analysis of {result['vcf_file']}:")
print(f"Total variants: {result['total_variants']}")
print("Variant types:")
for vtype, count in result['variant_types'].items():
    print(f"  {vtype}: {count}")

# Clean up
parsl.clear()
```

## Troubleshooting Examples

### Diagnosing Connection Issues

```python
import parsl
from parsl.config import Config
from parsl_ephemeral_aws import EphemeralAWSProvider
import logging

# Enable detailed logging
parsl.set_stream_logger()
logging.getLogger('parsl_ephemeral_aws').setLevel(logging.DEBUG)

# Configure the provider with troubleshooting options
provider = EphemeralAWSProvider(
    image_id='ami-0123456789abcdef0',
    instance_type='t3.micro',
    region='us-east-1',
    mode='standard',
    min_blocks=0,
    max_blocks=1,

    # Add connection diagnostic flags
    debug=True,
    wait_for_connection=True,
    connection_timeout=120,

    worker_init="""
        #!/bin/bash
        pip install parsl
        echo "Worker initializing" > /tmp/worker.log
    """
)

# Configure Parsl with more debug options
config = Config(
    executors=[
        parsl.executors.HighThroughputExecutor(
            label='debug_executor',
            provider=provider,
            address_probe_timeout=120,
            heartbeat_threshold=120,
        )
    ]
)

# Initialize Parsl with the configuration
parsl.load(config)

# Define a diagnostic app
@parsl.python_app
def diagnostic_check():
    import socket
    import os
    import subprocess
    import platform

    # Basic system info
    hostname = socket.gethostname()

    # Network connectivity check
    network_check = {}
    targets = ['8.8.8.8', 'amazon.com', 'github.com']
    for target in targets:
        try:
            subprocess.check_output(['ping', '-c', '3', target],
                                   stderr=subprocess.STDOUT,
                                   universal_newlines=True)
            network_check[target] = "OK"
        except subprocess.CalledProcessError as e:
            network_check[target] = f"FAIL: {e.output}"

    # Check environment
    env_vars = {k: v for k, v in os.environ.items() if 'AWS' in k or 'PARSL' in k}

    # File system check
    fs_info = {}
    try:
        df_output = subprocess.check_output(['df', '-h'],
                                           universal_newlines=True)
        fs_info['df'] = df_output
    except:
        fs_info['df'] = "Failed to run df command"

    return {
        'hostname': hostname,
        'platform': platform.platform(),
        'python_version': platform.python_version(),
        'network_check': network_check,
        'environment': env_vars,
        'filesystem': fs_info
    }

# Run diagnostic check
try:
    future = diagnostic_check()
    result = future.result()
    print("Diagnostic Results:")
    print(f"Hostname: {result['hostname']}")
    print(f"Platform: {result['platform']}")
    print(f"Python Version: {result['python_version']}")
    print("Network Checks:")
    for target, status in result['network_check'].items():
        print(f"  {target}: {status}")
    print("Environment Variables:")
    for var, value in result['environment'].items():
        print(f"  {var}: {value}")
except Exception as e:
    print(f"Diagnostic failed: {e}")
finally:
    # Clean up
    parsl.clear()
```

## Migration Examples

### Migrating from Parsl EC2Provider

```python
# Old configuration with EC2Provider
import parsl
from parsl.config import Config
from parsl.providers import AWSProvider
from parsl.executors import HighThroughputExecutor

# Old configuration
old_config = Config(
    executors=[
        HighThroughputExecutor(
            label='ec2_executor',
            provider=AWSProvider(
                image_id='ami-0123456789abcdef0',
                instance_type='t2.medium',
                region='us-east-1',
                key_name='my-key',
                state_file='ec2_state.json',
                spot_max_bid='0.1',
            ),
        )
    ]
)

# New configuration with EphemeralAWSProvider
from parsl_ephemeral_aws import EphemeralAWSProvider

# New configuration
new_config = Config(
    executors=[
        HighThroughputExecutor(
            label='aws_executor',
            provider=EphemeralAWSProvider(
                image_id='ami-0123456789abcdef0',
                instance_type='t3.medium',  # Upgraded to newer instance type
                region='us-east-1',
                mode='standard',
                min_blocks=0,
                max_blocks=10,
                use_spot=True,
                spot_max_price='0.1',
                state_file_path='ec2_state.json',
                worker_init="""
                    #!/bin/bash
                    pip install parsl
                """
            ),
        )
    ]
)
```

## Cleanup and Resource Management

```python
import parsl
from parsl.config import Config
from parsl_ephemeral_aws import EphemeralAWSProvider
import time

# Configure the provider with resource tracking
provider = EphemeralAWSProvider(
    image_id='ami-0123456789abcdef0',
    instance_type='t3.micro',
    region='us-east-1',
    mode='standard',
    min_blocks=0,
    max_blocks=5,

    # Resource tracking options
    tag_resources=True,
    resource_prefix='parsl-demo',
    auto_shutdown=True,
    max_idle_time=300,  # 5 minutes of idle time before shutdown

    worker_init="""
        #!/bin/bash
        pip install parsl
    """
)

# Configure Parsl
config = Config(
    executors=[
        parsl.executors.HighThroughputExecutor(
            label='tracked_executor',
            provider=provider,
        )
    ]
)

# Initialize Parsl with the configuration
parsl.load(config)

# Define a simple app
@parsl.python_app
def hello(sleep_time=0):
    import time
    import socket
    time.sleep(sleep_time)
    return f"Hello from {socket.gethostname()}"

# Run a few tasks with different execution times
futures = [hello(i * 10) for i in range(5)]

# Process results as they complete
for i, future in enumerate(futures):
    print(f"Task {i} result: {future.result()}")

# List all resources
print("\nListing all AWS resources created by this provider:")
resources = provider.list_resources()
print(f"Total resources: {len(resources)}")
for resource_type, resource_list in resources.items():
    print(f"{resource_type}: {len(resource_list)} resources")

# Demonstrate manual cleanup
print("\nDemonstrating manual cleanup of resources...")
provider.cleanup_all()

# Clean up Parsl
parsl.clear()
```

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors

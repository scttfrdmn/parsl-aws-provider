# Efficient Data Movement with AWS Provider

## The Data Architecture Problem

When running scientific computing on AWS through SSH tunnels, **data movement strategy is critical**. Poor data flow design can:

- Saturate SSH tunnel bandwidth with large files
- Create unnecessary latency for data-intensive workloads
- Waste AWS network resources on inefficient transfers
- Limit scalability due to tunnel bottlenecks

## Optimal Data Flow Architecture

### The Solution: Separation of Concerns

**SSH Tunnels**: Control and coordination (small messages)
- Job submission and status
- Worker registration and heartbeats
- Small result summaries
- Error messages and logging

**S3/HTTPS**: Large data movement (optimal bandwidth)
- Input datasets and files
- Intermediate processing results
- Final output data and reports
- Container images and dependencies

```
┌─────────────────┐    SSH Tunnel     ┌──────────────────┐
│ Local Controller│ ◄──────────────── │ AWS Worker       │
│                 │ Control Messages  │                  │
│                 │ (lightweight)     │ ┌──────────────┐ │
│                 │                   │ │ S3 Download  │ │
│ S3 Bucket       │ ◄─────────────────┼─┤ Process Data │ │
│ (large files)   │   AWS Network     │ │ S3 Upload    │ │
│                 │   (optimal)       │ └──────────────┘ │
└─────────────────┘                   └──────────────────┘
```

## Pattern 1: S3 Data Staging

### Upload Research Data to S3

```python
@python_app
def stage_research_data_to_s3(dataset_config):
    """Generate research data and upload to S3."""
    import boto3
    import json
    import time

    # Generate your research dataset
    research_data = generate_experimental_data(dataset_config)

    # Upload to S3 (AWS-internal transfer)
    s3_client = boto3.client('s3')
    bucket = dataset_config['s3_bucket']
    key = f"research_data/{dataset_config['experiment_id']}.json"

    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(research_data, indent=2),
        ContentType='application/json'
    )

    # Return S3 URI (small message via SSH tunnel)
    return {
        'experiment_id': dataset_config['experiment_id'],
        's3_uri': f's3://{bucket}/{key}',
        'sample_count': len(research_data),
        'upload_success': True
    }

# Usage
upload_task = stage_research_data_to_s3({
    'experiment_id': 'climate_2024_001',
    'sample_count': 10000,
    's3_bucket': 'my-research-bucket'
})

result = upload_task.result()
print(f"Data staged: {result['s3_uri']}")
```

### Process S3 Data with Results to S3

```python
@bash_app
def process_s3_research_data(s3_input_uri, s3_output_uri, analysis_config):
    """Download from S3, process, upload results to S3."""
    return f"""
    # Download research data (AWS-to-AWS, optimal bandwidth)
    aws s3 cp {s3_input_uri} /tmp/input_data.json --region us-east-1

    # Process data locally on AWS instance
    python3 << 'EOF'
import json
import boto3
import statistics

# Load data
with open('/tmp/input_data.json', 'r') as f:
    data = json.load(f)

# Perform analysis
analysis_results = analyze_research_data(data, "{analysis_config}")

# Save processed results
with open('/tmp/analysis_results.json', 'w') as f:
    json.dump(analysis_results, f, indent=2)

print(f"Analysis complete: {{len(data)}} samples processed")
EOF

    # Upload results (AWS-to-AWS)
    aws s3 cp /tmp/analysis_results.json {s3_output_uri} --region us-east-1

    echo "Processing complete: {s3_output_uri}"
    """

# Usage
process_task = process_s3_research_data(
    "s3://my-research-bucket/research_data/climate_2024_001.json",
    "s3://my-research-bucket/analysis_results/climate_2024_001_analysis.json",
    "comprehensive_stats"
)
```

## Pattern 2: Public Data via HTTPS

### Direct Public Dataset Processing

```python
@python_app
def analyze_public_dataset(dataset_url, analysis_params):
    """Download public dataset via HTTPS and analyze."""
    import urllib.request
    import json
    import csv
    import boto3

    # Download directly to AWS worker (no SSH tunnel)
    urllib.request.urlretrieve(dataset_url, "/tmp/public_data.csv")

    # Process data
    with open("/tmp/public_data.csv", 'r') as f:
        reader = csv.DictReader(f)
        data = list(reader)

    # Perform analysis
    results = perform_statistical_analysis(data, analysis_params)

    # Optionally upload detailed results to S3
    if analysis_params.get('save_to_s3'):
        s3_client = boto3.client('s3')
        s3_key = f"public_analysis/{int(time.time())}_analysis.json"

        s3_client.put_object(
            Bucket=analysis_params['s3_bucket'],
            Key=s3_key,
            Body=json.dumps(results, indent=2)
        )

        s3_uri = f"s3://{analysis_params['s3_bucket']}/{s3_key}"
    else:
        s3_uri = None

    # Return summary (small message via SSH tunnel)
    return {
        'dataset_url': dataset_url,
        'record_count': len(data),
        'analysis_complete': True,
        's3_detailed_results': s3_uri,
        'summary': extract_key_findings(results)
    }

# Usage
public_task = analyze_public_dataset(
    "https://example.com/research-dataset.csv",
    {
        'analysis_type': 'comprehensive',
        'save_to_s3': True,
        's3_bucket': 'my-research-bucket'
    }
)
```

## Pattern 3: Globus Compute with S3 Integration

### Endpoint Configuration for S3 Access

```yaml
# ~/.globus_compute/aws_s3_endpoint/config.yaml
display_name: "AWS S3 Research Endpoint"
engine:
  type: GlobusComputeEngine

  provider:
    type: AWSProvider
    region: us-east-1
    instance_type: c5.2xlarge  # Larger for data processing
    enable_ssm_tunneling: true
    init_blocks: 1
    max_blocks: 10

    # IAM role with S3 access permissions
    iam_instance_profile: "ParslS3AccessRole"

  max_workers_per_node: 1
```

### S3-Optimized Functions

```python
from globus_compute_sdk import Client

def large_scale_s3_analysis(s3_config):
    """Process large datasets via S3 with minimal Globus overhead."""
    import boto3
    import json
    import time

    start_time = time.time()
    s3_client = boto3.client('s3')

    # Download multiple datasets from S3
    datasets = []
    for dataset_name, s3_uri in s3_config['input_datasets'].items():
        bucket, key = parse_s3_uri(s3_uri)
        local_file = f'/tmp/{dataset_name}.json'

        s3_client.download_file(bucket, key, local_file)

        with open(local_file, 'r') as f:
            data = json.load(f)
            datasets.append({
                'name': dataset_name,
                'data': data,
                'size': len(data)
            })

    # Perform cross-dataset analysis
    analysis_results = cross_dataset_analysis(datasets, s3_config['analysis_params'])

    # Upload comprehensive results to S3
    output_bucket = s3_config['output_bucket']
    output_key = f"cross_analysis/{int(time.time())}_comprehensive.json"

    s3_client.put_object(
        Bucket=output_bucket,
        Key=output_key,
        Body=json.dumps(analysis_results, indent=2)
    )

    # Return lightweight summary via Globus
    return {
        'datasets_processed': len(datasets),
        'total_samples': sum(d['size'] for d in datasets),
        'processing_time': time.time() - start_time,
        's3_results_uri': f's3://{output_bucket}/{output_key}',
        'key_insights': extract_summary_insights(analysis_results)
    }

# Execute via Globus Compute
gc = Client()
endpoint_id = "your-s3-optimized-endpoint"

# Submit large-scale analysis
task_id = gc.run(
    large_scale_s3_analysis,
    endpoint_id=endpoint_id,
    s3_config={
        'input_datasets': {
            'experiment_1': 's3://research-bucket/exp1_data.json',
            'experiment_2': 's3://research-bucket/exp2_data.json',
            'experiment_3': 's3://research-bucket/exp3_data.json'
        },
        'analysis_params': {
            'correlation_analysis': True,
            'statistical_modeling': True,
            'outlier_detection': True
        },
        'output_bucket': 'research-bucket'
    }
)

result = gc.get_result(task_id)
print(f"✅ Analysis complete: {result['total_samples']} samples in {result['processing_time']:.1f}s")
print(f"📊 Detailed results: {result['s3_results_uri']}")
```

## Pattern 4: Hybrid Local + S3 Workflows

### Local Preprocessing with S3 Distribution

```python
# Local preprocessing
@python_app
def preprocess_local_data(local_data_path):
    """Preprocess local data and upload to S3 for distributed processing."""
    import boto3
    import json
    import pandas as pd  # If available locally

    # Process local data
    processed_data = preprocess_experimental_data(local_data_path)

    # Upload to S3 for distribution to workers
    s3_client = boto3.client('s3')
    bucket = 'research-pipeline-bucket'

    # Upload in chunks for parallel processing
    chunk_uris = []
    chunk_size = 1000

    for i in range(0, len(processed_data), chunk_size):
        chunk = processed_data[i:i+chunk_size]
        chunk_key = f'pipeline_data/chunk_{i//chunk_size:04d}.json'

        s3_client.put_object(
            Bucket=bucket,
            Key=chunk_key,
            Body=json.dumps(chunk, indent=2)
        )

        chunk_uris.append(f's3://{bucket}/{chunk_key}')

    return {
        'preprocessing_complete': True,
        'total_samples': len(processed_data),
        'chunk_count': len(chunk_uris),
        'chunk_uris': chunk_uris
    }

# Distributed processing of S3 chunks
@bash_app
def process_s3_chunk(s3_chunk_uri, chunk_analysis_config):
    """Process individual S3 data chunk."""
    return f"""
    # Download chunk from S3
    aws s3 cp {s3_chunk_uri} /tmp/chunk_data.json --region us-east-1

    # Process chunk
    python3 << 'EOF'
import json

with open('/tmp/chunk_data.json', 'r') as f:
    chunk_data = json.load(f)

# Process this chunk
chunk_results = process_data_chunk(chunk_data, "{chunk_analysis_config}")

with open('/tmp/chunk_results.json', 'w') as f:
    json.dump(chunk_results, f)

print(f"Chunk processed: {{len(chunk_data)}} samples")
EOF

    # Upload chunk results to S3
    OUTPUT_URI=$(echo {s3_chunk_uri} | sed 's/pipeline_data/chunk_results/')
    aws s3 cp /tmp/chunk_results.json $OUTPUT_URI --region us-east-1

    echo "Chunk complete: $OUTPUT_URI"
    """

# Orchestrate parallel chunk processing
preprocessing_result = preprocess_local_data("/path/to/local/data.csv").result()

chunk_tasks = []
for chunk_uri in preprocessing_result['chunk_uris']:
    task = process_s3_chunk(chunk_uri, "statistical_analysis")
    chunk_tasks.append(task)

# Wait for all chunks to complete
chunk_results = [task.result() for task in chunk_tasks]
print(f"✅ Processed {len(chunk_results)} data chunks via S3")
```

## Performance Comparison

### Traditional Approach (Inefficient)
```python
# BAD: Large files through SSH tunnels
@python_app
def inefficient_processing(large_dataset):  # 100MB dataset
    # This sends 100MB through SSH tunnel!
    return process_data(large_dataset)

# Problem: 100MB through tunnel = slow, bandwidth waste
```

### Optimal Approach (Efficient)
```python
# GOOD: Large files via S3, coordination via tunnels
@bash_app
def efficient_processing(s3_dataset_uri):
    return f"""
    # Fast S3 download (AWS-internal network)
    aws s3 cp {s3_dataset_uri} /tmp/data.csv

    # Process locally on AWS
    python process_script.py /tmp/data.csv /tmp/results.csv

    # Fast S3 upload
    aws s3 cp /tmp/results.csv s3://results-bucket/output.csv

    echo "Complete"  # Only this message via SSH tunnel
    """

# Benefits: 100MB via AWS network, 8 bytes via tunnel
```

## Data Movement Best Practices

### 1. Use S3 for Large Files

```python
# Generate data on AWS worker, upload to S3
@python_app
def create_large_dataset():
    import boto3

    # Generate GB-scale dataset
    large_dataset = generate_simulation_data(size_gb=2)

    # Upload to S3 (not through tunnel)
    s3_client = boto3.client('s3')
    s3_client.put_object(
        Bucket='research-data',
        Key='large_simulation_output.json',
        Body=json.dumps(large_dataset)
    )

    # Return only metadata via tunnel
    return {
        's3_uri': 's3://research-data/large_simulation_output.json',
        'size_gb': 2,
        'generation_complete': True
    }
```

### 2. Use HTTPS for Public Data

```python
@python_app
def process_public_research_data(public_url):
    import urllib.request

    # Download directly to AWS worker
    urllib.request.urlretrieve(public_url, '/tmp/public_data.csv')

    # Process and return summary
    results = analyze_csv_data('/tmp/public_data.csv')

    # Return summary via tunnel, detailed results via S3
    return {
        'source_url': public_url,
        'summary': results['summary'],
        'detailed_s3_uri': results['s3_detailed_uri']
    }
```

### 3. Chain S3 Operations for Pipelines

```python
# Multi-stage pipeline with S3 intermediate storage
stage1 = upload_raw_data_to_s3(raw_data_config)
stage2 = process_s3_data(stage1.result()['s3_uri'], processing_config)
stage3 = analyze_s3_results(stage2.result()['s3_output_uri'], analysis_config)

final_result = stage3.result()
print(f"Pipeline complete: {final_result['final_s3_uri']}")
```

### 4. Globus Compute with S3 Integration

```python
from globus_compute_sdk import Client

def s3_enabled_research_function(s3_inputs, analysis_config):
    """Research function optimized for S3 data flow."""
    import boto3

    # Download inputs from S3
    s3_client = boto3.client('s3')

    input_data = {}
    for name, s3_uri in s3_inputs.items():
        bucket, key = parse_s3_uri(s3_uri)
        local_file = f'/tmp/{name}.json'
        s3_client.download_file(bucket, key, local_file)

        with open(local_file, 'r') as f:
            input_data[name] = json.load(f)

    # Perform research computation
    research_results = complex_research_analysis(input_data, analysis_config)

    # Upload detailed results to S3
    output_key = f'research_results/{analysis_config["study_id"]}_results.json'
    s3_client.put_object(
        Bucket=analysis_config['output_bucket'],
        Key=output_key,
        Body=json.dumps(research_results, indent=2)
    )

    # Return lightweight summary via Globus
    return {
        'study_id': analysis_config['study_id'],
        'samples_analyzed': research_results['total_samples'],
        'significant_findings': research_results['significant_count'],
        's3_detailed_results': f's3://{analysis_config["output_bucket"]}/{output_key}',
        'analysis_complete': True
    }

# Submit to Globus endpoint
gc = Client()
task_id = gc.run(
    s3_enabled_research_function,
    endpoint_id="aws-s3-endpoint-uuid",
    s3_inputs={
        'experiment_a': 's3://research-data/exp_a.json',
        'experiment_b': 's3://research-data/exp_b.json',
        'control_group': 's3://research-data/control.json'
    },
    analysis_config={
        'study_id': 'multi_experiment_2024',
        'output_bucket': 'research-results',
        'statistical_tests': ['t_test', 'anova', 'correlation']
    }
)

result = gc.get_result(task_id)
print(f"Research complete: {result['samples_analyzed']} samples")
print(f"Detailed results: {result['s3_detailed_results']}")
```

## Container Data Patterns

### Container with S3 Data Access

```python
from container_executor import ContainerHighThroughputExecutor

# Container with AWS CLI and research tools
container_executor = ContainerHighThroughputExecutor(
    provider=AWSProvider(enable_ssm_tunneling=True),
    container_image="continuumio/miniconda3:latest",  # Has AWS CLI
    container_runtime="docker"
)

@parsl.python_app
def containerized_s3_pipeline():
    """Full research pipeline in container with S3 data flow."""
    import subprocess
    import json
    import os

    # Verify container execution
    in_container = os.path.exists("/.dockerenv")

    # Install required packages in container
    subprocess.run(['conda', 'install', '-y', 'pandas', 'numpy', 'scipy'],
                   check=True, capture_output=True)

    # Download research data from S3
    subprocess.run([
        'aws', 's3', 'cp',
        's3://research-bucket/large_dataset.csv',
        '/tmp/research_data.csv'
    ], check=True)

    # Process with scientific libraries
    import pandas as pd
    import numpy as np
    from scipy import stats

    df = pd.read_csv('/tmp/research_data.csv')

    # Scientific analysis
    correlation_matrix = df.corr()
    statistical_summary = df.describe()

    # Advanced analysis
    pca_results = perform_pca_analysis(df)
    clustering_results = perform_clustering(df)

    # Prepare results
    analysis_results = {
        'dataset_shape': df.shape,
        'correlation_analysis': correlation_matrix.to_dict(),
        'statistical_summary': statistical_summary.to_dict(),
        'pca_results': pca_results,
        'clustering_results': clustering_results
    }

    # Upload detailed results to S3
    with open('/tmp/analysis_results.json', 'w') as f:
        json.dump(analysis_results, f, indent=2)

    subprocess.run([
        'aws', 's3', 'cp',
        '/tmp/analysis_results.json',
        's3://research-bucket/analysis_results/containerized_analysis.json'
    ], check=True)

    # Return summary (lightweight via tunnel)
    return {
        'in_container': in_container,
        'samples_analyzed': len(df),
        'features_analyzed': len(df.columns),
        's3_results_uri': 's3://research-bucket/analysis_results/containerized_analysis.json',
        'analysis_complete': True
    }
```

## Multi-Institution Data Sharing

### Secure S3 Cross-Account Access

```python
def multi_institution_s3_analysis(institution_config):
    """Analyze data from multiple institutional S3 buckets."""
    import boto3
    import json

    # Access multiple institutional S3 buckets
    # (requires proper cross-account IAM permissions)

    s3_client = boto3.client('s3')
    institutional_datasets = {}

    for institution, s3_config in institution_config.items():
        try:
            # Download from institutional S3 bucket
            bucket = s3_config['bucket']
            key = s3_config['dataset_key']

            local_file = f'/tmp/{institution}_data.json'
            s3_client.download_file(bucket, key, local_file)

            with open(local_file, 'r') as f:
                data = json.load(f)
                institutional_datasets[institution] = data

            print(f"Loaded {len(data)} samples from {institution}")

        except Exception as e:
            print(f"Failed to access {institution} data: {e}")

    # Cross-institutional analysis
    collaborative_results = perform_collaborative_analysis(institutional_datasets)

    # Upload to shared results bucket
    shared_bucket = institution_config['shared_results_bucket']
    results_key = f'collaborative_analysis/{int(time.time())}_multi_institution.json'

    s3_client.put_object(
        Bucket=shared_bucket,
        Key=results_key,
        Body=json.dumps(collaborative_results, indent=2)
    )

    return {
        'institutions_included': list(institutional_datasets.keys()),
        'total_samples': sum(len(data) for data in institutional_datasets.values()),
        'collaborative_findings': collaborative_results['summary'],
        'shared_results_uri': f's3://{shared_bucket}/{results_key}',
        'collaboration_success': True
    }

# Submit collaborative analysis
gc = Client()
collab_task = gc.run(
    multi_institution_s3_analysis,
    endpoint_id="collaborative-endpoint-uuid",
    institution_config={
        'stanford': {
            'bucket': 'stanford-research-data',
            'dataset_key': 'genomics/study_2024.json'
        },
        'mit': {
            'bucket': 'mit-computational-bio',
            'dataset_key': 'proteomics/experiment_set_A.json'
        },
        'shared_results_bucket': 'multi-institution-results'
    }
)
```

## Data Movement Performance Guide

### Bandwidth Optimization

| Data Size | Recommended Path | Bandwidth | Use Case |
|-----------|------------------|-----------|----------|
| < 1MB | SSH Tunnel | ~10 MB/s | Small results, metadata |
| 1MB - 100MB | S3 Transfer | ~100 MB/s | Medium datasets |
| 100MB - 10GB | S3 Transfer | ~500 MB/s | Large datasets |
| > 10GB | S3 + Multipart | ~1+ GB/s | Massive datasets |

### Cost Optimization

```python
# S3 storage classes for research data lifecycle
s3_storage_config = {
    'active_research': 'STANDARD',           # Frequent access
    'analysis_cache': 'STANDARD_IA',         # Infrequent access
    'archive_results': 'GLACIER',            # Long-term archive
    'compliance_data': 'GLACIER_DEEP_ARCHIVE' # Rarely accessed
}

# Lifecycle management
def configure_s3_lifecycle(bucket_name, research_project):
    """Configure S3 lifecycle for cost optimization."""
    s3_client = boto3.client('s3')

    lifecycle_config = {
        'Rules': [{
            'ID': f'{research_project}_lifecycle',
            'Status': 'Enabled',
            'Filter': {'Prefix': f'{research_project}/'},
            'Transitions': [
                {
                    'Days': 30,
                    'StorageClass': 'STANDARD_IA'
                },
                {
                    'Days': 365,
                    'StorageClass': 'GLACIER'
                }
            ]
        }]
    }

    s3_client.put_bucket_lifecycle_configuration(
        Bucket=bucket_name,
        LifecycleConfiguration=lifecycle_config
    )
```

## Implementation Checklist

### ✅ S3 Setup Requirements

1. **Create S3 Bucket**: For research data storage
2. **IAM Permissions**: EC2 instances need S3 access
3. **Bucket Policy**: Configure access permissions
4. **Lifecycle Rules**: Set up automatic archiving

### ✅ AWS Provider Configuration

```python
provider = AWSProvider(
    instance_type="c5.2xlarge",  # Sufficient for data processing
    enable_ssm_tunneling=True,
    # IAM role with S3 permissions
    iam_instance_profile="ParslS3DataAccess"
)
```

### ✅ Function Design Patterns

1. **Input**: S3 URIs or HTTPS URLs (not raw data)
2. **Processing**: Local computation on AWS instance
3. **Output**: Upload to S3, return summary via tunnel
4. **Error Handling**: Robust S3 access error management

### ✅ Monitoring and Debugging

```python
# Add data movement logging to functions
def log_data_transfer(operation, size_bytes, duration_seconds):
    print(f"Data transfer: {operation} - {size_bytes/1024/1024:.1f}MB in {duration_seconds:.1f}s")
    print(f"Throughput: {(size_bytes/1024/1024)/duration_seconds:.1f} MB/s")
```

---

**Key Takeaway**: Use SSH tunnels for lightweight coordination, S3/HTTPS for heavy data movement. This architecture provides optimal performance, cost efficiency, and scalability for scientific computing workloads.

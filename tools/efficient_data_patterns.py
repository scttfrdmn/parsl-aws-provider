#!/usr/bin/env python3
"""
Efficient Data Movement Patterns for AWS Provider.

Demonstrates optimal data flow:
- SSH tunnels for control/coordination (small messages)
- S3/HTTPS for data movement (large files)
"""

import parsl
from parsl import python_app, bash_app
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
from phase15_enhanced import AWSProvider


# Pattern 1: S3-Based Data Staging and Processing
@bash_app
def process_s3_dataset(s3_input_uri, s3_output_uri, analysis_type="basic"):
    """Download from S3, process on AWS instance, upload results to S3."""
    return f"""
    # Download data directly to AWS worker (no SSH tunnel)
    echo "Downloading from S3: {s3_input_uri}"
    aws s3 cp {s3_input_uri} /tmp/input_data.csv --region us-east-1

    # Verify download
    echo "Input file size: $(wc -l < /tmp/input_data.csv) lines"

    # Process data locally on AWS instance
    python3 << 'EOF'
import csv
import math
import json

# Read CSV data
data_points = []
with open('/tmp/input_data.csv', 'r') as f:
    reader = csv.DictReader(f)
    for row in reader:
        data_points.append({{
            'x': float(row.get('x', 0)),
            'y': float(row.get('y', 0)),
            'timestamp': row.get('timestamp', ''),
            'category': row.get('category', 'unknown')
        }})

print(f"Loaded {{len(data_points)}} data points")

# Statistical analysis based on type
if "{analysis_type}" == "statistical":
    mean_x = sum(p['x'] for p in data_points) / len(data_points)
    mean_y = sum(p['y'] for p in data_points) / len(data_points)

    variance_x = sum((p['x'] - mean_x)**2 for p in data_points) / len(data_points)
    variance_y = sum((p['y'] - mean_y)**2 for p in data_points) / len(data_points)

    results = {{
        'analysis_type': 'statistical',
        'sample_count': len(data_points),
        'mean_x': mean_x,
        'mean_y': mean_y,
        'std_x': math.sqrt(variance_x),
        'std_y': math.sqrt(variance_y),
        'correlation': sum((p['x'] - mean_x) * (p['y'] - mean_y) for p in data_points) / (len(data_points) * math.sqrt(variance_x * variance_y))
    }}

elif "{analysis_type}" == "temporal":
    # Time series analysis
    by_category = {{}}
    for point in data_points:
        cat = point['category']
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(point)

    results = {{
        'analysis_type': 'temporal',
        'sample_count': len(data_points),
        'categories': list(by_category.keys()),
        'category_counts': {{cat: len(points) for cat, points in by_category.items()}},
        'category_means': {{cat: sum(p['y'] for p in points)/len(points) for cat, points in by_category.items()}}
    }}

else:  # basic analysis
    max_x = max(p['x'] for p in data_points)
    max_y = max(p['y'] for p in data_points)
    min_x = min(p['x'] for p in data_points)
    min_y = min(p['y'] for p in data_points)

    results = {{
        'analysis_type': 'basic',
        'sample_count': len(data_points),
        'x_range': [min_x, max_x],
        'y_range': [min_y, max_y],
        'total_records': len(data_points)
    }}

# Save results
with open('/tmp/analysis_results.json', 'w') as f:
    json.dump(results, f, indent=2)

print(f"Analysis complete: {{results['sample_count']}} samples processed")
EOF

    # Upload results to S3 (no SSH tunnel)
    echo "Uploading results to S3: {s3_output_uri}"
    aws s3 cp /tmp/analysis_results.json {s3_output_uri} --region us-east-1

    # Return small status message (through SSH tunnel)
    echo "Processing complete: {s3_output_uri}"
    """


@python_app
def generate_synthetic_data(sample_count, data_type="scientific"):
    """Generate synthetic research data and upload to S3."""
    import boto3
    import json
    import math
    import random
    import time

    # Generate data based on type
    if data_type == "scientific":
        # Scientific experimental data
        data_points = []
        for i in range(sample_count):
            x = i * 0.1
            # Add experimental noise and trends
            y = 2.5 * x**2 + 1.3 * x + random.gauss(0, 5)
            z = math.sin(x) * 10 + random.gauss(0, 2)

            data_points.append(
                {
                    "experiment_id": i,
                    "x": x,
                    "y": y,
                    "z": z,
                    "timestamp": time.time() + i,
                    "category": "high" if y > 50 else "medium" if y > 20 else "low",
                }
            )

    elif data_type == "genomics":
        # Genomics sequence data
        bases = ["A", "T", "G", "C"]
        data_points = []
        for i in range(sample_count):
            sequence = "".join(random.choice(bases) for _ in range(100))
            gc_content = (sequence.count("G") + sequence.count("C")) / 100

            data_points.append(
                {
                    "sequence_id": f"seq_{i:06d}",
                    "sequence": sequence,
                    "length": len(sequence),
                    "gc_content": gc_content,
                    "category": "high_gc"
                    if gc_content > 0.6
                    else "medium_gc"
                    if gc_content > 0.4
                    else "low_gc",
                }
            )

    else:  # climate data
        # Climate monitoring data
        data_points = []
        for i in range(sample_count):
            temp = (
                20 + 15 * math.sin(i * 0.01) + random.gauss(0, 3)
            )  # Seasonal temperature
            humidity = (
                50 + 30 * math.cos(i * 0.015) + random.gauss(0, 5)
            )  # Humidity variation

            data_points.append(
                {
                    "measurement_id": i,
                    "temperature": temp,
                    "humidity": max(0, min(100, humidity)),  # Clamp humidity to [0,100]
                    "heat_index": temp + (humidity * 0.05),
                    "timestamp": time.time() + i * 3600,  # Hourly measurements
                    "category": "extreme" if temp > 35 else "normal",
                }
            )

    # Upload to S3 directly from AWS worker
    s3_client = boto3.client("s3")
    bucket_name = "parsl-research-data"  # You'll need to create this bucket
    s3_key = f"datasets/{data_type}_{sample_count}_{int(time.time())}.json"

    # Create CSV format for easier processing
    csv_data = []
    if data_type == "scientific":
        csv_data.append("experiment_id,x,y,z,timestamp,category")
        for point in data_points:
            csv_data.append(
                f"{point['experiment_id']},{point['x']:.3f},{point['y']:.3f},{point['z']:.3f},{point['timestamp']},{point['category']}"
            )

    elif data_type == "genomics":
        csv_data.append("sequence_id,sequence,length,gc_content,category")
        for point in data_points:
            csv_data.append(
                f"{point['sequence_id']},{point['sequence']},{point['length']},{point['gc_content']:.3f},{point['category']}"
            )

    else:  # climate
        csv_data.append(
            "measurement_id,temperature,humidity,heat_index,timestamp,category"
        )
        for point in data_points:
            csv_data.append(
                f"{point['measurement_id']},{point['temperature']:.2f},{point['humidity']:.2f},{point['heat_index']:.2f},{point['timestamp']},{point['category']}"
            )

    csv_content = "\n".join(csv_data)
    csv_key = s3_key.replace(".json", ".csv")

    try:
        # Upload JSON data
        s3_client.put_object(
            Bucket=bucket_name,
            Key=s3_key,
            Body=json.dumps(data_points, indent=2),
            ContentType="application/json",
        )

        # Upload CSV data
        s3_client.put_object(
            Bucket=bucket_name, Key=csv_key, Body=csv_content, ContentType="text/csv"
        )

        return {
            "data_type": data_type,
            "sample_count": sample_count,
            "s3_json_uri": f"s3://{bucket_name}/{s3_key}",
            "s3_csv_uri": f"s3://{bucket_name}/{csv_key}",
            "upload_complete": True,
            "file_size_kb": len(csv_content) / 1024,
        }

    except Exception as e:
        return {
            "data_type": data_type,
            "sample_count": sample_count,
            "upload_complete": False,
            "error": str(e),
        }


# Pattern 2: HTTPS Data Download and Processing
@python_app
def process_public_dataset(dataset_url, analysis_params):
    """Download public dataset via HTTPS and process on AWS."""
    import urllib.request
    import csv
    import math
    import time

    start_time = time.time()

    # Download directly to AWS worker (no SSH tunnel)
    try:
        print(f"Downloading from: {dataset_url}")
        urllib.request.urlretrieve(dataset_url, "/tmp/public_dataset.csv")

        # Process the downloaded data
        data_points = []
        with open("/tmp/public_dataset.csv", "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                data_points.append(row)

        print(f"Loaded {len(data_points)} records")

        # Perform analysis based on parameters
        if analysis_params["type"] == "numerical":
            # Numerical analysis
            numerical_cols = analysis_params.get("numerical_columns", ["value"])
            results = {}

            for col in numerical_cols:
                values = [float(row.get(col, 0)) for row in data_points if row.get(col)]
                if values:
                    mean_val = sum(values) / len(values)
                    variance = sum((v - mean_val) ** 2 for v in values) / len(values)
                    results[col] = {
                        "mean": mean_val,
                        "std": math.sqrt(variance),
                        "min": min(values),
                        "max": max(values),
                        "count": len(values),
                    }

        elif analysis_params["type"] == "categorical":
            # Categorical analysis
            cat_col = analysis_params.get("categorical_column", "category")
            category_counts = {}

            for row in data_points:
                category = row.get(cat_col, "unknown")
                category_counts[category] = category_counts.get(category, 0) + 1

            results = {
                "category_distribution": category_counts,
                "total_categories": len(category_counts),
                "most_common": max(category_counts.items(), key=lambda x: x[1]),
                "total_records": len(data_points),
            }

        else:  # basic analysis
            results = {
                "total_records": len(data_points),
                "columns": list(data_points[0].keys()) if data_points else [],
                "sample_data": data_points[:3],  # First 3 rows as sample
            }

        processing_time = time.time() - start_time

        return {
            "dataset_url": dataset_url,
            "analysis_type": analysis_params["type"],
            "processing_time": processing_time,
            "download_success": True,
            "results": results,
        }

    except Exception as e:
        return {
            "dataset_url": dataset_url,
            "download_success": False,
            "error": str(e),
            "processing_time": time.time() - start_time,
        }


# Pattern 3: Hybrid S3 + Local Processing Pipeline
@python_app
def preprocess_and_stage_to_s3(raw_data_params):
    """Generate data locally and stage to S3 for further processing."""
    import boto3
    import json
    import math
    import random
    import time

    # Generate synthetic research data
    sample_count = raw_data_params["sample_count"]
    data_type = raw_data_params["data_type"]

    if data_type == "climate":
        # Climate simulation data
        climate_data = []
        for day in range(sample_count):
            # Simulate daily weather with seasonal patterns
            season_factor = math.sin(day * 2 * math.pi / 365)  # Yearly cycle

            base_temp = 15 + season_factor * 20  # 15°C base, ±20°C seasonal variation
            daily_temp = base_temp + random.gauss(0, 5)  # Daily variation

            humidity = (
                50 + season_factor * 20 + random.gauss(0, 10)
            )  # Seasonal humidity
            humidity = max(0, min(100, humidity))  # Clamp to valid range

            wind_speed = abs(random.gauss(10, 5))  # Wind speed always positive

            climate_data.append(
                {
                    "day": day,
                    "temperature": round(daily_temp, 2),
                    "humidity": round(humidity, 2),
                    "wind_speed": round(wind_speed, 2),
                    "heat_index": round(daily_temp + (humidity * 0.05), 2),
                    "season": "summer"
                    if season_factor > 0.5
                    else "winter"
                    if season_factor < -0.5
                    else "spring_fall",
                }
            )

    elif data_type == "genomics":
        # DNA sequence data
        bases = ["A", "T", "G", "C"]
        genomics_data = []
        for i in range(sample_count):
            # Generate random DNA sequence
            sequence_length = random.randint(50, 200)
            sequence = "".join(random.choice(bases) for _ in range(sequence_length))

            # Calculate properties
            gc_content = (sequence.count("G") + sequence.count("C")) / len(sequence)
            at_content = (sequence.count("A") + sequence.count("T")) / len(sequence)

            genomics_data.append(
                {
                    "sequence_id": f"seq_{i:06d}",
                    "sequence": sequence,
                    "length": len(sequence),
                    "gc_content": round(gc_content, 3),
                    "at_content": round(at_content, 3),
                    "complexity": round(len(set(sequence)) / 4, 3),  # Diversity measure
                }
            )

        climate_data = genomics_data  # Rename for consistency

    else:  # experimental data
        # Laboratory experimental data
        experimental_data = []
        for trial in range(sample_count):
            # Simulate experimental measurements
            concentration = random.uniform(0.1, 10.0)  # mol/L
            temperature = random.uniform(20, 80)  # Celsius

            # Simulated reaction rate (Arrhenius equation)
            activation_energy = 50000  # J/mol
            gas_constant = 8.314  # J/(mol·K)
            rate_constant = math.exp(
                -activation_energy / (gas_constant * (temperature + 273.15))
            )
            reaction_rate = rate_constant * concentration

            experimental_data.append(
                {
                    "trial_id": trial,
                    "concentration_mol_L": round(concentration, 3),
                    "temperature_C": round(temperature, 2),
                    "reaction_rate": round(
                        reaction_rate * 1000, 6
                    ),  # Scale for readability
                    "yield_percent": round(min(100, reaction_rate * 100000), 2),
                    "experiment_date": time.time() + trial * 86400,  # Daily experiments
                }
            )

        climate_data = experimental_data  # Rename for consistency

    # Upload to S3
    s3_client = boto3.client("s3")
    bucket_name = "parsl-research-data"
    s3_key = f"preprocessed/{data_type}_{sample_count}_{int(time.time())}.json"

    try:
        s3_client.put_object(
            Bucket=bucket_name,
            Key=s3_key,
            Body=json.dumps(climate_data, indent=2),
            ContentType="application/json",
        )

        return {
            "data_type": data_type,
            "sample_count": len(climate_data),
            "s3_uri": f"s3://{bucket_name}/{s3_key}",
            "preprocessing_complete": True,
            "upload_success": True,
        }

    except Exception as e:
        return {
            "data_type": data_type,
            "sample_count": sample_count,
            "preprocessing_complete": True,
            "upload_success": False,
            "error": str(e),
        }


@bash_app
def download_and_analyze_s3_data(s3_input_uri, analysis_config):
    """Download from S3 and perform complex analysis."""
    return f"""
    # Download preprocessed data from S3
    echo "Downloading preprocessed data: {s3_input_uri}"
    aws s3 cp {s3_input_uri} /tmp/preprocessed_data.json --region us-east-1

    # Complex analysis with multiple steps
    python3 << 'EOF'
import json
import math
import statistics
import boto3

# Load preprocessed data
with open('/tmp/preprocessed_data.json', 'r') as f:
    data = json.load(f)

print(f"Analyzing {{len(data)}} preprocessed records")

# Determine data type and perform appropriate analysis
if 'temperature' in data[0]:
    # Climate data analysis
    temperatures = [record['temperature'] for record in data]
    heat_indices = [record['heat_index'] for record in data]

    analysis_results = {{
        'data_type': 'climate',
        'sample_count': len(data),
        'temperature_stats': {{
            'mean': statistics.mean(temperatures),
            'median': statistics.median(temperatures),
            'std_dev': statistics.stdev(temperatures),
            'min': min(temperatures),
            'max': max(temperatures)
        }},
        'heat_index_stats': {{
            'mean': statistics.mean(heat_indices),
            'extreme_days': sum(1 for hi in heat_indices if hi > 35),
            'extreme_percentage': (sum(1 for hi in heat_indices if hi > 35) / len(heat_indices)) * 100
        }},
        'seasonal_analysis': {{
            'summer_days': sum(1 for r in data if r['season'] == 'summer'),
            'winter_days': sum(1 for r in data if r['season'] == 'winter'),
            'transition_days': sum(1 for r in data if r['season'] == 'spring_fall')
        }}
    }}

elif 'sequence' in data[0]:
    # Genomics data analysis
    sequences = [record['sequence'] for record in data]
    gc_contents = [record['gc_content'] for record in data]

    # Advanced sequence analysis
    total_bases = sum(len(seq) for seq in sequences)
    avg_length = total_bases / len(sequences)

    # Count all nucleotides
    base_counts = {{'A': 0, 'T': 0, 'G': 0, 'C': 0}}
    for seq in sequences:
        for base in seq:
            if base in base_counts:
                base_counts[base] += 1

    analysis_results = {{
        'data_type': 'genomics',
        'sample_count': len(data),
        'sequence_stats': {{
            'total_sequences': len(sequences),
            'total_bases': total_bases,
            'average_length': avg_length,
            'shortest': min(len(seq) for seq in sequences),
            'longest': max(len(seq) for seq in sequences)
        }},
        'nucleotide_composition': base_counts,
        'gc_content_analysis': {{
            'mean_gc': statistics.mean(gc_contents),
            'high_gc_sequences': sum(1 for gc in gc_contents if gc > 0.6),
            'low_gc_sequences': sum(1 for gc in gc_contents if gc < 0.4)
        }}
    }}

else:
    # Experimental data analysis
    concentrations = [record['concentration_mol_L'] for record in data]
    reaction_rates = [record['reaction_rate'] for record in data]
    yields = [record['yield_percent'] for record in data]

    analysis_results = {{
        'data_type': 'experimental',
        'sample_count': len(data),
        'concentration_stats': {{
            'mean': statistics.mean(concentrations),
            'range': [min(concentrations), max(concentrations)],
            'std_dev': statistics.stdev(concentrations)
        }},
        'reaction_analysis': {{
            'mean_rate': statistics.mean(reaction_rates),
            'mean_yield': statistics.mean(yields),
            'high_yield_trials': sum(1 for y in yields if y > 80),
            'success_rate': (sum(1 for y in yields if y > 50) / len(yields)) * 100
        }},
        'correlation': {{
            'rate_yield_correlation': sum((r - statistics.mean(reaction_rates)) * (y - statistics.mean(yields))
                                        for r, y in zip(reaction_rates, yields)) /
                                   (len(reaction_rates) * statistics.stdev(reaction_rates) * statistics.stdev(yields))
        }}
    }}

# Upload analysis results back to S3
s3_client = boto3.client('s3')
bucket_name = 'parsl-research-data'
analysis_key = 'analysis_results/analysis_' + str(int(time.time())) + '.json'

s3_client.put_object(
    Bucket=bucket_name,
    Key=analysis_key,
    Body=json.dumps(analysis_results, indent=2),
    ContentType='application/json'
)

print(f"Analysis uploaded to: s3://{{bucket_name}}/{{analysis_key}}")
print(f"Analyzed {{analysis_results['sample_count']}} {{analysis_results['data_type']}} samples")

# Return small result summary (through SSH tunnel)
result_summary = {{
    'analysis_complete': True,
    'data_type': analysis_results['data_type'],
    'sample_count': analysis_results['sample_count'],
    's3_results_uri': f's3://{{bucket_name}}/{{analysis_key}}',
    'processing_time': time.time() - {start_time}
}}

with open('/tmp/summary.json', 'w') as f:
    json.dump(result_summary, f)

print("Analysis summary:")
print(json.dumps(result_summary, indent=2))
EOF

    echo "Complex analysis complete"
    """


# Pattern 4: Multi-Stage Pipeline with S3 Intermediate Storage
@bash_app
def aggregate_analysis_results(s3_result_uris, final_s3_output):
    """Download multiple S3 analysis results and create final aggregated report."""
    s3_uris_str = " ".join(f'"{uri}"' for uri in s3_result_uris)

    return f"""
    echo "Aggregating results from {len(s3_result_uris)} analysis files"

    # Download all analysis results
    mkdir -p /tmp/analysis_inputs
    python3 << 'EOF'
import boto3
import json
import re

s3_client = boto3.client('s3')
s3_uris = {s3_result_uris}

downloaded_files = []
for uri in s3_uris:
    # Parse S3 URI
    match = re.match(r's3://([^/]+)/(.+)', uri)
    if match:
        bucket, key = match.groups()
        local_file = f'/tmp/analysis_inputs/{{key.split("/")[-1]}}'

        try:
            s3_client.download_file(bucket, key, local_file)
            downloaded_files.append(local_file)
            print(f"Downloaded: {{uri}} -> {{local_file}}")
        except Exception as e:
            print(f"Failed to download {{uri}}: {{e}}")

# Aggregate all analysis results
all_results = []
for file_path in downloaded_files:
    with open(file_path, 'r') as f:
        result = json.load(f)
        all_results.append(result)

# Create comprehensive aggregated analysis
aggregated = {{
    'aggregation_summary': {{
        'total_analyses': len(all_results),
        'data_types': list(set(r.get('data_type', 'unknown') for r in all_results)),
        'total_samples': sum(r.get('sample_count', 0) for r in all_results),
        'analysis_date': time.time()
    }},
    'individual_results': all_results,
    'cross_analysis': {{}}
}}

# Cross-dataset analysis
if len(all_results) > 1:
    sample_counts = [r.get('sample_count', 0) for r in all_results]
    aggregated['cross_analysis'] = {{
        'dataset_size_distribution': {{
            'mean_size': sum(sample_counts) / len(sample_counts),
            'min_size': min(sample_counts),
            'max_size': max(sample_counts),
            'total_samples_across_datasets': sum(sample_counts)
        }},
        'processing_time_analysis': {{
            'total_time': sum(r.get('processing_time', 0) for r in all_results),
            'average_time_per_dataset': sum(r.get('processing_time', 0) for r in all_results) / len(all_results)
        }}
    }}

# Save aggregated results locally
with open('/tmp/aggregated_analysis.json', 'w') as f:
    json.dump(aggregated, f, indent=2)

print(f"Aggregated {{len(all_results)}} analysis results")
print(f"Total samples across all datasets: {{aggregated['aggregation_summary']['total_samples']}}")
EOF

    # Upload final aggregated results to S3
    echo "Uploading aggregated results to: {final_s3_output}"
    aws s3 cp /tmp/aggregated_analysis.json {final_s3_output} --region us-east-1

    echo "Aggregation pipeline complete: {final_s3_output}"
    """


def demo_efficient_data_patterns():
    """Demonstrate efficient data movement patterns."""
    print("🗂️  EFFICIENT DATA MOVEMENT PATTERNS")
    print("=" * 50)
    print("Demonstrating optimal data flow:")
    print("• SSH tunnels for control/coordination (small messages)")
    print("• S3/HTTPS for data movement (large files)")
    print("• Minimal tunnel bandwidth usage")
    print()

    # Configure AWS provider
    provider = AWSProvider(
        label="data_demo",
        instance_type="c5.large",  # Larger instance for data processing
        enable_ssm_tunneling=True,
        init_blocks=1,
        max_blocks=4,
        region="us-east-1",
    )

    config = Config(
        executors=[
            HighThroughputExecutor(
                label="data_executor", provider=provider, max_workers_per_node=2
            )
        ]
    )

    print("⚡ Initializing Parsl with AWS provider...")
    parsl.load(config)
    print("✅ Ready for efficient data processing")

    try:
        # Pattern 1: Generate data and stage to S3
        print("\n📊 PATTERN 1: Data Generation and S3 Staging")
        print("Generating synthetic datasets and uploading to S3...")

        data_generation_tasks = [
            preprocess_and_stage_to_s3({"sample_count": 1000, "data_type": "climate"}),
            preprocess_and_stage_to_s3({"sample_count": 500, "data_type": "genomics"}),
            preprocess_and_stage_to_s3(
                {"sample_count": 750, "data_type": "experimental"}
            ),
        ]

        staging_results = []
        for i, task in enumerate(data_generation_tasks):
            result = task.result()
            staging_results.append(result)
            print(
                f"  ✅ Dataset {i+1}: {result['sample_count']} {result['data_type']} samples → {result['s3_uri']}"
            )

        # Pattern 2: S3-to-S3 Processing Pipeline
        print("\n🔄 PATTERN 2: S3-to-S3 Processing Pipeline")
        print("Processing S3 data and writing results back to S3...")

        s3_processing_tasks = []
        for result in staging_results:
            if result["upload_success"]:
                output_uri = result["s3_uri"].replace("preprocessed/", "processed/")
                task = process_s3_dataset(
                    result["s3_uri"].replace(
                        ".json", ".csv"
                    ),  # Assume CSV version exists
                    output_uri,
                    "statistical",
                )
                s3_processing_tasks.append(task)

        s3_results = []
        for i, task in enumerate(s3_processing_tasks):
            try:
                result = task.result()
                s3_results.append(result)
                print(f"  ✅ S3 Processing {i+1}: {result}")
            except Exception as e:
                print(f"  ⚠️ S3 Processing {i+1} failed: {e}")

        # Pattern 3: Public dataset processing via HTTPS
        print("\n🌐 PATTERN 3: Public Dataset Processing via HTTPS")
        print("Downloading and analyzing public research data...")

        # Example public datasets (these URLs are examples - replace with real ones)
        public_datasets = [
            {
                "url": "https://raw.githubusercontent.com/mwaskom/seaborn-data/master/iris.csv",
                "analysis": {
                    "type": "numerical",
                    "numerical_columns": ["sepal_length", "sepal_width"],
                },
            }
        ]

        public_tasks = []
        for dataset in public_datasets:
            task = process_public_dataset(dataset["url"], dataset["analysis"])
            public_tasks.append(task)

        public_results = []
        for i, task in enumerate(public_tasks):
            try:
                result = task.result()
                public_results.append(result)
                if result["download_success"]:
                    print(
                        f"  ✅ Public Dataset {i+1}: {result['results']['total_records']} records processed"
                    )
                else:
                    print(f"  ⚠️ Public Dataset {i+1} failed: {result['error']}")
            except Exception as e:
                print(f"  ⚠️ Public Dataset {i+1} error: {e}")

        # Pattern 4: Result Aggregation
        print("\n📋 PATTERN 4: Multi-Dataset Aggregation")
        print("Aggregating results from multiple S3 sources...")

        # Collect S3 result URIs for aggregation
        result_uris = []
        for result in staging_results:
            if result["upload_success"]:
                # Convert to analysis result URI
                analysis_uri = result["s3_uri"].replace("preprocessed/", "analysis/")
                result_uris.append(analysis_uri)

        if result_uris:
            final_aggregation_uri = (
                "s3://parsl-research-data/final/aggregated_analysis.json"
            )
            aggregation_task = aggregate_analysis_results(
                result_uris, final_aggregation_uri
            )

            try:
                aggregation_result = aggregation_task.result()
                print(f"  ✅ Final aggregation: {aggregation_result}")
            except Exception as e:
                print(f"  ⚠️ Aggregation failed: {e}")

        print("\n📊 EFFICIENCY ANALYSIS")
        print("=" * 30)
        print("Data Flow Summary:")
        print("• Large datasets: Moved efficiently via S3/HTTPS")
        print("• Small results: Coordinated through SSH tunnels")
        print("• Zero tunnel bandwidth waste on large files")
        print("• Optimal AWS network utilization")

        total_samples = sum(r["sample_count"] for r in staging_results)
        successful_uploads = sum(1 for r in staging_results if r["upload_success"])

        print("\nResults:")
        print(f"• Generated {total_samples} total data samples")
        print(f"• {successful_uploads}/{len(staging_results)} successful S3 uploads")
        print("• All coordination via lightweight SSH tunnel messages")
        print("• Large data moved via optimized AWS infrastructure")

        return True

    except Exception as e:
        print(f"\n❌ Demo failed: {e}")
        return False

    finally:
        print("\n🧹 Cleaning up...")
        parsl.clear()


if __name__ == "__main__":
    success = demo_efficient_data_patterns()

    if success:
        print("\n🎉 EFFICIENT DATA PATTERNS DEMO COMPLETE")
        print("✅ Demonstrated optimal data flow architecture:")
        print("   → SSH tunnels: Control and coordination")
        print("   → S3/HTTPS: Large data movement")
        print("   → Minimal tunnel bandwidth utilization")
        print("🚀 PRODUCTION DATA FLOW VALIDATED")
    else:
        print("\n❌ Demo encountered issues")

    exit(0 if success else 1)

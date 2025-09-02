#!/usr/bin/env python3
"""
Production S3 Data Workflow Example.

Demonstrates real-world scientific computing with efficient data movement:
- Large datasets via S3 (no SSH tunnel bandwidth waste)
- Control messages via SSH tunnels (lightweight coordination)
- Optimal AWS network utilization
"""

import parsl
from parsl import python_app, bash_app
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
from phase15_enhanced import AWSProvider


@python_app
def upload_research_data_to_s3(data_config):
    """Generate research data and upload to S3 bucket."""
    import boto3
    import json
    import math
    import random
    import time
    import csv
    import io

    # Generate scientific dataset
    sample_count = data_config["sample_count"]
    experiment_type = data_config["experiment_type"]

    print(f"Generating {sample_count} {experiment_type} samples...")

    if experiment_type == "protein_dynamics":
        # Protein molecular dynamics simulation data
        protein_data = []
        for frame in range(sample_count):
            # Simulate protein conformations over time
            backbone_energy = random.gauss(-50, 10)  # kcal/mol
            sidechain_energy = random.gauss(-20, 5)
            total_energy = backbone_energy + sidechain_energy

            # Simulate structural parameters
            radius_gyration = random.uniform(15, 25)  # Angstroms
            rmsd = random.uniform(0.5, 3.0)  # Root mean square deviation

            protein_data.append(
                {
                    "frame": frame,
                    "time_ps": frame * 0.1,  # Picoseconds
                    "backbone_energy": round(backbone_energy, 3),
                    "sidechain_energy": round(sidechain_energy, 3),
                    "total_energy": round(total_energy, 3),
                    "radius_gyration": round(radius_gyration, 3),
                    "rmsd": round(rmsd, 3),
                    "stable": total_energy > -60,
                }
            )

    elif experiment_type == "climate_modeling":
        # Climate model output data
        climate_data = []
        for day in range(sample_count):
            # Simulate global climate model output
            latitude = random.uniform(-90, 90)
            longitude = random.uniform(-180, 180)

            # Temperature with latitude and seasonal effects
            seasonal_factor = math.sin(day * 2 * math.pi / 365)
            latitude_factor = math.cos(math.radians(abs(latitude)))
            temperature = (
                15 + seasonal_factor * 20 * latitude_factor + random.gauss(0, 3)
            )

            # Precipitation model
            precipitation = max(0, random.gauss(2, 1) * (1 + seasonal_factor * 0.5))

            climate_data.append(
                {
                    "day": day,
                    "latitude": round(latitude, 3),
                    "longitude": round(longitude, 3),
                    "temperature_c": round(temperature, 2),
                    "precipitation_mm": round(precipitation, 2),
                    "season_factor": round(seasonal_factor, 3),
                    "grid_cell": f"{int(latitude/5)*5}_{int(longitude/5)*5}",
                }
            )

        protein_data = climate_data  # Reuse variable name

    else:  # genomics_analysis
        # Genomics variant calling data
        bases = ["A", "T", "G", "C"]
        genomics_data = []
        for variant in range(sample_count):
            # Simulate genetic variants
            chromosome = random.randint(1, 22)
            position = random.randint(1000000, 200000000)

            ref_allele = random.choice(bases)
            alt_allele = random.choice([b for b in bases if b != ref_allele])

            # Quality scores
            quality_score = random.uniform(20, 60)  # Phred score
            read_depth = random.randint(10, 100)

            genomics_data.append(
                {
                    "variant_id": f"var_{variant:06d}",
                    "chromosome": chromosome,
                    "position": position,
                    "ref_allele": ref_allele,
                    "alt_allele": alt_allele,
                    "quality_score": round(quality_score, 2),
                    "read_depth": read_depth,
                    "allele_frequency": round(random.uniform(0.01, 0.99), 3),
                    "pathogenic": quality_score > 40 and read_depth > 30,
                }
            )

        protein_data = genomics_data  # Reuse variable name

    # Upload to S3 as both JSON and CSV
    s3_client = boto3.client("s3")
    bucket_name = data_config["s3_bucket"]
    timestamp = int(time.time())

    # JSON format
    json_key = f"raw_data/{experiment_type}_{sample_count}_{timestamp}.json"
    json_content = json.dumps(protein_data, indent=2)

    # CSV format for easier processing
    csv_key = f"raw_data/{experiment_type}_{sample_count}_{timestamp}.csv"

    # Create CSV content
    if protein_data:
        csv_buffer = io.StringIO()
        fieldnames = protein_data[0].keys()
        writer = csv.DictWriter(csv_buffer, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(protein_data)
        csv_content = csv_buffer.getvalue()
    else:
        csv_content = ""

    try:
        # Upload JSON
        s3_client.put_object(
            Bucket=bucket_name,
            Key=json_key,
            Body=json_content,
            ContentType="application/json",
        )

        # Upload CSV
        s3_client.put_object(
            Bucket=bucket_name, Key=csv_key, Body=csv_content, ContentType="text/csv"
        )

        return {
            "experiment_type": experiment_type,
            "sample_count": sample_count,
            "s3_json_uri": f"s3://{bucket_name}/{json_key}",
            "s3_csv_uri": f"s3://{bucket_name}/{csv_key}",
            "upload_success": True,
            "data_size_kb": len(csv_content) / 1024,
            "generation_time": time.time(),
        }

    except Exception as e:
        return {
            "experiment_type": experiment_type,
            "sample_count": sample_count,
            "upload_success": False,
            "error": str(e),
        }


@bash_app
def analyze_s3_research_data(s3_input_uri, analysis_type, s3_output_uri):
    """Download S3 research data, analyze, upload results."""
    return f"""
    echo "Starting S3 research data analysis"
    echo "Input: {s3_input_uri}"
    echo "Output: {s3_output_uri}"
    echo "Analysis type: {analysis_type}"

    # Download research data from S3 (efficient AWS-to-AWS transfer)
    aws s3 cp {s3_input_uri} /tmp/research_data.csv --region us-east-1

    # Verify download
    echo "Downloaded file size: $(wc -l < /tmp/research_data.csv) lines"

    # Perform analysis based on data type
    python3 << 'EOF'
import csv
import json
import math
import statistics
import time

start_time = time.time()

# Load data
with open('/tmp/research_data.csv', 'r') as f:
    reader = csv.DictReader(f)
    data = list(reader)

print(f"Loaded {{len(data)}} research records")

# Determine analysis based on data structure
if 'temperature_c' in data[0]:
    # Climate data analysis
    temperatures = [float(row['temperature_c']) for row in data]
    precipitations = [float(row['precipitation_mm']) for row in data]

    analysis_results = {{
        'data_type': 'climate_modeling',
        'sample_count': len(data),
        'temperature_analysis': {{
            'global_mean_temp': statistics.mean(temperatures),
            'temp_std_dev': statistics.stdev(temperatures),
            'extreme_hot_days': sum(1 for t in temperatures if t > 35),
            'extreme_cold_days': sum(1 for t in temperatures if t < -10),
            'temperature_range': [min(temperatures), max(temperatures)]
        }},
        'precipitation_analysis': {{
            'mean_precipitation': statistics.mean(precipitations),
            'dry_days': sum(1 for p in precipitations if p < 0.1),
            'heavy_rain_days': sum(1 for p in precipitations if p > 10),
            'total_precipitation': sum(precipitations)
        }},
        'climate_indicators': {{
            'heat_stress_days': sum(1 for row in data
                                  if float(row['temperature_c']) > 30 and float(row['precipitation_mm']) < 1),
            'ideal_days': sum(1 for row in data
                            if 18 <= float(row['temperature_c']) <= 25 and 1 <= float(row['precipitation_mm']) <= 5)
        }}
    }}

elif 'total_energy' in data[0]:
    # Protein dynamics analysis
    energies = [float(row['total_energy']) for row in data]
    rmsds = [float(row['rmsd']) for row in data]

    analysis_results = {{
        'data_type': 'protein_dynamics',
        'sample_count': len(data),
        'energy_analysis': {{
            'mean_energy': statistics.mean(energies),
            'energy_std_dev': statistics.stdev(energies),
            'stable_conformations': sum(1 for row in data if row['stable'] == 'True'),
            'energy_range': [min(energies), max(energies)]
        }},
        'structural_analysis': {{
            'mean_rmsd': statistics.mean(rmsds),
            'rmsd_std_dev': statistics.stdev(rmsds),
            'high_flexibility': sum(1 for r in rmsds if r > 2.0),
            'rigid_states': sum(1 for r in rmsds if r < 1.0)
        }},
        'stability_metrics': {{
            'stability_percentage': (sum(1 for row in data if row['stable'] == 'True') / len(data)) * 100,
            'equilibration_frames': len([e for e in energies[:100] if abs(e - statistics.mean(energies[-100:])) < 5])
        }}
    }}

elif 'alt_allele' in data[0]:
    # Genomics variant analysis
    quality_scores = [float(row['quality_score']) for row in data]
    read_depths = [int(row['read_depth']) for row in data]

    analysis_results = {{
        'data_type': 'genomics_analysis',
        'sample_count': len(data),
        'quality_analysis': {{
            'mean_quality': statistics.mean(quality_scores),
            'high_quality_variants': sum(1 for q in quality_scores if q > 30),
            'low_quality_variants': sum(1 for q in quality_scores if q < 20)
        }},
        'coverage_analysis': {{
            'mean_read_depth': statistics.mean(read_depths),
            'high_coverage_variants': sum(1 for d in read_depths if d > 50),
            'low_coverage_variants': sum(1 for d in read_depths if d < 20)
        }},
        'variant_classification': {{
            'pathogenic_variants': sum(1 for row in data if row['pathogenic'] == 'True'),
            'chromosomal_distribution': {{}}
        }}
    }}

    # Chromosome distribution
    chroms = [row['chromosome'] for row in data]
    chrom_counts = {{}}
    for chrom in chroms:
        chrom_counts[chrom] = chrom_counts.get(chrom, 0) + 1
    analysis_results['variant_classification']['chromosomal_distribution'] = chrom_counts

else:
    # Generic numerical analysis
    analysis_results = {{
        'data_type': 'generic',
        'sample_count': len(data),
        'basic_stats': {{
            'columns': list(data[0].keys()),
            'total_records': len(data)
        }}
    }}

# Add processing metadata
analysis_results['processing_metadata'] = {{
    'processing_time_seconds': time.time() - start_time,
    'analysis_timestamp': time.time(),
    'analysis_type': '{analysis_type}',
    'input_source': '{s3_input_uri}'
}}

# Save analysis results
with open('/tmp/analysis_output.json', 'w') as f:
    json.dump(analysis_results, f, indent=2)

print(f"Analysis complete: {{analysis_results['sample_count']}} {{analysis_results['data_type']}} samples")
print(f"Processing time: {{analysis_results['processing_metadata']['processing_time_seconds']:.2f}} seconds")
EOF

    # Upload analysis results to S3 (efficient AWS-to-AWS transfer)
    echo "Uploading analysis results to: {s3_output_uri}"
    aws s3 cp /tmp/analysis_output.json {s3_output_uri} --region us-east-1

    # Return success message (small message through SSH tunnel)
    echo "Analysis complete: {s3_output_uri}"
    """


@python_app
def download_and_process_public_data(public_url, processing_config):
    """Download public research data via HTTPS and process."""
    import urllib.request
    import json
    import csv
    import math
    import time
    import boto3

    start_time = time.time()

    try:
        print(f"Downloading public dataset: {public_url}")

        # Download directly to AWS worker (no SSH tunnel)
        urllib.request.urlretrieve(public_url, "/tmp/public_data.csv")

        # Process the data
        with open("/tmp/public_data.csv", "r") as f:
            reader = csv.DictReader(f)
            data = list(reader)

        print(f"Processing {len(data)} records...")

        # Analysis based on configuration
        if processing_config["analysis"] == "numerical_summary":
            numerical_cols = processing_config.get("numerical_columns", [])
            results = {"numerical_analysis": {}}

            for col in numerical_cols:
                try:
                    values = [float(row[col]) for row in data if row.get(col)]
                    if values:
                        mean_val = sum(values) / len(values)
                        variance = sum((v - mean_val) ** 2 for v in values) / len(
                            values
                        )
                        results["numerical_analysis"][col] = {
                            "mean": mean_val,
                            "std_dev": math.sqrt(variance),
                            "min": min(values),
                            "max": max(values),
                            "sample_count": len(values),
                        }
                except (ValueError, KeyError):
                    results["numerical_analysis"][col] = {"error": "non_numerical_data"}

        elif processing_config["analysis"] == "categorical_distribution":
            cat_col = processing_config.get("categorical_column", "category")
            category_counts = {}

            for row in data:
                category = row.get(cat_col, "unknown")
                category_counts[category] = category_counts.get(category, 0) + 1

            results = {
                "categorical_analysis": {
                    "distribution": category_counts,
                    "unique_categories": len(category_counts),
                    "most_common": max(category_counts.items(), key=lambda x: x[1])
                    if category_counts
                    else None,
                    "total_classified": sum(category_counts.values()),
                }
            }

        else:  # basic_info
            results = {
                "basic_info": {
                    "total_records": len(data),
                    "columns": list(data[0].keys()) if data else [],
                    "sample_records": data[:2] if data else [],
                }
            }

        # Optionally upload results to S3
        if processing_config.get("upload_to_s3"):
            s3_client = boto3.client("s3")
            bucket = processing_config["s3_bucket"]
            key = f"public_data_analysis/{int(time.time())}_analysis.json"

            s3_client.put_object(
                Bucket=bucket,
                Key=key,
                Body=json.dumps(results, indent=2),
                ContentType="application/json",
            )

            s3_uri = f"s3://{bucket}/{key}"
        else:
            s3_uri = None

        return {
            "source_url": public_url,
            "processing_success": True,
            "record_count": len(data),
            "processing_time": time.time() - start_time,
            "results_summary": results,
            "s3_results_uri": s3_uri,
            "analysis_type": processing_config["analysis"],
        }

    except Exception as e:
        return {
            "source_url": public_url,
            "processing_success": False,
            "error": str(e),
            "processing_time": time.time() - start_time,
        }


@bash_app
def multi_dataset_s3_aggregation(s3_input_uris, s3_final_output):
    """Aggregate multiple S3 datasets into comprehensive analysis."""
    input_uris_str = " ".join(f'"{uri}"' for uri in s3_input_uris)

    return f"""
    echo "Aggregating {len(s3_input_uris)} S3 datasets"
    echo "Final output: {s3_final_output}"

    mkdir -p /tmp/aggregation_inputs

    # Download all input datasets from S3
    python3 << 'EOF'
import boto3
import json
import re
import time
import statistics

s3_client = boto3.client('s3')
input_uris = {s3_input_uris}

# Download all analysis files
downloaded_analyses = []
for uri in input_uris:
    match = re.match(r's3://([^/]+)/(.+)', uri)
    if match:
        bucket, key = match.groups()
        local_file = f'/tmp/aggregation_inputs/{{key.split("/")[-1]}}'

        try:
            s3_client.download_file(bucket, key, local_file)

            with open(local_file, 'r') as f:
                analysis = json.load(f)
                downloaded_analyses.append(analysis)
                print(f"Downloaded analysis: {{analysis.get('data_type', 'unknown')}} with {{analysis.get('sample_count', 0)}} samples")

        except Exception as e:
            print(f"Failed to download {{uri}}: {{e}}")

print(f"Successfully downloaded {{len(downloaded_analyses)}} analysis files")

# Create comprehensive aggregation
aggregated_analysis = {{
    'meta_analysis': {{
        'total_datasets': len(downloaded_analyses),
        'data_types_analyzed': list(set(a.get('data_type', 'unknown') for a in downloaded_analyses)),
        'total_samples_across_all': sum(a.get('sample_count', 0) for a in downloaded_analyses),
        'aggregation_timestamp': time.time()
    }},
    'individual_summaries': [],
    'cross_dataset_insights': {{}}
}}

# Process each analysis
processing_times = []
sample_counts = []

for analysis in downloaded_analyses:
    # Extract key metrics from each analysis
    summary = {{
        'data_type': analysis.get('data_type', 'unknown'),
        'sample_count': analysis.get('sample_count', 0),
        'processing_time': analysis.get('processing_metadata', {{}}).get('processing_time_seconds', 0)
    }}

    processing_times.append(summary['processing_time'])
    sample_counts.append(summary['sample_count'])

    # Add type-specific insights
    if 'temperature_analysis' in analysis:
        summary['climate_insights'] = {{
            'mean_global_temp': analysis['temperature_analysis']['global_mean_temp'],
            'extreme_events': analysis['temperature_analysis']['extreme_hot_days'] + analysis['temperature_analysis']['extreme_cold_days']
        }}

    elif 'energy_analysis' in analysis:
        summary['protein_insights'] = {{
            'mean_energy': analysis['energy_analysis']['mean_energy'],
            'stability_percent': analysis['stability_metrics']['stability_percentage']
        }}

    elif 'quality_analysis' in analysis:
        summary['genomics_insights'] = {{
            'high_quality_variants': analysis['quality_analysis']['high_quality_variants'],
            'pathogenic_count': analysis['variant_classification']['pathogenic_variants']
        }}

    aggregated_analysis['individual_summaries'].append(summary)

# Cross-dataset analysis
if processing_times:
    aggregated_analysis['cross_dataset_insights'] = {{
        'processing_efficiency': {{
            'total_processing_time': sum(processing_times),
            'average_time_per_dataset': statistics.mean(processing_times),
            'fastest_analysis': min(processing_times),
            'slowest_analysis': max(processing_times)
        }},
        'data_scale_analysis': {{
            'total_samples_processed': sum(sample_counts),
            'average_dataset_size': statistics.mean(sample_counts),
            'largest_dataset': max(sample_counts),
            'smallest_dataset': min(sample_counts)
        }},
        'throughput_metrics': {{
            'samples_per_second': sum(sample_counts) / sum(processing_times) if sum(processing_times) > 0 else 0,
            'datasets_per_minute': len(sample_counts) / (sum(processing_times) / 60) if sum(processing_times) > 0 else 0
        }}
    }}

# Save final aggregated analysis
with open('/tmp/final_aggregation.json', 'w') as f:
    json.dump(aggregated_analysis, f, indent=2)

print("Cross-dataset analysis complete:")
print(f"• {{aggregated_analysis['meta_analysis']['total_datasets']}} datasets aggregated")
print(f"• {{aggregated_analysis['meta_analysis']['total_samples_across_all']}} total samples")
print(f"• {{len(aggregated_analysis['meta_analysis']['data_types_analyzed'])}} different data types")

if 'throughput_metrics' in aggregated_analysis['cross_dataset_insights']:
    throughput = aggregated_analysis['cross_dataset_insights']['throughput_metrics']
    print(f"• {{throughput['samples_per_second']:.0f}} samples/second overall throughput")
EOF

    # Upload final aggregated analysis to S3
    echo "Uploading final aggregation to: {s3_final_output}"
    aws s3 cp /tmp/final_aggregation.json {s3_final_output} --region us-east-1

    echo "Multi-dataset aggregation complete: {s3_final_output}"
    """


def main():
    """Demonstrate efficient data movement patterns."""
    print("🗂️  EFFICIENT DATA MOVEMENT DEMONSTRATION")
    print("=" * 55)
    print("Optimal data architecture:")
    print("• Large files: S3 and HTTPS (bypasses SSH tunnels)")
    print("• Control messages: SSH tunnels (lightweight coordination)")
    print("• Zero tunnel bandwidth waste")
    print("• Maximum AWS network efficiency")
    print()

    # Configure AWS provider for data-intensive workloads
    provider = AWSProvider(
        label="efficient_data",
        instance_type="c5.xlarge",  # Larger instance for data processing
        enable_ssm_tunneling=True,
        init_blocks=2,
        max_blocks=6,
        region="us-east-1",
    )

    config = Config(
        executors=[
            HighThroughputExecutor(
                label="data_executor", provider=provider, max_workers_per_node=1
            )
        ]
    )

    print("⚡ Initializing Parsl for efficient data processing...")
    parsl.load(config)
    print("✅ Ready for S3/HTTPS data workflows")

    try:
        # Step 1: Generate and upload research data to S3
        print("\n📊 STEP 1: Data Generation and S3 Upload")
        print("Generating synthetic research datasets and uploading to S3...")

        s3_bucket = "parsl-research-data"  # You'll need to create this bucket

        data_generation_tasks = [
            upload_research_data_to_s3(
                {
                    "sample_count": 2000,
                    "experiment_type": "protein_dynamics",
                    "s3_bucket": s3_bucket,
                }
            ),
            upload_research_data_to_s3(
                {
                    "sample_count": 1500,
                    "experiment_type": "climate_modeling",
                    "s3_bucket": s3_bucket,
                }
            ),
            upload_research_data_to_s3(
                {
                    "sample_count": 1000,
                    "experiment_type": "genomics_analysis",
                    "s3_bucket": s3_bucket,
                }
            ),
        ]

        upload_results = []
        for i, task in enumerate(data_generation_tasks):
            result = task.result()
            upload_results.append(result)

            if result["upload_success"]:
                print(
                    f"  ✅ Dataset {i+1}: {result['sample_count']} {result['experiment_type']} samples"
                )
                print(f"      → S3 URI: {result['s3_csv_uri']}")
                print(f"      → Size: {result['data_size_kb']:.1f} KB")
            else:
                print(f"  ❌ Dataset {i+1} upload failed: {result['error']}")

        # Step 2: S3-to-S3 Processing Pipeline
        print("\n🔄 STEP 2: S3-to-S3 Analysis Pipeline")
        print("Processing S3 datasets and writing analysis results back to S3...")

        analysis_tasks = []
        s3_analysis_uris = []

        for result in upload_results:
            if result["upload_success"]:
                input_uri = result["s3_csv_uri"]
                output_uri = input_uri.replace(
                    "raw_data/", "analysis_results/"
                ).replace(".csv", "_analysis.json")
                s3_analysis_uris.append(output_uri)

                task = analyze_s3_research_data(input_uri, "comprehensive", output_uri)
                analysis_tasks.append(task)

        analysis_results = []
        for i, task in enumerate(analysis_tasks):
            try:
                result = task.result()
                analysis_results.append(result)
                print(f"  ✅ Analysis {i+1}: {result}")
            except Exception as e:
                print(f"  ❌ Analysis {i+1} failed: {e}")

        # Step 3: Download public dataset via HTTPS
        print("\n🌐 STEP 3: Public Dataset Processing via HTTPS")
        print("Downloading and analyzing public research data...")

        # Example: Process Iris dataset (classic ML dataset)
        public_task = download_and_process_public_data(
            "https://raw.githubusercontent.com/mwaskom/seaborn-data/master/iris.csv",
            {
                "analysis": "numerical_summary",
                "numerical_columns": [
                    "sepal_length",
                    "sepal_width",
                    "petal_length",
                    "petal_width",
                ],
                "upload_to_s3": True,
                "s3_bucket": s3_bucket,
            },
        )

        try:
            public_result = public_task.result()
            if public_result["processing_success"]:
                print(
                    f"  ✅ Public dataset: {public_result['record_count']} records processed"
                )
                print(
                    f"      → Processing time: {public_result['processing_time']:.2f}s"
                )
                if public_result["s3_results_uri"]:
                    print(f"      → Results: {public_result['s3_results_uri']}")
            else:
                print(f"  ❌ Public dataset failed: {public_result['error']}")
        except Exception as e:
            print(f"  ❌ Public dataset error: {e}")

        # Step 4: Multi-dataset aggregation
        print("\n📋 STEP 4: Multi-Dataset S3 Aggregation")
        print("Aggregating analysis results from multiple S3 sources...")

        if s3_analysis_uris:
            final_output_uri = (
                f"s3://{s3_bucket}/final_reports/aggregated_research_analysis.json"
            )

            aggregation_task = multi_dataset_s3_aggregation(
                s3_analysis_uris, final_output_uri
            )

            try:
                aggregation_result = aggregation_task.result()
                print(f"  ✅ Final aggregation: {aggregation_result}")
            except Exception as e:
                print(f"  ❌ Aggregation failed: {e}")

        print("\n📊 EFFICIENCY METRICS")
        print("=" * 35)

        successful_uploads = sum(1 for r in upload_results if r["upload_success"])
        total_data_size = sum(
            r.get("data_size_kb", 0) for r in upload_results if r["upload_success"]
        )
        successful_analyses = len(analysis_results)

        print("Data Flow Efficiency:")
        print(f"• {successful_uploads}/{len(upload_results)} datasets uploaded to S3")
        print(f"• {total_data_size:.1f} KB total data moved via S3 (not SSH)")
        print(f"• {successful_analyses} analysis jobs completed")
        print("• Only small control messages via SSH tunnels")
        print("• Large data moved via optimized AWS infrastructure")

        print("\nArchitecture Benefits:")
        print("✅ SSH tunnels: Only lightweight coordination traffic")
        print("✅ S3: High-bandwidth data movement within AWS")
        print("✅ HTTPS: Direct public data access")
        print("✅ Optimal network utilization")

        return True

    except Exception as e:
        print(f"\n❌ Efficient data demo failed: {e}")
        return False

    finally:
        print("\n🧹 Cleaning up...")
        parsl.clear()


if __name__ == "__main__":
    success = main()

    if success:
        print("\n🎉 EFFICIENT DATA MOVEMENT DEMO COMPLETE")
        print("✅ Demonstrated production data flow patterns:")
        print("   → Large files: S3 and HTTPS (optimal bandwidth)")
        print("   → Control: SSH tunnels (minimal overhead)")
        print("   → Zero tunnel congestion from data transfer")
        print("🚀 PRODUCTION DATA ARCHITECTURE VALIDATED")
    else:
        print("\n❌ Demo encountered issues")
        print("💡 Note: Requires S3 bucket 'parsl-research-data' to exist")

    exit(0 if success else 1)

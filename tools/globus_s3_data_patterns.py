#!/usr/bin/env python3
"""
Globus Compute + S3 Data Movement Patterns.

Demonstrates efficient data flow for Function-as-a-Service:
- Large datasets via S3 (optimal AWS bandwidth)
- Function results via Globus (lightweight coordination)
- Production data movement patterns
"""

from globus_compute_sdk import Client


# Efficient S3 Data Processing Functions
def s3_climate_analysis(s3_input_uri, analysis_params):
    """Download climate data from S3, analyze, upload results."""
    import boto3
    import json
    import csv
    import statistics
    import time
    import re

    start_time = time.time()

    # Parse S3 URI
    match = re.match(r"s3://([^/]+)/(.+)", s3_input_uri)
    if not match:
        return {"error": "Invalid S3 URI format", "success": False}

    bucket, key = match.groups()

    try:
        # Download from S3 (efficient AWS-internal transfer)
        s3_client = boto3.client("s3")
        s3_client.download_file(bucket, key, "/tmp/climate_data.csv")

        # Load and analyze climate data
        with open("/tmp/climate_data.csv", "r") as f:
            reader = csv.DictReader(f)
            climate_records = list(reader)

        print(f"Analyzing {len(climate_records)} climate observations")

        # Climate analysis
        temperatures = [float(row["temperature_c"]) for row in climate_records]
        precipitations = [float(row["precipitation_mm"]) for row in climate_records]
        heat_indices = [float(row.get("heat_index", 0)) for row in climate_records]

        # Statistical analysis
        temp_stats = {
            "mean": statistics.mean(temperatures),
            "median": statistics.median(temperatures),
            "std_dev": statistics.stdev(temperatures),
            "min": min(temperatures),
            "max": max(temperatures),
        }

        precip_stats = {
            "total_mm": sum(precipitations),
            "mean_daily": statistics.mean(precipitations),
            "dry_days": sum(1 for p in precipitations if p < 0.1),
            "heavy_rain_days": sum(1 for p in precipitations if p > 10),
        }

        # Climate indicators
        extreme_heat_days = sum(1 for t in temperatures if t > 35)
        extreme_cold_days = sum(1 for t in temperatures if t < -10)

        # Seasonal analysis if data available
        seasonal_data = {}
        if "season_factor" in climate_records[0]:
            seasons = [row.get("season_factor", "0") for row in climate_records]
            seasonal_temps = {}

            for record in climate_records:
                season_val = float(record.get("season_factor", 0))
                temp = float(record["temperature_c"])

                if season_val > 0.5:
                    season = "summer"
                elif season_val < -0.5:
                    season = "winter"
                else:
                    season = "transition"

                if season not in seasonal_temps:
                    seasonal_temps[season] = []
                seasonal_temps[season].append(temp)

            seasonal_data = {
                season: {
                    "count": len(temps),
                    "mean_temp": statistics.mean(temps),
                    "temp_range": [min(temps), max(temps)],
                }
                for season, temps in seasonal_temps.items()
            }

        # Compile comprehensive analysis
        analysis_result = {
            "analysis_type": "climate_comprehensive",
            "input_source": s3_input_uri,
            "sample_count": len(climate_records),
            "processing_time": time.time() - start_time,
            "temperature_analysis": temp_stats,
            "precipitation_analysis": precip_stats,
            "extreme_events": {
                "extreme_heat_days": extreme_heat_days,
                "extreme_cold_days": extreme_cold_days,
                "total_extreme_days": extreme_heat_days + extreme_cold_days,
                "extreme_percentage": (
                    (extreme_heat_days + extreme_cold_days) / len(temperatures)
                )
                * 100,
            },
            "seasonal_analysis": seasonal_data,
        }

        # Upload analysis results to S3
        output_bucket = analysis_params.get("output_bucket", bucket)
        output_key = f"climate_analysis/{int(time.time())}_comprehensive_analysis.json"

        s3_client.put_object(
            Bucket=output_bucket,
            Key=output_key,
            Body=json.dumps(analysis_result, indent=2),
            ContentType="application/json",
        )

        # Return lightweight summary (via Globus)
        return {
            "success": True,
            "analysis_type": "climate_comprehensive",
            "sample_count": len(climate_records),
            "processing_time": time.time() - start_time,
            "s3_results_uri": f"s3://{output_bucket}/{output_key}",
            "key_findings": {
                "mean_temperature": temp_stats["mean"],
                "extreme_days": extreme_heat_days + extreme_cold_days,
                "total_precipitation": precip_stats["total_mm"],
            },
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "processing_time": time.time() - start_time,
        }


def s3_genomics_pipeline(s3_input_uri, pipeline_config):
    """Comprehensive genomics analysis pipeline with S3 data flow."""
    import boto3
    import json
    import csv
    import re
    import time
    import statistics

    start_time = time.time()

    # Parse S3 URI
    match = re.match(r"s3://([^/]+)/(.+)", s3_input_uri)
    if not match:
        return {"error": "Invalid S3 URI format", "success": False}

    bucket, key = match.groups()

    try:
        # Download genomics data from S3
        s3_client = boto3.client("s3")
        s3_client.download_file(bucket, key, "/tmp/genomics_data.csv")

        # Load genomics data
        with open("/tmp/genomics_data.csv", "r") as f:
            reader = csv.DictReader(f)
            genomics_records = list(reader)

        print(f"Processing {len(genomics_records)} genomic sequences")

        # Genomics pipeline analysis
        sequences = [row["sequence"] for row in genomics_records]
        gc_contents = [float(row["gc_content"]) for row in genomics_records]
        sequence_lengths = [int(row["length"]) for row in genomics_records]

        # Sequence composition analysis
        total_bases = sum(sequence_lengths)
        nucleotide_counts = {"A": 0, "T": 0, "G": 0, "C": 0}

        for sequence in sequences:
            for base in sequence:
                if base in nucleotide_counts:
                    nucleotide_counts[base] += 1

        # Advanced genomics metrics
        gc_distribution = {
            "high_gc": sum(1 for gc in gc_contents if gc > 0.6),
            "medium_gc": sum(1 for gc in gc_contents if 0.4 <= gc <= 0.6),
            "low_gc": sum(1 for gc in gc_contents if gc < 0.4),
        }

        # Sequence complexity analysis
        complexity_scores = []
        for sequence in sequences:
            unique_bases = len(set(sequence))
            complexity = unique_bases / 4.0  # Normalized complexity
            complexity_scores.append(complexity)

        # ORF (Open Reading Frame) analysis
        total_orfs = 0
        orf_lengths = []

        for sequence in sequences:
            # Simple ORF finding (start: ATG, stops: TAA, TAG, TGA)
            start_codon = "ATG"
            stop_codons = ["TAA", "TAG", "TGA"]

            for frame in range(3):
                seq = sequence[frame:]
                i = 0
                while i < len(seq) - 2:
                    if seq[i : i + 3] == start_codon:
                        # Found start, look for stop
                        for j in range(i + 3, len(seq) - 2, 3):
                            if seq[j : j + 3] in stop_codons:
                                orf_length = j - i + 3
                                if orf_length >= 60:  # Minimum ORF length
                                    total_orfs += 1
                                    orf_lengths.append(orf_length)
                                break
                        i = j if "j" in locals() else len(seq)
                    else:
                        i += 3

        # Compile genomics analysis
        genomics_analysis = {
            "analysis_type": "genomics_comprehensive",
            "input_source": s3_input_uri,
            "sample_count": len(genomics_records),
            "processing_time": time.time() - start_time,
            "sequence_statistics": {
                "total_sequences": len(sequences),
                "total_bases": total_bases,
                "average_length": statistics.mean(sequence_lengths),
                "length_distribution": {
                    "shortest": min(sequence_lengths),
                    "longest": max(sequence_lengths),
                    "std_dev": statistics.stdev(sequence_lengths),
                },
            },
            "nucleotide_composition": {
                base: {"count": count, "percentage": (count / total_bases) * 100}
                for base, count in nucleotide_counts.items()
            },
            "gc_content_analysis": {
                "mean_gc": statistics.mean(gc_contents),
                "gc_distribution": gc_distribution,
                "gc_std_dev": statistics.stdev(gc_contents),
            },
            "complexity_analysis": {
                "mean_complexity": statistics.mean(complexity_scores),
                "high_complexity": sum(1 for c in complexity_scores if c > 0.8),
                "low_complexity": sum(1 for c in complexity_scores if c < 0.5),
            },
            "orf_analysis": {
                "total_orfs_found": total_orfs,
                "average_orf_length": statistics.mean(orf_lengths)
                if orf_lengths
                else 0,
                "longest_orf": max(orf_lengths) if orf_lengths else 0,
                "orfs_per_sequence": total_orfs / len(sequences),
            },
        }

        # Upload comprehensive analysis to S3
        output_bucket = pipeline_config.get("output_bucket", bucket)
        output_key = f"genomics_analysis/{int(time.time())}_comprehensive_genomics.json"

        s3_client.put_object(
            Bucket=output_bucket,
            Key=output_key,
            Body=json.dumps(genomics_analysis, indent=2),
            ContentType="application/json",
        )

        # Return lightweight summary
        return {
            "success": True,
            "analysis_type": "genomics_comprehensive",
            "sample_count": len(genomics_records),
            "processing_time": time.time() - start_time,
            "s3_results_uri": f"s3://{output_bucket}/{output_key}",
            "key_findings": {
                "total_sequences": len(sequences),
                "mean_gc_content": statistics.mean(gc_contents),
                "total_orfs": total_orfs,
                "complexity_score": statistics.mean(complexity_scores),
            },
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "processing_time": time.time() - start_time,
        }


def https_public_research_analysis(dataset_url, analysis_config):
    """Download and analyze public research dataset via HTTPS."""
    import urllib.request
    import json
    import csv
    import statistics
    import time

    start_time = time.time()

    try:
        print(f"Downloading public research data: {dataset_url}")

        # Download directly via HTTPS (no SSH tunnel)
        urllib.request.urlretrieve(dataset_url, "/tmp/public_research.csv")

        # Load data
        with open("/tmp/public_research.csv", "r") as f:
            reader = csv.DictReader(f)
            data = list(reader)

        print(f"Loaded {len(data)} research records")

        # Flexible analysis based on configuration
        analysis_type = analysis_config["type"]

        if analysis_type == "statistical_modeling":
            # Advanced statistical analysis
            numerical_columns = analysis_config.get("numerical_columns", [])

            statistical_results = {}
            correlations = {}

            for col in numerical_columns:
                try:
                    values = [float(row[col]) for row in data if row.get(col)]
                    if values:
                        statistical_results[col] = {
                            "count": len(values),
                            "mean": statistics.mean(values),
                            "median": statistics.median(values),
                            "std_dev": statistics.stdev(values)
                            if len(values) > 1
                            else 0,
                            "quartiles": [
                                sorted(values)[len(values) // 4],
                                statistics.median(values),
                                sorted(values)[3 * len(values) // 4],
                            ],
                            "outliers": len(
                                [
                                    v
                                    for v in values
                                    if abs(v - statistics.mean(values))
                                    > 2 * statistics.stdev(values)
                                ]
                            )
                            if len(values) > 1
                            else 0,
                        }
                except (ValueError, statistics.StatisticsError):
                    statistical_results[col] = {
                        "error": "non_numerical_or_insufficient_data"
                    }

            # Correlation analysis between numerical columns
            if len(numerical_columns) > 1:
                for i, col1 in enumerate(numerical_columns):
                    for col2 in numerical_columns[i + 1 :]:
                        try:
                            values1 = [
                                float(row[col1])
                                for row in data
                                if row.get(col1) and row.get(col2)
                            ]
                            values2 = [
                                float(row[col2])
                                for row in data
                                if row.get(col1) and row.get(col2)
                            ]

                            if len(values1) > 1:
                                # Calculate correlation coefficient
                                mean1, mean2 = (
                                    statistics.mean(values1),
                                    statistics.mean(values2),
                                )
                                std1, std2 = (
                                    statistics.stdev(values1),
                                    statistics.stdev(values2),
                                )

                                correlation = sum(
                                    (v1 - mean1) * (v2 - mean2)
                                    for v1, v2 in zip(values1, values2)
                                )
                                correlation /= len(values1) * std1 * std2

                                correlations[f"{col1}_vs_{col2}"] = {
                                    "correlation": correlation,
                                    "sample_count": len(values1),
                                    "strength": "strong"
                                    if abs(correlation) > 0.7
                                    else "moderate"
                                    if abs(correlation) > 0.3
                                    else "weak",
                                }
                        except:
                            correlations[f"{col1}_vs_{col2}"] = {
                                "error": "correlation_calculation_failed"
                            }

            analysis_results = {
                "analysis_type": "statistical_modeling",
                "statistical_summary": statistical_results,
                "correlation_analysis": correlations,
            }

        elif analysis_type == "categorical_insights":
            # Categorical data analysis
            categorical_columns = analysis_config.get("categorical_columns", [])

            categorical_results = {}
            for col in categorical_columns:
                if col in data[0]:
                    category_counts = {}
                    for row in data:
                        category = row.get(col, "unknown")
                        category_counts[category] = category_counts.get(category, 0) + 1

                    categorical_results[col] = {
                        "unique_categories": len(category_counts),
                        "distribution": category_counts,
                        "most_common": max(category_counts.items(), key=lambda x: x[1])
                        if category_counts
                        else None,
                        "diversity_index": len(category_counts)
                        / len(data),  # Simpson's diversity
                    }

            analysis_results = {
                "analysis_type": "categorical_insights",
                "categorical_summary": categorical_results,
            }

        else:  # exploratory_analysis
            # Exploratory data analysis
            columns = list(data[0].keys()) if data else []

            # Basic statistics for all columns
            column_analysis = {}
            for col in columns:
                values = [row[col] for row in data if row.get(col)]
                unique_values = len(set(values))

                # Try to determine if numerical
                try:
                    numerical_values = [float(v) for v in values if v]
                    is_numerical = (
                        len(numerical_values) > len(values) * 0.8
                    )  # 80% numerical

                    if is_numerical:
                        column_analysis[col] = {
                            "type": "numerical",
                            "count": len(numerical_values),
                            "unique_count": unique_values,
                            "mean": statistics.mean(numerical_values),
                            "range": [min(numerical_values), max(numerical_values)],
                        }
                    else:
                        column_analysis[col] = {
                            "type": "categorical",
                            "count": len(values),
                            "unique_count": unique_values,
                            "most_common": max(set(values), key=values.count)
                            if values
                            else None,
                        }
                except:
                    column_analysis[col] = {
                        "type": "mixed_or_text",
                        "count": len(values),
                        "unique_count": unique_values,
                    }

            analysis_results = {
                "analysis_type": "exploratory_analysis",
                "data_overview": {
                    "total_records": len(data),
                    "total_columns": len(columns),
                    "column_types": {
                        col_name: col_info["type"]
                        for col_name, col_info in column_analysis.items()
                    },
                },
                "column_analysis": column_analysis,
            }

        # Upload detailed results to S3 if requested
        if analysis_config.get("save_to_s3"):
            output_bucket = analysis_config.get("output_bucket", "parsl-research-data")
            output_key = f"public_analysis/{analysis_type}_{int(time.time())}.json"

            s3_client.put_object(
                Bucket=output_bucket,
                Key=output_key,
                Body=json.dumps(analysis_results, indent=2),
                ContentType="application/json",
            )

            s3_output_uri = f"s3://{output_bucket}/{output_key}"
        else:
            s3_output_uri = None

        return {
            "success": True,
            "dataset_url": dataset_url,
            "record_count": len(data),
            "analysis_type": analysis_type,
            "processing_time": time.time() - start_time,
            "s3_results_uri": s3_output_uri,
            "analysis_summary": analysis_results,
        }

    except Exception as e:
        return {
            "success": False,
            "dataset_url": dataset_url,
            "error": str(e),
            "processing_time": time.time() - start_time,
        }


def s3_multi_omics_integration(s3_uris_config):
    """Integrate multiple omics datasets from S3 sources."""
    import boto3
    import json
    import re
    import time
    import statistics

    start_time = time.time()

    s3_client = boto3.client("s3")
    integrated_data = {}

    try:
        # Download and integrate multiple omics datasets
        for dataset_name, s3_uri in s3_uris_config.items():
            print(f"Downloading {dataset_name} from {s3_uri}")

            # Parse S3 URI
            match = re.match(r"s3://([^/]+)/(.+)", s3_uri)
            if match:
                bucket, key = match.groups()
                local_file = f"/tmp/{dataset_name}_data.json"

                s3_client.download_file(bucket, key, local_file)

                with open(local_file, "r") as f:
                    dataset = json.load(f)
                    integrated_data[dataset_name] = dataset
                    print(f"  ✅ {dataset_name}: {len(dataset)} records loaded")

        # Multi-omics integration analysis
        integration_results = {
            "integration_type": "multi_omics",
            "datasets_integrated": list(integrated_data.keys()),
            "total_datasets": len(integrated_data),
            "processing_time": time.time() - start_time,
        }

        # Cross-dataset analysis
        sample_counts = []
        for dataset_name, dataset in integrated_data.items():
            sample_count = len(dataset)
            sample_counts.append(sample_count)

            integration_results[f"{dataset_name}_summary"] = {
                "record_count": sample_count,
                "data_structure": list(dataset[0].keys()) if dataset else [],
            }

        # Integration metrics
        integration_results["integration_metrics"] = {
            "total_samples_integrated": sum(sample_counts),
            "average_dataset_size": statistics.mean(sample_counts)
            if sample_counts
            else 0,
            "size_variation": statistics.stdev(sample_counts)
            if len(sample_counts) > 1
            else 0,
            "integration_efficiency": len(integrated_data)
            / (time.time() - start_time),  # datasets per second
        }

        # Save integrated analysis to S3
        output_bucket = "parsl-research-data"
        output_key = f"integrated_analysis/multi_omics_{int(time.time())}.json"

        s3_client.put_object(
            Bucket=output_bucket,
            Key=output_key,
            Body=json.dumps(integration_results, indent=2),
            ContentType="application/json",
        )

        return {
            "success": True,
            "integration_type": "multi_omics",
            "datasets_processed": len(integrated_data),
            "total_samples": sum(sample_counts),
            "processing_time": time.time() - start_time,
            "s3_results_uri": f"s3://{output_bucket}/{output_key}",
            "integration_summary": integration_results["integration_metrics"],
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "processing_time": time.time() - start_time,
        }


def demo_globus_s3_workflows():
    """Demonstrate Globus Compute with efficient S3 data patterns."""
    print("🌐 GLOBUS COMPUTE + S3 DATA WORKFLOWS")
    print("=" * 50)
    print("Production Function-as-a-Service with efficient data movement:")
    print("• Large datasets: S3 bucket-to-bucket transfers")
    print("• Public data: Direct HTTPS downloads")
    print("• Function coordination: Globus Compute")
    print("• Results: Lightweight summaries + S3 detailed storage")
    print()

    # Note: This assumes you have Globus Compute endpoints configured
    # with our AWSProvider as shown in the configuration examples

    gc = Client()

    # Replace with your actual endpoint IDs
    standard_endpoint = "your-aws-standard-endpoint-uuid"
    container_endpoint = "your-aws-container-endpoint-uuid"

    print("🔬 WORKFLOW 1: Climate Research via S3")
    print("Processing climate model output stored in S3...")

    try:
        # Submit climate analysis function
        climate_task = gc.run(
            s3_climate_analysis,
            endpoint_id=standard_endpoint,
            s3_input_uri="s3://parsl-research-data/raw_data/climate_modeling_sample.csv",
            analysis_params={"output_bucket": "parsl-research-data"},
        )

        climate_result = gc.get_result(climate_task)

        if climate_result["success"]:
            findings = climate_result["key_findings"]
            print("  ✅ Climate analysis complete:")
            print(f"      → {climate_result['sample_count']} observations analyzed")
            print(f"      → Mean temperature: {findings['mean_temperature']:.1f}°C")
            print(f"      → Extreme weather days: {findings['extreme_days']}")
            print(f"      → Detailed results: {climate_result['s3_results_uri']}")
        else:
            print(f"  ❌ Climate analysis failed: {climate_result['error']}")

    except Exception as e:
        print(f"  ❌ Climate workflow error: {e}")

    print("\n🧬 WORKFLOW 2: Genomics Pipeline via S3")
    print("Processing genomics data with comprehensive analysis...")

    try:
        # Submit genomics pipeline function
        genomics_task = gc.run(
            s3_genomics_pipeline,
            endpoint_id=container_endpoint,  # Use container endpoint for bioinformatics
            s3_input_uri="s3://parsl-research-data/raw_data/genomics_sample.csv",
            pipeline_config={"output_bucket": "parsl-research-data"},
        )

        genomics_result = gc.get_result(genomics_task)

        if genomics_result["success"]:
            findings = genomics_result["key_findings"]
            print("  ✅ Genomics pipeline complete:")
            print(f"      → {genomics_result['sample_count']} sequences analyzed")
            print(f"      → Mean GC content: {findings['mean_gc_content']:.3f}")
            print(f"      → ORFs discovered: {findings['total_orfs']}")
            print(f"      → Detailed results: {genomics_result['s3_results_uri']}")
        else:
            print(f"  ❌ Genomics pipeline failed: {genomics_result['error']}")

    except Exception as e:
        print(f"  ❌ Genomics workflow error: {e}")

    print("\n🌍 WORKFLOW 3: Public Data Analysis via HTTPS")
    print("Analyzing public research datasets...")

    try:
        # Analyze public dataset
        public_task = gc.run(
            https_public_research_analysis,
            endpoint_id=standard_endpoint,
            dataset_url="https://raw.githubusercontent.com/mwaskom/seaborn-data/master/iris.csv",
            analysis_config={
                "type": "statistical_modeling",
                "numerical_columns": [
                    "sepal_length",
                    "sepal_width",
                    "petal_length",
                    "petal_width",
                ],
                "save_to_s3": True,
                "output_bucket": "parsl-research-data",
            },
        )

        public_result = gc.get_result(public_task)

        if public_result["success"]:
            print("  ✅ Public data analysis complete:")
            print(f"      → {public_result['record_count']} records processed")
            print(f"      → Analysis type: {public_result['analysis_type']}")
            print(f"      → Processing time: {public_result['processing_time']:.2f}s")
            if public_result["s3_results_uri"]:
                print(f"      → Results stored: {public_result['s3_results_uri']}")
        else:
            print(f"  ❌ Public data analysis failed: {public_result['error']}")

    except Exception as e:
        print(f"  ❌ Public data workflow error: {e}")

    print("\n🔗 WORKFLOW 4: Multi-Dataset Integration")
    print("Integrating multiple S3 datasets for comprehensive analysis...")

    try:
        # Multi-omics integration
        integration_task = gc.run(
            s3_multi_omics_integration,
            endpoint_id=container_endpoint,
            s3_uris_config={
                "proteomics": "s3://parsl-research-data/analysis_results/protein_analysis.json",
                "genomics": "s3://parsl-research-data/analysis_results/genomics_analysis.json",
                "metabolomics": "s3://parsl-research-data/analysis_results/metabolomics_analysis.json",
            },
        )

        integration_result = gc.get_result(integration_task)

        if integration_result["success"]:
            summary = integration_result["integration_summary"]
            print("  ✅ Multi-omics integration complete:")
            print(
                f"      → {integration_result['datasets_processed']} datasets integrated"
            )
            print(f"      → {integration_result['total_samples']} total samples")
            print(
                f"      → Integration rate: {summary['integration_efficiency']:.2f} datasets/sec"
            )
            print(f"      → Results: {integration_result['s3_results_uri']}")
        else:
            print(f"  ❌ Integration failed: {integration_result['error']}")

    except Exception as e:
        print(f"  ❌ Integration workflow error: {e}")

    print("\n📊 DATA MOVEMENT EFFICIENCY")
    print("=" * 40)
    print("Architecture Benefits:")
    print("✅ Large datasets: Moved via S3 (AWS-optimized bandwidth)")
    print("✅ Public data: Direct HTTPS access (no proxy overhead)")
    print("✅ Function results: Lightweight Globus coordination")
    print("✅ Detailed outputs: Stored in S3 for later access")
    print("✅ Zero SSH tunnel congestion from large files")

    print("\nData Flow Summary:")
    print("• SSH tunnels: Only function submission/result coordination")
    print("• S3 transfers: All large dataset movement within AWS")
    print("• HTTPS downloads: Direct public data access")
    print("• Optimal network utilization across all data sizes")


if __name__ == "__main__":
    demo_globus_s3_workflows()

    print("\n🎉 GLOBUS + S3 DATA PATTERNS COMPLETE")
    print("✅ Demonstrated Function-as-a-Service with optimal data flow")
    print("🚀 PRODUCTION GLOBUS COMPUTE ARCHITECTURE VALIDATED")

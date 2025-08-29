#!/usr/bin/env python3
"""Real compute test with meaningful computation for Parsl AWS Provider."""

import parsl
from parsl import python_app
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
from phase15_enhanced import AWSProvider
import sys


@python_app
def matrix_multiply(size: int):
    """Perform matrix multiplication - a real compute task."""
    import numpy as np

    start_time = time.time()

    # Create random matrices
    A = np.random.rand(size, size)
    B = np.random.rand(size, size)

    # Perform matrix multiplication
    C = np.dot(A, B)

    end_time = time.time()
    compute_time = end_time - start_time

    # Return compute metrics
    return {
        "matrix_size": size,
        "compute_time_seconds": compute_time,
        "result_shape": C.shape,
        "result_sum": float(np.sum(C)),
        "flops_estimate": 2 * size**3,  # Rough FLOPS estimate for matrix multiply
    }


@python_app
def prime_factorization(number: int):
    """Find prime factors - CPU-intensive computation."""

    start_time = time.time()
    factors = []
    d = 2

    while d * d <= number:
        while number % d == 0:
            factors.append(d)
            number //= d
        d += 1
    if number > 1:
        factors.append(number)

    end_time = time.time()

    return {
        "original_number": number
        if len(factors) == 0
        else factors[0]
        if len(factors) == 1
        else factors,
        "prime_factors": factors,
        "compute_time_seconds": end_time - start_time,
        "factor_count": len(factors),
    }


@python_app
def data_processing_task():
    """Data processing task - simulate real workload."""
    import time

    start_time = time.time()

    # Simulate data processing
    data = []
    for i in range(10000):
        record = {
            "id": i,
            "value": i * 2.5,
            "category": "A" if i % 2 == 0 else "B",
            "processed": True,
        }
        data.append(record)

    # Aggregate data
    total_value = sum(r["value"] for r in data)
    category_a_count = sum(1 for r in data if r["category"] == "A")
    category_b_count = sum(1 for r in data if r["category"] == "B")

    end_time = time.time()

    return {
        "records_processed": len(data),
        "total_value": total_value,
        "category_a_count": category_a_count,
        "category_b_count": category_b_count,
        "compute_time_seconds": end_time - start_time,
    }


def main():
    try:
        print("STARTING REAL COMPUTE TEST")
        print("=" * 50)
        sys.stdout.flush()

        # Configure provider with Python 3.10 for compatibility
        provider = AWSProvider(
            label="real_compute",
            init_blocks=1,
            max_blocks=2,  # Allow scaling for multiple tasks
            python_version="3.10",
        )

        config = Config(
            executors=[HighThroughputExecutor(label="compute_exec", provider=provider)]
        )

        print("LOADING PARSL")
        sys.stdout.flush()
        parsl.load(config)

        print("SUBMITTING COMPUTE TASKS")
        sys.stdout.flush()

        # Submit multiple real compute tasks
        print("1. Matrix multiplication (500x500)")
        matrix_future = matrix_multiply(500)

        print("2. Prime factorization (large number)")
        prime_future = prime_factorization(982451653)

        print("3. Data processing (10K records)")
        data_future = data_processing_task()

        print("\nWAITING FOR COMPUTE RESULTS...")
        sys.stdout.flush()

        # Collect results
        try:
            print("\n--- MATRIX MULTIPLICATION RESULTS ---")
            matrix_result = matrix_future.result(timeout=300)
            print(
                f"Matrix size: {matrix_result['matrix_size']}x{matrix_result['matrix_size']}"
            )
            print(f"Compute time: {matrix_result['compute_time_seconds']:.2f} seconds")
            print(f"FLOPS estimate: {matrix_result['flops_estimate']:,}")
            print(f"Result sum: {matrix_result['result_sum']:.2f}")

            print("\n--- PRIME FACTORIZATION RESULTS ---")
            prime_result = prime_future.result(timeout=300)
            print(f"Number: {prime_result.get('original_number', 'N/A')}")
            print(f"Prime factors: {prime_result['prime_factors']}")
            print(f"Factor count: {prime_result['factor_count']}")
            print(f"Compute time: {prime_result['compute_time_seconds']:.2f} seconds")

            print("\n--- DATA PROCESSING RESULTS ---")
            data_result = data_future.result(timeout=300)
            print(f"Records processed: {data_result['records_processed']:,}")
            print(f"Total value: {data_result['total_value']:,.2f}")
            print(f"Category A: {data_result['category_a_count']:,}")
            print(f"Category B: {data_result['category_b_count']:,}")
            print(f"Compute time: {data_result['compute_time_seconds']:.2f} seconds")

            print("\n" + "=" * 50)
            print("🎉 REAL COMPUTE TEST SUCCESS")
            print("All tasks completed successfully on AWS infrastructure")
            print("✅ SSH reverse tunneling working with real workloads")

        except Exception as task_error:
            print(f"\n❌ COMPUTE TASK FAILED: {task_error}")
            sys.stdout.flush()

    except Exception as e:
        print(f"\n❌ REAL COMPUTE TEST FAILED: {e}")
        sys.stdout.flush()
    finally:
        try:
            parsl.clear()
            print("\nCLEANUP COMPLETE")
            sys.stdout.flush()
        except:
            pass


if __name__ == "__main__":
    main()

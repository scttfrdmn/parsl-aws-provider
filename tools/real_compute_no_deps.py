#!/usr/bin/env python3
"""Real compute test using only Python standard library."""

import parsl
from parsl import python_app
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
from phase15_enhanced import AWSProvider
import sys


@python_app
def cpu_intensive_task(iterations: int):
    """CPU-intensive computation using only standard library."""
    import time
    import math

    start_time = time.time()

    # Compute-intensive operations
    result = 0
    for i in range(iterations):
        # Complex mathematical operations
        result += math.sqrt(i * 2.5) * math.sin(i / 1000.0) * math.cos(i / 500.0)
        if i % 10000 == 0:
            result = math.log(abs(result) + 1)  # Keep numbers manageable

    end_time = time.time()

    return {
        "iterations": iterations,
        "final_result": result,
        "compute_time_seconds": end_time - start_time,
        "ops_per_second": iterations / (end_time - start_time),
    }


@python_app
def fibonacci_compute(n: int):
    """Compute large Fibonacci number - real CPU work."""
    import time

    start_time = time.time()

    def fib(num):
        if num <= 1:
            return num
        return fib(num - 1) + fib(num - 2)

    # Use iterative approach for larger numbers
    def fib_iterative(num):
        if num <= 1:
            return num
        a, b = 0, 1
        for _ in range(2, num + 1):
            a, b = b, a + b
        return b

    result = fib_iterative(n)
    end_time = time.time()

    return {
        "fibonacci_n": n,
        "fibonacci_result": result,
        "compute_time_seconds": end_time - start_time,
    }


@python_app
def string_processing_task(data_size: int):
    """String processing - real data manipulation."""
    import time
    import random
    import string

    start_time = time.time()

    # Generate test data
    data = []
    for i in range(data_size):
        # Create random strings
        text = "".join(random.choices(string.ascii_letters + string.digits, k=20))
        data.append(f"Record_{i}:{text}")

    # Process data - sorting, filtering, transforming
    sorted_data = sorted(data)
    filtered_data = [item for item in sorted_data if "A" in item or "a" in item]
    processed_count = len(filtered_data)

    # Additional string operations
    total_length = sum(len(item) for item in filtered_data)

    end_time = time.time()

    return {
        "original_count": data_size,
        "processed_count": processed_count,
        "total_string_length": total_length,
        "compute_time_seconds": end_time - start_time,
        "processing_rate": data_size / (end_time - start_time),
    }


def main():
    try:
        print("STARTING REAL COMPUTE TEST (No External Dependencies)")
        print("=" * 60)
        sys.stdout.flush()

        provider = AWSProvider(
            label="real_compute", init_blocks=1, max_blocks=2, python_version="3.10"
        )

        config = Config(
            executors=[HighThroughputExecutor(label="compute_exec", provider=provider)]
        )

        print("LOADING PARSL")
        sys.stdout.flush()
        parsl.load(config)

        print("SUBMITTING REAL COMPUTE TASKS")
        sys.stdout.flush()

        # Submit real compute tasks
        print("1. CPU-intensive mathematical computation (1M operations)")
        cpu_future = cpu_intensive_task(1000000)

        print("2. Fibonacci computation (n=50)")
        fib_future = fibonacci_compute(50)

        print("3. String processing (50K records)")
        string_future = string_processing_task(50000)

        print("\nWAITING FOR REAL COMPUTE RESULTS...")
        sys.stdout.flush()

        # Collect results with generous timeouts for real computation
        try:
            print("\n--- CPU INTENSIVE RESULTS ---")
            cpu_result = cpu_future.result(timeout=600)  # 10 minutes
            print(f"Iterations: {cpu_result['iterations']:,}")
            print(f"Final result: {cpu_result['final_result']:.6f}")
            print(f"Compute time: {cpu_result['compute_time_seconds']:.2f} seconds")
            print(f"Operations/sec: {cpu_result['ops_per_second']:,.0f}")

            print("\n--- FIBONACCI COMPUTATION RESULTS ---")
            fib_result = fib_future.result(timeout=600)
            print(
                f"Fibonacci({fib_result['fibonacci_n']}) = {fib_result['fibonacci_result']}"
            )
            print(f"Compute time: {fib_result['compute_time_seconds']:.2f} seconds")

            print("\n--- STRING PROCESSING RESULTS ---")
            string_result = string_future.result(timeout=600)
            print(f"Records processed: {string_result['original_count']:,}")
            print(f"Filtered results: {string_result['processed_count']:,}")
            print(f"Total string length: {string_result['total_string_length']:,}")
            print(f"Compute time: {string_result['compute_time_seconds']:.2f} seconds")
            print(
                f"Processing rate: {string_result['processing_rate']:,.0f} records/sec"
            )

            print("\n" + "=" * 60)
            print("🎉 REAL COMPUTE TEST SUCCESS")
            print("✅ All CPU-intensive tasks completed successfully")
            print("✅ SSH reverse tunneling working with real workloads")
            print("✅ AWS infrastructure handled genuine computation")

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

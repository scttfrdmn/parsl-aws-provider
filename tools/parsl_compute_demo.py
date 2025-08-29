#!/usr/bin/env python3
"""
Real-world Parsl computational demo - parallel computing workload.
"""

import parsl
from parsl import python_app, bash_app
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
import sys
import time
from phase15_enhanced import AWSProvider


@python_app
def monte_carlo_pi(n_samples):
    """Monte Carlo estimation of π using random sampling."""
    import random

    inside_circle = 0
    for _ in range(n_samples):
        x = random.random()
        y = random.random()
        if x * x + y * y <= 1.0:
            inside_circle += 1

    pi_estimate = 4.0 * inside_circle / n_samples
    return {
        "samples": n_samples,
        "pi_estimate": pi_estimate,
        "accuracy": abs(pi_estimate - 3.141592653589793),
    }


@python_app
def prime_factorization(n):
    """Find prime factors of a number."""
    factors = []
    d = 2
    while d * d <= n:
        while n % d == 0:
            factors.append(d)
            n //= d
        d += 1
    if n > 1:
        factors.append(n)
    return {
        "number": n if not factors else factors[0] * (n if n > 1 else 1),
        "factors": factors,
    }


@bash_app
def system_benchmark():
    """Simple system benchmark."""
    return """
    echo "=== SYSTEM BENCHMARK ==="
    echo "Hostname: $(hostname)"
    echo "CPU Info: $(nproc) cores"
    echo "Memory: $(free -h | grep ^Mem | awk '{print $2}')"
    echo "Disk: $(df -h / | tail -1 | awk '{print $4}' | cut -d'G' -f1)"
    echo "Uptime: $(uptime)"
    echo "Test computation: $(python3 -c 'print(sum(i*i for i in range(10000)))')"
    """


def main():
    """Run computational demo."""
    print("🧮 PARSL COMPUTATIONAL DEMO")
    print("=" * 40)
    print("Distributed computing tasks:")
    print("• Monte Carlo π estimation")
    print("• Prime factorization")
    print("• System benchmarking")
    print()

    # AWS provider for computation
    provider = AWSProvider(label="compute_demo", init_blocks=1, max_blocks=2)

    config = Config(
        executors=[
            HighThroughputExecutor(
                label="compute_executor", provider=provider, max_workers_per_node=1
            )
        ]
    )

    print("⚡ Starting Parsl...")
    parsl.load(config)
    print("✅ Connected to AWS")

    print("\n🎯 Submitting computational tasks...")

    # Monte Carlo π estimation with different sample sizes
    pi_tasks = []
    sample_sizes = [100000, 500000, 1000000]

    for size in sample_sizes:
        future = monte_carlo_pi(size)
        pi_tasks.append((size, future))
        print(f"  → Monte Carlo π with {size:,} samples")

    # Prime factorization
    numbers = [982451653, 15485863, 982451653]
    prime_tasks = []
    for num in numbers:
        future = prime_factorization(num)
        prime_tasks.append((num, future))
        print(f"  → Prime factorization of {num:,}")

    # System benchmark
    benchmark_future = system_benchmark()
    print("  → System benchmark")

    print("\n🔄 Computing on AWS instances...")

    # Collect Monte Carlo results
    print("\n📊 Monte Carlo π Estimation:")
    for size, future in pi_tasks:
        result = future.result()
        print(
            f"  {size:,} samples: π ≈ {result['pi_estimate']:.6f} "
            f"(error: {result['accuracy']:.6f})"
        )

    # Collect prime factorization results
    print("\n🔢 Prime Factorization:")
    for num, future in prime_tasks:
        result = future.result()
        print(f"  {num:,} = {' × '.join(map(str, result['factors']))}")

    # System benchmark
    print("\n⚡ System Benchmark:")
    benchmark_result = benchmark_future.result()
    for line in benchmark_result.strip().split("\n"):
        print(f"  {line}")

    print(f"\n✅ Completed {len(pi_tasks) + len(prime_tasks) + 1} computational tasks")
    return True


if __name__ == "__main__":
    try:
        start_time = time.time()
        success = main()
        elapsed = time.time() - start_time

        print("\n🧹 Cleaning up...")
        parsl.clear()

        if success:
            print(f"\n🎉 COMPUTATIONAL DEMO COMPLETE ({elapsed:.1f}s)")
            print("🚀 Parsl + AWS Provider validated with real workloads")

        sys.exit(0 if success else 1)

    except Exception as e:
        print(f"\n❌ Demo failed: {e}")
        try:
            parsl.clear()
        except:
            pass
        sys.exit(1)

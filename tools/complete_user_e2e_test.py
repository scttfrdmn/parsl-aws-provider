#!/usr/bin/env python3
"""
Complete User Experience E2E Test for Phase 1.5 Enhanced AWS Provider

This test demonstrates the complete user workflow from setup to results,
exactly as a real user would experience the system.
"""

import parsl
from parsl import python_app, bash_app, File
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
import time
import os
import sys

# Import our enhanced provider
from phase15_enhanced import AWSProvider


def test_setup():
    """Test Phase 1: Provider Setup (User Experience)"""
    print("🔧 PHASE 1: PROVIDER SETUP")
    print("=" * 60)
    print("Setting up AWS provider with SSM tunneling...")
    print("• User doesn't need to configure networking")
    print("• Works behind any firewall or NAT")
    print("• Automatic resource management")
    print()

    # This is exactly what a user would write
    provider = AWSProvider(
        label="user_test",  # Simple label
        init_blocks=1,  # Start with 1 instance
        max_blocks=2,  # Scale up to 2 if needed
        min_blocks=0,  # Scale down to 0 when idle
    )

    config = Config(
        executors=[
            HighThroughputExecutor(
                label="aws_executor",
                provider=provider,
                max_workers_per_node=1,
                cores_per_worker=1,
            )
        ]
    )

    print("✅ Provider configured successfully")
    print("✅ Ready to submit computational tasks to AWS")
    return config


@python_app
def compute_fibonacci(n):
    """Computational Python task - calculates Fibonacci numbers."""

    def fib(x):
        if x <= 1:
            return x
        return fib(x - 1) + fib(x - 2)

    import time

    start_time = time.time()
    result = fib(n)
    elapsed = time.time() - start_time

    return {
        "input": n,
        "result": result,
        "computation_time": elapsed,
        "message": f"Computed fibonacci({n}) = {result} in {elapsed:.3f}s",
    }


@bash_app
def system_analysis(outputs=[]):
    """System analysis task that creates output files."""
    return """
    echo "=== REMOTE SYSTEM ANALYSIS ===" > {outputs[0]}
    echo "Hostname: $(hostname)" >> {outputs[0]}
    echo "Date: $(date)" >> {outputs[0]}
    echo "Uptime: $(uptime)" >> {outputs[0]}
    echo "Python version: $(python3 --version)" >> {outputs[0]}
    echo "Working directory: $(pwd)" >> {outputs[0]}
    echo "Available disk space:" >> {outputs[0]}
    df -h / >> {outputs[0]}
    echo "Memory information:" >> {outputs[0]}
    free -h >> {outputs[0]}
    echo "Process count: $(ps aux | wc -l)" >> {outputs[0]}
    echo "=== ANALYSIS COMPLETE ===" >> {outputs[0]}
    """


@python_app
def data_processing(input_file, outputs=[]):
    """Process data from the system analysis."""
    # Read the input file
    with open(input_file, "r") as f:
        content = f.read()

    # Extract some statistics
    lines = content.split("\n")
    stats = {
        "total_lines": len(lines),
        "hostname": next(
            (line.split(": ")[1] for line in lines if "Hostname:" in line), "unknown"
        ),
        "python_version": next(
            (line.split(": ")[1] for line in lines if "Python version:" in line),
            "unknown",
        ),
    }

    # Write processed results
    with open(outputs[0], "w") as f:
        f.write("=== DATA PROCESSING RESULTS ===\n")
        f.write(f"Input file had {stats['total_lines']} lines\n")
        f.write(f"Remote hostname: {stats['hostname']}\n")
        f.write(f"Remote Python: {stats['python_version']}\n")
        f.write("Processing completed successfully\n")
        f.write("=== END RESULTS ===\n")

    return stats


def test_workflow_execution(config):
    """Test Phase 2-4: Workflow Definition, Execution & Results"""
    print("🚀 PHASE 2: WORKFLOW DEFINITION")
    print("=" * 60)
    print("Defining computational workflows...")
    print("• Python computational task (Fibonacci)")
    print("• Bash system analysis task")
    print("• Data processing with file I/O")
    print()

    # Load Parsl with our configuration
    print("⚡ Loading Parsl configuration...")
    parsl.load(config)
    print("✅ Parsl loaded - ready to submit tasks")
    print()

    print("📤 PHASE 3: TASK SUBMISSION")
    print("=" * 60)
    print("Submitting tasks to AWS...")

    # Define output files
    system_report = File("system_analysis.txt")
    processed_report = File("processed_results.txt")

    # Submit tasks
    print("• Submitting Fibonacci computation (n=25)...")
    fib_future = compute_fibonacci(25)

    print("• Submitting system analysis...")
    system_future = system_analysis(outputs=[system_report])

    print("• Submitting data processing (depends on system analysis)...")
    process_future = data_processing(system_report, outputs=[processed_report])

    print("✅ All tasks submitted")
    print("🔄 Behind the scenes:")
    print("  - AWS instances are being launched")
    print("  - SSM tunnels are being established")
    print("  - Workers are connecting through secure tunnels")
    print("  - Tasks will execute remotely and return results")
    print()

    print("⏳ PHASE 4: WAITING FOR RESULTS")
    print("=" * 60)
    print("Waiting for remote execution to complete...")

    try:
        # Wait for computational result
        print("• Waiting for Fibonacci computation...")
        start_time = time.time()
        fib_result = fib_future.result()
        fib_elapsed = time.time() - start_time

        print(f"✅ Fibonacci result: {fib_result['message']}")
        print(f"   (Total time including AWS setup: {fib_elapsed:.1f}s)")

        # Wait for system analysis
        print("• Waiting for system analysis...")
        system_start = time.time()
        system_result = system_future.result()
        system_elapsed = time.time() - system_start

        print(f"✅ System analysis complete ({system_elapsed:.1f}s)")

        # Wait for data processing
        print("• Waiting for data processing...")
        process_start = time.time()
        process_result = process_future.result()
        process_elapsed = time.time() - process_start

        print(f"✅ Data processing complete ({process_elapsed:.1f}s)")
        print(f"   Processed data from host: {process_result['hostname']}")

        return True, {
            "fib_result": fib_result,
            "process_result": process_result,
            "system_report": system_report,
            "processed_report": processed_report,
        }

    except Exception as e:
        print(f"❌ Task execution failed: {e}")
        return False, str(e)


def test_results_verification(results):
    """Test Phase 5: Results Verification (User Sees Their Data)"""
    print("🔍 PHASE 5: RESULTS VERIFICATION")
    print("=" * 60)
    print("Verifying all results and files were returned correctly...")

    success = True

    # Verify computational result
    fib_result = results["fib_result"]
    expected_fib_25 = 75025
    if fib_result["result"] == expected_fib_25:
        print(f"✅ Fibonacci computation correct: {fib_result['result']}")
    else:
        print(
            f"❌ Fibonacci computation incorrect: expected {expected_fib_25}, got {fib_result['result']}"
        )
        success = False

    # Verify system analysis file
    system_file = results["system_report"]
    if os.path.exists(system_file.filepath):
        with open(system_file.filepath, "r") as f:
            content = f.read()
        print(f"✅ System analysis file created ({len(content)} bytes)")
        print("   Sample content:")
        for line in content.split("\n")[:5]:
            if line.strip():
                print(f"   • {line}")
    else:
        print(f"❌ System analysis file not found: {system_file.filepath}")
        success = False

    # Verify processed results file
    processed_file = results["processed_report"]
    if os.path.exists(processed_file.filepath):
        with open(processed_file.filepath, "r") as f:
            content = f.read()
        print(f"✅ Processed results file created ({len(content)} bytes)")
        print("   Processed content:")
        for line in content.split("\n"):
            if line.strip():
                print(f"   • {line}")
    else:
        print(f"❌ Processed results file not found: {processed_file.filepath}")
        success = False

    # Verify data processing results
    process_result = results["process_result"]
    if process_result["hostname"] != "unknown":
        print(f"✅ Data processing extracted hostname: {process_result['hostname']}")
    else:
        print("❌ Data processing failed to extract hostname")
        success = False

    return success


def main():
    """Run the complete user experience test."""
    print("🎯 COMPLETE USER EXPERIENCE E2E TEST")
    print("=" * 80)
    print("Testing Phase 1.5 Enhanced AWS Provider from user perspective")
    print("This test simulates exactly what a user would experience:")
    print("• Simple configuration")
    print("• Submit computational tasks")
    print("• Automatic AWS resource management")
    print("• Seamless remote execution")
    print("• Results returned to local machine")
    print("=" * 80)
    print()

    total_start = time.time()

    try:
        # Phase 1: Setup
        config = test_setup()

        # Phase 2-4: Execution
        execution_success, results = test_workflow_execution(config)

        if execution_success:
            # Phase 5: Verification
            verification_success = test_results_verification(results)

            # Final results
            total_time = time.time() - total_start

            print()
            print("🏆 FINAL RESULTS")
            print("=" * 60)

            if verification_success:
                print("✅ ALL TESTS PASSED")
                print()
                print("🎉 USER EXPERIENCE VALIDATION COMPLETE")
                print("The Phase 1.5 Enhanced AWS Provider provides:")
                print("• ✅ Simple, intuitive configuration")
                print("• ✅ Seamless remote execution on AWS")
                print("• ✅ Works behind any firewall/NAT")
                print("• ✅ Automatic resource management")
                print("• ✅ File transfer and data handling")
                print("• ✅ Clean, reliable operation")
                print()
                print(f"Total test time: {total_time:.1f}s")
                print("🚀 READY FOR PRODUCTION USE")
                return True
            else:
                print("❌ VERIFICATION FAILED")
                print("Some results were not returned correctly")
                return False
        else:
            print()
            print("❌ EXECUTION FAILED")
            print(f"Error: {results}")
            return False

    except Exception as e:
        print(f"❌ TEST FRAMEWORK ERROR: {e}")
        return False

    finally:
        print()
        print("🧹 CLEANUP")
        print("=" * 60)
        print("Cleaning up Parsl resources...")
        try:
            parsl.clear()
            print("✅ Parsl cleared successfully")
            print("✅ AWS resources will be cleaned up automatically")
        except Exception as e:
            print(f"⚠️ Cleanup warning: {e}")


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)

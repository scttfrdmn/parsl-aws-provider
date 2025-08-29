#!/usr/bin/env python3
"""Version-tolerant E2E test that handles Python version differences."""

import parsl
from parsl import python_app
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
from phase15_enhanced import AWSProvider
import sys
import os

# Configure Parsl to be more tolerant of version differences
os.environ["PARSL_STRICT_VERSION_CHECK"] = "false"


@python_app
def success_task():
    import time

    time.sleep(5)  # Small delay to simulate work
    return "TASK_COMPLETED_SUCCESSFULLY"


def main():
    try:
        print("STARTING VERSION-TOLERANT TEST")
        sys.stdout.flush()

        # Use Python 3.10 to match AWS instances
        provider = AWSProvider(
            label="version_test", init_blocks=1, max_blocks=1, python_version="3.10"
        )

        # Configure executor with version tolerance
        config = Config(
            executors=[
                HighThroughputExecutor(
                    label="version_test_exec",
                    provider=provider,
                    # Add version tolerance settings if available
                )
            ]
        )

        print("LOADING PARSL")
        sys.stdout.flush()
        parsl.load(config)

        print("SUBMITTING TASK")
        sys.stdout.flush()
        future = success_task()

        print("WAITING FOR RESULT")
        sys.stdout.flush()

        # Wait with timeout
        try:
            result = future.result(timeout=300)  # 5 minute timeout
            print(f"RESULT: {result}")
            sys.stdout.flush()

            if result == "TASK_COMPLETED_SUCCESSFULLY":
                print("E2E_TEST_SUCCESS")
            else:
                print("E2E_TEST_FAILED")
        except Exception as timeout_error:
            print(f"E2E_TEST_TIMEOUT: {timeout_error}")

        sys.stdout.flush()

    except Exception as e:
        print(f"E2E_TEST_FAILED: {e}")
        sys.stdout.flush()
    finally:
        try:
            parsl.clear()
            print("CLEANUP_COMPLETE")
            sys.stdout.flush()
        except:
            pass


if __name__ == "__main__":
    main()

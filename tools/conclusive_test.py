#!/usr/bin/env python3
import parsl
from parsl import python_app
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
from phase15_enhanced import AWSProvider
import sys


@python_app
def success_task():
    import time

    time.sleep(5)  # Small delay to simulate work
    return "TASK_COMPLETED_SUCCESSFULLY"


def main():
    try:
        print("STARTING TEST")
        sys.stdout.flush()

        provider = AWSProvider(
            label="conclusive", init_blocks=1, max_blocks=1, python_version="3.10"
        )
        config = Config(
            executors=[HighThroughputExecutor(label="test_exec", provider=provider)]
        )

        print("LOADING PARSL")
        sys.stdout.flush()
        parsl.load(config)

        print("SUBMITTING TASK")
        sys.stdout.flush()
        future = success_task()

        print("WAITING FOR RESULT")
        sys.stdout.flush()
        result = future.result()

        print(f"RESULT: {result}")
        sys.stdout.flush()

        if result == "TASK_COMPLETED_SUCCESSFULLY":
            print("E2E_TEST_SUCCESS")
        else:
            print("E2E_TEST_FAILED")
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

#!/usr/bin/env python3

import parsl
from parsl import python_app
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
from phase15_enhanced import AWSProvider


@python_app
def compute():
    return "TASK COMPLETED SUCCESSFULLY"


try:
    print("Starting test...")
    provider = AWSProvider(label="debug", init_blocks=1, max_blocks=1)
    config = Config(
        executors=[HighThroughputExecutor(label="debug_exec", provider=provider)]
    )

    print("Loading Parsl...")
    parsl.load(config)

    print("Submitting task...")
    future = compute()

    print("Waiting for result...")
    result = future.result()

    print(f"RESULT: {result}")
    print("SUCCESS")

except Exception as e:
    print(f"ERROR: {e}")

finally:
    try:
        parsl.clear()
        print("Cleaned up")
    except:
        pass

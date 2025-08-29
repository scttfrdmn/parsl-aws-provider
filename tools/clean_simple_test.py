#!/usr/bin/env python3
"""
Clean simple test - no timeouts, no complexity.
"""

import parsl
from parsl import python_app
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
from phase15_enhanced import AWSProvider


@python_app
def simple_compute():
    """Simple computation task."""
    result = 0
    for i in range(1000000):
        result += i * i
    return f"Computation result: {result}"


def main():
    # Simple provider
    provider = AWSProvider(label="simple", init_blocks=1, max_blocks=1)

    config = Config(
        executors=[HighThroughputExecutor(label="simple_exec", provider=provider)]
    )

    # Load and run
    parsl.load(config)
    future = simple_compute()
    result = future.result()

    print(result)
    parsl.clear()
    return True


if __name__ == "__main__":
    main()

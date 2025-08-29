#!/usr/bin/env python3
import parsl
from parsl import python_app
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
from phase15_enhanced import AWSProvider


@python_app
def task():
    return "COMPLETE"


provider = AWSProvider(label="instant", init_blocks=1, max_blocks=1)
config = Config(executors=[HighThroughputExecutor(label="exec", provider=provider)])

parsl.load(config)
future = task()
result = future.result()
print(f"RESULT: {result}")
parsl.clear()

Quickstart
==========

This page provides a quick introduction to using the Parsl Ephemeral AWS Provider.

Basic Usage
----------

Here's a simple example of using the Parsl Ephemeral AWS Provider with Parsl:

.. code-block:: python

   import parsl
   from parsl.config import Config
   from parsl_ephemeral_aws import EphemeralAWSProvider

   # Configure the provider
   provider = EphemeralAWSProvider(
       image_id='ami-0123456789abcdef0',  # Amazon Linux 2 AMI
       instance_type='t3.micro',
       region='us-east-1',
       mode='standard',
       min_blocks=0,
       max_blocks=10,
       worker_init="""
           #!/bin/bash
           pip install parsl
       """
   )

   # Configure Parsl
   config = Config(
       executors=[
           parsl.executors.HighThroughputExecutor(
               label='aws_executor',
               provider=provider,
           )
       ]
   )

   # Initialize Parsl with the configuration
   parsl.load(config)

   # Define a simple Parsl app
   @parsl.python_app
   def hello(name):
       import platform
       return f"Hello {name} from {platform.node()}"

   # Execute the app
   future = hello("World")
   print(future.result())

   # Clean up
   parsl.clear()

Provider Configuration
--------------------

The `EphemeralAWSProvider` accepts many configuration parameters to customize its behavior:

Essential Parameters
~~~~~~~~~~~~~~~~~~~

- **image_id**: AMI ID to use for EC2 instances
- **instance_type**: EC2 instance type
- **region**: AWS region
- **mode**: Operating mode ('standard', 'detached', or 'serverless')
- **min_blocks**: Minimum number of blocks
- **max_blocks**: Maximum number of blocks

Optional Parameters
~~~~~~~~~~~~~~~~~~

- **worker_init**: Script to run on worker startup
- **vpc_id**: Existing VPC ID to use
- **subnet_id**: Existing subnet ID to use
- **security_group_id**: Existing security group ID to use
- **use_spot**: Whether to use spot instances
- **spot_max_price**: Maximum price for spot instances
- **state_file_path**: Path to state file

Operating Modes
--------------

The provider supports three operating modes:

Standard Mode
~~~~~~~~~~~~

Direct client-to-worker communication:

.. code-block:: python

   provider = EphemeralAWSProvider(
       image_id='ami-0123456789abcdef0',
       instance_type='t3.micro',
       region='us-east-1',
       mode='standard',
       min_blocks=0,
       max_blocks=10,
   )

Detached Mode
~~~~~~~~~~~~

Uses a bastion host for long-running workflows:

.. code-block:: python

   provider = EphemeralAWSProvider(
       image_id='ami-0123456789abcdef0',
       instance_type='m5.large',
       region='us-east-1',
       mode='detached',
       bastion_instance_type='t3.micro',
       min_blocks=0,
       max_blocks=10,
   )

Serverless Mode
~~~~~~~~~~~~~

Uses Lambda or ECS for execution:

.. code-block:: python

   provider = EphemeralAWSProvider(
       region='us-east-1',
       mode='serverless',
       compute_type='lambda',
       memory_size=1024,  # Memory in MB
       timeout=300,       # Timeout in seconds
       max_blocks=50,     # Max concurrent Lambda invocations
   )

Running a Simple Workflow
-----------------------

Here's a complete example of a simple workflow:

.. code-block:: python

   import parsl
   from parsl.config import Config
   from parsl_ephemeral_aws import EphemeralAWSProvider
   import time

   # Configure the provider
   provider = EphemeralAWSProvider(
       image_id='ami-0123456789abcdef0',
       instance_type='t3.micro',
       region='us-east-1',
       mode='standard',
       min_blocks=0,
       max_blocks=5,
       worker_init="""
           #!/bin/bash
           pip install parsl numpy
       """
   )

   # Configure Parsl
   config = Config(
       executors=[
           parsl.executors.HighThroughputExecutor(
               label='aws_executor',
               provider=provider,
               max_workers=2,
           )
       ]
   )

   # Initialize Parsl with the configuration
   parsl.load(config)

   # Define apps
   @parsl.python_app
   def compute(x):
       import numpy as np
       import time
       import socket

       # Simulate computation
       time.sleep(2)
       result = np.square(x) + np.sqrt(abs(x))

       # Return result with host information
       return {
           'input': x,
           'result': float(result),
           'hostname': socket.gethostname()
       }

   @parsl.python_app
   def combine(results):
       total = sum(r['result'] for r in results)
       hosts = set(r['hostname'] for r in results)
       return {
           'total': total,
           'num_results': len(results),
           'hosts': list(hosts)
       }

   # Execute workflow
   print("Submitting tasks...")
   start = time.time()

   # Submit 10 compute tasks
   futures = [compute(i) for i in range(-5, 5)]

   # Wait for all compute tasks to complete
   compute_results = [f.result() for f in futures]

   # Combine results
   final = combine(compute_results).result()

   end = time.time()

   # Print results
   print(f"Workflow completed in {end - start:.2f} seconds")
   print(f"Total result: {final['total']}")
   print(f"Number of tasks: {final['num_results']}")
   print(f"Executed on hosts: {', '.join(final['hosts'])}")

   # Clean up
   parsl.clear()

Next Steps
---------

Now that you've seen the basics, explore the following topics:

- :doc:`configuration` - Detailed configuration options
- :doc:`operating_modes` - Learn about different operating modes
- :doc:`examples` - More examples and usage patterns
- :doc:`troubleshooting` - Troubleshooting common issues

.. SPDX-License-Identifier: Apache-2.0
.. SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors

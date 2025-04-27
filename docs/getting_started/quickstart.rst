Quickstart
==========

This guide will help you quickly set up and run your first workflow using the Parsl Ephemeral AWS Provider.

Basic Setup
----------

First, let's create a minimal configuration for running a Parsl workflow on AWS:

.. code-block:: python

   import parsl
   from parsl.config import Config
   from parsl.executors import HighThroughputExecutor
   from parsl_ephemeral_aws import EphemeralAWSProvider

   # Configure the Ephemeral AWS Provider
   provider = EphemeralAWSProvider(
       image_id='ami-0c55b159cbfafe1f0',  # Amazon Linux 2 AMI (replace with current AMI)
       instance_type='t3.micro',          # Small instance type for testing
       region='us-west-2',                # AWS region to use
       
       # Resource allocation parameters
       init_blocks=1,                     # Start with one block of resources
       min_blocks=0,                      # Allow scaling down to zero
       max_blocks=1,                      # Limit to one block for testing
       
       # Basic AWS configuration
       use_public_ips=True,               # Use public IPs for connectivity
       key_name=None,                     # No SSH key for this example
   )

   # Create the Parsl configuration
   config = Config(
       executors=[
           HighThroughputExecutor(
               label='aws_executor',
               provider=provider,
           )
       ]
   )

   # Load the configuration
   parsl.load(config)

Hello World App
-------------

Now let's define a simple "Hello World" app and run it:

.. code-block:: python

   # Define a simple Python app
   @parsl.python_app
   def hello():
       import socket
       return f"Hello from {socket.gethostname()}"

   # Run the app
   future = hello()
   
   # Wait for the result and print it
   print(f"Result: {future.result()}")

You should see output similar to:

.. code-block:: text

   Result: Hello from ip-172-31-23-45.us-west-2.compute.internal

Cleanup
------

Parsl will automatically clean up AWS resources when you exit. To force cleanup explicitly:

.. code-block:: python

   # Clean up all AWS resources
   parsl.dfk().cleanup()

Complete Example
--------------

Here's a complete example that you can run:

.. code-block:: python

   import parsl
   from parsl.config import Config
   from parsl.executors import HighThroughputExecutor
   from parsl_ephemeral_aws import EphemeralAWSProvider
   
   # Configure the AWS Provider
   provider = EphemeralAWSProvider(
       image_id='ami-0c55b159cbfafe1f0',    # Replace with current Amazon Linux 2 AMI
       instance_type='t3.micro',
       region='us-west-2',
       init_blocks=1,
       min_blocks=0,
       max_blocks=1,
       use_public_ips=True,
   )
   
   # Create Parsl configuration
   config = Config(
       executors=[
           HighThroughputExecutor(
               label='aws_executor',
               provider=provider,
               max_workers=2,
           )
       ]
   )
   
   # Load the configuration
   parsl.load(config)
   
   # Define some apps
   @parsl.python_app
   def hello(name):
       import socket
       host = socket.gethostname()
       return f"Hello {name} from {host}"
   
   @parsl.python_app
   def add(a, b):
       return a + b
   
   # Run the apps
   hello_future = hello("World")
   add_future = add(2, 3)
   
   # Wait for and print results
   print(hello_future.result())
   print(f"2 + 3 = {add_future.result()}")
   
   # Clean up
   parsl.dfk().cleanup()

Explanation
----------

This example:

1. Configures the Ephemeral AWS Provider with a minimal setup
2. Creates a Parsl configuration with a HighThroughputExecutor
3. Defines two simple apps: one that returns a greeting and one that adds numbers
4. Runs the apps and waits for their results
5. Explicitly cleans up all AWS resources

Next Steps
---------

After successfully running this example, you can:

1. Learn more about the provider's :doc:`../basic_concepts`
2. Explore different :doc:`../operating_modes/index`
3. See the complete :doc:`../user_guide/configuration` options
4. Check out more complex :doc:`../examples/index`
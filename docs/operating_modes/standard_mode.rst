Standard Mode
============

Standard Mode is the simplest operating mode in the Parsl Ephemeral AWS Provider, featuring direct communication between your client and worker nodes.

.. figure:: ../images/standard_mode_architecture.png
   :alt: Standard Mode Architecture
   :align: center
   :width: 80%

   Architecture diagram of Standard Mode showing direct client-to-worker communication

Overview
-------

In Standard Mode, your local client machine:

1. Creates AWS resources (VPC, subnets, security groups, EC2 instances, etc.)
2. Communicates directly with worker nodes for task submission and data transfer
3. Monitors worker status and performs scaling operations
4. Cleans up all resources when the workflow completes

This mode is best suited for:

* Development and testing
* Small to medium-sized workflows
* Scenarios where your client can maintain a constant connection to AWS

Configuration
-----------

Here's a basic configuration for Standard Mode:

.. code-block:: python

   from parsl.config import Config
   from parsl.executors import HighThroughputExecutor
   from parsl_ephemeral_aws import EphemeralAWSProvider

   provider = EphemeralAWSProvider(
       # Standard Mode is the default (no need to specify mode='standard')
       image_id='ami-12345678',
       instance_type='t3.medium',
       region='us-west-2',

       # Block parameters
       init_blocks=1,
       min_blocks=0,
       max_blocks=10,

       # Network settings
       use_public_ips=True,  # Important for direct communication

       # Optional: spot instance settings
       use_spot_instances=True,
       spot_max_price_percentage=80,
   )

   config = Config(
       executors=[
           HighThroughputExecutor(
               label='aws_executor',
               provider=provider,
           )
       ]
   )

Key Configuration Options
----------------------

``use_public_ips`` (Boolean)
  Set to ``True`` to allow your client to communicate with workers over the public internet. Required if your client is not in the same VPC.

``vpc_id`` (String, optional)
  Specify an existing VPC ID to use instead of creating a new VPC. Useful for integrating with existing infrastructure.

``subnet_id`` (String, optional)
  Specify an existing subnet ID instead of creating a new subnet.

``security_group_id`` (String, optional)
  Specify an existing security group ID instead of creating a new security group.

``worker_init`` (String, optional)
  Shell commands to run on instances at startup, useful for installing dependencies.

Operation and Workflow
------------------

During operation, Standard Mode follows this workflow:

1. **Initialization**:
   * Provider creates network resources (VPC, subnet, security group) if not provided
   * Provider establishes communication channels with AWS

2. **Resource Provisioning**:
   * EC2 instances are launched based on the initial block count
   * Each instance runs worker processes based on its resources

3. **Task Execution**:
   * Tasks are submitted directly from the client to workers
   * Results are returned directly to the client

4. **Scaling**:
   * Provider monitors workload and creates/destroys instances as needed
   * Scaling decisions are made by the client based on task queue length

5. **Termination**:
   * When the workflow completes or `dfk.cleanup()` is called, all resources are terminated
   * VPC, subnets, and security groups are deleted unless they were provided by the user

Advantages and Limitations
-----------------------

Advantages:
  * Simplest setup with minimal complexity
  * Lowest latency for task submission and results
  * Direct control and visibility of resources
  * Good for interactive development and debugging

Limitations:
  * Client must remain connected for the duration of the workflow
  * Not suitable for long-running workflows if the client may disconnect
  * No persistent state by default (though state persistence can be enabled)
  * Client IP must have network access to worker instances

Best Practices
------------

1. **Network Connectivity**:
   * Ensure your client can connect to the AWS region you're using
   * Use `use_public_ips=True` unless your client is in the same VPC

2. **Resource Management**:
   * Start with small instance types for testing
   * Use spot instances when possible to reduce costs
   * Set reasonable `min_blocks` and `max_blocks` to control costs

3. **Error Handling**:
   * Enable state persistence for recovery capability
   * Monitor client connectivity to avoid losing worker contact

4. **Security**:
   * Consider using a custom security group with restricted access
   * Use IAM roles with minimum necessary permissions

Example: Complete Workflow
------------------------

Here's a complete example showing a Standard Mode workflow:

.. code-block:: python

   import parsl
   from parsl.config import Config
   from parsl.executors import HighThroughputExecutor
   from parsl_ephemeral_aws import EphemeralAWSProvider
   import time

   # Configure AWS Provider in Standard Mode
   provider = EphemeralAWSProvider(
       image_id='ami-0c55b159cbfafe1f0',  # Amazon Linux 2 (update to current AMI)
       instance_type='t3.micro',
       region='us-west-2',
       init_blocks=1,
       min_blocks=0,
       max_blocks=4,
       use_public_ips=True,
       worker_init='''
           # Install dependencies
           sudo yum update -y
           sudo yum install -y python3-devel
           python3 -m pip install --upgrade pip
           python3 -m pip install numpy scipy
       ''',
       # Enable state persistence for recovery
       state_store='parameter_store',
       state_prefix='/parsl/demo',
   )

   # Create Parsl configuration
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

   # Define a compute-intensive app
   @parsl.python_app
   def compute(x):
       import numpy as np
       import time
       import socket

       # Simulate work
       time.sleep(2)
       result = np.sum([x**i for i in range(1000)])

       return {
           'input': x,
           'result': result,
           'hostname': socket.gethostname()
       }

   # Submit multiple tasks
   results = []
   for i in range(20):
       results.append(compute(i))

   # Print results as they complete
   for r in results:
       print(f"Task result from {r.result()['hostname']}: {r.result()['result']}")

   # Clean up all AWS resources
   parsl.dfk().cleanup()

Next Steps
---------

* Learn about :doc:`detached_mode` for disconnection-tolerant workflows
* Explore :doc:`serverless_mode` for pay-per-use Lambda and ECS execution
* See :doc:`../user_guide/spot_handling` for handling spot instance interruptions
* Check out :doc:`../advanced_topics/cost_optimization` for reducing AWS costs

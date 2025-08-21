Spot Handling
=============

This guide covers the Parsl Ephemeral AWS Provider's robust mechanisms for handling AWS Spot Instances, including interruption detection, mitigation strategies, and recovery workflows.

.. figure:: ../images/spot_handling.svg
   :alt: Spot Handling Architecture
   :align: center
   :width: 80%
   :figclass: align-center

   Spot instance handling architecture showing interruption and recovery flow

Introduction to Spot Instances
---------------------------

AWS Spot Instances provide significant cost savings (up to 90% compared to On-Demand prices) by utilizing unused EC2 capacity. However, they come with a trade-off: AWS can reclaim these instances with only a 2-minute warning when capacity is needed elsewhere.

The Parsl Ephemeral AWS Provider implements sophisticated mechanisms to:

1. Detect spot instance interruption signals
2. Save task and workflow state prior to interruption
3. Replace interrupted instances with new capacity
4. Resume tasks that were interrupted
5. Optimize spot instance selection for better availability

Enabling Spot Instances
--------------------

You can enable spot instances with minimal configuration:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic configuration
       region='us-west-2',
       instance_type='m5.large',

       # Enable spot instances
       use_spot_instances=True,
       spot_max_price_percentage=80,  # 80% of on-demand price
   )

But for better reliability and advanced features, consider a more comprehensive configuration:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic configuration
       region='us-west-2',
       instance_type='m5.large',

       # Spot configuration
       use_spot_instances=True,
       spot_max_price_percentage=80,
       spot_interruption_behavior='terminate',  # 'terminate', 'stop', or 'hibernate'

       # Spot Fleet for better availability
       use_spot_fleet=True,
       instance_types=[
           'm5.large',
           'm5a.large',
           'm5n.large',
           'c5.large',
           'r5.large'
       ],
       allocation_strategy='capacityOptimized',

       # Enable state persistence for recovery
       state_store='parameter_store',
       state_prefix='/parsl/spot-workflow',

       # Enable interruption detection
       spot_interruption_detection=True,
       spot_interruption_handler='terminate_and_replace',
   )

Spot Configuration Parameters
-------------------------

Core Parameters
~~~~~~~~~~~~

``use_spot_instances`` (Boolean, default: False)
  Whether to use EC2 Spot Instances instead of On-Demand Instances.

``spot_max_price_percentage`` (Integer, default: 100)
  Maximum price for spot instances as a percentage of the on-demand price.

``spot_interruption_behavior`` (String, default: 'terminate')
  Action AWS should take when a spot instance is interrupted. Options are:
  - 'terminate': Instance is terminated (quickest, no additional charges)
  - 'stop': Instance is stopped (preserved EBS state, quicker restart)
  - 'hibernate': Instance is hibernated (preserves memory state, quicker restart)

Spot Fleet Parameters
~~~~~~~~~~~~~~~~

``use_spot_fleet`` (Boolean, default: False)
  Whether to use AWS Spot Fleet for managing spot instances.

``instance_types`` (List[String], optional)
  List of instance types to use with Spot Fleet. Diversifying instance types significantly improves availability.

``allocation_strategy`` (String, default: 'lowestPrice')
  Strategy for allocating spot instances. Options are:
  - 'lowestPrice': Select lowest-priced instance types (cost-optimized)
  - 'diversified': Distribute across multiple instance types (availability-optimized)
  - 'capacityOptimized': Select instance types with lowest interruption probability

Interruption Handling Parameters
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``spot_interruption_detection`` (Boolean, default: True)
  Whether to detect and handle spot instance interruptions.

``spot_interruption_handler`` (String, default: 'terminate_and_replace')
  Handling strategy when interruption is detected. Options are:
  - 'terminate_and_replace': Terminate the instance and replace it
  - 'checkpoint_and_terminate': Save task state, terminate, and resume elsewhere
  - 'restart_task': Simply restart the interrupted task on a new instance

Interruption Detection and Handling
-------------------------------

The provider implements multiple mechanisms to detect spot interruptions:

1. **EC2 Instance Metadata Service Monitoring**
   * Worker processes periodically check the EC2 Instance Metadata Service
   * Detects the interruption signal 2 minutes before termination
   * Most reliable method with lowest latency

2. **CloudWatch Events Integration**
   * Provider subscribes to EC2 Spot Instance Interruption Warning events
   * Events are delivered to an SQS queue and processed by the provider
   * Provides redundancy for the metadata service monitoring

3. **Instance State Monitoring**
   * Provider monitors instance state changes
   * Detects when instances have been terminated or stopped
   * Fallback method if other detection mechanisms fail

When an interruption is detected, the provider follows this workflow:

1. **Notification Phase**
   * Interruption is detected and logged
   * Current tasks on the instance are identified
   * Executor is notified about the imminent loss of resources

2. **Checkpoint Phase**
   * For checkpointable tasks, state is persisted
   * Task status and progress information is saved
   * Checkpoint location is recorded in the provider state

3. **Transition Phase**
   * Based on interruption behavior, the instance is handled appropriately
   * In most cases, the provider lets AWS terminate the instance
   * Task state is marked as "interrupted" rather than "failed"

4. **Recovery Phase**
   * Provider initiates replacement capacity
   * Interrupted tasks are re-queued or resumed
   * New instances are provisioned according to the scaling policy

Spot Fleet Management
------------------

Spot Fleet provides significant advantages over individual spot requests:

1. **Diversity and Resilience**
   * Spot Fleet can use multiple instance types and availability zones
   * Diversification reduces the overall interruption probability
   * Provider can continue operating even if one instance type is unavailable

2. **Capacity-Optimized Allocation**
   * AWS's capacity-optimized allocation strategy selects instance types with lowest interruption probability
   * Provider can automatically adjust to market conditions

3. **Automatic Replacement**
   * Spot Fleet automatically attempts to replace interrupted instances
   * Provider works with Spot Fleet to maintain target capacity
   * Faster recovery after interruptions

Configuring for Best Availability
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

For maximum spot instance availability, we recommend:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic configuration
       region='us-west-2',

       # Enable Spot Fleet
       use_spot_instances=True,
       use_spot_fleet=True,

       # Diversify instance types
       instance_types=[
           # Multiple instance families
           'm5.large', 'm5a.large', 'm5n.large',   # General purpose
           'c5.large', 'c5a.large', 'c5n.large',   # Compute optimized
           'r5.large', 'r5a.large', 'r5n.large',   # Memory optimized

           # Multiple sizes within families
           'm5.large', 'm5.xlarge', 'm5.2xlarge',
       ],

       # Optimize for availability
       allocation_strategy='capacityOptimized',

       # Use multiple availability zones
       availability_zones=['us-west-2a', 'us-west-2b', 'us-west-2c'],
   )

State Persistence for Recovery
---------------------------

Proper handling of spot interruptions requires state persistence. The provider uses the configured state store to:

1. Save the state of tasks before interruption
2. Track which instances and tasks were interrupted
3. Maintain workflow progress information
4. Enable resuming tasks after interruption

For spot-intensive workflows, we recommend using Parameter Store or S3:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic spot configuration
       use_spot_instances=True,
       use_spot_fleet=True,

       # State persistence for recovery
       state_store='parameter_store',  # or 's3' for larger state
       state_prefix='/parsl/spot-workflow',
       state_cleanup='success',  # Only clean up on successful completion

       # Checkpoint configuration
       checkpoint_mode='task',  # 'task', 'block', or 'workflow'
       checkpoint_interval=300,  # seconds between checkpoints
   )

Checkpointing Parameters
~~~~~~~~~~~~~~~~~~~~

``checkpoint_mode`` (String, default: 'task')
  Level at which checkpointing occurs. Options are:
  - 'task': Individual tasks are checkpointed
  - 'block': All tasks in a block are checkpointed together
  - 'workflow': Entire workflow is checkpointed

``checkpoint_interval`` (Integer, default: 300)
  Seconds between automatic checkpoints (independent of interruption).

``checkpoint_files`` (Boolean, default: True)
  Whether to checkpoint files created by tasks.

Task-Level Spot Handling
---------------------

You can implement task-level spot handling for greater control:

.. code-block:: python

   @parsl.python_app(checkpointable=True)
   def spot_resilient_task(data):
       import time
       import os
       import json
       import pickle

       # Get the checkpoint ID (if any)
       checkpoint_id = os.environ.get('PARSL_CHECKPOINT_ID')

       # Initialize state
       state = {
           'progress': 0,
           'result': None,
           'last_processed': 0
       }

       # Load checkpoint if available
       if checkpoint_id:
           checkpoint_path = f"/tmp/checkpoint_{checkpoint_id}.pkl"
           if os.path.exists(checkpoint_path):
               with open(checkpoint_path, 'rb') as f:
                   state = pickle.load(f)

       # Process data from last checkpoint
       for i in range(state['last_processed'], len(data)):
           # Do some processing
           result = process_item(data[i])

           # Update state
           state['progress'] = (i + 1) / len(data) * 100
           state['last_processed'] = i + 1
           state['result'] = result

           # Create checkpoint periodically
           if i % 10 == 0:
               with open(f"/tmp/checkpoint_{os.getpid()}.pkl", 'wb') as f:
                   pickle.dump(state, f)

           # Check for spot interruption
           if check_spot_interruption():
               # Final checkpoint before interruption
               with open(f"/tmp/checkpoint_{os.getpid()}.pkl", 'wb') as f:
                   pickle.dump(state, f)
               break

       return state['result']

Spot Instance Monitoring
---------------------

The provider includes monitoring capabilities for spot instances:

1. **Interruption Metrics**
   * Tracks interruption frequency by instance type and availability zone
   * Records interruption history and duration
   * Calculates mean time between interruptions

2. **Cost Tracking**
   * Monitors spot prices versus on-demand prices
   * Calculates cost savings from using spot instances
   * Tracks spot instance hours used

You can access this information programmatically:

.. code-block:: python

   # Get spot instance metrics
   spot_metrics = provider.get_spot_metrics()

   # Print interruption statistics
   print(f"Total interruptions: {spot_metrics['total_interruptions']}")
   print(f"Interruption rate: {spot_metrics['interruption_rate']:.2f}%")
   print(f"Cost savings: {spot_metrics['cost_savings']:.2f}%")

   # Get interruption history by instance type
   history = provider.get_interruption_history()
   for instance_type, data in history.items():
       print(f"{instance_type}: {data['count']} interruptions")

Best Practices
------------

1. **Instance Diversification**
   * Use multiple instance types across different families
   * Distribute across availability zones
   * Use the capacity-optimized allocation strategy

2. **Checkpointing Strategy**
   * Implement frequent checkpoints for long-running tasks
   * Store checkpoints in durable storage (S3, Parameter Store)
   * Make tasks idempotent for safe retries

3. **Pricing Strategy**
   * Use a spot_max_price_percentage appropriate for your use case
   * For critical workloads, consider higher percentages for better availability
   * For cost-sensitive workloads, use lower percentages and handle more interruptions

4. **Instance Selection**
   * Prefer newer generation instances (usually better availability)
   * Avoid the most popular instance types during peak hours
   * Consider less-popular regions for better availability

5. **Error Handling**
   * Implement robust error handling in your tasks
   * Use state persistence even for non-critical workflows
   * Test your workflow with simulated interruptions

Advanced Spot Strategies
---------------------

Hybrid On-Demand and Spot Strategy
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

For workloads with critical and non-critical components:

.. code-block:: python

   # Spot fleet for cost-efficient computing
   spot_provider = EphemeralAWSProvider(
       region='us-west-2',
       use_spot_instances=True,
       use_spot_fleet=True,
       instance_types=['c5.large', 'c5a.large', 'c5n.large'],
       allocation_strategy='capacityOptimized',
   )

   # On-demand for critical tasks
   ondemand_provider = EphemeralAWSProvider(
       region='us-west-2',
       instance_type='m5.large',
       use_spot_instances=False,  # Explicit on-demand
   )

   config = Config(
       executors=[
           HighThroughputExecutor(
               label='spot_executor',
               provider=spot_provider,
           ),
           HighThroughputExecutor(
               label='ondemand_executor',
               provider=ondemand_provider,
           )
       ]
   )

   # Use appropriate executor for each task type
   @parsl.python_app(executors=['spot_executor'])
   def non_critical_task():
       # This task can be interrupted
       pass

   @parsl.python_app(executors=['ondemand_executor'])
   def critical_task():
       # This task needs guaranteed resources
       pass

Spot Block Strategy
~~~~~~~~~~~~~~~

For workloads with predictable duration, consider AWS Spot Blocks (spot instances with 1-6 hour duration guarantee):

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic configuration
       region='us-west-2',
       instance_type='m5.large',

       # Spot Block configuration
       use_spot_instances=True,
       use_spot_blocks=True,
       spot_block_duration=2,  # Hours (1-6)

       # State persistence still recommended
       state_store='parameter_store',
   )

Real-Time Spot Market Integration
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The provider can dynamically adjust to spot market conditions:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic configuration
       region='us-west-2',

       # Dynamic market adaptation
       use_spot_instances=True,
       spot_market_monitoring=True,
       spot_pricing_update_interval=300,  # Seconds

       # Use multiple instance types
       instance_types=[
           'm5.large', 'm5a.large', 'c5.large', 'r5.large'
       ],

       # Select instance type dynamically based on market
       spot_selection_strategy='lowest_interruption_probability',  # or 'lowest_price'
   )

Example: Complete Spot Workflow
----------------------------

Here's a comprehensive example of a spot-resilient workflow:

.. code-block:: python

   import parsl
   import time
   from parsl.config import Config
   from parsl.executors import HighThroughputExecutor
   from parsl_ephemeral_aws import EphemeralAWSProvider

   # Configure the provider for optimal spot usage
   provider = EphemeralAWSProvider(
       # Region and basic configuration
       region='us-west-2',

       # Spot configuration
       use_spot_instances=True,
       use_spot_fleet=True,
       instance_types=[
           'm5.large', 'm5a.large', 'c5.large', 'r5.large'
       ],
       allocation_strategy='capacityOptimized',
       spot_max_price_percentage=90,

       # Block parameters
       init_blocks=1,
       min_blocks=0,
       max_blocks=10,

       # State persistence
       state_store='parameter_store',
       state_prefix='/parsl/spot-demo',

       # Interruption handling
       spot_interruption_detection=True,
       spot_interruption_handler='checkpoint_and_terminate',

       # Worker initialization
       worker_init='''
           # Install dependencies
           sudo yum update -y
           sudo yum install -y python3-devel
           python3 -m pip install --upgrade pip
           python3 -m pip install numpy pandas scikit-learn
       ''',
   )

   # Parsl configuration
   config = Config(
       executors=[
           HighThroughputExecutor(
               label='spot_executor',
               provider=provider,
           )
       ]
   )

   parsl.load(config)

   # Define a checkpointable task
   @parsl.python_app(checkpointable=True)
   def process_chunk(chunk_id, data_size=1000, checkpoint_interval=10):
       import numpy as np
       import time
       import os
       import json
       import random

       # Simulated data
       np.random.seed(chunk_id)
       data = np.random.rand(data_size, 100)

       # Initialize or load state
       checkpoint_file = f"/tmp/checkpoint_chunk_{chunk_id}.json"

       if os.path.exists(checkpoint_file):
           with open(checkpoint_file, 'r') as f:
               state = json.load(f)
           print(f"Loaded checkpoint at iteration {state['iteration']}")
       else:
           state = {
               "iteration": 0,
               "result": 0.0
           }

       # Process data with checkpoints
       for i in range(state["iteration"], data_size):
           # Simulate processing
           row_result = np.mean(data[i]) * np.sum(data[i])
           state["result"] += row_result
           state["iteration"] = i + 1

           # Create checkpoint at intervals
           if (i + 1) % checkpoint_interval == 0:
               with open(checkpoint_file, 'w') as f:
                   json.dump(state, f)
               print(f"Created checkpoint at iteration {state['iteration']}")

           # Simulate work
           time.sleep(0.1)

           # Randomly simulate spot interruption (1% chance)
           if random.random() < 0.01:
               print(f"Simulating spot interruption at iteration {i+1}")
               # Save final checkpoint before "interruption"
               with open(checkpoint_file, 'w') as f:
                   json.dump(state, f)
               # Raise exception to simulate failure
               raise Exception("Spot instance interrupted!")

       # Final result
       return {
           "chunk_id": chunk_id,
           "iterations_completed": state["iteration"],
           "result": state["result"]
       }

   # Submit multiple tasks
   results = []
   for i in range(20):
       results.append(process_chunk(i))

   # Monitor results with interruption awareness
   successful = 0
   retries = 0

   while results and successful + retries < 20:
       time.sleep(5)

       # Check each future
       completed = []
       for i, r in enumerate(results):
           if r.done():
               completed.append(i)
               try:
                   result = r.result()
                   print(f"Task {result['chunk_id']} completed with result {result['result']:.2f}")
                   successful += 1
               except Exception as e:
                   if "Spot instance interrupted" in str(e):
                       print(f"Task {i} was interrupted, resubmitting...")
                       results[i] = process_chunk(i)  # Resubmit
                       retries += 1
                   else:
                       print(f"Task {i} failed with error: {e}")

       # Remove completed futures
       for i in sorted(completed, reverse=True):
           del results[i]

   print(f"All tasks completed. Successful: {successful}, Retries due to interruption: {retries}")

   # Clean up
   parsl.dfk().cleanup()

Next Steps
---------

* Learn about :doc:`../operating_modes/index` for different execution models
* Explore :doc:`../advanced_topics/cost_optimization` for overall AWS cost strategies
* See :doc:`state_persistence` for more details on state storage options
* Check out :doc:`../examples/scientific_computing` for real-world scientific workflow examples

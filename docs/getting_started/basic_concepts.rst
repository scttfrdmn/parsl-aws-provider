Basic Concepts
=============

This guide introduces the key concepts and terminology used in the Parsl Ephemeral AWS Provider.

Core Concepts
-----------

Ephemeral Resources
~~~~~~~~~~~~~~~~

The term "ephemeral" in this provider refers to AWS resources that are:

* Created on-demand when needed for computation
* Automatically destroyed when no longer needed
* Not maintained between workflow executions (unless state persistence is enabled)

This approach minimizes costs by ensuring you only pay for the resources you actively use.

Blocks
~~~~~

A "block" is a unit of compute resources, typically corresponding to one or more AWS instances. The provider manages blocks by:

* Creating them when workload increases (scaling out)
* Terminating them when workload decreases (scaling in)
* Tracking their status and health

The provider's ``min_blocks``, ``max_blocks``, and ``nodes_per_block`` parameters control block allocation.

Operating Modes
~~~~~~~~~~~~

The provider supports three distinct operating modes:

* **Standard Mode**: Direct connection between your client and AWS workers
* **Detached Mode**: Uses a bastion/coordinator instance for connection persistence
* **Serverless Mode**: Uses AWS Lambda and/or ECS/Fargate for true serverless execution

Each mode has different characteristics regarding connection requirements, durability, and cost.

State Persistence
~~~~~~~~~~~~~~

State persistence allows the provider to save and recover its state, which can be useful for:

* Handling client disconnections
* Recovering from failures
* Resuming workflows across sessions

The provider supports multiple state persistence backends:

* **Parameter Store**: AWS Systems Manager Parameter Store
* **S3**: Amazon Simple Storage Service
* **File**: Local file storage

AWS Resource Types
---------------

EC2 Instances
~~~~~~~~~~

Amazon EC2 (Elastic Compute Cloud) provides resizable compute capacity in the form of virtual servers (instances). The provider can manage:

* On-demand instances: Pay as you go with no upfront commitment
* Spot instances: Unused EC2 capacity at up to 90% discount with potential interruptions

Spot Fleet
~~~~~~~~

AWS Spot Fleet is a collection of Spot Instances that provides:

* Automatic selection of instance types to meet capacity and price constraints
* Automatic recovery from spot instance interruptions
* Cost optimization through diversification of instance types

Lambda Functions
~~~~~~~~~~~~~

AWS Lambda provides serverless, event-driven compute that:

* Requires zero infrastructure management
* Scales automatically from zero to thousands of concurrent executions
* Charges only for compute time consumed

ECS/Fargate
~~~~~~~~~

Amazon Elastic Container Service (ECS) with AWS Fargate allows running containers without managing servers:

* No EC2 instances to manage
* Per-second billing
* Automatic scaling and infrastructure management

Key Terms
--------

Worker
~~~~~

A worker is a process running on a compute resource that executes Parsl tasks. Multiple workers can run on a single node.

Block
~~~~

As described above, a block is a unit of resources (typically instances) that the provider allocates.

Task
~~~~

A task is a unit of computation in Parsl, represented as a Python function decorated with ``@python_app`` or ``@bash_app``.

Executor
~~~~~~~

An executor is responsible for managing task execution. The provider works with Parsl's executors, particularly the ``HighThroughputExecutor``.

Data Flow Kernel (DFK)
~~~~~~~~~~~~~~~~~~~

The DFK is Parsl's runtime that manages the execution of apps and data dependencies.

Scaling Concepts
--------------

Provider Parameters
~~~~~~~~~~~~~~~~

Key scaling parameters include:

* ``init_blocks``: Initial number of blocks to provision
* ``min_blocks``: Minimum number of blocks to maintain
* ``max_blocks``: Maximum number of blocks allowed
* ``nodes_per_block``: Number of compute nodes per block

Scaling Strategies
~~~~~~~~~~~~~~~

The provider scales resources based on workload:

* **Scale Out**: Adding blocks when there are more tasks than can be processed
* **Scale In**: Removing blocks when resources are underutilized
* **Maintenance**: Replacing blocks that become unhealthy

Next Steps
---------

Now that you understand the basic concepts, you can:

1. Explore different :doc:`../operating_modes/index`
2. Learn about :doc:`../user_guide/configuration` options
3. See how to implement :doc:`../user_guide/state_persistence`
4. Check out various :doc:`../examples/index`

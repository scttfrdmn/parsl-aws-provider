Resource Management
==================

This guide covers how the Parsl Ephemeral AWS Provider manages AWS resources throughout the workflow lifecycle, providing insights into resource creation, tracking, scaling, and cleanup.

.. figure:: ../images/resource_management.svg
   :alt: Resource Management Lifecycle
   :align: center
   :width: 80%
   :figclass: align-center

   Resource lifecycle management showing creation, tracking, scaling, and cleanup phases

Resource Lifecycle
---------------

The provider manages AWS resources through a complete lifecycle:

1. **Resource Planning**
   * Provider evaluates configuration and requirements
   * Resource specifications are determined
   * Dependencies between resources are mapped

2. **Resource Creation**
   * Network infrastructure (VPC, subnets, security groups)
   * Compute resources (EC2 instances, Spot Fleet, Lambda functions)
   * Supporting resources (IAM roles, CloudWatch alarms)

3. **Resource Tracking**
   * All created resources are tracked with unique identifiers
   * Resource state and health are monitored
   * Dependencies between resources are maintained

4. **Resource Scaling**
   * Resources are scaled based on workload and configuration
   * New resources are created as needed
   * Underutilized resources are terminated

5. **Resource Cleanup**
   * All resources are terminated when no longer needed
   * Cleanup occurs in dependency order to prevent errors
   * Resource states are verified during cleanup

Resource Creation
--------------

By default, the provider creates all necessary resources:

Network Resources
~~~~~~~~~~~~~~

1. **Virtual Private Cloud (VPC)**
   * Default CIDR block: 10.0.0.0/16
   * Configurable with `vpc_cidr_block` parameter
   * Tagged for identification and tracking

2. **Subnet**
   * Default CIDR block: 10.0.0.0/24
   * Configurable with `subnet_cidr_block` parameter
   * Created in the selected availability zone

3. **Internet Gateway**
   * Attached to the VPC for internet connectivity
   * Required for worker nodes to access the internet
   * Required for client to connect to workers

4. **Route Tables**
   * Configured to route traffic through the internet gateway
   * Associated with the subnet
   * Allows bidirectional internet access

5. **Security Groups**
   * Default rules allow SSH access and Parsl communication
   * Custom rules can be specified with `security_group_ingress`
   * Self-referencing rules for worker-to-worker communication

Compute Resources
~~~~~~~~~~~~~~

1. **EC2 Instances**
   * Created according to configuration (instance type, count, etc.)
   * AMI selection based on region and configuration
   * Tagged with provider, workflow, and block identifiers

2. **Spot Instances**
   * If enabled, uses spot requests or spot fleet
   * Configured with interruption behavior and maximum price
   * Tagged with additional spot-specific identifiers

3. **Lambda Functions**
   * In Serverless Mode, Lambda functions are created
   * Configured with memory, timeout, and runtime
   * Packaged with necessary dependencies

4. **ECS Tasks**
   * In Serverless Mode, ECS tasks may be created
   * Task definitions specify container requirements
   * Executed in Fargate or EC2 mode as configured

Supporting Resources
~~~~~~~~~~~~~~~~

1. **IAM Roles and Policies**
   * Role for EC2 instances with required permissions
   * Role for Lambda functions with execution permissions
   * Role for ECS tasks with container execution permissions

2. **CloudWatch Resources**
   * Log groups for instance logs
   * Alarms for resource monitoring
   * Event rules for spot interruption detection

Using Existing Resources
---------------------

For greater control or to integrate with existing infrastructure, you can provide pre-existing resources:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic configuration
       region='us-west-2',
       instance_type='t3.medium',

       # Existing network resources
       vpc_id='vpc-12345678',
       subnet_id='subnet-12345678',
       security_group_id='sg-12345678',

       # Existing IAM resources
       iam_instance_profile='MyInstanceProfile',

       # Do not delete provided resources on cleanup
       preserve_vpc=True,
       preserve_subnet=True,
       preserve_security_group=True,
   )

This approach has several benefits:
* Integration with existing infrastructure
* More precise control over network configuration
* Ability to use specialized VPC configurations
* Resource reuse across multiple workflows

Resource Tagging
-------------

The provider tags all created resources for identification, tracking, and management:

Default Tags
~~~~~~~~~~

All resources receive these tags by default:

* `Name`: Resource name with provider and workflow identifiers
* `ParslWorkflow`: Workflow identifier
* `ParslProvider`: Provider identifier
* `ParslExecutor`: Executor identifier
* `ParslBlock`: Block identifier (for compute resources)

Custom Tags
~~~~~~~~~

You can specify additional tags to apply to all resources:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic configuration
       region='us-west-2',
       instance_type='t3.medium',

       # Custom tags
       tags={
           'Project': 'GenomeAnalysis',
           'Environment': 'Production',
           'Department': 'Bioinformatics',
           'CostCenter': 'CC123456',
           'Owner': 'jane.doe@example.com',
       },
   )

These tags are useful for:
* Cost allocation and tracking
* Resource organization
* Access control policies
* Automation and scripting
* Compliance and auditing

Resource Scaling
-------------

The provider dynamically scales resources based on workload and configuration:

Scaling Parameters
~~~~~~~~~~~~~~

``min_blocks`` (Integer, default: 0)
  Minimum number of blocks to maintain, even when idle.

``max_blocks`` (Integer, default: 10)
  Maximum number of blocks that can be provisioned.

``init_blocks`` (Integer, default: 1)
  Initial number of blocks to provision.

``nodes_per_block`` (Integer, default: 1)
  Number of worker nodes to provision per block.

``parallelism`` (Float, default: 1.0)
  Scaling factor for parallelism. Values >1 provision more resources than strictly needed, <1 provision fewer resources.

Scaling Workflow
~~~~~~~~~~~~

1. **Initial Scaling**
   * The provider creates `init_blocks` blocks when initialized
   * These blocks are provisioned even before tasks are submitted

2. **Scale-Out**
   * When pending tasks exceed available resources, the provider scales out
   * New blocks are created up to `max_blocks`
   * Scale-out rate can be controlled with `scaling_interval`

3. **Scale-In**
   * When blocks are idle for longer than `idle_timeout`, they are terminated
   * The provider always maintains at least `min_blocks` blocks
   * Scale-in decisions prioritize newer, healthier instances

4. **Control Parameters**

   .. code-block:: python

      provider = EphemeralAWSProvider(
          # Basic configuration
          region='us-west-2',
          instance_type='t3.medium',

          # Scaling parameters
          min_blocks=1,
          max_blocks=20,
          init_blocks=2,
          nodes_per_block=2,

          # Scaling control
          scaling_interval=60,        # Seconds between scaling decisions
          idle_timeout=300,           # Seconds before idle block termination
          parallelism=1.2,            # Provision 20% more capacity than needed
      )

Resource Monitoring
----------------

The provider includes robust resource monitoring capabilities:

Instance Monitoring
~~~~~~~~~~~~~~

* Status monitoring (pending, running, terminated, etc.)
* Health monitoring (instance health checks)
* Performance monitoring (CPU, memory, network)

Automated Health Checks
~~~~~~~~~~~~~~~~~~

The provider performs automated health checks on resources:

1. **Worker Process Checks**
   * Verifies worker processes are running
   * Monitors worker responsiveness
   * Detects hung or crashed workers

2. **Resource Validation**
   * Validates AWS resource status
   * Checks for unexpected state changes
   * Detects and responds to AWS service issues

3. **Network Connectivity**
   * Ensures connectivity between client and workers
   * Verifies internet access if required
   * Detects and responds to networking issues

Health Remediation
~~~~~~~~~~~~~~

When issues are detected, the provider can take remedial action:

* Restarting failed worker processes
* Replacing unhealthy instances
* Recreating network resources if necessary

Monitoring Configuration
~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic configuration
       region='us-west-2',
       instance_type='t3.medium',

       # Monitoring configuration
       monitoring_enabled=True,
       monitoring_interval=30,          # Seconds between monitoring checks
       health_check_interval=60,        # Seconds between health checks
       health_check_threshold=3,        # Failed checks before remediation

       # Remediation options
       auto_restart_workers=True,       # Restart failed workers
       replace_unhealthy_workers=True,  # Replace unhealthy instances
   )

Resource Cleanup
-------------

The provider automatically cleans up all resources it creates:

Cleanup Process
~~~~~~~~~~~

1. **Compute Resources**
   * EC2 instances are terminated
   * Spot requests are canceled
   * Lambda functions are deleted
   * ECS tasks are stopped and task definitions deregistered

2. **Supporting Resources**
   * IAM roles and policies are detached and deleted
   * CloudWatch resources are deleted
   * Other supporting resources are cleaned up

3. **Network Resources**
   * Security groups are deleted
   * Subnets are deleted
   * Route tables are disassociated and deleted
   * Internet gateways are detached and deleted
   * VPCs are deleted

Cleanup Triggers
~~~~~~~~~~~~

Cleanup occurs under these conditions:

* When `dfk.cleanup()` is called
* When the Python process exits normally
* When cleanup is explicitly requested via `provider.cleanup()`

Cleanup Configuration
~~~~~~~~~~~~~~~~

You can control cleanup behavior:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic configuration
       region='us-west-2',
       instance_type='t3.medium',

       # Cleanup configuration
       preserve_vpc=False,             # Don't preserve VPC (default)
       preserve_subnet=False,          # Don't preserve subnet (default)
       preserve_security_group=False,  # Don't preserve security group (default)

       # Preserve provided resources
       preserve_provided_resources=True,  # Don't delete resources provided in config

       # Force cleanup of all resources
       force_cleanup=False,            # Don't force cleanup if errors occur
   )

Preserving Resources
~~~~~~~~~~~~~~~~

You can preserve resources for reuse or inspection:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic configuration
       region='us-west-2',
       instance_type='t3.medium',

       # Preserve all network resources
       preserve_vpc=True,
       preserve_subnet=True,
       preserve_security_group=True,

       # Add identifier for reuse
       tags={
           'Preserved': 'True',
           'PreservationReason': 'For debugging',
           'PreservedUntil': '2023-12-31',
       },
   )

To reuse these resources later:

.. code-block:: python

   # Get preserved resource IDs
   vpc_id = provider.vpc_id
   subnet_id = provider.subnet_id
   security_group_id = provider.security_group_id

   print(f"Preserved VPC: {vpc_id}")
   print(f"Preserved Subnet: {subnet_id}")
   print(f"Preserved Security Group: {security_group_id}")

   # In a later session, use these resources
   new_provider = EphemeralAWSProvider(
       region='us-west-2',
       instance_type='t3.medium',
       vpc_id=vpc_id,
       subnet_id=subnet_id,
       security_group_id=security_group_id,
   )

Resource Management API
--------------------

The provider exposes an API for direct resource management:

Resource Information
~~~~~~~~~~~~~~~~

.. code-block:: python

   # Get all managed resources
   resources = provider.resources

   # Get resources by type
   vpc_resources = provider.get_resources_by_type('vpc')
   ec2_resources = provider.get_resources_by_type('ec2')

   # Get specific resource by ID
   instance = provider.get_resource('i-12345678abcdef')

   # Print resource details
   for resource_id, resource in resources.items():
       print(f"Resource {resource_id} ({resource['type']}): {resource['status']}")

Resource Operations
~~~~~~~~~~~~~~~

.. code-block:: python

   # Create a new instance programmatically
   instance_id = provider.create_instance(instance_type='c5.xlarge')

   # Terminate a specific instance
   provider.terminate_instance(instance_id)

   # Create a new block
   block_id = provider.create_block()

   # Scale to a specific number of blocks
   provider.scale_to_blocks(5)

   # Get instance status
   status = provider.get_instance_status(instance_id)
   print(f"Instance {instance_id} status: {status}")

Resource Challenges and Solutions
-----------------------------

Common Resource Challenges
~~~~~~~~~~~~~~~~~~~~~

1. **AWS Service Limits**
   * Default AWS service limits may constrain your workflows
   * Solution: Request limit increases or distribute across regions

2. **Resource Creation Failures**
   * AWS resource creation can sometimes fail
   * Solution: Provider includes retry logic with exponential backoff

3. **Resource Leakage**
   * Resources may not be properly cleaned up if the process crashes
   * Solution: Use resource tags for identification and cleanup scripts

4. **Networking Issues**
   * VPC and subnet configuration can be complex
   * Solution: Provider includes connectivity checks and networking diagnostics

5. **Permission Issues**
   * Insufficient IAM permissions can cause failures
   * Solution: Provider validates permissions and provides detailed error messages

Advanced Resource Management
------------------------

Hybrid Resource Strategy
~~~~~~~~~~~~~~~~~~

For complex workflows with diverse requirements:

.. code-block:: python

   # Standard mode provider for regular tasks
   standard_provider = EphemeralAWSProvider(
       region='us-west-2',
       instance_type='m5.large',
       max_blocks=10,
   )

   # Serverless provider for burst capacity
   serverless_provider = EphemeralAWSProvider(
       region='us-west-2',
       mode='serverless',
       worker_type='lambda',
       lambda_memory=1024,
       max_blocks=100,  # Much higher limit for burst capacity
   )

   # High-memory provider for memory-intensive tasks
   highmen_provider = EphemeralAWSProvider(
       region='us-west-2',
       instance_type='r5.4xlarge',  # Memory-optimized instance
       max_blocks=2,  # Limited due to cost
   )

   # GPU provider for accelerated computing
   gpu_provider = EphemeralAWSProvider(
       region='us-west-2',
       instance_type='p3.2xlarge',  # GPU instance
       max_blocks=1,  # Very limited due to cost
   )

   config = Config(
       executors=[
           HighThroughputExecutor(label='standard', provider=standard_provider),
           HighThroughputExecutor(label='serverless', provider=serverless_provider),
           HighThroughputExecutor(label='highmem', provider=highmen_provider),
           HighThroughputExecutor(label='gpu', provider=gpu_provider),
       ]
   )

Cross-Region Resource Strategy
~~~~~~~~~~~~~~~~~~~~~~~~~

For improved availability or to access region-specific resources:

.. code-block:: python

   # US East provider
   us_east_provider = EphemeralAWSProvider(
       region='us-east-1',
       instance_type='m5.large',
       max_blocks=5,
   )

   # US West provider
   us_west_provider = EphemeralAWSProvider(
       region='us-west-2',
       instance_type='m5.large',
       max_blocks=5,
   )

   # EU provider
   eu_provider = EphemeralAWSProvider(
       region='eu-west-1',
       instance_type='m5.large',
       max_blocks=5,
   )

   config = Config(
       executors=[
           HighThroughputExecutor(label='us_east', provider=us_east_provider),
           HighThroughputExecutor(label='us_west', provider=us_west_provider),
           HighThroughputExecutor(label='eu', provider=eu_provider),
       ]
   )

   # Tasks can be submitted to specific regions
   @parsl.python_app(executors=['us_east'])
   def east_task():
       import socket
       return f"Running in US East: {socket.gethostname()}"

   @parsl.python_app(executors=['us_west'])
   def west_task():
       import socket
       return f"Running in US West: {socket.gethostname()}"

   @parsl.python_app(executors=['eu'])
   def eu_task():
       import socket
       return f"Running in EU: {socket.gethostname()}"

Resource Reporting and Analytics
----------------------------

The provider includes resource reporting capabilities:

Usage Reports
~~~~~~~~~~

.. code-block:: python

   # Get resource usage summary
   usage = provider.get_resource_usage()

   print(f"Total instances: {usage['total_instances']}")
   print(f"Instance hours: {usage['instance_hours']}")
   print(f"Average active blocks: {usage['avg_active_blocks']}")

   # Get cost estimate
   costs = provider.estimate_costs()
   print(f"Estimated cost: ${costs['total_cost']:.2f}")
   print(f"Cost breakdown:")
   for resource_type, cost in costs['breakdown'].items():
       print(f"  {resource_type}: ${cost:.2f}")

Performance Metrics
~~~~~~~~~~~~~~~

.. code-block:: python

   # Get performance metrics
   metrics = provider.get_performance_metrics()

   print(f"Average instance startup time: {metrics['avg_startup_time']:.2f}s")
   print(f"Average scaling response time: {metrics['avg_scaling_response']:.2f}s")
   print(f"Resource creation success rate: {metrics['creation_success_rate']*100:.1f}%")

Resource Optimization Recommendations
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   # Get optimization recommendations
   recommendations = provider.get_optimization_recommendations()

   print("Resource optimization recommendations:")
   for rec in recommendations:
       print(f"- {rec['recommendation']}")
       print(f"  Estimated savings: ${rec['estimated_savings']:.2f}")
       print(f"  Confidence: {rec['confidence']*100:.0f}%")

Next Steps
---------

* Learn about :doc:`spot_handling` for optimizing spot instance usage
* Explore :doc:`../operating_modes/index` for different execution models
* See :doc:`../advanced_topics/cost_optimization` for overall AWS cost strategies
* Check out :doc:`../examples/scientific_computing` for real-world scientific workflow examples

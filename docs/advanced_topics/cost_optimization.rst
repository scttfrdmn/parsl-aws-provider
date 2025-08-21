Cost Optimization
================

This guide provides comprehensive strategies and techniques for optimizing AWS costs when using the Parsl Ephemeral AWS Provider, helping you maximize computational efficiency while minimizing expenses.

.. figure:: ../images/cost_optimization.svg
   :alt: Cost Optimization Strategies
   :align: center
   :width: 80%
   :figclass: align-center

   Cost optimization strategies across compute, storage, and operational dimensions

Understanding AWS Costs
--------------------

When using AWS for scientific workflows, costs come from multiple sources:

1. **Compute Costs**
   * EC2 instance usage (on-demand, spot, or reserved)
   * Lambda invocations and execution time
   * ECS/Fargate task usage

2. **Storage Costs**
   * EBS volumes for instances
   * S3 storage for data and state
   * Parameter Store advanced parameters

3. **Network Costs**
   * Data transfer between AWS and the internet
   * Data transfer between AWS regions
   * VPC endpoints and NAT gateways

4. **Supporting Service Costs**
   * CloudWatch logs and metrics
   * CloudTrail for auditing
   * IAM and other management services

Cost Optimization Strategies
-------------------------

Compute Optimization
~~~~~~~~~~~~~~~~

**1. Use Spot Instances Strategically**

Spot instances offer up to 90% cost savings compared to on-demand prices but can be interrupted. Configure for optimal usage:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic configuration
       region='us-west-2',

       # Spot configuration
       use_spot_instances=True,
       use_spot_fleet=True,

       # Diversify instance types for better availability
       instance_types=[
           'm5.large', 'm5a.large', 'm5n.large',
           'c5.large', 'c5a.large',
           'r5.large', 'r5a.large',
       ],

       # Set appropriate price limit
       spot_max_price_percentage=80,  # 80% of on-demand price

       # Optimize for cost vs. availability
       allocation_strategy='lowestPrice',  # For maximum cost savings
       # or
       # allocation_strategy='capacityOptimized',  # For better availability
   )

**2. Right-Size Instances**

Choose appropriate instance types for your workload:

* Compute-intensive workloads: C-family instances (c5, c6g)
* Memory-intensive workloads: R-family instances (r5, r6g)
* Balanced workloads: M-family instances (m5, m6g)
* Cost-constrained workloads: T-family instances (t3, t4g)

Regularly analyze your workload characteristics to identify optimal instance types:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Use the right instance size
       instance_type='c5.large',  # Instead of c5.4xlarge if underutilized

       # Or use spot fleet with weighted capacity
       use_spot_fleet=True,
       instance_types=[
           {'type': 'c5.large', 'weight': 1},
           {'type': 'c5.xlarge', 'weight': 2},  # Twice the capacity
           {'type': 'c5.2xlarge', 'weight': 4},  # Four times the capacity
       ],

       # Let Spot Fleet optimize capacity vs. cost
       target_capacity=4,  # Total capacity needed
   )

**3. Use Graviton (ARM) Instances**

AWS Graviton processors offer better price-performance ratios:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Use Graviton2 instances
       instance_types=[
           'c6g.large',  # Graviton2
           'm6g.large',  # Graviton2
           'r6g.large',  # Graviton2
       ],

       # Ensure your AMI supports ARM
       image_id='ami-0123456789abcdef0',  # ARM-compatible AMI

       # Worker initialization for ARM compatibility
       worker_init='''
           # Install ARM-compatible packages
           sudo yum update -y
           sudo yum install -y python3-devel
           python3 -m pip install --only-binary=:all: numpy scipy pandas
       ''',
   )

**4. Implement Autoscaling**

Proper autoscaling ensures you only pay for needed resources:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic configuration
       region='us-west-2',
       instance_type='t3.medium',

       # Start small
       init_blocks=1,

       # Scale to zero when idle
       min_blocks=0,

       # Set reasonable maximum
       max_blocks=10,

       # Scale down quickly when idle
       idle_timeout=300,  # 5 minutes
   )

**5. Use Serverless for Variable Workloads**

For highly variable workloads with periods of inactivity, use Serverless Mode:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Serverless mode
       mode='serverless',
       region='us-west-2',

       # Configure for cost efficiency
       worker_type='auto',  # Choose the most cost-effective service

       # Lambda configuration
       lambda_memory=1024,  # Only pay for what you need
       lambda_timeout=300,  # Set appropriate timeout

       # ECS/Fargate configuration
       ecs_task_cpu=512,   # 0.5 vCPU
       ecs_task_memory=1024,  # 1 GB
   )

**6. Optimize Lambda Configuration**

For Lambda-based workloads, optimize memory and timeout:

.. code-block:: python

   provider = EphemeralAWSProvider(
       mode='serverless',
       worker_type='lambda',

       # Start with lower memory and increase only if needed
       lambda_memory=1024,  # 1 GB

       # Set timeout based on actual task duration
       lambda_timeout=60,  # 1 minute for short tasks

       # Only include necessary dependencies
       lambda_python_dependencies=[
           'numpy==1.21.0',  # Specify only what you need
       ],
   )

**7. Use Reserved Instances for Steady Workloads**

For predictable, steady-state workloads, consider Reserved Instances:

* Purchase Reserved Instances in the AWS console
* Configure the provider to use the same instance type

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Match your Reserved Instance attributes exactly
       region='us-west-2',
       instance_type='m5.large',  # Same as your RI

       # Maintain minimum blocks to utilize RIs
       min_blocks=5,  # If you have 5 Reserved Instances

       # Use spot for bursting above RI capacity
       use_spot_instances=True,
   )

Storage Optimization
~~~~~~~~~~~~~~~~

**1. Optimize EBS Volumes**

Configure appropriate EBS volumes:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic configuration
       region='us-west-2',
       instance_type='m5.large',

       # Minimize root volume size
       root_volume_size=20,  # GB, minimum required

       # Use gp3 for better performance/cost
       root_volume_type='gp3',

       # Only add EBS volumes if needed
       ebs_volumes=[
           {
               'device_name': '/dev/sdf',
               'volume_size': 100,
               'volume_type': 'gp3',  # More cost-effective than gp2
           }
       ],
   )

**2. Use Instance Store When Appropriate**

For temporary data, use instance store volumes:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Choose instance types with instance store
       instance_type='m5d.large',  # 'd' indicates NVMe instance store

       # Initialize the instance store
       worker_init='''
           # Format and mount instance store
           sudo mkfs -t xfs /dev/nvme1n1
           sudo mkdir -p /mnt/instance-store
           sudo mount /dev/nvme1n1 /mnt/instance-store
           sudo chmod 777 /mnt/instance-store

           # Use it for temporary data
           export TMPDIR=/mnt/instance-store/tmp
           mkdir -p $TMPDIR
       ''',
   )

**3. Optimize S3 Usage**

When using S3 for state persistence or data storage:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic configuration
       region='us-west-2',

       # S3 state persistence configuration
       state_store='s3',
       state_prefix='workflow/state',
       state_config={
           'bucket_name': 'my-parsl-bucket',
           'storage_class': 'STANDARD',  # Or 'STANDARD_IA' for less frequent access
           'lifecycle_policy': True,  # Enable S3 lifecycle policies
           'lifecycle_days': 30,  # Move to glacier after 30 days
       },
   )

Additionally, implement S3 lifecycle policies in the AWS console to automatically transition or expire objects.

**4. Optimize Parameter Store Usage**

For state persistence with Parameter Store:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic configuration
       region='us-west-2',

       # Parameter Store configuration
       state_store='parameter_store',
       state_prefix='/parsl/workflows',
       state_config={
           'parameter_type': 'String',  # Use standard tier when possible
           'parameter_tier': 'Standard',  # Less expensive than Advanced
       },

       # Clean up parameters after workflow completion
       state_cleanup='always',
   )

Network Optimization
~~~~~~~~~~~~~~~~

**1. Region Selection**

Choose the AWS region closest to your data sources and users:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Choose cost-effective region
       region='us-east-1',  # Generally the most cost-effective region
       # or
       # region='us-west-2',  # If your data is on the west coast
   )

**2. Minimize Cross-Region Transfer**

Keep data and compute in the same region:

.. code-block:: python

   # Provider in the same region as your data
   provider = EphemeralAWSProvider(
       region='us-west-2',  # Same region as your S3 bucket

       # Use regional S3 endpoint
       worker_init='''
           # Configure AWS CLI to use regional endpoint
           aws configure set s3.us-west-2.endpoint s3.us-west-2.amazonaws.com
       ''',
   )

**3. Use VPC Endpoints for AWS Services**

For high-volume access to AWS services, use VPC endpoints:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic configuration
       region='us-west-2',

       # Request VPC endpoints creation
       create_vpc_endpoints=True,
       vpc_endpoints=[
           's3',           # For S3 access
           'dynamodb',     # For DynamoDB access
           'ssm',          # For Parameter Store access
       ],
   )

**4. Optimize Parsl Data Movement**

Configure Parsl to minimize unnecessary data transfer:

.. code-block:: python

   import parsl
   from parsl.config import Config
   from parsl.executors import HighThroughputExecutor
   from parsl.data_provider.files import File

   provider = EphemeralAWSProvider(
       # Basic configuration
       region='us-west-2',
   )

   config = Config(
       executors=[
           HighThroughputExecutor(
               label='aws_executor',
               provider=provider,

               # Configure for data efficiency
               storage_access=storage_access,  # Define appropriate storage access
               working_dir='/mnt/instance-store/scratch',  # Use fast local storage
           )
       ]
   )

   parsl.load(config)

   # Use Parsl's File abstraction for efficient data movement
   input_file = File('s3://my-bucket/input.dat')

   @parsl.python_app
   def process_data(file):
       with open(file.local_path, 'r') as f:
           data = f.read()
       # Process data
       return result

Operational Optimization
~~~~~~~~~~~~~~~~~~~~

**1. Implement Proper Cleanup**

Ensure resources are always cleaned up:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic configuration
       region='us-west-2',

       # Ensure cleanup happens
       force_cleanup=True,  # Try harder to clean up resources
       cleanup_on_exit=True,  # Clean up when Python exits

       # Don't preserve resources unless needed
       preserve_vpc=False,
       preserve_subnet=False,
       preserve_security_group=False,
   )

Always explicitly cleanup at the end of your workflow:

.. code-block:: python

   # After workflow completes
   parsl.dfk().cleanup()

**2. Optimize Initialization**

Minimize instance startup time to reduce costs:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic configuration
       region='us-west-2',
       instance_type='m5.large',

       # Use optimized AMI with pre-installed dependencies
       image_id='ami-0123456789abcdef0',  # Custom AMI with dependencies

       # Minimal worker initialization
       worker_init='''
           # Only necessary initialization
           export PATH=$PATH:/opt/custom/bin
           export PYTHONPATH=$PYTHONPATH:/opt/custom/lib
       ''',
   )

**3. Implement Cost Monitoring**

Use AWS Cost Explorer and CloudWatch to monitor costs:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic configuration
       region='us-west-2',

       # Tag for cost tracking
       tags={
           'Project': 'GenomeAnalysis',
           'CostCenter': 'Research',
           'Environment': 'Production',
       },

       # Enable detailed monitoring
       detailed_monitoring=True,
   )

**4. Use Hibernation for Long-Running Workflows**

For workflows that can be paused and resumed:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic configuration
       region='us-west-2',
       instance_type='m5.large',  # Ensure instance type supports hibernation

       # Enable hibernation
       hibernation_enabled=True,

       # For spot instances
       spot_interruption_behavior='hibernate',
   )

**5. Implement Resource Quotas**

Set limits to prevent runaway costs:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic configuration
       region='us-west-2',

       # Strict resource limits
       max_blocks=10,  # Maximum number of blocks
       max_instances=20,  # Maximum number of instances

       # Budget constraints
       max_cost_per_hour=10.0,  # USD
       budget_alert_threshold=0.8,  # Alert at 80% of budget
       enforce_budget_constraints=True,  # Stop scaling when budget is exceeded
   )

Advanced Cost Optimization
-----------------------

**1. Workflow-Aware Scaling**

For workflows with predictable phases:

.. code-block:: python

   # Initial configuration for data loading phase
   provider = EphemeralAWSProvider(
       region='us-west-2',
       instance_type='i3.large',  # Storage optimized
       max_blocks=2,
   )

   # Mid-workflow reconfiguration for compute phase
   provider.reconfigure(
       instance_type='c5.2xlarge',  # Compute optimized
       max_blocks=10,
   )

   # Final phase for data aggregation
   provider.reconfigure(
       instance_type='r5.large',  # Memory optimized
       max_blocks=1,
   )

**2. Cross-Instance-Family Bursting**

Optimize for both cost and performance with hybrid instance types:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic configuration
       region='us-west-2',

       # Use spot fleet with diverse options
       use_spot_fleet=True,

       # Base capacity with burstable instances
       instance_types=[
           {'type': 't3.medium', 'weight': 1, 'priority': 1},  # Base capacity
           {'type': 'c5.large', 'weight': 2, 'priority': 5},   # Burst compute
           {'type': 'r5.large', 'weight': 2, 'priority': 10},  # Burst memory
       ],

       # Set priority order for instance selection
       allocation_strategy='prioritized',
   )

**3. Serverless Hybrid Approach**

Combine serverless and instance-based execution:

.. code-block:: python

   # Serverless provider for small tasks
   lambda_provider = EphemeralAWSProvider(
       mode='serverless',
       worker_type='lambda',
       region='us-west-2',
   )

   # EC2 provider for larger tasks
   ec2_provider = EphemeralAWSProvider(
       mode='standard',
       instance_type='m5.large',
       region='us-west-2',
       use_spot_instances=True,
   )

   config = Config(
       executors=[
           HighThroughputExecutor(
               label='lambda_executor',
               provider=lambda_provider,
           ),
           HighThroughputExecutor(
               label='ec2_executor',
               provider=ec2_provider,
           )
       ]
   )

   # Small task uses Lambda
   @parsl.python_app(executors=['lambda_executor'])
   def small_task():
       # Quick processing
       pass

   # Larger task uses EC2
   @parsl.python_app(executors=['ec2_executor'])
   def large_task():
       # Intensive processing
       pass

**4. Sharing Resources Across Workflows**

For multiple concurrent workflows:

.. code-block:: python

   # Create a shared provider
   provider = EphemeralAWSProvider(
       region='us-west-2',
       instance_type='m5.large',
       max_blocks=20,

       # Enable resource sharing
       shared_resources=True,
       resource_sharing_key='shared-key',

       # Configure resource allocation
       fair_share=True,
       priority=1,  # Higher numbers get priority
   )

   # Each workflow uses the same provider
   config1 = Config(executors=[HighThroughputExecutor(provider=provider)])
   config2 = Config(executors=[HighThroughputExecutor(provider=provider)])

   # They share the resources, reducing total cost

**5. Scheduled Workflows**

For workflows that can run during off-peak hours:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic configuration
       region='us-west-2',

       # Schedule specific hours for execution
       scheduled_execution=True,
       execution_hours=[
           # Define execution windows (UTC)
           {'start': '00:00', 'end': '08:00'},  # Overnight hours
           {'start': '22:00', 'end': '23:59'},  # Late night
       ],

       # Use Spot instances during these times
       use_spot_instances=True,
       spot_max_price_percentage=60,  # Lower prices during off-hours
   )

Cost Analysis and Optimization Workflow
------------------------------------

Implement this workflow to continuously optimize costs:

1. **Baseline Current Costs**

.. code-block:: python

   # Get cost baseline
   baseline = provider.estimate_costs()
   print(f"Current estimated cost: ${baseline['total_cost']:.2f} per hour")
   print(f"Monthly projection: ${baseline['total_cost']*24*30:.2f}")

2. **Analyze Resource Utilization**

.. code-block:: python

   # Get utilization metrics
   metrics = provider.get_performance_metrics()

   print(f"CPU Utilization: {metrics['cpu_utilization']:.1f}%")
   print(f"Memory Utilization: {metrics['memory_utilization']:.1f}%")
   print(f"Instance Idle Time: {metrics['idle_time_percentage']:.1f}%")

3. **Apply Optimization Recommendations**

.. code-block:: python

   # Get optimization recommendations
   recommendations = provider.get_optimization_recommendations()

   for rec in recommendations:
       print(f"Recommendation: {rec['action']}")
       print(f"Estimated Savings: ${rec['savings']:.2f}")
       print(f"Confidence: {rec['confidence']*100:.0f}%")

       if rec['confidence'] > 0.8 and rec['apply_automatically']:
           print("Applying automatically...")
           provider.apply_recommendation(rec['id'])

4. **Monitor Results**

.. code-block:: python

   # Measure impact of changes
   new_costs = provider.estimate_costs()
   savings = baseline['total_cost'] - new_costs['total_cost']

   print(f"New estimated cost: ${new_costs['total_cost']:.2f} per hour")
   print(f"Savings: ${savings:.2f} per hour (${savings*24*30:.2f} per month)")
   print(f"Savings percentage: {savings/baseline['total_cost']*100:.1f}%")

Complete Cost-Optimized Example
----------------------------

Here's a comprehensive example implementing multiple cost optimization strategies:

.. code-block:: python

   import parsl
   from parsl.config import Config
   from parsl.executors import HighThroughputExecutor
   from parsl_ephemeral_aws import EphemeralAWSProvider

   # Create a cost-optimized provider
   provider = EphemeralAWSProvider(
       # Region optimization
       region='us-east-1',  # Generally the most cost-effective region

       # Compute optimization
       use_spot_instances=True,
       use_spot_fleet=True,
       instance_types=[
           'c6g.large',    # ARM-based for better price/performance
           'c5a.large',    # AMD-based, typically lower cost
           'c5.large',     # Intel fallback
       ],
       allocation_strategy='lowestPrice',
       spot_max_price_percentage=70,

       # Scaling optimization
       min_blocks=0,       # Scale to zero when idle
       max_blocks=10,
       init_blocks=1,
       idle_timeout=300,   # 5 minutes

       # Storage optimization
       root_volume_size=20,
       root_volume_type='gp3',

       # State persistence optimization
       state_store='parameter_store',
       state_config={
           'parameter_tier': 'Standard',
       },
       state_cleanup='always',

       # Resource tagging for cost analysis
       tags={
           'Project': 'CostOptimizedWorkflow',
           'CostCenter': 'Research',
           'Environment': 'Production',
       },

       # Worker initialization optimization
       worker_init='''
           # Optimize package installation
           pip install --no-cache-dir -U pip
           pip install --no-cache-dir numpy scipy pandas

           # Configure AWS CLI for efficiency
           aws configure set default.s3.max_concurrent_requests 20
           aws configure set default.s3.max_queue_size 10000
           aws configure set default.s3.use_accelerate_endpoint true
       ''',

       # Budget constraints
       max_cost_per_hour=5.0,
       enforce_budget_constraints=True,
   )

   # Create Parsl configuration
   config = Config(
       executors=[
           HighThroughputExecutor(
               label='cost_optimized_executor',
               provider=provider,
               max_workers_per_node=4,  # Optimize worker density
           )
       ],
       strategy='htex_auto_scale',  # Dynamic scaling
   )

   # Load the configuration
   parsl.load(config)

   # Example workflow
   @parsl.python_app
   def process_data(file_idx):
       import time
       import numpy as np

       # Simulate data processing
       data_size = 1000000
       data = np.random.rand(data_size)

       # Process efficiently
       start = time.time()
       result = np.fft.fft(data)
       processing_time = time.time() - start

       return {
           'file_idx': file_idx,
           'data_size': data_size,
           'processing_time': processing_time,
           'result_sum': np.sum(result.real),
       }

   # Submit tasks
   results = []
   for i in range(100):
       results.append(process_data(i))

   # Process results efficiently
   for r in results:
       print(f"Processed file {r.result()['file_idx']} in {r.result()['processing_time']:.2f}s")

   # Clean up resources
   parsl.dfk().cleanup()

Conclusion and Next Steps
---------------------

AWS cost optimization is an iterative process. After implementing these strategies:

1. **Regular Monitoring**
   * Use AWS Cost Explorer to track actual costs
   * Compare against provider cost estimates
   * Identify trends and anomalies

2. **Continuous Improvement**
   * Regularly review and update your configuration
   * Apply new optimization techniques as they become available
   * Test different strategies for your specific workloads

3. **Automate Optimization**
   * Implement automated scaling policies
   * Set up budget alerts and constraints
   * Use AWS Budgets for organizational control

Next Steps
---------

* Explore :doc:`../operating_modes/serverless_mode` for ultimate cost efficiency
* Learn about :doc:`spot_handling` for resilient spot instance usage
* See how to implement :doc:`../user_guide/resource_management` for better resource control
* Check out :doc:`gpu_computing` for optimizing GPU-accelerated workflows

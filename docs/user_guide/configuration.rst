Configuration
=============

This guide covers the comprehensive configuration options for the Parsl Ephemeral AWS Provider, helping you customize the provider for your specific needs.

.. figure:: ../images/configuration_overview.svg
   :alt: Configuration Overview
   :align: center
   :width: 70%
   :figclass: align-center

   Configuration categories and relationships

Basic Configuration
----------------

The minimum configuration requires only a few parameters:

.. code-block:: python

   from parsl.config import Config
   from parsl.executors import HighThroughputExecutor
   from parsl_ephemeral_aws import EphemeralAWSProvider

   provider = EphemeralAWSProvider(
       # Core parameters
       region='us-west-2',            # AWS region
       image_id='ami-12345678',       # AMI ID for worker instances
       instance_type='t3.medium',     # Instance type for workers

       # Parsl block parameters
       init_blocks=1,                 # Initial number of blocks to provision
       min_blocks=0,                  # Minimum number of blocks to maintain
       max_blocks=10,                 # Maximum number of blocks allowed
   )

   config = Config(
       executors=[
           HighThroughputExecutor(
               label='aws_executor',
               provider=provider,
           )
       ]
   )

Required Parameters
~~~~~~~~~~~~~~~~

``region`` (String)
  AWS region where resources will be created (e.g., 'us-east-1', 'us-west-2', 'eu-west-1', etc.).

``instance_type`` (String)
  EC2 instance type for worker nodes in Standard and Detached modes (e.g., 't3.micro', 'm5.large', 'c5.xlarge', etc.).

Optional Parameters
~~~~~~~~~~~~~~~

``image_id`` (String, optional)
  Amazon Machine Image (AMI) ID to use for worker instances. If not specified, the provider will use a default Amazon Linux 2 AMI.

``min_blocks`` (Integer, default: 0)
  Minimum number of blocks to maintain.

``max_blocks`` (Integer, default: 10)
  Maximum number of blocks that can be provisioned.

``nodes_per_block`` (Integer, default: 1)
  Number of worker nodes per block.

Operating Mode Configuration
-------------------------

The provider supports three operating modes:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic configuration
       region='us-west-2',
       instance_type='t3.medium',

       # Mode selection
       mode='standard',  # 'standard', 'detached', or 'serverless'

       # Standard Mode specific parameters
       # (none, this is the default mode)

       # Detached Mode specific parameters
       bastion_instance_type='t3.micro',   # Only used in Detached Mode
       bastion_idle_timeout=30,            # In minutes, 0 for no timeout

       # Serverless Mode specific parameters
       worker_type='auto',                 # 'lambda', 'ecs', or 'auto'
       lambda_memory=1024,                 # MB for Lambda functions
       lambda_timeout=900,                 # Seconds for Lambda functions
       ecs_task_cpu=1024,                  # CPU units for ECS tasks
       ecs_task_memory=2048,               # MB for ECS tasks
   )

Standard Mode Parameters
~~~~~~~~~~~~~~~~~~~~

No specific parameters required as this is the default mode.

Detached Mode Parameters
~~~~~~~~~~~~~~~~~~~

``bastion_instance_type`` (String, default: 't3.micro')
  EC2 instance type for the bastion/coordinator instance.

``bastion_image_id`` (String, optional)
  Specific AMI ID for the bastion. If not specified, uses the same as worker instances.

``bastion_idle_timeout`` (Integer, default: 30)
  Number of minutes of idle time before the bastion automatically shuts down. Set to 0 to disable auto-shutdown.

Serverless Mode Parameters
~~~~~~~~~~~~~~~~~~~~~~

``worker_type`` (String, default: 'auto')
  Type of serverless worker to use. Options are 'lambda', 'ecs', or 'auto' (automatically selects based on task requirements).

``lambda_memory`` (Integer, default: 1024)
  Memory in MB for Lambda functions (128 to 10240 MB).

``lambda_timeout`` (Integer, default: 900)
  Maximum execution time in seconds for Lambda functions (1 to 900 seconds).

``lambda_max_concurrency`` (Integer, default: 100)
  Maximum number of concurrent Lambda invocations.

``lambda_python_dependencies`` (List[String], optional)
  List of Python package requirements for Lambda functions (e.g., ['numpy==1.21.0', 'pandas==1.3.0']).

``ecs_task_cpu`` (Integer, default: 1024)
  CPU units for ECS tasks (256 = 0.25 vCPU, 1024 = 1 vCPU, etc.).

``ecs_task_memory`` (Integer, default: 2048)
  Memory in MB for ECS tasks.

``ecs_max_tasks`` (Integer, default: 10)
  Maximum number of concurrent ECS tasks.

``container_image`` (String, optional)
  Docker image to use for ECS tasks. If not specified, uses a default Amazon Linux 2 image.

AWS Resource Configuration
-----------------------

You can customize AWS resources with these parameters:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic configuration
       region='us-west-2',
       instance_type='t3.medium',

       # Network configuration
       vpc_id='vpc-12345678',              # Use existing VPC
       subnet_id='subnet-12345678',        # Use existing subnet
       security_group_id='sg-12345678',    # Use existing security group
       use_public_ips=True,                # Assign public IPs to instances

       # Compute configuration
       key_name='my-key-pair',             # EC2 key pair for SSH access
       iam_instance_profile='MyProfile',   # IAM instance profile
       placement_group='my-placement',     # EC2 placement group
       availability_zone='us-west-2a',     # Specific AZ to use

       # Storage configuration
       root_volume_size=30,                # Size in GB for the root volume
       ebs_volumes=[                       # Additional EBS volumes
           {
               'device_name': '/dev/sdf',
               'volume_size': 100,
               'volume_type': 'gp3',
           }
       ],

       # Tags for all resources
       tags={
           'Project': 'MyProject',
           'Environment': 'Development',
           'Owner': 'MyTeam',
       },
   )

Network Parameters
~~~~~~~~~~~~~~

``vpc_id`` (String, optional)
  Existing VPC ID to use. If not specified, a new VPC will be created.

``subnet_id`` (String, optional)
  Existing subnet ID to use. If not specified, a new subnet will be created.

``security_group_id`` (String, optional)
  Existing security group ID to use. If not specified, a new security group will be created.

``use_public_ips`` (Boolean, default: True)
  Whether to assign public IP addresses to instances. Required for internet access unless you have a NAT gateway.

Compute Parameters
~~~~~~~~~~~~~~

``key_name`` (String, optional)
  EC2 key pair name for SSH access to instances.

``iam_instance_profile`` (String, optional)
  IAM instance profile name to attach to instances for AWS permissions.

``placement_group`` (String, optional)
  EC2 placement group for optimizing instance placement.

``availability_zone`` (String, optional)
  Specific AWS availability zone to use.

Storage Parameters
~~~~~~~~~~~~~~

``root_volume_size`` (Integer, default: 20)
  Size in GB for the root EBS volume.

``root_volume_type`` (String, default: 'gp3')
  EBS volume type for the root volume ('gp3', 'gp2', 'io1', 'io2', 'sc1', 'st1', 'standard').

``ebs_volumes`` (List[Dict], optional)
  Additional EBS volumes to attach to instances.

Tagging Parameters
~~~~~~~~~~~~~~

``tags`` (Dict, optional)
  Tags to apply to all AWS resources created by the provider.

Spot Instance Configuration
------------------------

The provider supports AWS Spot Instances for cost savings:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic configuration
       region='us-west-2',
       instance_type='t3.medium',

       # Spot instance configuration
       use_spot_instances=True,               # Use spot instances
       spot_max_price_percentage=80,          # Max price as percentage of on-demand
       spot_interruption_behavior='terminate', # 'terminate', 'stop', or 'hibernate'

       # Spot Fleet configuration (advanced)
       use_spot_fleet=True,                   # Use Spot Fleet instead of single requests
       instance_types=[                       # Multiple instance types for diversity
           't3.medium',
           't3a.medium',
           'm5.large',
       ],
       instance_weights={                      # Optional instance weightings
           't3.medium': 1,
           't3a.medium': 1,
           'm5.large': 2,
       },
       allocation_strategy='lowestPrice',     # 'lowestPrice', 'diversified', 'capacityOptimized'
   )

Spot Instance Parameters
~~~~~~~~~~~~~~~~~~~~

``use_spot_instances`` (Boolean, default: False)
  Whether to use EC2 Spot Instances instead of On-Demand Instances.

``spot_max_price_percentage`` (Integer, default: 100)
  Maximum price for spot instances as a percentage of the on-demand price.

``spot_interruption_behavior`` (String, default: 'terminate')
  What to do when a spot instance is interrupted. Options are 'terminate', 'stop', or 'hibernate'.

Spot Fleet Parameters
~~~~~~~~~~~~~~~~

``use_spot_fleet`` (Boolean, default: False)
  Whether to use AWS Spot Fleet for managing spot instances.

``instance_types`` (List[String] or List[Dict], optional)
  List of instance types to use with Spot Fleet. Either a simple list of instance type strings or a list of dictionaries with instance specifications.

``instance_weights`` (Dict, optional)
  Dictionary mapping instance types to their relative capacity weights.

``allocation_strategy`` (String, default: 'lowestPrice')
  Strategy for allocating spot instances. Options are 'lowestPrice', 'diversified', or 'capacityOptimized'.

State Persistence Configuration
---------------------------

Configure state persistence for recovery capabilities:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic configuration
       region='us-west-2',
       instance_type='t3.medium',

       # State persistence configuration
       state_store='parameter_store',          # 'parameter_store', 's3', 'file', or 'none'
       state_prefix='/parsl/workflow-1',       # Prefix for state storage

       # Persistence options
       state_cleanup='always',                 # 'always', 'never', or 'success'
       state_retention_days=7,                 # Days to retain state

       # Backend-specific configuration
       state_config={
           # Parameter Store options
           'parameter_type': 'String',         # 'String', 'StringList', or 'SecureString'
           'parameter_tier': 'Standard',       # 'Standard' or 'Advanced'

           # S3 options
           'bucket_name': 'my-parsl-state',    # Custom bucket name
           'versioning': True,                 # Enable S3 versioning

           # File options
           'directory': '/path/to/state',      # Custom directory path
           'backup': True,                     # Enable file backups
       },
   )

State Persistence Parameters
~~~~~~~~~~~~~~~~~~~~~~~

``state_store`` (String, default: 'none')
  Storage backend for state persistence. Options are 'parameter_store', 's3', 'file', or 'none'.

``state_prefix`` (String, optional)
  Prefix for state storage paths/keys. Useful for organizing multiple workflows.

``state_cleanup`` (String, default: 'always')
  When to clean up state. Options are 'always', 'never', or 'success'.

``state_retention_days`` (Integer, default: 7)
  Number of days to retain state after workflow completion.

``state_config`` (Dict, optional)
  Additional backend-specific configuration options.

Worker Initialization Configuration
-------------------------------

Customize worker initialization with boot scripts or environment variables:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic configuration
       region='us-west-2',
       instance_type='t3.medium',

       # Worker initialization
       worker_init='''
           # Update packages
           sudo yum update -y

           # Install dependencies
           sudo yum install -y python3-devel gcc git

           # Install Python packages
           python3 -m pip install --upgrade pip
           python3 -m pip install numpy scipy pandas scikit-learn
       ''',

       # Environment variables
       worker_environment={
           'PYTHONUNBUFFERED': '1',
           'PARSL_WORKER_LOGGING': 'debug',
           'MY_API_KEY': 'secret-key',
       },
   )

Worker Initialization Parameters
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``worker_init`` (String, optional)
  Shell script to run on worker instances during initialization.

``worker_environment`` (Dict, optional)
  Environment variables to set for worker processes.

Parsl Integration Configuration
---------------------------

Configure how the provider integrates with Parsl:

.. code-block:: python

   from parsl.launchers import MpiRunLauncher

   provider = EphemeralAWSProvider(
       # Basic configuration
       region='us-west-2',
       instance_type='t3.medium',

       # Parsl integration
       launcher=MpiRunLauncher(),               # Custom launcher for workers
       cmd_timeout=60,                          # Command timeout in seconds
       parallelism=1.0,                         # Scale factor for parallelism
       scheduler_options='',                    # Additional scheduler options
   )

Parsl Integration Parameters
~~~~~~~~~~~~~~~~~~~~~~~~

``launcher`` (Launcher, optional)
  Custom launcher for worker processes. Default is SimpleLauncher.

``cmd_timeout`` (Integer, default: 30)
  Timeout in seconds for provider commands.

``parallelism`` (Float, default: 1.0)
  Scaling factor for parallelism.

``scheduler_options`` (String, optional)
  Additional options to pass to the scheduler.

Advanced Configuration
-------------------

Additional advanced configuration options:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic configuration
       region='us-west-2',
       instance_type='t3.medium',

       # AWS credentials (if not using default profile)
       aws_access_key_id='AKIAXXXXXXXXXXXXXXXX',
       aws_secret_access_key='xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
       aws_session_token='xxxxxxxx',           # For temporary credentials
       aws_profile='my-profile',               # Use a specific AWS profile

       # Advanced networking
       vpc_cidr_block='10.0.0.0/16',           # Custom VPC CIDR block
       subnet_cidr_block='10.0.0.0/24',        # Custom subnet CIDR block
       security_group_ingress=[                # Custom security group rules
           {
               'from_port': 22,
               'to_port': 22,
               'ip_protocol': 'tcp',
               'cidr_ip': '0.0.0.0/0',
               'description': 'SSH access',
           },
           {
               'from_port': 8080,
               'to_port': 8080,
               'ip_protocol': 'tcp',
               'cidr_ip': '0.0.0.0/0',
               'description': 'Web access',
           },
       ],

       # Debugging options
       debug=True,                              # Enable debug logging
       verbose=True,                            # Enable verbose output
   )

AWS Credentials Parameters
~~~~~~~~~~~~~~~~~~~~~~

``aws_access_key_id`` (String, optional)
  AWS access key ID. If not specified, uses the default credential provider chain.

``aws_secret_access_key`` (String, optional)
  AWS secret access key.

``aws_session_token`` (String, optional)
  AWS session token for temporary credentials.

``aws_profile`` (String, optional)
  AWS profile name to use from ~/.aws/credentials.

Advanced Networking Parameters
~~~~~~~~~~~~~~~~~~~~~~~~~

``vpc_cidr_block`` (String, default: '10.0.0.0/16')
  CIDR block for the VPC when creating a new VPC.

``subnet_cidr_block`` (String, default: '10.0.0.0/24')
  CIDR block for the subnet when creating a new subnet.

``security_group_ingress`` (List[Dict], optional)
  Custom security group ingress rules.

Debugging Parameters
~~~~~~~~~~~~~~~

``debug`` (Boolean, default: False)
  Enable debug-level logging.

``verbose`` (Boolean, default: False)
  Enable verbose output.

Configuration Examples
------------------

Standard Mode with Spot Instances
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   provider = EphemeralAWSProvider(
       region='us-west-2',
       instance_type='m5.large',
       image_id='ami-0c55b159cbfafe1f0',  # Amazon Linux 2

       # Block parameters
       init_blocks=1,
       min_blocks=0,
       max_blocks=10,

       # Spot configuration
       use_spot_instances=True,
       spot_max_price_percentage=70,

       # Tag resources
       tags={
           'Project': 'DataProcessing',
           'Environment': 'Production',
       },

       # Worker initialization
       worker_init='''
           sudo yum update -y
           sudo yum install -y python3-devel gcc
           python3 -m pip install numpy pandas scikit-learn
       ''',
   )

Detached Mode with State Persistence
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Mode
       mode='detached',
       region='us-west-2',

       # Worker configuration
       instance_type='c5.2xlarge',
       image_id='ami-0c55b159cbfafe1f0',

       # Block parameters
       init_blocks=2,
       min_blocks=0,
       max_blocks=20,

       # Bastion configuration
       bastion_instance_type='t3.small',
       bastion_idle_timeout=60,  # Minutes

       # State persistence
       state_store='parameter_store',
       state_prefix='/parsl/production-workflow',
       state_cleanup='never',  # Preserve state for analysis

       # Use key pair for SSH access
       key_name='my-key-pair',
   )

Serverless Mode with Lambda and ECS
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Mode
       mode='serverless',
       region='us-west-2',

       # Use both Lambda and ECS
       worker_type='auto',

       # Lambda configuration
       lambda_memory=2048,
       lambda_timeout=900,
       lambda_max_concurrency=100,
       lambda_python_dependencies=[
           'numpy==1.21.0',
           'pandas==1.3.0',
           'scikit-learn==0.24.2',
       ],

       # ECS configuration
       ecs_task_cpu=2048,   # 2 vCPU
       ecs_task_memory=4096,  # 4 GB
       ecs_max_tasks=20,

       # State persistence
       state_store='s3',
       state_prefix='serverless-workflow',
       state_config={
           'bucket_name': 'my-parsl-state',
       },
   )

Multi-node MPI Configuration
~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   from parsl.launchers import MpiRunLauncher

   provider = EphemeralAWSProvider(
       region='us-west-2',
       instance_type='c5n.18xlarge',  # High-performance networking
       image_id='ami-0c55b159cbfafe1f0',

       # Multi-node configuration
       nodes_per_block=4,  # 4 nodes per block for MPI
       init_blocks=1,
       max_blocks=5,

       # MPI launcher
       launcher=MpiRunLauncher(
           bind_cmd="--bind-to core",
           overrides="--allow-run-as-root"
       ),

       # Worker initialization for MPI
       worker_init='''
           sudo yum update -y
           sudo amazon-linux-extras install -y lustre2.10
           sudo yum install -y openmpi-devel

           # Configure MPI
           echo "export PATH=$PATH:/usr/lib64/openmpi/bin" >> ~/.bashrc
           echo "export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/usr/lib64/openmpi/lib" >> ~/.bashrc

           # Install Python dependencies
           python3 -m pip install mpi4py numpy scipy
       ''',

       # Network optimization
       placement_group='cluster',  # Use cluster placement group
   )

Configuration Best Practices
-------------------------

1. **Start Simple**
   * Begin with minimal configuration and add options as needed
   * Use Standard Mode for development, then switch to Detached or Serverless as appropriate

2. **Resource Efficiency**
   * Use spot instances for non-critical or fault-tolerant workloads
   * Set appropriate min_blocks and max_blocks to control costs
   * Consider serverless mode for highly variable workloads

3. **Reliability**
   * Always enable state persistence for production workloads
   * Use Spot Fleet with multiple instance types for better availability
   * Implement worker_init commands idempotently

4. **Performance**
   * Choose instance types appropriate for your workload (compute, memory, or I/O optimized)
   * Use placement groups for network-intensive workloads
   * Set appropriate nodes_per_block for your parallelism needs

5. **Security**
   * Use IAM instance profiles instead of hard-coded credentials
   * Limit security group ingress rules to necessary ports and IP ranges
   * Consider using SecureString for sensitive parameters in Parameter Store

Next Steps
---------

* Learn about :doc:`state_persistence` options in detail
* Explore :doc:`resource_management` for controlling AWS resources
* See :doc:`spot_handling` for optimizing spot instance usage
* Check out :doc:`../operating_modes/index` for mode-specific details

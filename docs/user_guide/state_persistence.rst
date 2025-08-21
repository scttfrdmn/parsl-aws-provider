State Persistence
================

The Parsl Ephemeral AWS Provider supports state persistence mechanisms that allow it to save and recover its state, enabling workflow resilience and recovery across sessions and failures.

.. figure:: ../images/state_persistence.svg
   :alt: State Persistence Architecture
   :align: center
   :width: 80%
   :figclass: align-center

   State persistence architecture showing different storage options

Overview
-------

State persistence is a critical feature that allows the provider to:

* Recover from client or worker failures
* Resume workflows across sessions or client disconnections
* Handle spot instance interruptions gracefully
* Enable the Detached Mode of operation
* Provide workflow history and auditing capabilities

The provider offers multiple state persistence backends, each with different characteristics and use cases.

Available Storage Backends
-----------------------

Parameter Store
~~~~~~~~~~~~~

AWS Systems Manager Parameter Store is a secure, hierarchical storage service that's ideal for storing configuration and state data.

**Advantages:**
* Simple to use with minimal setup
* Integrated with AWS IAM for security
* Hierarchical organization of parameters
* Free tier available for standard parameters
* Built-in encryption options

**Limitations:**
* Maximum parameter size of 4KB (standard) or 8KB (advanced)
* Maximum 10,000 parameters per account (can be increased)
* Rate limiting on API calls
* Higher cost for advanced parameters

**Best for:**
* Small to medium workflows
* Configurations with many small parameters
* Workflows requiring secure parameter storage
* Default choice for most use cases

S3
~~

Amazon Simple Storage Service (S3) provides object storage optimized for high durability, availability, and virtually unlimited capacity.

**Advantages:**
* Virtually unlimited storage capacity
* High durability and availability
* Cost-effective for large volumes of data
* No size limitations per object (up to 5TB)
* Versioning and lifecycle policies

**Limitations:**
* Slightly more complex setup than Parameter Store
* Per-request costs (though generally very low)
* Eventually consistent by default

**Best for:**
* Large workflows with substantial state
* Workflows with large individual state objects
* Long-term storage of workflow state for archival
* When Parameter Store size limits are a concern

File
~~~~

Local file system storage on the client machine.

**Advantages:**
* Simplest setup with no AWS dependencies
* No additional costs
* Fastest access for local operations
* No AWS permissions required

**Limitations:**
* Not accessible from worker nodes
* Lost if client machine crashes or storage fails
* Not suitable for Detached Mode
* Requires careful file management

**Best for:**
* Development and testing
* Simple workflows in Standard Mode only
* When AWS storage is not available or desired

Configuration
-----------

To enable state persistence, set the following parameters in your provider configuration:

.. code-block:: python

   from parsl_ephemeral_aws import EphemeralAWSProvider

   provider = EphemeralAWSProvider(
       # Basic provider configuration
       image_id='ami-12345678',
       instance_type='t3.medium',
       region='us-west-2',

       # State persistence configuration
       state_store='parameter_store',  # 'parameter_store', 's3', or 'file'
       state_prefix='/parsl/workflows/my-workflow',  # Optional prefix

       # Optional: additional backend-specific settings
       state_config={
           # Parameter Store specific settings
           'parameter_type': 'String',  # 'String', 'StringList', or 'SecureString'
           'parameter_tier': 'Standard',  # 'Standard' or 'Advanced'

           # S3 specific settings
           'bucket_name': 'my-parsl-state-bucket',  # Custom bucket name
           'versioning': True,  # Enable S3 versioning

           # File specific settings
           'directory': '/path/to/state',  # Custom directory path
           'backup': True,  # Enable file backups
       }
   )

Required Parameters
~~~~~~~~~~~~~~~~

``state_store`` (String)
  The storage backend to use. Must be one of:
  - 'parameter_store': AWS Systems Manager Parameter Store
  - 's3': Amazon S3
  - 'file': Local file system
  - 'none': Disable state persistence (default)

Optional Parameters
~~~~~~~~~~~~~~~

``state_prefix`` (String)
  A prefix for state storage paths/keys. Useful for organizing multiple workflows and preventing collisions.

``state_config`` (Dict)
  Additional backend-specific configuration options as described above.

State Models
----------

The provider persists several types of state information:

Provider State
~~~~~~~~~~~

Overall provider state, including:
* Configured parameters
* Resource tracking
* Block management information
* Scaling history

This state is critical for basic operation and recovery.

Worker State
~~~~~~~~~~

Information about worker resources:
* Instance status and details
* IP addresses and connectivity information
* Job assignments and capacity

Used to track and manage compute resources.

Task State
~~~~~~~~

Information about Parsl tasks:
* Task definitions and dependencies
* Execution status and history
* Results or exception information

Enables task recovery and result retrieval.

Usage Examples
-----------

Parameter Store Example
~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic provider configuration
       image_id='ami-12345678',
       instance_type='t3.medium',
       region='us-west-2',

       # Parameter Store configuration
       state_store='parameter_store',
       state_prefix='/parsl/workflows/genome-analysis',
       state_config={
           'parameter_type': 'String',
           'parameter_tier': 'Standard',
       }
   )

S3 Example
~~~~~~~~

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic provider configuration
       image_id='ami-12345678',
       instance_type='t3.medium',
       region='us-west-2',

       # S3 configuration
       state_store='s3',
       state_prefix='workflows/climate-model',
       state_config={
           'bucket_name': 'my-parsl-state',
           'versioning': True,
       }
   )

File Example (Standard Mode Only)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic provider configuration
       image_id='ami-12345678',
       instance_type='t3.medium',
       region='us-west-2',

       # File configuration
       state_store='file',
       state_prefix='ml-training',
       state_config={
           'directory': '/home/user/parsl_state',
           'backup': True,
       }
   )

Workflow Recovery
--------------

To recover a workflow from persisted state:

1. **Use the same state configuration**:

   .. code-block:: python

      # Use the same state_store and state_prefix as the original workflow
      provider = EphemeralAWSProvider(
          region='us-west-2',
          state_store='parameter_store',
          state_prefix='/parsl/workflows/my-workflow',
      )

2. **Load the Parsl configuration**:

   .. code-block:: python

      config = Config(
          executors=[
              HighThroughputExecutor(
                  label='aws_executor',
                  provider=provider,
              )
          ]
      )

      parsl.load(config)

3. **Retrieve futures if needed**:

   If you saved task IDs from the original session, you can retrieve the futures:

   .. code-block:: python

      # Original session
      task_ids = [f.tid for f in futures]

      # Recovery session
      recovered_futures = [parsl.dfk().tasks[tid] for tid in task_ids]

      # Now you can use recovered_futures as normal
      for f in recovered_futures:
          print(f.result())

State Management
-------------

Managing State Lifecycle
~~~~~~~~~~~~~~~~~~~~

The provider automatically manages state lifecycle, including:

* Creating the initial state on workflow start
* Updating state as resources are created or modified
* Cleaning up state when resources are deleted

You can control this behavior with these parameters:

``state_cleanup`` (String)
  Controls when state is cleaned up. Options are:
  - 'always': Always clean up state when the provider is cleaned up (default)
  - 'never': Never automatically clean up state
  - 'success': Clean up state only if the workflow completes successfully

``state_retention_days`` (Integer)
  Number of days to retain state after the workflow completes (default: 7)

Manual State Operations
~~~~~~~~~~~~~~~~~~~

You can manually manage state using the provider's API:

.. code-block:: python

   # Save current state to a specific key
   provider.save_state("manual-checkpoint")

   # Load state from a specific key
   provider.load_state("manual-checkpoint")

   # List available state keys
   state_keys = provider.list_states()

   # Delete a specific state key
   provider.delete_state("old-checkpoint")

Troubleshooting
------------

State Corruption
~~~~~~~~~~~~~

If state becomes corrupted, you can reset it:

.. code-block:: python

   # Completely reset the provider state
   provider.reset_state()

   # Reset only worker state
   provider.reset_worker_state()

Permission Issues
~~~~~~~~~~~~~~

For AWS backends, ensure your IAM policies allow:

* For Parameter Store: `ssm:PutParameter`, `ssm:GetParameter`, and `ssm:DeleteParameter`
* For S3: `s3:PutObject`, `s3:GetObject`, and `s3:DeleteObject`

Rate Limiting
~~~~~~~~~~

Parameter Store has API rate limits. If you encounter them:

1. Switch to the Advanced parameter tier for higher throughput
2. Consider using S3 for larger workflows
3. Implement exponential backoff in your application

Best Practices
------------

1. **Always use state persistence for production workflows**
   * Even in Standard Mode, it provides important recovery capabilities
   * Essential for Detached Mode

2. **Choose the right backend for your workflow**
   * Parameter Store for most workflows
   * S3 for large-scale workflows
   * File only for development/testing in Standard Mode

3. **Use meaningful prefixes**
   * Include workflow name, version, and date
   * Example: `/parsl/workflows/genome-analysis/v2/2023-04-15`

4. **Consider security requirements**
   * Use SecureString parameter type for sensitive data
   * Enable S3 encryption for sensitive workflows
   * Apply appropriate IAM policies

5. **Implement state cleanup policies**
   * Don't keep state indefinitely unless needed
   * Set appropriate retention period

Next Steps
---------

* Learn about :doc:`spot_handling` for resilient execution with spot instances
* Explore :doc:`../operating_modes/detached_mode` which requires state persistence
* See :doc:`../advanced_topics/cost_optimization` for optimizing storage costs

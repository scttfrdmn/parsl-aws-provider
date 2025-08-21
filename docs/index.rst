Parsl Ephemeral AWS Provider
==========================

A modern, flexible AWS provider for the `Parsl <https://parsl-project.org/>`_ parallel scripting library
that leverages ephemeral resources for cost-effective, scalable scientific computation.

.. image:: https://badge.fury.io/py/parsl-ephemeral-aws.svg
   :target: https://badge.fury.io/py/parsl-ephemeral-aws
   :alt: PyPI version

.. image:: https://img.shields.io/badge/License-Apache%202.0-blue.svg
   :target: https://opensource.org/licenses/Apache-2.0
   :alt: License

.. image:: https://img.shields.io/pypi/pyversions/parsl-ephemeral-aws.svg
   :target: https://pypi.org/project/parsl-ephemeral-aws/
   :alt: Python Versions

.. image:: https://readthedocs.org/projects/parsl-ephemeral-aws/badge/?version=latest
   :target: https://parsl-ephemeral-aws.readthedocs.io/en/latest/?badge=latest
   :alt: Documentation Status

.. image:: https://github.com/scttfrdmn/parsl-aws-provider/actions/workflows/ci.yml/badge.svg
   :target: https://github.com/scttfrdmn/parsl-aws-provider/actions/workflows/ci.yml
   :alt: Build Status

.. image:: https://codecov.io/gh/scttfrdmn/parsl-aws-provider/branch/main/graph/badge.svg
   :target: https://codecov.io/gh/scttfrdmn/parsl-aws-provider
   :alt: codecov

.. note::

   The Parsl Ephemeral AWS Provider enables seamless execution of Parsl workflows on dynamically provisioned
   AWS resources with true ephemerality - resources are created when needed and destroyed when not,
   minimizing costs while maximizing scalability.

Key Features
-----------

* **Truly Ephemeral**: All resources (including VPC, security groups, etc.) are cleaned up automatically
* **Flexible Compute Options**: Supports EC2, Spot instances, Lambda, and ECS/Fargate
* **Modern AWS Integration**: Uses EC2 Fleet, Spot Fleet, auto-scaling groups, and other advanced AWS features
* **Resilient Execution**: Intelligently handles spot interruptions with state persistence
* **Multi-mode Operation**: Choose between standard, detached, or serverless execution modes

Documentation Overview
--------------------

.. grid:: 3

   .. grid-item-card:: Getting Started
      :link: getting_started/index
      :link-type: doc

      Learn how to install and get up and running quickly with basic examples.

   .. grid-item-card:: User Guide
      :link: user_guide/index
      :link-type: doc

      Comprehensive documentation for core features and configurations.

   .. grid-item-card:: Operating Modes
      :link: operating_modes/index
      :link-type: doc

      Explore the different operating modes: Standard, Detached, and Serverless.

   .. grid-item-card:: Advanced Topics
      :link: advanced_topics/index
      :link-type: doc

      Dive into advanced features like spot interruption handling, MPI, and more.

   .. grid-item-card:: Developer Guide
      :link: developer/index
      :link-type: doc

      Contributing, architecture, testing, and extending the provider.

   .. grid-item-card:: Examples & Tutorials
      :link: examples/index
      :link-type: doc

      Complete working examples and tutorials for common use cases.

Quick Example
-----------

.. code-block:: python

   from parsl.config import Config
   from parsl.executors import HighThroughputExecutor
   from parsl_ephemeral_aws import EphemeralAWSProvider

   # Configure the ephemeral AWS provider
   provider = EphemeralAWSProvider(
       image_id='ami-12345678',  # Amazon Linux 2 AMI
       instance_type='t3.medium',
       region='us-west-2',

       # Block parameters
       init_blocks=1,
       min_blocks=0,
       max_blocks=10,

       # Ephemeral settings
       use_spot_instances=True,
       spot_max_price_percentage=80,  # 80% of on-demand price

       # State persistence
       state_store='parameter_store',  # 'parameter_store', 's3', 'file', 'none'
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
   import parsl
   parsl.load(config)

   # Define and run your Parsl workflows
   @parsl.python_app
   def hello_world():
       return "Hello, World!"

   result = hello_world()
   print(result.result())

.. toctree::
   :maxdepth: 1
   :hidden:
   :caption: Getting Started

   getting_started/index
   getting_started/installation
   getting_started/quickstart
   getting_started/basic_concepts

.. toctree::
   :maxdepth: 1
   :hidden:
   :caption: User Guide

   user_guide/index
   user_guide/configuration
   user_guide/state_persistence
   user_guide/resource_management
   user_guide/spot_handling

.. toctree::
   :maxdepth: 1
   :hidden:
   :caption: Operating Modes

   operating_modes/index
   operating_modes/standard_mode
   operating_modes/detached_mode
   operating_modes/serverless_mode

.. toctree::
   :maxdepth: 1
   :hidden:
   :caption: Advanced Topics

   advanced_topics/index
   advanced_topics/cost_optimization
   advanced_topics/mpi_workflows
   advanced_topics/gpu_computing
   advanced_topics/security

.. toctree::
   :maxdepth: 1
   :hidden:
   :caption: Developer Guide

   developer/index
   developer/architecture
   developer/contributing
   developer/testing
   developer/extending

.. toctree::
   :maxdepth: 1
   :hidden:
   :caption: Examples & Tutorials

   examples/index
   examples/data_analysis
   examples/machine_learning
   examples/scientific_computing
   examples/hybrid_workflows

.. toctree::
   :maxdepth: 1
   :hidden:
   :caption: API Reference

   api/index
   api/provider
   api/modes
   api/compute
   api/network
   api/state

.. toctree::
   :maxdepth: 1
   :hidden:
   :caption: Project

   project/roadmap
   project/changelog
   project/license

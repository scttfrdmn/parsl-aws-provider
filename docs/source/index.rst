Welcome to Parsl Ephemeral AWS Provider's documentation!
=======================================================

.. image:: https://img.shields.io/github/license/scttfrdmn/parsl-aws-provider
   :alt: License
   :target: https://github.com/scttfrdmn/parsl-aws-provider/blob/main/LICENSE

.. image:: https://img.shields.io/github/workflow/status/scttfrdmn/parsl-aws-provider/CI/main
   :alt: GitHub Workflow Status
   :target: https://github.com/scttfrdmn/parsl-aws-provider/actions/workflows/ci-cd.yml

.. image:: https://codecov.io/gh/scttfrdmn/parsl-aws-provider/branch/main/graph/badge.svg
   :alt: Codecov
   :target: https://codecov.io/gh/scttfrdmn/parsl-aws-provider

.. image:: https://img.shields.io/pypi/v/parsl-ephemeral-aws
   :alt: PyPI
   :target: https://pypi.org/project/parsl-ephemeral-aws/

.. image:: https://img.shields.io/pypi/pyversions/parsl-ephemeral-aws
   :alt: PyPI - Python Version
   :target: https://pypi.org/project/parsl-ephemeral-aws/

The Parsl Ephemeral AWS Provider enables efficient execution of Parsl workflows on AWS infrastructure with ephemeral resources that are created on-demand and automatically cleaned up when no longer needed.

Features
--------

* **Multiple Operating Modes**: Standard, Detached, and Serverless modes to suit different workflow needs
* **Diverse Compute Resources**: Support for EC2 instances, Lambda functions, and ECS/Fargate tasks
* **Advanced Networking**: Automatic VPC configuration and security group management
* **State Persistence**: Multiple mechanisms for workflow state persistence and recovery
* **Cost Optimization**: Support for spot instances, auto-shutdown policies, and resource tagging
* **HPC Features**: MPI support for high-performance computing workloads
* **Testing Framework**: Comprehensive testing with LocalStack integration
* **Infrastructure-as-Code**: CloudFormation templates and Terraform modules

User Guide
---------

.. toctree::
   :maxdepth: 2
   :caption: User Guide
   
   user/installation
   user/quickstart
   user/configuration
   user/operating_modes
   user/compute_resources
   user/networking
   user/state_persistence
   user/examples
   user/troubleshooting
   user/faq

API Reference
------------

.. toctree::
   :maxdepth: 2
   :caption: API Reference
   
   api/provider
   api/modes
   api/compute
   api/network
   api/state
   api/utils
   api/templates

Advanced Topics
-------------

.. toctree::
   :maxdepth: 2
   :caption: Advanced Topics
   
   advanced/spot_instances
   advanced/mpi_support
   advanced/custom_networking
   advanced/security
   advanced/resource_tagging
   advanced/cost_optimization
   advanced/performance_tuning

Developer Guide
-------------

.. toctree::
   :maxdepth: 2
   :caption: Developer Guide
   
   dev/architecture
   dev/contributing
   dev/testing
   dev/localstack
   dev/ci_cd
   dev/release_process
   dev/roadmap

.. toctree::
   :maxdepth: 1
   :caption: Additional Information
   
   changelog
   license

Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`

.. SPDX-License-Identifier: Apache-2.0
.. SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
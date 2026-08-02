Parsl AWS Provider
==================

A provider for the `Parsl <https://parsl-project.org/>`_ parallel scripting library
that runs workflows on ephemeral AWS resources: instances are created when work
arrives and destroyed when it finishes.

.. image:: https://img.shields.io/badge/License-Apache%202.0-blue.svg
   :target: https://opensource.org/licenses/Apache-2.0
   :alt: License

.. image:: https://github.com/scttfrdmn/parsl-aws-provider/actions/workflows/ci.yml/badge.svg
   :target: https://github.com/scttfrdmn/parsl-aws-provider/actions/workflows/ci.yml
   :alt: Build Status

.. warning::

   This package is **alpha**. The public interface and configuration schema may
   change in any release before 1.0.0.

Key features
------------

* **Three operating modes** -- standard (direct client-to-worker), detached
  (a bastion orchestrates jobs so the client can disconnect), and serverless
  (Lambda or ECS/Fargate workers).
* **Spot support** -- on-demand or spot, single instances or EC2 Fleet, with
  ``price-capacity-optimized`` allocation and two-minute interruption warnings
  delivered through EventBridge and SQS.
* **State persistence** -- file, S3, or SSM Parameter Store, so a provider can be
  reconstructed after a restart.
* **Pre-provisioned networking** -- you supply the VPC, subnet, and security
  group; the provider never creates or deletes them. See
  :doc:`network-prerequisites`.

Networking is a prerequisite, not an option
-------------------------------------------

Since v0.7.0 ``vpc_id``, ``subnet_id``, and ``security_group_id`` are **required**
for every mode except serverless-with-Lambda workers. Read
:doc:`network-prerequisites` before your first run.

Quick example
-------------

.. code-block:: python

   import parsl
   from parsl.config import Config
   from parsl.executors import HighThroughputExecutor
   from parsl_aws_provider import EphemeralAWSProvider

   provider = EphemeralAWSProvider(
       region="us-east-1",
       instance_type="t3.medium",
       # Pre-provisioned network resources. Required -- see network-prerequisites.
       vpc_id="vpc-0123456789abcdef0",
       subnet_id="subnet-0123456789abcdef0",
       security_group_id="sg-0123456789abcdef0",
       # Block parameters
       init_blocks=1,
       min_blocks=0,
       max_blocks=10,
       # Spot instances at up to 80% of the on-demand price
       use_spot=True,
       spot_max_price_percentage=80,
       # State persistence: "file", "s3", or "parameter_store"
       state_store_type="file",
   )

   config = Config(
       executors=[
           HighThroughputExecutor(
               label="aws_executor",
               provider=provider,
               # See network-prerequisites: CurveZMQ certificates live in the
               # driver's run_dir and workers cannot read them (#62).
               encrypted=False,
           )
       ]
   )

   with parsl.load(config):

       @parsl.python_app
       def hello():
           return "Hello from AWS"

       print(hello().result())

``image_id`` is optional: an Amazon Linux 2023 AMI is resolved from AWS's public
SSM parameters for the region and architecture in use.

.. toctree::
   :maxdepth: 2
   :caption: Using the provider

   getting_started
   network-prerequisites
   operating_modes
   state_persistence
   spot_fleet
   examples
   troubleshooting

.. toctree::
   :maxdepth: 2
   :caption: Integrations

   globus_compute

.. toctree::
   :maxdepth: 2
   :caption: Reference

   api_reference
   security
   architecture

.. toctree::
   :maxdepth: 2
   :caption: Development

   substrate_testing
   ci_cd

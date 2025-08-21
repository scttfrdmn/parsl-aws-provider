Installation
============

This page provides instructions for installing the Parsl Ephemeral AWS Provider.

Requirements
-----------

* Python 3.8 or higher
* Parsl 1.2.0 or higher
* boto3 1.20.0 or higher
* AWS credentials configured

Installation Methods
------------------

From PyPI
~~~~~~~~~

The recommended installation method is via PyPI:

.. code-block:: bash

   pip install parsl-ephemeral-aws

From Source
~~~~~~~~~~~

You can also install from source:

.. code-block:: bash

   git clone https://github.com/scttfrdmn/parsl-aws-provider.git
   cd parsl-aws-provider
   pip install -e .

Optional Dependencies
-------------------

The package has several optional dependency groups:

Development Tools
~~~~~~~~~~~~~~~~

Install development dependencies:

.. code-block:: bash

   pip install "parsl-ephemeral-aws[dev]"

LocalStack Testing
~~~~~~~~~~~~~~~~~

Install LocalStack dependencies for local AWS service emulation:

.. code-block:: bash

   pip install "parsl-ephemeral-aws[localstack]"

Documentation
~~~~~~~~~~~~

Install documentation dependencies:

.. code-block:: bash

   pip install "parsl-ephemeral-aws[docs]"

Terraform Support
~~~~~~~~~~~~~~~~

Install Terraform integration dependencies:

.. code-block:: bash

   pip install "parsl-ephemeral-aws[terraform]"

All Dependencies
~~~~~~~~~~~~~~~

To install all optional dependencies:

.. code-block:: bash

   pip install "parsl-ephemeral-aws[all]"

AWS Credentials Setup
-------------------

The provider requires AWS credentials to interact with AWS services. There are several ways to configure credentials:

Environment Variables
~~~~~~~~~~~~~~~~~~~~

Set your AWS credentials as environment variables:

.. code-block:: bash

   export AWS_ACCESS_KEY_ID=your_access_key
   export AWS_SECRET_ACCESS_KEY=your_secret_key
   export AWS_DEFAULT_REGION=your_region  # e.g., us-east-1

Configuration Files
~~~~~~~~~~~~~~~~~~

Alternatively, configure credentials in the AWS credentials file:

.. code-block:: bash

   # Create or edit ~/.aws/credentials
   [default]
   aws_access_key_id = your_access_key
   aws_secret_access_key = your_secret_key

   # Create or edit ~/.aws/config
   [default]
   region = your_region

Parsl Configuration
-----------------

Once installed, you can configure Parsl to use the Ephemeral AWS Provider:

.. code-block:: python

   import parsl
   from parsl.config import Config
   from parsl_ephemeral_aws import EphemeralAWSProvider

   # Configure the provider
   provider = EphemeralAWSProvider(
       image_id='ami-0123456789abcdef0',
       instance_type='t3.micro',
       region='us-east-1',
       mode='standard',
       min_blocks=0,
       max_blocks=10,
   )

   # Configure Parsl
   config = Config(
       executors=[
           parsl.executors.HighThroughputExecutor(
               label='aws_executor',
               provider=provider,
           )
       ]
   )

   # Initialize Parsl with the configuration
   parsl.load(config)

Next Steps
---------

After installation, proceed to the :doc:`quickstart` guide to learn how to use the provider in your Parsl workflows.

.. SPDX-License-Identifier: Apache-2.0
.. SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors

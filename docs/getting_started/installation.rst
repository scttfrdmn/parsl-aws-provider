Installation
============

This guide walks you through the process of installing the Parsl Ephemeral AWS Provider and its dependencies.

Prerequisites
------------

Before installing, ensure you have:

* Python 3.9 or newer
* pip (Python package installer)
* AWS account with appropriate permissions
* AWS credentials configured locally

Installation Methods
-----------------

Standard Installation
~~~~~~~~~~~~~~~~~~

To install the latest stable version from PyPI:

.. code-block:: bash

   pip install parsl-ephemeral-aws

This will install the provider and its core dependencies.

Development Installation
~~~~~~~~~~~~~~~~~~~~~

To install the latest development version from GitHub:

.. code-block:: bash

   git clone https://github.com/scttfrdmn/parsl-aws-provider.git
   cd parsl-aws-provider
   pip install -e .

This creates an editable installation that reflects any changes you make to the code.

With Optional Dependencies
~~~~~~~~~~~~~~~~~~~~~~~

Install with additional dependencies for specific features:

.. code-block:: bash

   # For development tools
   pip install parsl-ephemeral-aws[dev]

   # For testing
   pip install parsl-ephemeral-aws[test]

   # For documentation generation
   pip install parsl-ephemeral-aws[docs]

   # For all optional dependencies
   pip install parsl-ephemeral-aws[all]

AWS Credentials Setup
-------------------

The provider requires AWS credentials to create and manage resources. You can configure these in several ways:

Using AWS CLI
~~~~~~~~~~

The easiest way is to use the AWS CLI:

.. code-block:: bash

   # Install the AWS CLI
   pip install awscli

   # Configure credentials
   aws configure

This will prompt you for your AWS Access Key ID, Secret Access Key, default region, and output format.

Environment Variables
~~~~~~~~~~~~~~~~~

You can also set credentials via environment variables:

.. code-block:: bash

   # Linux/macOS
   export AWS_ACCESS_KEY_ID=your_access_key
   export AWS_SECRET_ACCESS_KEY=your_secret_key
   export AWS_DEFAULT_REGION=us-west-2

   # Windows (Command Prompt)
   set AWS_ACCESS_KEY_ID=your_access_key
   set AWS_SECRET_ACCESS_KEY=your_secret_key
   set AWS_DEFAULT_REGION=us-west-2

Configuration File
~~~~~~~~~~~~~~~

Create or edit the AWS configuration file:

.. code-block:: bash

   # Location: ~/.aws/credentials (Linux/macOS) or %USERPROFILE%\.aws\credentials (Windows)
   [default]
   aws_access_key_id=your_access_key
   aws_secret_access_key=your_secret_key

   # Location: ~/.aws/config (Linux/macOS) or %USERPROFILE%\.aws\config (Windows)
   [default]
   region=us-west-2

Verifying Installation
-------------------

To verify that the installation was successful:

.. code-block:: python

   import parsl_ephemeral_aws
   print(parsl_ephemeral_aws.__version__)

This should print the version number without any errors.

Next Steps
---------

After successful installation, proceed to the :doc:`quickstart` guide to set up your first workflow.

Operating Modes
===============

The Parsl Ephemeral AWS Provider offers three distinct operating modes, each designed to support different workflow requirements and environments.

.. toctree::
   :maxdepth: 2

   standard_mode
   detached_mode
   serverless_mode

Available Modes
--------------

* :doc:`standard_mode` - Direct client-to-worker communication (simplest, best for development)
* :doc:`detached_mode` - Uses a bastion host to coordinate workers (best for long-running workflows)
* :doc:`serverless_mode` - Uses Lambda and ECS/Fargate for true serverless computing (best for burst workloads)

Choosing the Right Mode
---------------------

Consider these factors when selecting a mode:

* **Workflow Duration**: For short workflows, use Standard mode; for long-running workflows, use Detached mode
* **Client Stability**: If your client may disconnect, use Detached mode
* **Cost Sensitivity**: For maximum cost efficiency, use Serverless mode
* **Compute Requirements**: For large memory/CPU needs, use Standard or Detached mode; for short-running tasks, use Serverless mode

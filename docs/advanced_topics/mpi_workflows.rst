MPI Workflows
=============

This guide covers how to configure and run MPI (Message Passing Interface) workflows using the Parsl Ephemeral AWS Provider, enabling parallel scientific applications across multiple nodes.

.. figure:: ../images/mpi_workflows.svg
   :alt: MPI Workflow Architecture
   :align: center
   :width: 80%
   :figclass: align-center

   Multi-node MPI workflow architecture with orchestration across workers

Introduction to MPI with Parsl
---------------------------

Message Passing Interface (MPI) is a standardized communication protocol for parallel computing, allowing programs to run across multiple nodes while exchanging data through message passing. With the Parsl Ephemeral AWS Provider, you can:

1. Create clusters of EC2 instances for MPI computation
2. Configure the network, security, and placement for optimal MPI performance
3. Launch MPI jobs across multiple nodes with the appropriate launchers
4. Manage MPI resources dynamically based on workload requirements

MPI Configuration
-------------

Basic MPI Setup
~~~~~~~~~~~~

To enable MPI support, you need to configure both the Parsl launcher and the provider:

.. code-block:: python

   import parsl
   from parsl.config import Config
   from parsl.launchers import MpiRunLauncher
   from parsl.executors import HighThroughputExecutor
   from parsl_ephemeral_aws import EphemeralAWSProvider

   # Configure the provider for MPI
   provider = EphemeralAWSProvider(
       # Region and instance type
       region='us-west-2',
       instance_type='c5n.18xlarge',  # High-performance networking instance

       # Multi-node configuration
       nodes_per_block=4,             # 4 nodes per block for MPI
       init_blocks=1,
       max_blocks=5,

       # Network optimization for MPI
       placement_group='cluster',     # Use cluster placement group for low latency

       # Worker initialization for MPI
       worker_init='''
           # Update packages
           sudo yum update -y

           # Install OpenMPI
           sudo amazon-linux-extras install -y openmpi

           # Configure MPI environment
           echo "export PATH=$PATH:/usr/lib64/openmpi/bin" >> ~/.bashrc
           echo "export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/usr/lib64/openmpi/lib" >> ~/.bashrc

           # Install Python dependencies
           python3 -m pip install mpi4py numpy scipy
       ''',
   )

   # Configure the executor with MPI launcher
   config = Config(
       executors=[
           HighThroughputExecutor(
               label='mpi_executor',
               provider=provider,
               # Use MPI launcher
               launcher=MpiRunLauncher(
                   # MPI options
                   bind_cmd="--bind-to core",
                   overrides="--allow-run-as-root --mca btl_tcp_if_include eth0"
               ),
           )
       ]
   )

   # Load the configuration
   parsl.load(config)

Key Configuration Options
---------------------

Provider Configuration
~~~~~~~~~~~~~~~~~

``nodes_per_block`` (Integer)
  Number of nodes per block. For MPI, this should be the number of nodes you want to use for a single MPI job.

``instance_type`` (String)
  Choose instances optimized for HPC or with enhanced networking. Good options include:
  - c5n/m5n/r5n instances: Enhanced network performance
  - hpc6a instances: Optimized for HPC workloads
  - p4d instances: For GPU-accelerated MPI

``placement_group`` (String)
  Use 'cluster' placement group for minimum inter-node latency.

``worker_init`` (String)
  Script to install and configure MPI and related dependencies.

Launcher Configuration
~~~~~~~~~~~~~~~~~

``launcher`` (MpiRunLauncher)
  Configures how MPI jobs are launched. Options include:

  ``bind_cmd`` (String)
    Process binding options for mpirun (e.g., "--bind-to core").

  ``overrides`` (String)
    Additional options for mpirun command.

MPI Implementations
----------------

The provider supports multiple MPI implementations:

OpenMPI Configuration
~~~~~~~~~~~~~~~~

.. code-block:: python

   # Worker initialization for OpenMPI
   worker_init='''
       # Install OpenMPI
       sudo amazon-linux-extras install -y openmpi

       # Configure OpenMPI
       echo "export PATH=$PATH:/usr/lib64/openmpi/bin" >> ~/.bashrc
       echo "export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/usr/lib64/openmpi/lib" >> ~/.bashrc

       # Configure SSH for OpenMPI (not needed if using shared memory transport)
       mkdir -p ~/.ssh
       touch ~/.ssh/known_hosts
       ssh-keygen -t rsa -N "" -f ~/.ssh/id_rsa
       cat ~/.ssh/id_rsa.pub >> ~/.ssh/authorized_keys
       chmod 600 ~/.ssh/authorized_keys

       # Install mpi4py with OpenMPI
       python3 -m pip install mpi4py
   '''

   # MPI launcher for OpenMPI
   launcher=MpiRunLauncher(
       bind_cmd="--bind-to core",
       overrides="--mca btl_tcp_if_include eth0 --mca btl_base_verbose 30"
   )

Intel MPI Configuration
~~~~~~~~~~~~~~~~~~

.. code-block:: python

   # Worker initialization for Intel MPI
   worker_init='''
       # Install Intel MPI
       sudo yum-config-manager --add-repo https://yum.repos.intel.com/oneapi
       sudo rpm --import https://yum.repos.intel.com/intel-gpg-keys/GPG-PUB-KEY-INTEL-SW-PRODUCTS.PUB
       sudo yum install -y intel-oneapi-mpi intel-oneapi-mpi-devel

       # Configure Intel MPI environment
       source /opt/intel/oneapi/mpi/latest/env/vars.sh
       echo "source /opt/intel/oneapi/mpi/latest/env/vars.sh" >> ~/.bashrc

       # Install mpi4py with Intel MPI
       python3 -m pip install mpi4py
   '''

   # MPI launcher for Intel MPI
   launcher=MpiRunLauncher(
       bind_cmd="-binding process",
       overrides="-genv I_MPI_FABRICS=shm:ofi -genv FI_PROVIDER=efa"
   )

MPICH Configuration
~~~~~~~~~~~~~~~

.. code-block:: python

   # Worker initialization for MPICH
   worker_init='''
       # Install MPICH
       sudo yum install -y mpich mpich-devel

       # Configure MPICH environment
       echo "export PATH=$PATH:/usr/lib64/mpich/bin" >> ~/.bashrc
       echo "export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/usr/lib64/mpich/lib" >> ~/.bashrc

       # Install mpi4py with MPICH
       python3 -m pip install mpi4py
   '''

   # MPI launcher for MPICH
   launcher=MpiRunLauncher(
       bind_cmd="-binding core",
       overrides="-iface eth0"
   )

Network Optimization for MPI
-------------------------

The network configuration is crucial for MPI performance:

Instance Types with Enhanced Networking
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Choose instances with enhanced networking capabilities:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # C5n instances with up to 100 Gbps networking
       instance_type='c5n.18xlarge',

       # Or hpc6a instances for HPC workloads
       # instance_type='hpc6a.48xlarge',

       # Other options for EFA-supported instances
       # instance_type='m5n.24xlarge',
       # instance_type='r5n.24xlarge',
   )

Elastic Fabric Adapter (EFA)
~~~~~~~~~~~~~~~~~~~~~~~~~

For high-performance applications, use EFA - a network interface optimized for inter-instance communication:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Use an EFA-capable instance
       instance_type='c5n.18xlarge',

       # Request EFA interfaces
       elastic_fabric_adapter=True,

       # Configure security group for EFA
       security_group_ingress_additional=[
           # Allow all traffic between instances in the security group
           {
               'source_security_group': True,  # Self-reference
               'ip_protocol': '-1',            # All protocols
               'from_port': -1,
               'to_port': -1,
           }
       ],

       # Worker initialization for EFA
       worker_init='''
           # Install EFA drivers
           curl -O https://s3.amazonaws.com/ec2-efa-installer/aws-efa-installer-latest.tar.gz
           tar -xf aws-efa-installer-latest.tar.gz
           cd aws-efa-installer
           sudo ./efa_installer.sh -y

           # Configure Open MPI to use EFA
           echo "export FI_PROVIDER=efa" >> ~/.bashrc
           echo "export FI_EFA_USE_DEVICE_RDMA=1" >> ~/.bashrc

           # Install MPI and mpi4py
           sudo yum install -y openmpi-devel
           source /etc/profile.d/modules.sh
           module load mpi/openmpi-x86_64
           python3 -m pip install mpi4py
       ''',
   )

Placement Groups
~~~~~~~~~~~~

For lowest-latency networking, use cluster placement groups:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Basic configuration
       region='us-west-2',
       instance_type='c5n.18xlarge',

       # Use cluster placement group
       placement_group='cluster',

       # Or create a new placement group with a specific name
       create_placement_group=True,
       placement_group_name='mpi-cluster',
   )

Creating MPI Applications
---------------------

There are multiple ways to create MPI applications with Parsl:

Python MPI Applications with mpi4py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   @parsl.python_app
   def mpi_hello_world(nodes, ranks_per_node):
       """MPI Hello World using mpi4py."""
       # Create a temporary python script
       with open('mpi_hello.py', 'w') as f:
           f.write('''
from mpi4py import MPI
import socket
import os

comm = MPI.COMM_WORLD
rank = comm.Get_rank()
size = comm.Get_size()

hostname = socket.gethostname()
pid = os.getpid()

print(f"Hello from rank {rank}/{size} on {hostname} (PID: {pid})")

if rank == 0:
    print(f"Total processes: {size}")
    # Gather hostnames from all ranks
    hostnames = comm.gather(hostname, root=0)
    unique_hosts = set(hostnames)
    print(f"Running on {len(unique_hosts)} nodes: {', '.join(unique_hosts)}")
else:
    comm.gather(hostname, root=0)
''')

       # Use mpirun directly in the app function
       from subprocess import check_output
       import sys

       cmd = f"mpirun -n {nodes * ranks_per_node} -npernode {ranks_per_node} python3 mpi_hello.py"
       output = check_output(cmd, shell=True, universal_newlines=True)
       return output

Bash MPI Applications
~~~~~~~~~~~~~~~~~

.. code-block:: python

   @parsl.bash_app
   def mpi_bash_app(nodes, ranks_per_node, stdout=parsl.AUTO_LOGNAME, stderr=parsl.AUTO_LOGNAME):
       """MPI application using a bash script."""
       return f'''
# Create an MPI C program
cat > mpi_hello.c << EOL
#include <mpi.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

int main(int argc, char** argv) {
    MPI_Init(&argc, &argv);

    int world_size, world_rank;
    char hostname[256];

    MPI_Comm_size(MPI_COMM_WORLD, &world_size);
    MPI_Comm_rank(MPI_COMM_WORLD, &world_rank);
    gethostname(hostname, 256);

    printf("Hello from rank %d/%d on %s\\n", world_rank, world_size, hostname);

    if (world_rank == 0) {
        printf("Total processes: %d\\n", world_size);
    }

    MPI_Finalize();
    return 0;
}
EOL

# Compile the program
mpicc -o mpi_hello mpi_hello.c

# Run with mpirun
mpirun -n {nodes * ranks_per_node} -npernode {ranks_per_node} ./mpi_hello
'''

File-Based MPI Applications
~~~~~~~~~~~~~~~~~~~~~~~

For existing MPI applications, create a script file and run it:

.. code-block:: python

   # Write the MPI script first
   with open("run_lammps.sh", "w") as f:
       f.write('''#!/bin/bash
# Load required modules
module load mpi/openmpi-x86_64

# Run LAMMPS simulation
mpirun -n $1 lmp -in input.lammps
''')

   # Make the script executable
   import os
   os.chmod("run_lammps.sh", 0o755)

   # Define the app
   @parsl.bash_app
   def run_lammps(nodes, ranks_per_node, input_file, stdout=parsl.AUTO_LOGNAME, stderr=parsl.AUTO_LOGNAME):
       total_ranks = nodes * ranks_per_node
       return f"./run_lammps.sh {total_ranks} < {input_file}"

   # Run the app
   job = run_lammps(4, 16, "simulation.in")
   print(job.result())

Running MPI Jobs
------------

Here's a complete example showing how to run an MPI workflow:

.. code-block:: python

   import parsl
   from parsl.config import Config
   from parsl.launchers import MpiRunLauncher
   from parsl.executors import HighThroughputExecutor
   from parsl_ephemeral_aws import EphemeralAWSProvider

   # Configure provider for MPI
   provider = EphemeralAWSProvider(
       # Region and instance type
       region='us-west-2',
       instance_type='c5n.9xlarge',  # 36 vCPUs, 96 GB RAM, 50 Gbps network

       # Multi-node configuration
       nodes_per_block=4,            # 4 nodes per block
       init_blocks=1,
       max_blocks=1,

       # Network optimization
       placement_group='cluster',

       # Worker initialization
       worker_init='''
           # Install MPI and dependencies
           sudo yum update -y
           sudo amazon-linux-extras install -y openmpi
           sudo yum install -y openmpi-devel

           # Configure environment
           echo "export PATH=\$PATH:/usr/lib64/openmpi/bin" >> ~/.bashrc
           echo "export LD_LIBRARY_PATH=\$LD_LIBRARY_PATH:/usr/lib64/openmpi/lib" >> ~/.bashrc
           source ~/.bashrc

           # Install Python dependencies
           python3 -m pip install mpi4py numpy

           # Show MPI version
           mpirun --version
       ''',
   )

   # Configure Parsl
   config = Config(
       executors=[
           HighThroughputExecutor(
               label='mpi_executor',
               provider=provider,
               launcher=MpiRunLauncher(
                   bind_cmd="--bind-to core",
                   overrides="--allow-run-as-root"
               ),
           )
       ]
   )

   # Load configuration
   parsl.load(config)

   # Define an MPI Python app
   @parsl.python_app
   def mpi_matrix_multiply(size=1000, ranks_per_node=36):
       import os
       import numpy as np

       # Create a temporary Python script for MPI
       with open('mpi_matmul.py', 'w') as f:
           f.write('''
from mpi4py import MPI
import numpy as np
import time
import socket

def matrix_multiply(rank, size, matrix_size):
    # Initialize MPI
    comm = MPI.COMM_WORLD
    hostname = socket.gethostname()

    # Create matrices
    if rank == 0:
        # Only root creates the full matrices
        A = np.random.rand(matrix_size, matrix_size)
        B = np.random.rand(matrix_size, matrix_size)
        start_time = time.time()
    else:
        # Other ranks create empty matrices to receive their portions
        A = None
        B = np.random.rand(matrix_size, matrix_size)  # All ranks need the full B matrix

    # Calculate rows per process
    rows_per_process = matrix_size // size

    # Create buffer for scatter
    if rank == 0:
        A_local = np.zeros((rows_per_process, matrix_size))
    else:
        A_local = np.zeros((rows_per_process, matrix_size))

    # Scatter rows of A to different processes
    # Since scatter requires same size chunks, we do this manually for flexibility
    if rank == 0:
        # Root sends portions to each process
        for i in range(1, size):
            start_row = i * rows_per_process
            end_row = (i + 1) * rows_per_process
            if i == size - 1:  # Last process gets any remaining rows
                end_row = matrix_size
            comm.send(A[start_row:end_row], dest=i)
        # Root keeps its own portion
        A_local = A[:rows_per_process]
    else:
        # Other processes receive their portion
        A_local = comm.recv(source=0)

    # Let everyone know we've distributed the data
    comm.Barrier()

    # Broadcast matrix B to all processes
    if rank == 0:
        B_local = B
    else:
        B_local = np.zeros((matrix_size, matrix_size))

    B_local = comm.bcast(B_local, root=0)

    # Perform local matrix multiplication
    C_local = np.matmul(A_local, B_local)

    # Gather results back to root
    if rank == 0:
        # Initialize the result matrix
        C = np.zeros((matrix_size, matrix_size))
        # Copy local result to the appropriate position
        C[:rows_per_process] = C_local
        # Receive results from other processes
        for i in range(1, size):
            start_row = i * rows_per_process
            end_row = (i + 1) * rows_per_process
            if i == size - 1:  # Last process may have extra rows
                end_row = matrix_size
            C[start_row:end_row] = comm.recv(source=i)

        # Calculate performance
        end_time = time.time()
        elapsed = end_time - start_time
        gflops = (2 * matrix_size**3) / (elapsed * 1e9)  # 2*N^3 FLOPs for matmul

        print(f"Matrix size: {matrix_size}x{matrix_size}")
        print(f"Total processes: {size} across {len(set(comm.allgather(hostname)))} nodes")
        print(f"Execution time: {elapsed:.2f} seconds")
        print(f"Performance: {gflops:.2f} GFLOPS")

        # Verify with sequential computation for small matrices
        if matrix_size <= 1000:
            C_seq = np.matmul(A, B)
            error = np.max(np.abs(C - C_seq))
            print(f"Maximum error: {error}")

        return {
            'matrix_size': matrix_size,
            'processes': size,
            'nodes': len(set(comm.allgather(hostname))),
            'execution_time': elapsed,
            'gflops': gflops,
            'hostnames': list(set(comm.allgather(hostname)))
        }
    else:
        # Send local result back to root
        comm.send(C_local, dest=0)
        return None

# Main function
if __name__ == "__main__":
    # Initialize MPI
    comm = MPI.COMM_WORLD
    rank = comm.Get_rank()
    size = comm.Get_size()

    # Set matrix size from command line or default
    import sys
    matrix_size = int(sys.argv[1]) if len(sys.argv) > 1 else 1000

    # Run the matrix multiplication
    result = matrix_multiply(rank, size, matrix_size)

    # Only rank 0 returns a result
    if rank == 0:
        print(f"Results: {result}")
''')

       # Determine total number of ranks
       nodes = 4  # Hard-coded to match nodes_per_block
       total_ranks = nodes * ranks_per_node

       # Execute the MPI program
       from subprocess import check_output
       cmd = f"mpirun -n {total_ranks} -npernode {ranks_per_node} python3 mpi_matmul.py {size}"
       output = check_output(cmd, shell=True, universal_newlines=True)

       # Parse the results (if needed)
       results = {}
       for line in output.splitlines():
           if 'Execution time:' in line:
               results['time'] = float(line.split(':')[1].strip().split()[0])
           elif 'Performance:' in line:
               results['gflops'] = float(line.split(':')[1].strip().split()[0])

       return {
           'output': output,
           'time': results.get('time'),
           'gflops': results.get('gflops'),
           'size': size,
           'nodes': nodes,
           'ranks_per_node': ranks_per_node,
           'total_ranks': total_ranks
       }

   # Run the MPI matrix multiplication with different sizes
   results = []
   for size in [1000, 2000, 4000, 8000]:
       print(f"Starting matrix multiplication with size {size}...")
       future = mpi_matrix_multiply(size=size)
       results.append(future)

   # Wait for results and print performance
   for future in results:
       result = future.result()
       print(f"Matrix size: {result['size']}x{result['size']}")
       print(f"Execution time: {result['time']:.2f} seconds")
       print(f"Performance: {result['gflops']:.2f} GFLOPS")
       print(f"Using {result['total_ranks']} ranks across {result['nodes']} nodes")
       print("-" * 40)

   # Clean up
   parsl.dfk().cleanup()

MPI with Different Operating Modes
-------------------------------

MPI works differently with each operating mode:

Standard Mode
~~~~~~~~~~

In Standard Mode, MPI processes run directly on EC2 instances, with the client coordinating the execution:

.. code-block:: python

   provider = EphemeralAWSProvider(
       mode='standard',  # Default mode
       nodes_per_block=4,
       instance_type='c5n.18xlarge',
       placement_group='cluster',
   )

Detached Mode
~~~~~~~~~~

In Detached Mode, the bastion host coordinates the MPI execution:

.. code-block:: python

   provider = EphemeralAWSProvider(
       mode='detached',
       nodes_per_block=4,
       instance_type='c5n.18xlarge',
       bastion_instance_type='c5.xlarge',  # Need sufficient capacity for MPI coordination
       placement_group='cluster',

       # Must use state persistence
       state_store='parameter_store',
   )

Serverless Mode with Spot Fleet
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

For MPI in Serverless Mode, use Spot Fleet rather than Lambda/ECS:

.. code-block:: python

   provider = EphemeralAWSProvider(
       mode='serverless',

       # Use Spot Fleet for MPI
       use_spot_fleet=True,
       nodes_per_block=4,
       instance_types=['c5n.18xlarge'],

       # MPI configuration
       placement_group='cluster',
   )

Performance Optimization
--------------------

To achieve optimal MPI performance:

Instance Sizing
~~~~~~~~~~~

Choose instances with adequate resources:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # High-performance instances
       instance_type='c5n.18xlarge',  # 72 vCPUs, 192 GB RAM, 100 Gbps networking

       # Or memory-optimized
       # instance_type='r5n.24xlarge',  # 96 vCPUs, 768 GB RAM, 100 Gbps networking

       # Or compute-optimized
       # instance_type='c6a.48xlarge',  # 192 vCPUs, 384 GB RAM, 50 Gbps networking
   )

Process Binding
~~~~~~~~~~~

Configure MPI to bind processes to cores:

.. code-block:: python

   # For OpenMPI
   launcher=MpiRunLauncher(
       bind_cmd="--bind-to core",
   )

   # For Intel MPI
   launcher=MpiRunLauncher(
       bind_cmd="-binding process",
   )

   # For MPICH
   launcher=MpiRunLauncher(
       bind_cmd="-binding core",
   )

Network Interface Selection
~~~~~~~~~~~~~~~~~~~~~~~

Explicitly select the appropriate network interface:

.. code-block:: python

   # For OpenMPI
   launcher=MpiRunLauncher(
       overrides="--mca btl_tcp_if_include eth0",
   )

   # For Intel MPI with EFA
   launcher=MpiRunLauncher(
       overrides="-genv I_MPI_FABRICS=shm:ofi -genv FI_PROVIDER=efa",
   )

   # For MPICH
   launcher=MpiRunLauncher(
       overrides="-iface eth0",
   )

Proper Process Distribution
~~~~~~~~~~~~~~~~~~~~~~~

Balance processes across nodes:

.. code-block:: python

   # For processes-per-node equal to vCPUs
   @parsl.python_app
   def mpi_app_balanced():
       import multiprocessing
       cores_per_node = multiprocessing.cpu_count()
       nodes = 4

       cmd = f"mpirun -n {nodes * cores_per_node} -npernode {cores_per_node} ./my_mpi_app"
       # ... (rest of the app)

Common MPI Challenges and Solutions
-------------------------------

1. **SSH Configuration for MPI**

   Some MPI implementations require SSH connectivity between nodes:

   .. code-block:: python

      worker_init='''
          # Configure SSH for MPI
          mkdir -p ~/.ssh
          ssh-keygen -t rsa -N "" -f ~/.ssh/id_rsa
          cat ~/.ssh/id_rsa.pub >> ~/.ssh/authorized_keys
          chmod 600 ~/.ssh/authorized_keys

          # Allow SSH between nodes without host key checking
          echo "Host *
            StrictHostKeyChecking no
            UserKnownHostsFile=/dev/null" > ~/.ssh/config
      '''

2. **Handling Host Files**

   Some workflows benefit from explicit host files:

   .. code-block:: python

      @parsl.python_app
      def create_hostfile():
          """Create a hostfile for MPI."""
          import socket
          import os
          from subprocess import check_output

          # Get hostnames of all nodes
          cmd = 'sinfo -N -o "%N" | tail -n +2'
          hostnames = check_output(cmd, shell=True).decode().splitlines()

          # Write hostfile
          with open('hostfile', 'w') as f:
              for host in hostnames:
                  f.write(f"{host} slots=36\n")

          return os.path.abspath('hostfile')

      @parsl.bash_app
      def mpi_app(hostfile):
          return f"mpirun --hostfile {hostfile} -n 144 ./mpi_program"

      # Create hostfile then run MPI program
      hostfile = create_hostfile()
      job = mpi_app(hostfile)
      print(job.result())

3. **Environment Propagation**

   Ensure environment variables are properly propagated to MPI processes:

   .. code-block:: python

      launcher=MpiRunLauncher(
          overrides="--forward-env PATH,LD_LIBRARY_PATH,PYTHONPATH",
      )

4. **Debugging MPI Issues**

   Enable verbose output for debugging:

   .. code-block:: python

      # For OpenMPI
      launcher=MpiRunLauncher(
          overrides="--verbose --mca btl_base_verbose 30",
      )

      # For Intel MPI
      launcher=MpiRunLauncher(
          overrides="-verbose -trace",
      )

Best Practices
-----------

1. **Test Configuration with Small Jobs**
   * Start with small test jobs before scaling up
   * Use smaller instance types during development

2. **Monitor Performance and Scaling**
   * Track strong and weak scaling of your application
   * Find the optimal number of nodes and processes per node

3. **Use Proper Network Configuration**
   * Cluster placement groups for lowest latency
   * EFA for HPC workloads
   * Select appropriate network interfaces

4. **Optimize Instance Selection**
   * Match instance type to application requirements
   * Consider CPU, memory, and network bandwidth needs

5. **Handle Fault Tolerance**
   * Implement state persistence for job recovery
   * Use spot fleet with multiple instance types for better availability

Example: Real-world Scientific Simulation
-------------------------------------

Here's a complete example for running LAMMPS molecular dynamics simulations:

.. code-block:: python

   import parsl
   from parsl.config import Config
   from parsl.launchers import MpiRunLauncher
   from parsl.executors import HighThroughputExecutor
   from parsl_ephemeral_aws import EphemeralAWSProvider

   # Configure provider for HPC simulation
   provider = EphemeralAWSProvider(
       # Region and instance type
       region='us-west-2',
       instance_type='c5n.18xlarge',

       # Multi-node configuration
       nodes_per_block=4,
       init_blocks=1,
       max_blocks=1,

       # Network optimization
       placement_group='cluster',

       # Worker initialization for LAMMPS
       worker_init='''
           # Update and install dependencies
           sudo yum update -y
           sudo yum install -y amazon-linux-extras
           sudo amazon-linux-extras install -y openmpi
           sudo yum install -y openmpi-devel fftw-devel

           # Configure environment
           echo "export PATH=\$PATH:/usr/lib64/openmpi/bin" >> ~/.bashrc
           echo "export LD_LIBRARY_PATH=\$LD_LIBRARY_PATH:/usr/lib64/openmpi/lib" >> ~/.bashrc
           source ~/.bashrc

           # Download and build LAMMPS
           mkdir -p ~/software
           cd ~/software
           git clone -b stable https://github.com/lammps/lammps.git
           cd lammps
           mkdir build
           cd build
           cmake ../cmake -D BUILD_MPI=yes -D BUILD_OMP=yes
           cmake --build .
           sudo make install

           # Verify installation
           lmp -help
       ''',
   )

   # Configure executor with MPI launcher
   config = Config(
       executors=[
           HighThroughputExecutor(
               label='lammps_executor',
               provider=provider,
               launcher=MpiRunLauncher(
                   bind_cmd="--bind-to core",
                   overrides="--mca btl_tcp_if_include eth0"
               ),
           )
       ]
   )

   # Load configuration
   parsl.load(config)

   # Define a function to create a LAMMPS input file
   @parsl.python_app
   def create_lammps_input(size=50, timesteps=10000):
       """Create a LAMMPS input file for LJ fluid simulation."""
       with open('lj.lammps', 'w') as f:
           f.write(f'''# 3D Lennard-Jones fluid simulation

   # Initialization
   units           lj
   dimension       3
   boundary        p p p
   atom_style      atomic

   # System definition
   lattice         fcc 0.8442
   region          box block 0 {size} 0 {size} 0 {size}
   create_box      1 box
   create_atoms    1 box
   mass            1 1.0

   # Force field
   pair_style      lj/cut 2.5
   pair_coeff      1 1 1.0 1.0 2.5

   # Settings
   neighbor        0.3 bin
   neigh_modify    every 20 delay 0 check no

   # Equilibration
   velocity        all create 1.44 87287 loop geom
   fix             1 all nve
   timestep        0.005

   # Diagnostics
   thermo_style    custom step temp pe ke etotal press
   thermo          500

   # Run simulation
   run             {timesteps}
   ''')
       return 'lj.lammps'

   # Define a LAMMPS simulation app
   @parsl.bash_app
   def run_lammps_simulation(input_file, ranks_per_node=36, timesteps=10000, size=50, stdout=parsl.AUTO_LOGNAME, stderr=parsl.AUTO_LOGNAME):
       """Run a LAMMPS simulation using MPI."""
       nodes = 4  # Match nodes_per_block
       total_ranks = nodes * ranks_per_node

       return f'''
   # Check environment
   echo "Running on $(hostname) with $(nproc) cores"

   # Run LAMMPS
   mpirun -n {total_ranks} -npernode {ranks_per_node} lmp -in {input_file} -var size {size} -var timesteps {timesteps} -pk omp 1

   # Analyze results
   echo "Simulation completed with {total_ranks} processes"
   grep "Loop time" log.lammps
   '''

   # Create input file for simulation
   system_sizes = [50, 100, 200]
   timesteps = 5000

   results = []
   for size in system_sizes:
       print(f"Setting up simulation with system size {size}...")
       input_file = create_lammps_input(size=size, timesteps=timesteps)

       print(f"Running simulation with system size {size}...")
       sim = run_lammps_simulation(input_file, timesteps=timesteps, size=size)
       results.append((size, sim))

   # Wait for results and print performance
   for size, future in results:
       result = future.result()
       print(f"\nSystem size: {size}^3")
       print(f"Result:\n{result}")

   # Clean up
   parsl.dfk().cleanup()

Next Steps
---------

* Explore :doc:`gpu_computing` for accelerated MPI workloads
* Learn about :doc:`../user_guide/resource_management` for managing MPI resources
* See :doc:`cost_optimization` for optimizing MPI workload costs
* Check out :doc:`../examples/scientific_computing` for more MPI examples

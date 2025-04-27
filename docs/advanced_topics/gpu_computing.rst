GPU Computing
=============

This guide explains how to configure and use GPU-accelerated computing with the Parsl Ephemeral AWS Provider, enabling high-performance machine learning, scientific computing, and visualization workflows.

.. figure:: ../images/gpu_computing.svg
   :alt: GPU Computing Architecture
   :align: center
   :width: 80%
   :figclass: align-center

   GPU computing architecture with various AWS GPU instance types and software stacks

Introduction to GPU Computing with AWS
----------------------------------

Graphics Processing Units (GPUs) excel at parallel workloads like machine learning, high-performance computing, and rendering. The Parsl Ephemeral AWS Provider enables GPU-accelerated computing by:

1. Provisioning AWS GPU-enabled instance types
2. Setting up the required drivers and acceleration libraries
3. Configuring the environment for popular GPU frameworks
4. Managing GPU resources efficiently

GPU Configuration
-------------

Basic GPU Setup
~~~~~~~~~~~~

Configure the provider to use GPU-enabled instances:

.. code-block:: python

   import parsl
   from parsl.config import Config
   from parsl.executors import HighThroughputExecutor
   from parsl_ephemeral_aws import EphemeralAWSProvider
   
   # Configure the provider for GPU computing
   provider = EphemeralAWSProvider(
       # Region and instance type
       region='us-west-2',
       instance_type='g4dn.xlarge',  # NVIDIA T4 GPU instance
       
       # Resource configuration
       init_blocks=1,
       min_blocks=0,
       max_blocks=4,
       
       # Spot instances for cost savings (optional)
       use_spot_instances=True,
       spot_max_price_percentage=80,
       
       # Worker initialization for CUDA setup
       worker_init='''
           # Update packages
           sudo yum update -y
           
           # Install GPU drivers and CUDA
           sudo yum install -y amazon-linux-extras
           sudo amazon-linux-extras install -y epel
           sudo amazon-linux-extras install -y kernel-ng
           sudo yum install -y gcc make dkms
           sudo yum install -y kernel-devel-$(uname -r)
           
           # Install NVIDIA drivers and CUDA toolkit
           sudo yum install -y nvidia cuda-toolkit-11-4
           
           # Install Python and essential libraries
           sudo yum install -y python3-devel
           python3 -m pip install --upgrade pip
           python3 -m pip install torch torchvision torchaudio --extra-index-url https://download.pytorch.org/whl/cu114
           python3 -m pip install tensorflow
           
           # Test GPU availability
           nvidia-smi
           python3 -c "import torch; print('CUDA available:', torch.cuda.is_available()); print('GPU count:', torch.cuda.device_count())"
       ''',
   )
   
   # Configure the executor
   config = Config(
       executors=[
           HighThroughputExecutor(
               label='gpu_executor',
               provider=provider,
           )
       ]
   )
   
   # Load the configuration
   parsl.load(config)

Available GPU Instance Types
------------------------

AWS offers several GPU instance families for different workloads:

G-series (Graphics)
~~~~~~~~~~~~~~~

Ideal for graphics workloads, machine learning inference, and small-scale training:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # G4dn instances with NVIDIA T4 GPUs
       instance_type='g4dn.xlarge',     # 1 T4 GPU, 4 vCPUs, 16 GB RAM
       # or
       # instance_type='g4dn.4xlarge',  # 1 T4 GPU, 16 vCPUs, 64 GB RAM
       # or
       # instance_type='g4dn.8xlarge',  # 1 T4 GPU, 32 vCPUs, 128 GB RAM
       # or
       # instance_type='g4dn.16xlarge', # 1 T4 GPU, 64 vCPUs, 256 GB RAM
       # or
       # instance_type='g4dn.12xlarge', # 4 T4 GPUs, 48 vCPUs, 192 GB RAM
       # or 
       # instance_type='g4dn.metal',    # 8 T4 GPUs, 96 vCPUs, 384 GB RAM
   )

P-series (Performance)
~~~~~~~~~~~~~~~~~

For compute-intensive workloads, deep learning training, and HPC:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # P3 instances with NVIDIA V100 GPUs
       instance_type='p3.2xlarge',      # 1 V100 GPU, 8 vCPUs, 61 GB RAM
       # or
       # instance_type='p3.8xlarge',    # 4 V100 GPUs, 32 vCPUs, 244 GB RAM
       # or
       # instance_type='p3.16xlarge',   # 8 V100 GPUs, 64 vCPUs, 488 GB RAM
       # or
       # instance_type='p3dn.24xlarge', # 8 V100 GPUs, 96 vCPUs, 768 GB RAM, 100 Gbps networking
       
       # P4d instances with NVIDIA A100 GPUs
       # instance_type='p4d.24xlarge',  # 8 A100 GPUs, 96 vCPUs, 1152 GB RAM, EFA networking
   )

P5 (Latest Generation)
~~~~~~~~~~~~~~~~~

For the most demanding machine learning workloads:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # P5 instances with NVIDIA H100 GPUs
       instance_type='p5.48xlarge',     # 8 H100 GPUs, 192 vCPUs, 2 TB RAM, EFA networking
   )

GPU Software Setup
--------------

NVIDIA Drivers and CUDA
~~~~~~~~~~~~~~~~~~~

Install NVIDIA drivers and CUDA toolkit:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Instance configuration
       region='us-west-2',
       instance_type='g4dn.xlarge',
       
       # NVIDIA driver and CUDA setup
       worker_init='''
           # Install NVIDIA drivers
           sudo yum install -y amazon-linux-extras
           sudo amazon-linux-extras install -y epel
           sudo amazon-linux-extras install -y kernel-ng
           sudo yum install -y gcc make dkms kernel-devel-$(uname -r)
           sudo yum install -y nvidia
           
           # Install CUDA toolkit
           sudo yum install -y cuda-toolkit-11-6
           
           # Configure environment
           echo 'export PATH=/usr/local/cuda-11.6/bin${PATH:+:${PATH}}' >> ~/.bashrc
           echo 'export LD_LIBRARY_PATH=/usr/local/cuda-11.6/lib64${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}' >> ~/.bashrc
           source ~/.bashrc
           
           # Test installation
           nvidia-smi
           nvcc --version
       ''',
   )

Deep Learning Frameworks
~~~~~~~~~~~~~~~~~~~~

Set up environments for popular frameworks:

PyTorch Setup
^^^^^^^^^^

.. code-block:: python

   worker_init='''
       # Install system dependencies
       sudo yum update -y
       sudo yum install -y python3-devel
       
       # Upgrade pip
       python3 -m pip install --upgrade pip
       
       # Install PyTorch with CUDA support
       python3 -m pip install torch torchvision torchaudio --extra-index-url https://download.pytorch.org/whl/cu116
       
       # Test PyTorch GPU support
       python3 -c "import torch; print('CUDA available:', torch.cuda.is_available()); print('GPU count:', torch.cuda.device_count()); print('GPU name:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'N/A')"
   '''

TensorFlow Setup
^^^^^^^^^^^^

.. code-block:: python

   worker_init='''
       # Install system dependencies
       sudo yum update -y
       sudo yum install -y python3-devel
       
       # Upgrade pip
       python3 -m pip install --upgrade pip
       
       # Install TensorFlow with GPU support
       python3 -m pip install tensorflow
       
       # Configure environment variables
       echo 'export TF_FORCE_GPU_ALLOW_GROWTH=true' >> ~/.bashrc
       source ~/.bashrc
       
       # Test TensorFlow GPU support
       python3 -c "import tensorflow as tf; print('GPU available:', tf.config.list_physical_devices('GPU')); print('TensorFlow version:', tf.__version__)"
   '''

JAX Setup
^^^^^^^

.. code-block:: python

   worker_init='''
       # Install system dependencies
       sudo yum update -y
       sudo yum install -y python3-devel
       
       # Upgrade pip
       python3 -m pip install --upgrade pip
       
       # Install JAX with CUDA support
       python3 -m pip install --upgrade jax jaxlib==0.3.10+cuda11.cudnn82 -f https://storage.googleapis.com/jax-releases/jax_cuda_releases.html
       
       # Test JAX GPU support
       python3 -c "import jax; print('GPU devices:', jax.devices()); print('JAX version:', jax.__version__)"
   '''

HPC Applications
^^^^^^^^^^^^^

For HPC applications like NAMD, LAMMPS, etc.:

.. code-block:: python

   worker_init='''
       # Install system dependencies
       sudo yum update -y
       sudo yum install -y gcc-c++ make openmpi-devel fftw-devel
       
       # Install NVIDIA HPC SDK
       curl -O https://developer.download.nvidia.com/hpc-sdk/21.9/nvhpc_2021_219_Linux_x86_64_cuda_11.4.tar.gz
       tar xpzf nvhpc_2021_219_Linux_x86_64_cuda_11.4.tar.gz
       cd nvhpc_2021_219_Linux_x86_64_cuda_11.4
       sudo ./install
       
       # Configure environment
       echo 'export PATH=/opt/nvidia/hpc_sdk/Linux_x86_64/21.9/compilers/bin:${PATH}' >> ~/.bashrc
       echo 'export LD_LIBRARY_PATH=/opt/nvidia/hpc_sdk/Linux_x86_64/21.9/cuda/lib64:${LD_LIBRARY_PATH}' >> ~/.bashrc
       source ~/.bashrc
   '''

Using Docker for GPU Computing
--------------------------

For reproducible GPU environments, Docker containers are ideal:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Instance configuration
       region='us-west-2',
       instance_type='g4dn.xlarge',
       
       # Docker setup
       worker_init='''
           # Install NVIDIA drivers
           sudo yum install -y nvidia
           
           # Install Docker
           sudo amazon-linux-extras install -y docker
           sudo systemctl start docker
           sudo systemctl enable docker
           sudo usermod -a -G docker ec2-user
           newgrp docker
           
           # Install NVIDIA Container Toolkit
           distribution=$(. /etc/os-release;echo $ID$VERSION_ID)
           curl -s -L https://nvidia.github.io/nvidia-docker/$(echo $distribution | tr -d '.')/nvidia-docker.repo | sudo tee /etc/yum.repos.d/nvidia-docker.repo
           sudo yum install -y nvidia-container-toolkit
           sudo systemctl restart docker
           
           # Test NVIDIA Docker
           docker run --rm --gpus all nvidia/cuda:11.6.2-base-ubuntu20.04 nvidia-smi
       ''',
   )

Then create GPU applications using Docker:

.. code-block:: python

   @parsl.bash_app
   def docker_gpu_app(input_data, stdout=parsl.AUTO_LOGNAME, stderr=parsl.AUTO_LOGNAME):
       return f'''
           # Run GPU workload in Docker
           docker run --rm --gpus all -v $(pwd):/data pytorch/pytorch:1.12.0-cuda11.3-cudnn8-runtime \
               python -c "
   import torch
   import torch.nn as nn
   import torch.optim as optim
   import numpy as np
   
   # Check GPU
   print('CUDA available:', torch.cuda.is_available())
   print('GPU count:', torch.cuda.device_count())
   
   # Simple neural network
   class Net(nn.Module):
       def __init__(self):
           super(Net, self).__init__()
           self.fc1 = nn.Linear(10, 50)
           self.fc2 = nn.Linear(50, 1)
           
       def forward(self, x):
           x = torch.relu(self.fc1(x))
           x = self.fc2(x)
           return x
   
   # Create a model and move to GPU
   model = Net().cuda()
   
   # Generate random data
   x = torch.randn(1000, 10).cuda()
   y = torch.randn(1000, 1).cuda()
   
   # Training loop
   optimizer = optim.SGD(model.parameters(), lr=0.01)
   criterion = nn.MSELoss()
   
   for epoch in range(100):
       optimizer.zero_grad()
       outputs = model(x)
       loss = criterion(outputs, y)
       loss.backward()
       optimizer.step()
       
       if epoch % 10 == 0:
           print(f'Epoch {epoch}, Loss: {loss.item():.4f}')
   "
       '''

Creating GPU Applications
---------------------

There are multiple approaches to create GPU-accelerated applications:

PyTorch GPU Application
~~~~~~~~~~~~~~~~~~

.. code-block:: python

   @parsl.python_app
   def pytorch_gpu_app(batch_size=64, epochs=10):
       import torch
       import torch.nn as nn
       import torch.optim as optim
       import torchvision
       import torchvision.transforms as transforms
       import time
       
       # Check for GPU
       device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
       print(f"Using device: {device}")
       
       # Define a simple CNN
       class Net(nn.Module):
           def __init__(self):
               super(Net, self).__init__()
               self.conv1 = nn.Conv2d(1, 32, 3, 1)
               self.conv2 = nn.Conv2d(32, 64, 3, 1)
               self.dropout1 = nn.Dropout2d(0.25)
               self.dropout2 = nn.Dropout2d(0.5)
               self.fc1 = nn.Linear(9216, 128)
               self.fc2 = nn.Linear(128, 10)
   
           def forward(self, x):
               x = self.conv1(x)
               x = torch.relu(x)
               x = self.conv2(x)
               x = torch.relu(x)
               x = torch.max_pool2d(x, 2)
               x = self.dropout1(x)
               x = torch.flatten(x, 1)
               x = self.fc1(x)
               x = torch.relu(x)
               x = self.dropout2(x)
               x = self.fc2(x)
               return x
       
       # Prepare data
       transform = transforms.Compose([
           transforms.ToTensor(),
           transforms.Normalize((0.1307,), (0.3081,))
       ])
       
       trainset = torchvision.datasets.MNIST(root='./data', train=True, download=True, transform=transform)
       trainloader = torch.utils.data.DataLoader(trainset, batch_size=batch_size, shuffle=True)
       
       testset = torchvision.datasets.MNIST(root='./data', train=False, download=True, transform=transform)
       testloader = torch.utils.data.DataLoader(testset, batch_size=batch_size, shuffle=False)
       
       # Create the model
       model = Net().to(device)
       
       # Training parameters
       criterion = nn.CrossEntropyLoss()
       optimizer = optim.SGD(model.parameters(), lr=0.01, momentum=0.9)
       
       # Training loop
       start_time = time.time()
       for epoch in range(epochs):
           running_loss = 0.0
           for i, data in enumerate(trainloader, 0):
               inputs, labels = data[0].to(device), data[1].to(device)
               
               optimizer.zero_grad()
               
               outputs = model(inputs)
               loss = criterion(outputs, labels)
               loss.backward()
               optimizer.step()
               
               running_loss += loss.item()
               if i % 100 == 99:
                   print(f'Epoch {epoch + 1}, Batch {i + 1}, Loss: {running_loss / 100:.3f}')
                   running_loss = 0.0
       
       # Calculate total training time
       total_time = time.time() - start_time
       
       # Evaluate model
       correct = 0
       total = 0
       with torch.no_grad():
           for data in testloader:
               images, labels = data[0].to(device), data[1].to(device)
               outputs = model(images)
               _, predicted = torch.max(outputs.data, 1)
               total += labels.size(0)
               correct += (predicted == labels).sum().item()
       
       accuracy = 100 * correct / total
       
       return {
           'device': str(device),
           'batch_size': batch_size,
           'epochs': epochs,
           'training_time': total_time,
           'accuracy': accuracy,
           'images_per_second': len(trainset) * epochs / total_time
       }

TensorFlow GPU Application
~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   @parsl.python_app
   def tensorflow_gpu_app(batch_size=64, epochs=10):
       import tensorflow as tf
       import time
       
       # Check for GPU
       gpus = tf.config.list_physical_devices('GPU')
       print(f"GPUs available: {gpus}")
       
       # Load MNIST dataset
       mnist = tf.keras.datasets.mnist
       (x_train, y_train), (x_test, y_test) = mnist.load_data()
       x_train, x_test = x_train / 255.0, x_test / 255.0
       
       # Reshape for CNN
       x_train = x_train.reshape(x_train.shape[0], 28, 28, 1)
       x_test = x_test.reshape(x_test.shape[0], 28, 28, 1)
       
       # Define model
       model = tf.keras.models.Sequential([
           tf.keras.layers.Conv2D(32, (3, 3), activation='relu', input_shape=(28, 28, 1)),
           tf.keras.layers.MaxPooling2D((2, 2)),
           tf.keras.layers.Conv2D(64, (3, 3), activation='relu'),
           tf.keras.layers.MaxPooling2D((2, 2)),
           tf.keras.layers.Flatten(),
           tf.keras.layers.Dense(128, activation='relu'),
           tf.keras.layers.Dropout(0.5),
           tf.keras.layers.Dense(10, activation='softmax')
       ])
       
       # Compile model
       model.compile(
           optimizer='adam',
           loss='sparse_categorical_crossentropy',
           metrics=['accuracy']
       )
       
       # Create callback to track training time
       class TimingCallback(tf.keras.callbacks.Callback):
           def on_train_begin(self, logs=None):
               self.start_time = time.time()
           
           def on_train_end(self, logs=None):
               self.training_time = time.time() - self.start_time
       
       timing_callback = TimingCallback()
       
       # Train model
       history = model.fit(
           x_train, y_train,
           batch_size=batch_size,
           epochs=epochs,
           validation_data=(x_test, y_test),
           callbacks=[timing_callback]
       )
       
       # Evaluate model
       test_loss, test_acc = model.evaluate(x_test, y_test, verbose=0)
       
       return {
           'gpu_available': len(gpus) > 0,
           'batch_size': batch_size,
           'epochs': epochs,
           'training_time': timing_callback.training_time,
           'accuracy': test_acc * 100,
           'images_per_second': len(x_train) * epochs / timing_callback.training_time
       }

CUDA Application
~~~~~~~~~~~~~

For direct CUDA programming:

.. code-block:: python

   @parsl.bash_app
   def cuda_vector_add(vector_size=1000000, stdout=parsl.AUTO_LOGNAME, stderr=parsl.AUTO_LOGNAME):
       return f'''
           # Create CUDA vector addition program
           cat > vector_add.cu << EOL
           #include <stdio.h>
           #include <stdlib.h>
           #include <math.h>
           #include <cuda_runtime.h>
           
           // CUDA kernel for vector addition
           __global__ void vectorAdd(const float *A, const float *B, float *C, int numElements) {
               int i = blockDim.x * blockIdx.x + threadIdx.x;
               if (i < numElements) {
                   C[i] = A[i] + B[i];
               }
           }
           
           int main(void) {
               // Print device properties
               cudaDeviceProp prop;
               cudaGetDeviceProperties(&prop, 0);
               printf("GPU: %s\\n", prop.name);
               printf("Compute capability: %d.%d\\n", prop.major, prop.minor);
               printf("Total global memory: %.2f GB\\n", prop.totalGlobalMem / (1024.0 * 1024.0 * 1024.0));
               
               // Error code
               cudaError_t err = cudaSuccess;
               
               // Vector size
               int numElements = {vector_size};
               size_t size = numElements * sizeof(float);
               printf("Vector size: %d\\n", numElements);
               
               // Allocate host memory
               float *h_A = (float *)malloc(size);
               float *h_B = (float *)malloc(size);
               float *h_C = (float *)malloc(size);
               
               // Initialize vectors
               for (int i = 0; i < numElements; ++i) {
                   h_A[i] = rand()/(float)RAND_MAX;
                   h_B[i] = rand()/(float)RAND_MAX;
               }
               
               // Allocate device memory
               float *d_A = NULL;
               err = cudaMalloc((void **)&d_A, size);
               if (err != cudaSuccess) {
                   fprintf(stderr, "Failed to allocate device vector A (error code %s)!\\n", cudaGetErrorString(err));
                   exit(EXIT_FAILURE);
               }
               
               float *d_B = NULL;
               err = cudaMalloc((void **)&d_B, size);
               if (err != cudaSuccess) {
                   fprintf(stderr, "Failed to allocate device vector B (error code %s)!\\n", cudaGetErrorString(err));
                   exit(EXIT_FAILURE);
               }
               
               float *d_C = NULL;
               err = cudaMalloc((void **)&d_C, size);
               if (err != cudaSuccess) {
                   fprintf(stderr, "Failed to allocate device vector C (error code %s)!\\n", cudaGetErrorString(err));
                   exit(EXIT_FAILURE);
               }
               
               // Copy vectors from host to device
               cudaMemcpy(d_A, h_A, size, cudaMemcpyHostToDevice);
               cudaMemcpy(d_B, h_B, size, cudaMemcpyHostToDevice);
               
               // Launch the CUDA kernel
               int threadsPerBlock = 256;
               int blocksPerGrid = (numElements + threadsPerBlock - 1) / threadsPerBlock;
               
               // Start timer
               cudaEvent_t start, stop;
               cudaEventCreate(&start);
               cudaEventCreate(&stop);
               cudaEventRecord(start);
               
               vectorAdd<<<blocksPerGrid, threadsPerBlock>>>(d_A, d_B, d_C, numElements);
               
               // End timer
               cudaEventRecord(stop);
               cudaEventSynchronize(stop);
               float milliseconds = 0;
               cudaEventElapsedTime(&milliseconds, start, stop);
               
               err = cudaGetLastError();
               if (err != cudaSuccess) {
                   fprintf(stderr, "Failed to launch vectorAdd kernel (error code %s)!\\n", cudaGetErrorString(err));
                   exit(EXIT_FAILURE);
               }
               
               // Copy result back to host
               cudaMemcpy(h_C, d_C, size, cudaMemcpyDeviceToHost);
               
               // Verify result
               for (int i = 0; i < numElements; ++i) {
                   if (fabs(h_A[i] + h_B[i] - h_C[i]) > 1e-5) {
                       fprintf(stderr, "Result verification failed at element %d!\\n", i);
                       exit(EXIT_FAILURE);
                   }
               }
               
               printf("Test PASSED\\n");
               printf("Execution time: %.2f ms\\n", milliseconds);
               printf("Bandwidth: %.2f GB/s\\n", 
                      (3 * size) / (milliseconds * 1e-3) / (1024 * 1024 * 1024));
               
               // Free device memory
               cudaFree(d_A);
               cudaFree(d_B);
               cudaFree(d_C);
               
               // Free host memory
               free(h_A);
               free(h_B);
               free(h_C);
               
               return 0;
           }
           EOL
           
           # Compile CUDA program
           nvcc -o vector_add vector_add.cu
           
           # Run the program
           ./vector_add
       '''

Multi-GPU Configuration
-------------------

For applications that can use multiple GPUs:

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Multi-GPU instance
       instance_type='p3.8xlarge',  # 4 V100 GPUs
       # or
       # instance_type='p3.16xlarge',  # 8 V100 GPUs
       # or
       # instance_type='p4d.24xlarge',  # 8 A100 GPUs
       
       # Additional settings for multi-GPU
       worker_init='''
           # NVIDIA driver and CUDA setup
           sudo yum install -y nvidia cuda-toolkit-11-6
           
           # NVIDIA Collective Communications Library (NCCL) for multi-GPU
           sudo yum install -y libnccl libnccl-devel
           
           # Install PyTorch with CUDA and NCCL support
           python3 -m pip install torch torchvision torchaudio --extra-index-url https://download.pytorch.org/whl/cu116
           
           # Test multi-GPU
           python3 -c "import torch; print('CUDA available:', torch.cuda.is_available()); print('GPU count:', torch.cuda.device_count()); [print(f'GPU {i}: {torch.cuda.get_device_name(i)}') for i in range(torch.cuda.device_count())]"
       ''',
   )

Multi-GPU PyTorch Example
~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   @parsl.python_app
   def multi_gpu_pytorch(batch_size=256, epochs=10):
       import torch
       import torch.nn as nn
       import torch.optim as optim
       import torchvision
       import torchvision.transforms as transforms
       import time
       
       # Check for GPUs
       if not torch.cuda.is_available():
           return {"error": "CUDA not available"}
       
       num_gpus = torch.cuda.device_count()
       print(f"Using {num_gpus} GPUs")
       for i in range(num_gpus):
           print(f"GPU {i}: {torch.cuda.get_device_name(i)}")
       
       # ResNet model (support for DataParallel)
       model = torchvision.models.resnet50(pretrained=False)
       
       # Wrap model with DataParallel
       if num_gpus > 1:
           model = nn.DataParallel(model)
       model = model.cuda()
       
       # Prepare synthetic data for benchmarking
       input_size = 224
       dataset_size = 10000
       
       # Create random tensors
       inputs = torch.randn(dataset_size, 3, input_size, input_size)
       labels = torch.randint(0, 1000, (dataset_size,))
       
       # Create DataLoader
       class SyntheticDataset(torch.utils.data.Dataset):
           def __init__(self, inputs, labels):
               self.inputs = inputs
               self.labels = labels
           
           def __len__(self):
               return len(self.inputs)
           
           def __getitem__(self, idx):
               return self.inputs[idx], self.labels[idx]
       
       dataset = SyntheticDataset(inputs, labels)
       dataloader = torch.utils.data.DataLoader(
           dataset, batch_size=batch_size, shuffle=True, num_workers=4)
       
       # Training parameters
       criterion = nn.CrossEntropyLoss().cuda()
       optimizer = optim.SGD(model.parameters(), lr=0.01, momentum=0.9)
       
       # Training loop
       start_time = time.time()
       model.train()
       
       for epoch in range(epochs):
           running_loss = 0.0
           for i, (inputs, labels) in enumerate(dataloader):
               inputs, labels = inputs.cuda(), labels.cuda()
               
               optimizer.zero_grad()
               
               outputs = model(inputs)
               loss = criterion(outputs, labels)
               loss.backward()
               optimizer.step()
               
               running_loss += loss.item()
               if i % 10 == 9:
                   print(f'Epoch {epoch + 1}, Batch {i + 1}, Loss: {running_loss / 10:.3f}')
                   running_loss = 0.0
       
       total_time = time.time() - start_time
       images_per_second = dataset_size * epochs / total_time
       
       return {
           'num_gpus': num_gpus,
           'gpu_names': [torch.cuda.get_device_name(i) for i in range(num_gpus)],
           'batch_size': batch_size,
           'epochs': epochs,
           'total_training_time': total_time,
           'images_per_second': images_per_second
       }

GPU Monitoring and Optimization
----------------------------

Monitoring GPU Utilization
~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   @parsl.bash_app
   def monitor_gpu_utilization(duration=60, interval=1, stdout=parsl.AUTO_LOGNAME, stderr=parsl.AUTO_LOGNAME):
       return f'''
           # Create a monitoring script
           cat > gpu_monitor.py << EOL
   import subprocess
   import time
   import datetime
   import csv
   
   # Function to get GPU stats
   def get_gpu_stats():
       result = subprocess.run(['nvidia-smi', '--query-gpu=timestamp,index,name,utilization.gpu,utilization.memory,memory.total,memory.used,temperature.gpu', '--format=csv,noheader,nounits'], stdout=subprocess.PIPE, text=True)
       return result.stdout.strip().split('\\n')
   
   # Monitor for the specified duration
   print("Starting GPU monitoring...")
   duration = {duration}  # seconds
   interval = {interval}  # seconds
   
   with open('gpu_stats.csv', 'w', newline='') as csvfile:
       csvwriter = csv.writer(csvfile)
       csvwriter.writerow(['Timestamp', 'GPU Index', 'GPU Name', 'GPU Utilization (%)', 'Memory Utilization (%)', 'Total Memory (MB)', 'Used Memory (MB)', 'Temperature (C)'])
       
       start_time = time.time()
       while time.time() - start_time < duration:
           stats = get_gpu_stats()
           for stat in stats:
               csvwriter.writerow(stat.split(', '))
           time.sleep(interval)
   
   print(f"Monitoring complete. Data saved to gpu_stats.csv")
   
   # Generate a summary
   print("\\nGPU Utilization Summary:")
   summary = subprocess.run(['nvidia-smi', '--query-gpu=index,name,utilization.gpu,memory.used,memory.total', '--format=csv'], stdout=subprocess.PIPE, text=True)
   print(summary.stdout)
   EOL
           
           # Run the monitoring script
           python3 gpu_monitor.py
           
           # Generate a simple plot if matplotlib is available
           python3 -c "
   try:
       import matplotlib.pyplot as plt
       import pandas as pd
       import numpy as np
       
       # Load data
       df = pd.read_csv('gpu_stats.csv')
       
       # Convert utilization to numeric
       df['GPU Utilization (%)'] = pd.to_numeric(df['GPU Utilization (%)'])
       df['Memory Utilization (%)'] = pd.to_numeric(df['Memory Utilization (%)'])
       
       # Create a time index
       df['Timestamp'] = pd.to_datetime(df['Timestamp'])
       
       # Create plots
       plt.figure(figsize=(12, 8))
       
       # Plot GPU utilization
       plt.subplot(2, 1, 1)
       for gpu_idx in df['GPU Index'].unique():
           gpu_data = df[df['GPU Index'] == gpu_idx]
           plt.plot(gpu_data['Timestamp'], gpu_data['GPU Utilization (%)'], label=f'GPU {gpu_idx}')
       
       plt.title('GPU Utilization Over Time')
       plt.xlabel('Time')
       plt.ylabel('GPU Utilization (%)')
       plt.legend()
       plt.grid(True)
       
       # Plot Memory utilization
       plt.subplot(2, 1, 2)
       for gpu_idx in df['GPU Index'].unique():
           gpu_data = df[df['GPU Index'] == gpu_idx]
           plt.plot(gpu_data['Timestamp'], gpu_data['Memory Utilization (%)'], label=f'GPU {gpu_idx}')
       
       plt.title('GPU Memory Utilization Over Time')
       plt.xlabel('Time')
       plt.ylabel('Memory Utilization (%)')
       plt.legend()
       plt.grid(True)
       
       plt.tight_layout()
       plt.savefig('gpu_utilization.png')
       print('Utilization plot saved to gpu_utilization.png')
   except ImportError:
       print('Matplotlib or pandas not available. Skipping plot generation.')
   "
       '''

Performance Tuning
~~~~~~~~~~~~~~

Optimize GPU applications with these approaches:

1. **Batch Size Optimization**

.. code-block:: python

   @parsl.python_app
   def batch_size_benchmark(batch_sizes=[32, 64, 128, 256, 512, 1024]):
       import torch
       import torch.nn as nn
       import time
       
       # Ensure GPU is available
       device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
       
       # Create a model
       model = torch.hub.load('pytorch/vision:v0.10.0', 'resnet50', pretrained=False).to(device)
       criterion = nn.CrossEntropyLoss().to(device)
       
       # Results dictionary
       results = {}
       
       for batch_size in batch_sizes:
           # Create synthetic data
           inputs = torch.randn(batch_size, 3, 224, 224).to(device)
           targets = torch.randint(0, 1000, (batch_size,)).to(device)
           
           # Warm-up run
           model(inputs)
           torch.cuda.synchronize()
           
           # Timed run
           start_time = time.time()
           for _ in range(10):  # Average over 10 runs
               outputs = model(inputs)
               loss = criterion(outputs, targets)
               loss.backward()
           torch.cuda.synchronize()
           end_time = time.time()
           
           # Calculate time per batch
           time_per_batch = (end_time - start_time) / 10
           images_per_second = batch_size / time_per_batch
           
           results[batch_size] = {
               'time_per_batch': time_per_batch,
               'images_per_second': images_per_second
           }
       
       # Find optimal batch size
       optimal_batch_size = max(results.items(), key=lambda x: x[1]['images_per_second'])[0]
       
       return {
           'results': results,
           'optimal_batch_size': optimal_batch_size,
           'device': str(device),
           'cuda_version': torch.version.cuda
       }

2. **Memory Management**

.. code-block:: python

   worker_init='''
       # Configure CUDA memory management
       export PYTORCH_CUDA_ALLOC_CONF=max_split_size_mb:128
       
       # For TensorFlow
       export TF_FORCE_GPU_ALLOW_GROWTH=true
       export TF_GPU_MEMORY_ALLOCATION=0.8  # 80% of GPU memory
   '''

GPU Cost Optimization
-----------------

Optimize GPU costs with these strategies:

1. **Use Spot Instances**

.. code-block:: python

   provider = EphemeralAWSProvider(
       # GPU instance
       instance_type='g4dn.xlarge',
       
       # Spot configuration for 70% savings
       use_spot_instances=True,
       spot_max_price_percentage=70,
       
       # Spot fleet for better availability
       use_spot_fleet=True,
       instance_types=[
           'g4dn.xlarge',     # Primary choice
           'g4dn.2xlarge',    # Backup choice
           'g5.xlarge',       # Alternative family
       ],
   )

2. **Right-Size Instances**

Choose the smallest GPU instance that meets your needs:

.. code-block:: python

   # For inference or small training jobs
   provider = EphemeralAWSProvider(
       instance_type='g4dn.xlarge',  # 1 T4 GPU, 4 vCPUs
   )
   
   # For medium training jobs
   provider = EphemeralAWSProvider(
       instance_type='p3.2xlarge',  # 1 V100 GPU, 8 vCPUs
   )
   
   # For large-scale distributed training
   provider = EphemeralAWSProvider(
       instance_type='p3.16xlarge',  # 8 V100 GPUs, 64 vCPUs
   )

3. **Scale to Zero When Idle**

.. code-block:: python

   provider = EphemeralAWSProvider(
       # Standard configuration
       instance_type='g4dn.xlarge',
       
       # Scale to zero when idle
       min_blocks=0,
       idle_timeout=300,  # 5 minutes
   )

Complete GPU Workflow Example
--------------------------

Here's a comprehensive example of a deep learning workflow:

.. code-block:: python

   import parsl
   import os
   from parsl.config import Config
   from parsl.executors import HighThroughputExecutor
   from parsl_ephemeral_aws import EphemeralAWSProvider
   
   # Configure the provider for GPU computing
   provider = EphemeralAWSProvider(
       # Region and instance
       region='us-west-2',
       instance_type='g4dn.xlarge',  # Single T4 GPU
       
       # Resource configuration
       init_blocks=1,
       min_blocks=0,
       max_blocks=4,
       
       # Spot instances for cost savings
       use_spot_instances=True,
       spot_max_price_percentage=80,
       
       # State persistence
       state_store='parameter_store',
       state_prefix='/parsl/deep-learning',
       
       # Worker initialization
       worker_init='''
           # Update packages
           sudo yum update -y
           
           # Install NVIDIA drivers and CUDA
           sudo yum install -y amazon-linux-extras
           sudo amazon-linux-extras install -y epel
           sudo yum install -y nvidia cuda-toolkit-11-6
           
           # Install Python dependencies
           sudo yum install -y python3-devel
           python3 -m pip install --upgrade pip
           python3 -m pip install torch torchvision torchaudio --extra-index-url https://download.pytorch.org/whl/cu116
           python3 -m pip install numpy pandas matplotlib scikit-learn
           
           # Verify GPU is available
           nvidia-smi
           python3 -c "import torch; print('CUDA available:', torch.cuda.is_available())"
       ''',
       
       # Tags
       tags={
           'Project': 'DeepLearningBenchmark',
           'Environment': 'Development',
       },
   )
   
   # Configure Parsl
   config = Config(
       executors=[
           HighThroughputExecutor(
               label='gpu_executor',
               provider=provider,
               max_workers=1,  # One worker per GPU
           )
       ],
       strategy='simple',
   )
   
   # Load configuration
   parsl.load(config)
   
   # Define a function to train a ResNet model
   @parsl.python_app
   def train_resnet(dataset='cifar10', batch_size=128, epochs=10, learning_rate=0.001):
       import torch
       import torch.nn as nn
       import torch.optim as optim
       import torchvision
       import torchvision.transforms as transforms
       import time
       import os
       
       # Set random seed for reproducibility
       torch.manual_seed(42)
       
       # Check for GPU
       device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
       print(f"Using device: {device}")
       
       if torch.cuda.is_available():
           print(f"GPU: {torch.cuda.get_device_name(0)}")
           print(f"CUDA Version: {torch.version.cuda}")
       
       # Define transformations
       if dataset == 'cifar10':
           transform_train = transforms.Compose([
               transforms.RandomCrop(32, padding=4),
               transforms.RandomHorizontalFlip(),
               transforms.ToTensor(),
               transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
           ])
           
           transform_test = transforms.Compose([
               transforms.ToTensor(),
               transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
           ])
           
           # Load datasets
           trainset = torchvision.datasets.CIFAR10(root='./data', train=True, download=True, transform=transform_train)
           trainloader = torch.utils.data.DataLoader(trainset, batch_size=batch_size, shuffle=True, num_workers=2)
           
           testset = torchvision.datasets.CIFAR10(root='./data', train=False, download=True, transform=transform_test)
           testloader = torch.utils.data.DataLoader(testset, batch_size=batch_size, shuffle=False, num_workers=2)
           
           num_classes = 10
       else:
           # Default to CIFAR10 if dataset not recognized
           print(f"Dataset {dataset} not recognized, using CIFAR10 instead.")
           return train_resnet(dataset='cifar10', batch_size=batch_size, epochs=epochs, learning_rate=learning_rate)
       
       # Load a pretrained ResNet model
       if dataset == 'cifar10':
           model = torchvision.models.resnet18(pretrained=False)
           # Modify the first layer to accept 32x32 images
           model.conv1 = nn.Conv2d(3, 64, kernel_size=3, stride=1, padding=1, bias=False)
           model.maxpool = nn.Identity()
           # Modify the last layer for the number of classes
           model.fc = nn.Linear(model.fc.in_features, num_classes)
       
       # Move model to device
       model = model.to(device)
       
       # Loss function and optimizer
       criterion = nn.CrossEntropyLoss()
       optimizer = optim.Adam(model.parameters(), lr=learning_rate)
       
       # Learning rate scheduler
       scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, 'min', patience=2, factor=0.5)
       
       # Training loop
       start_time = time.time()
       train_losses = []
       test_losses = []
       test_accuracies = []
       
       for epoch in range(epochs):
           epoch_start = time.time()
           
           # Training phase
           model.train()
           running_loss = 0.0
           for i, (inputs, labels) in enumerate(trainloader):
               inputs, labels = inputs.to(device), labels.to(device)
               
               # Zero the parameter gradients
               optimizer.zero_grad()
               
               # Forward pass
               outputs = model(inputs)
               loss = criterion(outputs, labels)
               
               # Backward pass and optimize
               loss.backward()
               optimizer.step()
               
               # Statistics
               running_loss += loss.item()
               if i % 100 == 99:
                   print(f'Epoch {epoch+1}, Batch {i+1}, Loss: {running_loss/100:.3f}')
                   running_loss = 0.0
           
           # Testing phase
           model.eval()
           test_loss = 0.0
           correct = 0
           total = 0
           with torch.no_grad():
               for inputs, labels in testloader:
                   inputs, labels = inputs.to(device), labels.to(device)
                   outputs = model(inputs)
                   loss = criterion(outputs, labels)
                   test_loss += loss.item()
                   _, predicted = torch.max(outputs.data, 1)
                   total += labels.size(0)
                   correct += (predicted == labels).sum().item()
           
           # Calculate average test loss and accuracy
           avg_test_loss = test_loss / len(testloader)
           accuracy = 100 * correct / total
           
           # Update learning rate
           scheduler.step(avg_test_loss)
           
           # Record metrics
           train_losses.append(running_loss)
           test_losses.append(avg_test_loss)
           test_accuracies.append(accuracy)
           
           # Print epoch summary
           epoch_time = time.time() - epoch_start
           print(f'Epoch {epoch+1}/{epochs}, Test Loss: {avg_test_loss:.4f}, Accuracy: {accuracy:.2f}%, Time: {epoch_time:.2f}s')
       
       # Calculate total training time
       total_time = time.time() - start_time
       
       # Save the model
       model_path = f'resnet18_{dataset}_{epochs}ep.pt'
       torch.save(model.state_dict(), model_path)
       
       # Create visualization if matplotlib is available
       try:
           import matplotlib.pyplot as plt
           import numpy as np
           
           plt.figure(figsize=(12, 4))
           
           # Plot losses
           plt.subplot(1, 2, 1)
           plt.plot(test_losses, label='Test Loss')
           plt.xlabel('Epoch')
           plt.ylabel('Loss')
           plt.title('Training and Test Loss')
           plt.legend()
           
           # Plot accuracy
           plt.subplot(1, 2, 2)
           plt.plot(test_accuracies, label='Test Accuracy')
           plt.xlabel('Epoch')
           plt.ylabel('Accuracy (%)')
           plt.title('Test Accuracy')
           plt.legend()
           
           plt.tight_layout()
           plt.savefig(f'training_metrics_{dataset}_{epochs}ep.png')
       except ImportError:
           print("Matplotlib not available, skipping visualization.")
       
       # Return training summary
       return {
           'device': str(device),
           'gpu_name': torch.cuda.get_device_name(0) if torch.cuda.is_available() else "N/A",
           'dataset': dataset,
           'batch_size': batch_size,
           'epochs': epochs,
           'learning_rate': learning_rate,
           'final_accuracy': accuracy,
           'training_time': total_time,
           'images_per_second': len(trainset) * epochs / total_time,
           'model_path': os.path.abspath(model_path),
           'metrics_path': os.path.abspath(f'training_metrics_{dataset}_{epochs}ep.png') if 'plt' in locals() else None
       }
   
   # Define a function to run inference with the trained model
   @parsl.python_app
   def run_inference(model_path, batch_size=64, num_samples=1000):
       import torch
       import torchvision
       import torchvision.transforms as transforms
       import time
       
       # Check for GPU
       device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
       print(f"Using device: {device}")
       
       # Load test dataset
       transform_test = transforms.Compose([
           transforms.ToTensor(),
           transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
       ])
       
       testset = torchvision.datasets.CIFAR10(root='./data', train=False, download=True, transform=transform_test)
       # Limit number of samples if specified
       if num_samples and num_samples < len(testset):
           indices = torch.randperm(len(testset))[:num_samples]
           testset = torch.utils.data.Subset(testset, indices)
       
       testloader = torch.utils.data.DataLoader(testset, batch_size=batch_size, shuffle=False, num_workers=2)
       
       # Load model
       model = torchvision.models.resnet18(pretrained=False)
       model.conv1 = torch.nn.Conv2d(3, 64, kernel_size=3, stride=1, padding=1, bias=False)
       model.maxpool = torch.nn.Identity()
       model.fc = torch.nn.Linear(model.fc.in_features, 10)
       model.load_state_dict(torch.load(model_path))
       model = model.to(device)
       model.eval()
       
       # Warm-up run
       with torch.no_grad():
           for inputs, _ in testloader:
               inputs = inputs.to(device)
               _ = model(inputs)
               break
       
       # Inference benchmarking
       correct = 0
       total = 0
       start_time = time.time()
       
       with torch.no_grad():
           for inputs, labels in testloader:
               inputs, labels = inputs.to(device), labels.to(device)
               outputs = model(inputs)
               _, predicted = torch.max(outputs.data, 1)
               total += labels.size(0)
               correct += (predicted == labels).sum().item()
       
       inference_time = time.time() - start_time
       accuracy = 100 * correct / total
       
       return {
           'device': str(device),
           'gpu_name': torch.cuda.get_device_name(0) if torch.cuda.is_available() else "N/A",
           'batch_size': batch_size,
           'num_samples': total,
           'accuracy': accuracy,
           'inference_time': inference_time,
           'samples_per_second': total / inference_time,
           'inference_latency_ms': (inference_time / len(testloader)) * 1000  # ms per batch
       }
   
   # Run benchmarks with different configurations
   batch_sizes = [64, 128, 256]
   results = []
   
   for batch_size in batch_sizes:
       print(f"Starting training with batch size {batch_size}...")
       training_future = train_resnet(batch_size=batch_size, epochs=5)
       results.append((batch_size, training_future))
   
   # Wait for all training jobs to complete
   for batch_size, future in results:
       result = future.result()
       print(f"\nTraining completed for batch size {batch_size}:")
       print(f"GPU: {result['gpu_name']}")
       print(f"Final accuracy: {result['final_accuracy']:.2f}%")
       print(f"Training time: {result['training_time']:.2f} seconds")
       print(f"Throughput: {result['images_per_second']:.2f} images/second")
       print(f"Model saved to: {result['model_path']}")
       
       # Run inference with the trained model
       print(f"\nRunning inference with the trained model...")
       inference_future = run_inference(result['model_path'], batch_size=batch_size)
       inference_result = inference_future.result()
       
       print(f"Inference results:")
       print(f"Accuracy: {inference_result['accuracy']:.2f}%")
       print(f"Inference time: {inference_result['inference_time']:.2f} seconds")
       print(f"Throughput: {inference_result['samples_per_second']:.2f} samples/second")
       print(f"Batch inference latency: {inference_result['inference_latency_ms']:.2f} ms")
       print("-" * 50)
   
   # Clean up
   parsl.dfk().cleanup()

Next Steps
---------

* Explore :doc:`mpi_workflows` for multi-node GPU computing
* Learn about :doc:`../user_guide/resource_management` for managing GPU resources
* See :doc:`cost_optimization` for optimizing GPU costs
* Check out :doc:`../examples/machine_learning` for more ML workflow examples
# Globus Compute Integration Guide

Run Python functions on ephemeral AWS EC2 instances through the **Globus Compute**
platform — from any network environment, including corporate firewalls, university
networks, and home routers.

---

## Architecture

```
Your Machine (any network)          Globus Compute Service           AWS EC2
┌──────────────────────┐            ┌──────────────────┐         ┌──────────────────┐
│ globus_compute_sdk   │            │ Globus Compute   │         │ GlobusCompute-   │
│ Executor.submit(fn)  │◄──AMQP────►│ Message Router   │◄──SSM──►│ Engine worker    │
│                      │            │ (globusid.org)   │  tunnel │ + Parsl HTEX     │
└──────────────────────┘            └──────────────────┘         └──────────────────┘
                                                                       ▲
                                                              GlobusComputeProvider
                                                              (creates VPC / EC2)
```

Key points:

* **No inbound ports required.** The EC2 instance connects *outward* to the Globus
  message router via AMQP (port 443). AWS Session Manager tunneling handles all
  management traffic.
* **Ephemeral infrastructure.** EC2 instances are created when the endpoint starts
  workers and terminated when they idle out or the endpoint is stopped.
* **Globus auth.** Functions are submitted to an endpoint UUID. Auth uses the Globus
  identity platform — no AWS credentials required on the client machine.

---

## Prerequisites

### 1. AWS account and credentials

```bash
aws configure --profile aws   # Access Key ID, Secret, region
```

The IAM user / role must have EC2, SSM, and IAM permissions.
Generate the minimum policy document with:

```python
from parsl_ephemeral_aws import GlobusComputeProvider
import json
print(json.dumps(GlobusComputeProvider.minimum_iam_policy(), indent=2))
# For private ECR images add: minimum_iam_policy(include_ecr=True)
```

### 2. Python package

```bash
# Core install + Globus Compute extras
pip install "parsl-ephemeral-aws[globus]"

# Or install the Globus packages separately
pip install parsl-ephemeral-aws globus-compute-sdk globus-compute-endpoint
```

> **Note on Python version**: `globus-compute-endpoint` requires Python ≥ 3.10.

### 3. Globus account and authentication

```bash
# Log in once (opens a browser for OAuth)
globus-compute-endpoint login
```

Tokens are cached at `~/.globus_compute/storage.db` and refreshed automatically.

---

## Quick Start

### Step 1 — Generate an endpoint config

```python
from parsl_ephemeral_aws import GlobusComputeProvider

provider = GlobusComputeProvider(
    region="us-east-1",
    instance_type="t3.medium",
    mode="standard",
    auto_create_instance_profile=True,   # creates SSM role automatically
    max_blocks=4,
    display_name="My Ephemeral AWS Endpoint",
)

# Writes ~/.globus_compute/my_aws_endpoint/config.yaml
provider.generate_endpoint_config("~/.globus_compute/my_aws_endpoint")
```

### Step 2 — Start the endpoint daemon

```bash
globus-compute-endpoint start my_aws_endpoint
```

The endpoint registers with the Globus Compute service and prints its UUID:

```
Starting endpoint; registered endpoint ID: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

Copy the UUID — you need it to submit functions.

### Step 3 — Submit functions from your client machine

```python
from globus_compute_sdk import Executor

ENDPOINT_ID = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"   # from step 2

def square(x):
    return x * x

with Executor(endpoint_id=ENDPOINT_ID) as ex:
    future = ex.submit(square, 7)
    print(future.result())   # → 49
```

### Step 4 — Stop the endpoint

```bash
globus-compute-endpoint stop my_aws_endpoint
```

This gracefully drains pending functions and terminates all EC2 worker instances.

---

## Configuration Reference

`GlobusComputeProvider` accepts all `EphemeralAWSProvider` parameters plus three
additional ones:

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `endpoint_id` | `str \| None` | `None` | Globus Compute endpoint UUID. Written as a comment into the generated `config.yaml`. |
| `container_image` | `str \| None` | `None` | Docker image URI for containerised workers. Sets `container_type: docker` and `container_uri` in the config. Supports Docker Hub, ECR, and any registry reachable from EC2. |
| `display_name` | `str` | `"Ephemeral AWS Endpoint"` | Human-readable label shown in the Globus Compute web console. |

All `EphemeralAWSProvider` parameters are forwarded unchanged. The most relevant
ones for Globus Compute deployments:

| Parameter | Recommended value | Notes |
|-----------|-------------------|-------|
| `region` | Your nearest AWS region | |
| `instance_type` | `"t3.medium"` — `"c5.2xlarge"` | Match to function workload |
| `mode` | `"standard"` | Most Globus Compute use-cases |
| `min_blocks` | `0` | Scale to zero when idle |
| `max_blocks` | `4` — `20` | Maximum parallel workers |
| `use_spot` | `True` | 70–90 % cost reduction for fault-tolerant workloads |
| `auto_create_instance_profile` | `True` | Automatically grants SSM access |
| `auto_shutdown` | `True` | Terminate idle workers |
| `max_idle_time` | `300` | Seconds before idle worker terminates |

---

## Examples

### Spot-instance endpoint (cost-optimised)

```python
from parsl_ephemeral_aws import GlobusComputeProvider

provider = GlobusComputeProvider(
    region="us-east-1",
    instance_type="c5.large",
    mode="standard",
    use_spot=True,
    spot_interruption_handling=True,
    checkpoint_bucket="my-parsl-checkpoints",   # required for interruption recovery
    min_blocks=0,
    max_blocks=10,
    auto_create_instance_profile=True,
    display_name="Spot AWS Endpoint",
)

provider.generate_endpoint_config("~/.globus_compute/spot_aws")
```

Start the endpoint and submit long-running functions — spot interruptions are
detected automatically and the work is re-queued from the last checkpoint.

### Container endpoint (reproducible environments)

```python
from parsl_ephemeral_aws import GlobusComputeProvider

provider = GlobusComputeProvider(
    region="us-west-2",
    instance_type="t3.large",
    container_image="python:3.11-slim",      # or a private ECR image
    min_blocks=0,
    max_blocks=5,
    auto_create_instance_profile=True,
    display_name="Python 3.11 Container Endpoint",
)

provider.generate_endpoint_config("~/.globus_compute/python311_aws")
```

For private ECR images your IAM role needs the ECR permissions from
`GlobusComputeProvider.minimum_iam_policy(include_ecr=True)`.

### Multi-region deployment

Deploy one endpoint per region for geographic locality:

```python
for region, name in [
    ("us-east-1", "aws-us-east"),
    ("eu-west-1", "aws-eu-west"),
    ("ap-southeast-1", "aws-ap-se"),
]:
    GlobusComputeProvider(
        region=region,
        instance_type="c5.xlarge",
        auto_create_instance_profile=True,
        display_name=f"AWS {region}",
    ).generate_endpoint_config(f"~/.globus_compute/{name}")
```

Then start each endpoint and route functions to the nearest one:

```python
from globus_compute_sdk import Executor

ENDPOINTS = {
    "us-east-1": "uuid-for-us-east",
    "eu-west-1": "uuid-for-eu-west",
}

with Executor(endpoint_id=ENDPOINTS["us-east-1"]) as ex:
    result = ex.submit(my_function, data).result()
```

### IAM policy document

Print the minimum IAM policy and attach it to your IAM user/role:

```python
import json
from parsl_ephemeral_aws import GlobusComputeProvider

# Without ECR
policy = GlobusComputeProvider.minimum_iam_policy()

# With ECR (needed for private container images)
policy_with_ecr = GlobusComputeProvider.minimum_iam_policy(include_ecr=True)

print(json.dumps(policy, indent=2))
```

---

## Running the E2E Tests

```bash
# 1. Install Globus Compute extras (separate venv recommended due to dill pin)
pip install "parsl-ephemeral-aws[globus]"

# 2. Authenticate
globus-compute-endpoint login

# 3. Start your endpoint and export its UUID
export GLOBUS_COMPUTE_ENDPOINT_ID="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

# 4. Run the tests
AWS_PROFILE=aws pytest tests/aws/test_globus_compute_e2e.py \
    -m "aws and globus" --no-cov -v
```

Config-generation tests (no running endpoint needed):

```bash
AWS_PROFILE=aws pytest tests/aws/test_globus_compute_e2e.py \
    -m "aws and globus" -k "Config" --no-cov -v
```

---

## Troubleshooting

### `globus-compute-endpoint start` hangs

The endpoint waits for an EC2 worker to come online.  Typical causes:

* **IAM permissions**: ensure `AmazonSSMManagedInstanceCore` is attached to the
  instance profile (set `auto_create_instance_profile=True` or specify
  `iam_instance_profile_arn`).
* **VPC / subnet routing**: the public subnet must have an Internet Gateway and a
  route to `0.0.0.0/0`. The provider creates this automatically.
* **AMI not found**: the provider auto-selects the latest Amazon Linux 2023 AMI
  for the region. If you supply a custom `image_id`, verify it exists in the
  target region.

### `ResourceNotFoundException` on endpoint start

The endpoint UUID stored in `~/.globus_compute/<name>/` does not exist in the
Globus Compute service (e.g. it was deleted via the web console). Delete the
local directory and re-run `globus-compute-endpoint start`:

```bash
rm -rf ~/.globus_compute/my_aws_endpoint
globus-compute-endpoint start my_aws_endpoint
```

### Function raises `TaskExecutionFailed`

The Globus Compute worker ran the function but it raised an exception.  Check the
endpoint log:

```bash
tail -f ~/.globus_compute/my_aws_endpoint/endpoint.log
```

### `globus-compute-endpoint login` prompts even after first login

Tokens expire after 48 hours of inactivity.  Re-run `login` — it will refresh
existing tokens without requiring a new browser flow if the refresh token is
still valid (up to 6 months).

### SSM connectivity: instance not reachable

Verify SSM agent is running on the instance:

```bash
aws ssm describe-instance-information --profile aws
```

The instance must have:
1. `AmazonSSMManagedInstanceCore` policy attached via instance profile.
2. Network path to `ssm.<region>.amazonaws.com` (outbound 443).
3. SSM agent version ≥ 3.0 (Amazon Linux 2023 ships with a current version).

### Spot interruptions causing function failures

Enable `spot_interruption_handling=True` and provide a `checkpoint_bucket`.
The interruption monitor checkpoints running tasks to S3 every
`checkpoint_interval` seconds (default 60 s) and re-queues them when a new
worker comes online.

---

## See Also

* [EphemeralAWSProvider API reference](operating_modes.md)
* [Spot instance guide](spot_fleet.md)
* [State persistence backends](state_persistence.md)
* [Security and IAM](security.md)
* [Globus Compute documentation](https://globus-compute.readthedocs.io/)
* [Globus Compute SDK on PyPI](https://pypi.org/project/globus-compute-sdk/)

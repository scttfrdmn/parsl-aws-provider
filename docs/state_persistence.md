# State Persistence

The provider persists its state so that resources can be tracked across sessions,
handed to a bastion in detached mode, and cleaned up after a crash.

## Overview

State persistence matters because:

1. Every AWS resource the provider creates is recorded, so nothing is orphaned.
2. A restarted provider can adopt the resources a previous run left behind
   instead of duplicating them.
3. Detached mode needs state the bastion and the client can both read.

## Selecting a store

You do **not** construct a store yourself. Choose one with `state_store_type` and
the provider builds it, wiring in its own session, region, credentials, and audit
logger:

```python
from parsl_aws_provider import EphemeralAWSProvider

provider = EphemeralAWSProvider(
    region="us-east-1",
    vpc_id="vpc-0123456789abcdef0",
    subnet_id="subnet-0123456789abcdef0",
    security_group_id="sg-0123456789abcdef0",
    state_store_type="file",  # "file" | "s3" | "parameter_store"
    state_file_path="./aws_provider_state.json",  # used by "file"
)
```

`state_store_type` accepts a string or a `StateStoreType` enum member
(`from parsl_aws_provider.provider import StateStoreType`).

### File store (default)

State lives in a local JSON file, written under an `fcntl` lock.

```python
provider = EphemeralAWSProvider(
    # ... network and compute options ...
    state_store_type="file",
    state_file_path="./aws_provider_state.json",
)
```

Good for local development and standard mode. Not usable from a bastion, and the
state is lost with the machine.

### Parameter Store

State lives in an AWS Systems Manager parameter, readable from both the client and
the bastion — the reason detached mode prefers it.

```python
provider = EphemeralAWSProvider(
    # ... network and compute options ...
    state_store_type="parameter_store",
    parameter_store_path="/parsl/my-workflow-state",
)
```

Requires `ssm:PutParameter`, `ssm:GetParameter`, and `ssm:DeleteParameter`.
Standard parameters cap at 4 KB, which a workflow with very many tracked resources
can reach; use S3 if you expect that.

### S3

```python
provider = EphemeralAWSProvider(
    # ... network and compute options ...
    state_store_type="s3",
    s3_bucket="my-parsl-state-bucket",
    s3_key="my-workflow/state.json",
)
```

`s3_bucket` is mandatory for this backend — omitting it raises
`ProviderConfigurationError`. The bucket must already exist; the provider does not
create it.

## Recommended store by mode

| Mode | Store | Reason |
|------|-------|--------|
| Standard | `file` | Simplest; the client is the only reader |
| Detached | `parameter_store` | Readable by both client and bastion |
| Serverless | `s3` | Durable and reachable from Lambda or Fargate |

## The store interface

All three stores implement the same **keyed** interface from
`parsl_aws_provider.state.base.StateStore`:

```text
save_state(state_key: str, state_data: Dict[str, Any]) -> None
load_state(state_key: str) -> Optional[Dict[str, Any]]
delete_state(state_key: str) -> None
```

Keys matter. The provider writes under `"provider"` and the operating mode under
`"mode"`. Before v0.7.0 both wrote a full overwrite to the same slot with
different key sets, so each destroyed the other's fields — which lost `job_map`
and, worse, the baked-AMI ID and its ownership flag, leaking an AMI and its EBS
snapshots on every shutdown.

State files written by v0.6.0 and earlier hold a single flat document with no
`_states` wrapper. Those still load: the flat document is offered under every key,
each reader takes the fields it recognises, and the first write upgrades the file.

## State contents

Under the `"provider"` key:

- `provider_id`, `mode`, `timestamp`
- `resources` — tracked resource IDs and their metadata
- `job_map` — job ID to resource mapping
- `warm_instances` — warm-pool members, in standard mode

Under the `"mode"` key, mode-specific fields: the validated `vpc_id`,
`subnet_id`, `security_group_id`, `initialized`, the launch template ID, and for
standard mode `baked_ami_id` / `owns_baked_ami`; for detached mode `bastion_id`.

```json
{
  "_version": 2,
  "_states": {
    "provider": {
      "provider_id": "8f7e3a4d",
      "mode": "standard",
      "job_map": {"1": {"resource_id": "res-1"}},
      "resources": {"res-1": {"instance_id": "i-087654321", "status": "RUNNING"}}
    },
    "mode": {
      "vpc_id": "vpc-0123456789abcdef0",
      "subnet_id": "subnet-0123456789abcdef0",
      "security_group_id": "sg-0123456789abcdef0",
      "initialized": true
    }
  }
}
```

## Recovery

Recovery is automatic. `EphemeralAWSProvider.__init__` loads whatever is stored at
the configured location, and if the state records a `provider_id` and you did not
pass one, that ID is adopted — so a second provider pointed at the same state
location continues the first one's work rather than starting a parallel run:

```python
# First run
provider = EphemeralAWSProvider(
    mode="detached",
    state_store_type="parameter_store",
    parameter_store_path="/parsl/my-workflow-state",
    vpc_id="vpc-0123456789abcdef0",
    subnet_id="subnet-0123456789abcdef0",
    security_group_id="sg-0123456789abcdef0",
)

# Later, in a new process: same store and path, so the bastion, tracked jobs,
# and any baked AMI are picked back up.
provider = EphemeralAWSProvider(
    mode="detached",
    state_store_type="parameter_store",
    parameter_store_path="/parsl/my-workflow-state",
    vpc_id="vpc-0123456789abcdef0",
    subnet_id="subnet-0123456789abcdef0",
    security_group_id="sg-0123456789abcdef0",
)
```

Network IDs are re-validated on load. If a recorded resource has gone,
`ResourceNotFoundError` is raised rather than the ID being silently blanked — the
pre-v0.7.0 behaviour, which produced an opaque boto3 error later.

## Cleanup

```python
provider.shutdown()
```

`shutdown()` terminates tracked compute, deletes the launch template, and deletes
the state entry. To remove state without touching resources:

```python
provider.state_store.delete_state("provider")
provider.state_store.delete_state("mode")
```

## Custom stores

Subclass `StateStore` and implement the three keyed methods:

```python
from typing import Any, Dict, Optional

from parsl_aws_provider.state.base import StateStore


class MyCustomStateStore(StateStore):
    def __init__(self, connection_string: str) -> None:
        self.connection_string = connection_string

    def save_state(self, state_key: str, state_data: Dict[str, Any]) -> None: ...

    def load_state(self, state_key: str) -> Optional[Dict[str, Any]]: ...

    def delete_state(self, state_key: str) -> None: ...
```

`load_state` must return `None` for a key that has never been written — the
provider treats that as "first run", not as an error.

There is no configuration hook for injecting a custom store; assign it after
construction (`provider.state_store = MyCustomStateStore(...)`) before the first
`submit()`.

## Notes

- Keep one state location per workflow. Two concurrent providers sharing a
  location will adopt each other's `provider_id` and fight over the same
  resources.
- Parameter Store operations emit `STATE_ACCESS` audit events when the provider
  has an `audit_logger`.
- After any crash, run `parsl-aws-cleanup --dry-run --region <region>`
  to check for resources the state no longer names.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors

# Fleets: multiple instance types per block

Requesting several instance types at once is the single most effective way to
reduce spot interruptions: EC2 draws from whichever pool has capacity instead of
failing when your one chosen type is exhausted.

As of v0.7.0 this uses the **EC2 Fleet** API (`CreateFleet`), not Spot Fleet.
AWS describes Spot Fleet as "a legacy API with no planned investment"
([#86](https://github.com/scttfrdmn/parsl-ephemeral-provider/issues/86)). The option is
still spelled `use_spot_fleet` for compatibility.

## What changed with EC2 Fleet

| | Spot Fleet (before) | EC2 Fleet (now) |
|---|---|---|
| API | `RequestSpotFleet` | `CreateFleet` with `Type="instant"` |
| IAM service role | Required (`IamFleetRole`) | None — no role to create or grant |
| Launch form | Launch specifications or template | Launch template, mandatory |
| Instance IDs | Polled until the request filled | Returned by the create call |
| Allocation strategy spelling | camelCase (`priceCapacityOptimized`) | kebab-case (`price-capacity-optimized`) |

The two APIs reject each other's enum spelling, and neither `DryRun` nor a
zero-capacity request validates it — only a real launch does. The provider uses
the kebab-case form; the camelCase constant survives for the CloudFormation
templates and the detached-mode bastion, which drive `AWS::EC2::SpotFleet`
resources.

## Benefits

- **Higher availability** — draws across instance types, sizes, and families
- **Fewer interruptions** — `price-capacity-optimized` prefers pools with the
  deepest spare capacity, then the lowest price among those
- **Price ceiling** — cap as a percentage of on-demand with
  `spot_max_price_percentage`
- **No IAM service role** — one fewer permission to grant

## Configuration

```python
from parsl_ephemeral_provider import EphemeralProvider

provider = EphemeralProvider(
    region="us-east-1",
    vpc_id="vpc-0123456789abcdef0",
    subnet_id="subnet-0123456789abcdef0",
    security_group_id="sg-0123456789abcdef0",
    use_spot=True,
    use_spot_fleet=True,
    instance_types=["m5.large", "m5a.large", "m6i.large", "c5.large"],
    nodes_per_block=2,  # instances per fleet
    spot_max_price_percentage=80,  # cap at 80% of on-demand
    spot_allocation_strategy="price-capacity-optimized",  # the default
    mode="standard",
    max_blocks=4,
)
```

**Both flags are required.** `use_spot_fleet=True` on its own does nothing: the
gate that builds the fleet manager reads `use_spot and use_spot_fleet`
(`modes/standard.py:233`), so with `use_spot` unset no manager exists, the launch
falls through to a single on-demand instance, and nothing reports it
([#137](https://github.com/scttfrdmn/parsl-ephemeral-provider/issues/137)). With both
set, the fleet path takes precedence over the single-spot-instance path.

## Parameters

**Required for the fleet path**

- `use_spot=True` **and** `use_spot_fleet=True`
- `instance_types` — a list of instance type **names**, e.g.
  `["m5.large", "c5.large"]`. Not weighted dictionaries. If omitted, the fleet is
  created with `instance_type` as its only type, which forfeits the whole point.

**Optional**

- `nodes_per_block` (default `1`) — the fleet's target capacity. This is the only
  path where `nodes_per_block` has an effect; the single-instance launch paths
  always launch one instance per block.
- `spot_max_price_percentage` (default: none) — maximum price as a percentage of
  on-demand. Leave unset to pay up to the on-demand price, which is the AWS
  default and rarely reached.
- `spot_allocation_strategy` (default `"price-capacity-optimized"`) — one of
  `lowest-price`, `diversified`, `capacity-optimized`,
  `capacity-optimized-prioritized`, `price-capacity-optimized`. An unrecognised
  value is rejected with a useful message rather than an opaque
  `InvalidParameterValue` from EC2.

## Instance types are not synthesized for you

Earlier versions claimed the provider would derive alternatives from your primary
`instance_type` (`t3.small` → `m5.small`, `c5.small`, …). It does not, and that
approach was removed deliberately: string-slicing the family and generation
produces invalid names for multi-character families like `m5a` and `c6g`, and
`m5.small` does not exist at all.

Choose types yourself. Effective pools share a size and mix families and
generations:

```python
instance_types = ["m5.xlarge", "m5a.xlarge", "m5n.xlarge", "m6i.xlarge"]
```

Keep vCPU and memory comparable across the list, since Parsl sizes its worker pool
from a single `instance_type`'s capacity.

## Spot interruption warnings

An `instant` fleet gets no Capacity Rebalance — `CreateFleet` rejects
`SpotOptions.MaintenanceStrategies` for that type — so the two-minute warning
comes from EventBridge instead. Set `spot_interruption_handling=True` and the
provider creates a rule matching the *EC2 Spot Instance Interruption Warning*
event with an SQS queue target, which it polls.

```python
provider = EphemeralProvider(
    # ... network options ...
    use_spot=True,
    use_spot_fleet=True,
    instance_types=["m5.large", "m5a.large"],
    spot_interruption_handling=True,
)
```

Detection was verified end to end against real EC2 with a Fault Injection
Simulator experiment (`aws:ec2:send-spot-instance-interruptions`): the warning
reached the queue 15.2 s in, with the instance still `running`. That is the point
— polling EC2 instance state cannot see anything until `shutting-down`, by which
time it is far too late to react.

*Rebalance Recommendation* is deliberately not matched: it signals elevated risk,
not an impending reclaim, and treating it as one would tear down healthy workers.

A detected reclaim marks the affected block `FAILED`. That is the whole response,
and it is the correct one: Parsl stops dispatching to a failed block and re-runs
its tasks elsewhere under `retries`. **Set `retries` on the Parsl `Config`** — the
provider cannot checkpoint a task itself, because a Parsl provider is never told
which tasks a block is running; it is handed a command and returns a block ID
([#137](https://github.com/scttfrdmn/parsl-ephemeral-provider/issues/137)).

The block is marked for the *whole fleet*, not just the reclaimed instance, since
the fleet is what carries a Parsl job ID. Note the marker is deliberately sticky:
a fleet whose instances AWS is taking back still reports itself active, so
re-deriving status would overwrite it on the next poll.

## Resource tracking

Fleets and their instances are tagged alike, so one sweep finds both:

- `ParslResource=true`
- `ParslWorkflowId=<provider_id>`
- `ParslBlockId=<block_id>`
- `Name=parsl-fleet-<block>` on the fleet, `parsl-node-<block>` on instances

The orphan sweep goes through instances rather than fleets: `describe_fleets` does
not filter on the `aws:ec2:fleet-id` tag, but `describe_instances` does.

Fleets appear in `provider.status()`, `provider.list_resources()`, and are removed
by `cleanup_resources()` and `cleanup_infrastructure()`.

## Full example

```python
import parsl
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
from parsl_ephemeral_provider import EphemeralProvider


@parsl.python_app
def hello(name):
    import platform

    return f"Hello, {name} from {platform.node()}"


provider = EphemeralProvider(
    region="us-east-1",
    vpc_id="vpc-0123456789abcdef0",
    subnet_id="subnet-0123456789abcdef0",
    security_group_id="sg-0123456789abcdef0",
    mode="standard",
    use_spot=True,
    use_spot_fleet=True,
    instance_types=["m5.large", "m5a.large", "m6i.large"],
    nodes_per_block=2,
    spot_max_price_percentage=80,
    spot_interruption_handling=True,
    max_blocks=4,
    state_store_type="s3",
    s3_bucket="your-state-bucket",
    s3_key="spot-fleet-demo/state.json",
)

config = Config(
    executors=[
        HighThroughputExecutor(
            label="spot_fleet_executor",
            provider=provider,
            encrypted=False,  # same-VPC; else distribute_certificates=True
        )
    ],
    retries=3,  # a reclaimed instance loses its in-flight tasks
)

parsl.load(config)

for future in [hello(f"Task {i}") for i in range(10)]:
    print(future.result())

parsl.clear()
provider.shutdown()
```

You select the state backend with `state_store_type` and let the provider build
it; you do not construct a store yourself. See
[state_persistence.md](state_persistence.md).

## Limitations

- Not every instance type exists in every region or Availability Zone. A fleet
  that cannot be filled returns fewer instances than requested — or none — rather
  than raising.
- The provider passes a single subnet, so the fleet is confined to one
  Availability Zone. Multi-AZ diversification is not yet exposed.
- Fleet capacity is not maintained: `Type="instant"` is a one-shot request, so a
  reclaimed instance is not replaced automatically. Parsl's scaling strategy
  submits a new block instead.

## Serverless mode also has a fleet path

`mode="serverless"` with `compute_type="ecs"` and `use_spot_fleet=True` launches an
`instant` EC2 Fleet per job, bypassing ECS entirely — so it is no longer
serverless in any useful sense, and you are managing instances again. It exists
because a fleet gives access to instance types and sizes Fargate does not offer.
`instance_types`, `nodes_per_block`, and `spot_max_price_percentage` all apply.

Note the distinction from `use_spot=True` in the same mode, which stays on Fargate
and switches the cluster's capacity provider to `FARGATE_SPOT`. `compute_type="lambda"`
ignores both flags; Lambda has no spot pricing.

If you want a fleet, standard mode is the more direct route. This path is
primarily useful when the rest of a serverless deployment is already in place.

## Troubleshooting

**The fleet is created but launches no instances.** Capacity was unavailable at
your price. Check `describe_fleets` for the error in
`Instances[].Lifecycle`/`Errors`, add more instance types, and raise or unset
`spot_max_price_percentage`.

**`InvalidParameterValue` on the allocation strategy.** You passed a camelCase
name; `CreateFleet` wants kebab-case (`price-capacity-optimized`).

**Instances launch and then vanish quickly.** That is spot reclamation. Enable
`spot_interruption_handling=True` to see it coming, diversify further, and set
`retries` on the Parsl `Config`.

**Interruptions are frequent regardless.** Add families and generations rather
than sizes, and keep `price-capacity-optimized` — `lowest-price` optimises for
cost at the direct expense of interruption rate.

## References

- [EC2 Fleet](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-fleet.html)
- [Spot allocation strategies](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-fleet-allocation-strategy.html)
- [Spot interruption notices](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/spot-instance-termination-notices.html)

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors

# Using Spot Fleet with Parsl AWS Provider

The Parsl AWS Provider supports AWS Spot Fleet for more reliable and cost-effective spot instance provisioning. Spot Fleet allows you to request multiple instance types at once, improving availability and potentially reducing costs.

## Benefits of Spot Fleet

- **Higher Availability**: Spreads requests across multiple instance types and sizes
- **Cost Optimization**: Automatically selects the lowest-priced instances from your specified pool
- **Capacity Management**: Maintains target capacity by replacing terminated instances
- **Simplified Management**: Single request manages multiple instance types
- **Flexible Pricing Control**: Set maximum price as a percentage of on-demand pricing

## Configuration

To use Spot Fleet with the Parsl AWS Provider, you need to set the following parameters:

```python
from parsl.providers import EphemeralAWSProvider

provider = EphemeralAWSProvider(
    # Basic configuration
    region="us-east-1",
    
    # Enable both spot instances and spot fleet
    use_spot=True,
    use_spot_fleet=True,
    
    # Configure multiple instance types
    instance_types=["t3.small", "t3.medium", "m5.small"],
    
    # Set number of nodes per block
    nodes_per_block=2,
    
    # Maximum price as percentage of on-demand (optional)
    spot_max_price_percentage=80,
    
    # Other standard configuration...
    operating_mode="detached",  # Recommended for long-running workflows
    workflow_id="my-workflow",
    # ...
)
```

## Required Parameters

- `use_spot`: Must be set to `True` to use spot instances
- `use_spot_fleet`: Set to `True` to enable Spot Fleet (instead of individual spot instances)
- `instance_types`: List of instance types to include in the Spot Fleet request

## Optional Parameters

- `nodes_per_block`: Number of instances to request per block (default: 1)
- `spot_max_price_percentage`: Maximum price as percentage of on-demand price (default: 100)

## Recommended Operating Mode

Spot Fleet is most effective with the `detached` operating mode, which provides better support for maintaining state across spot interruptions.

## Automatic Instance Type Selection

If you don't specify `instance_types`, the provider will automatically generate a list based on the primary `instance_type`:

1. Uses the specified `instance_type` as the first choice
2. Adds similar instance types from the same family (compute, memory, general purpose)
3. Adds a newer generation if applicable

For example, if `instance_type` is "t3.small", it might automatically use:
- "t3.small" (primary choice)
- "m5.small" (similar general purpose)
- "c5.small" (similar compute family)
- "t4g.small" (newer generation)

## Resource Tracking

The provider automatically tracks all Spot Fleet requests and instances, making them visible in:

- Job status tracking via `get_status()`
- Resource cleanup during `cleanup_resources()`
- Infrastructure cleanup during `cleanup_infrastructure()`

## Full Example

```python
import parsl
from parsl.app.app import python_app
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
from parsl.providers import EphemeralAWSProvider
from parsl.addresses import address_by_hostname

# Import StateStore implementation
from parsl_ephemeral_aws.state import S3StateStore

@python_app
def hello(name):
    import platform
    return f"Hello, {name} from {platform.node()}"

# Configure provider with Spot Fleet
provider = EphemeralAWSProvider(
    region="us-east-1",
    operating_mode="detached",
    workflow_id="spot-fleet-demo",
    use_spot=True,
    use_spot_fleet=True,
    instance_types=["t3.small", "t3.medium", "m5.small"],
    nodes_per_block=2,
    spot_max_price_percentage=80,
    max_blocks=4,
    state_store=S3StateStore(
        bucket_name="your-state-bucket",
        key_prefix="parsl-states",
    ),
)

# Create executor with provider
executor = HighThroughputExecutor(
    label="spot_fleet_executor",
    address=address_by_hostname(),
    provider=provider,
)

# Create Parsl configuration
config = Config(
    executors=[executor],
    strategy=None,
)

# Load configuration
parsl.load(config)

# Submit and run tasks
futures = [hello(f"Task {i}") for i in range(10)]
for future in futures:
    print(future.result())

# Cleanup
parsl.dfk().cleanup()
```

## Limitations

- Spot Fleet is only available in AWS regions where EC2 Spot Fleet is supported
- IAM permissions must include Spot Fleet related permissions
- Not all instance types may be available in all regions
- The IAM role for Spot Fleet is automatically created if it doesn't exist

## Troubleshooting

If you encounter issues with Spot Fleet:

1. Check if Spot Fleet requests are being created in the AWS Console
2. Verify IAM permissions for the Spot Fleet role
3. Try using instance types that are more commonly available
4. Consider increasing the spot_max_price_percentage
5. Check AWS CloudTrail logs for Spot Fleet API errors

## Resources

- [AWS Spot Fleet Documentation](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/spot-fleet.html)
- [Parsl AWS Provider Documentation](https://parsl.readthedocs.io/en/stable/userguide/providers.html#ephemeral-aws-provider)
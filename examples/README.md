# Parsl Ephemeral AWS Provider Examples

This directory contains example scripts demonstrating various ways to use the Parsl Ephemeral AWS Provider.

## Example Files

- **[basic_usage.py](basic_usage.py)**: Combined example showing basic usage of all three operating modes.
- **[standard_mode.py](standard_mode.py)**: Example of using Standard mode with direct client-to-worker communication.
- **[detached_mode.py](detached_mode.py)**: Example of using Detached mode with a persistent bastion host.
- **[serverless_mode.py](serverless_mode.py)**: Example of using Serverless mode with Lambda and/or ECS/Fargate.
- **[spot_fleet_example.py](spot_fleet_example.py)**: Example of using Spot Fleet in Detached mode for reliable spot instance management.
- **[serverless_spot_fleet_example.py](serverless_spot_fleet_example.py)**: Example of using Spot Fleet in Serverless mode for reliable, cost-effective EC2 resources.

## Running the Examples

These examples require:

1. Valid AWS credentials with appropriate permissions
2. The Parsl Ephemeral AWS Provider package installed
3. Parsl installed

To run an example:

```bash
# Ensure AWS credentials are set up (you can use environment variables, AWS config file, etc.)
export AWS_ACCESS_KEY_ID=your_access_key
export AWS_SECRET_ACCESS_KEY=your_secret_key
export AWS_REGION=us-west-2  # Or your preferred region

# Run the example
python serverless_spot_fleet_example.py
```

## Important Notes

- These examples create real AWS resources which may incur costs
- All resources should be automatically cleaned up when the scripts complete
- If a script is interrupted, you may need to manually clean up resources
- Make sure you have appropriate IAM permissions before running examples

## Example Configuration

Each example demonstrates specific features of the provider. You can modify the examples to fit your needs by changing:

- Region
- Instance types
- Scaling options (number of nodes, max blocks, etc.)
- Spot pricing options 
- Storage options
- Network configuration
- Worker initialization commands

## Additional Resources

For more detailed information about the Parsl Ephemeral AWS Provider:

- [Documentation](https://parsl-ephemeral-aws.readthedocs.io/)
- [GitHub Repository](https://github.com/scttfrdmn/parsl-aws-provider)
- [PyPI Package](https://pypi.org/project/parsl-ephemeral-aws/)

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
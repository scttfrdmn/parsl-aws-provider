#!/usr/bin/env python3
"""Minimal configurations for each of the three operating modes.

This file is a configuration reference rather than a workflow — each function
builds a provider and prints what it would run on, so the shapes can be compared
side by side. For runnable end-to-end workflows see standard_mode.py,
detached_mode.py, serverless_mode.py, and one_shot_mode.py.

`mode` is a string, not a mode object. The provider constructs the mode itself so
it can inject the AWS session, state store, and resolved AMI; passing
`StandardMode(...)` raises a `TypeCheckError`. Unknown keyword arguments raise
`ProviderConfigurationError` rather than being ignored, so options from older
tutorials fail loudly.

Usage
-----
    export AWS_PROFILE=aws
    export AWS_TEST_REGION=us-east-1
    export AWS_TEST_VPC_ID=vpc-...
    export AWS_TEST_SUBNET_ID=subnet-...
    export AWS_TEST_SG_ID=sg-...

    uv run python examples/basic_usage.py

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import logging
import os
import sys

from parsl_ephemeral_aws import EphemeralAWSProvider

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("basic-usage")


def _network() -> dict:
    """Return the pre-provisioned network IDs, which are required since v0.7.0."""
    return {
        "region": os.environ.get("AWS_TEST_REGION", "us-east-1"),
        "vpc_id": os.environ["AWS_TEST_VPC_ID"],
        "subnet_id": os.environ["AWS_TEST_SUBNET_ID"],
        "security_group_id": os.environ["AWS_TEST_SG_ID"],
    }


def standard_mode_provider() -> EphemeralAWSProvider:
    """EC2 workers that dial back to this machine. Simplest, needs reachability."""
    return EphemeralAWSProvider(
        mode="standard",
        instance_type="t3.small",
        min_blocks=0,
        max_blocks=4,
        # Local file state is fine when one machine owns the workflow.
        state_store_type="file",
        state_file_path="basic_usage_standard.json",
        auto_create_instance_profile=True,
        auto_shutdown=True,
        max_idle_time=300,
        additional_tags={"Project": "ParslExample", "Mode": "standard"},
        **_network(),
    )


def detached_mode_provider() -> EphemeralAWSProvider:
    """A bastion owns the worker lifecycle, so this machine need not be reachable."""
    return EphemeralAWSProvider(
        mode="detached",
        instance_type="t3.small",
        bastion_instance_type="t3.micro",
        min_blocks=0,
        max_blocks=4,
        # Parameter Store is reachable from both this machine and the bastion,
        # which is what makes reconnecting from a new process work.
        state_store_type="parameter_store",
        parameter_store_path="/parsl/basic-usage-detached",
        auto_create_instance_profile=True,
        additional_tags={"Project": "ParslExample", "Mode": "detached"},
        **_network(),
    )


def serverless_mode_provider() -> EphemeralAWSProvider:
    """Lambda functions instead of instances. No network IDs needed."""
    return EphemeralAWSProvider(
        # Lambda runs in the Lambda-managed VPC, so vpc_id/subnet_id/
        # security_group_id are not required here. ECS/Fargate does need them.
        region=os.environ.get("AWS_TEST_REGION", "us-east-1"),
        mode="serverless",
        compute_type="lambda",  # there is no "auto" at the provider level
        memory_size=1024,  # MB; Lambda CPU scales with memory
        timeout=300,  # seconds, Lambda's own ceiling is 900
        min_blocks=0,
        max_blocks=20,
        state_store_type="file",
        state_file_path="basic_usage_serverless.json",
        additional_tags={"Project": "ParslExample", "Mode": "serverless"},
    )


def main() -> int:
    """Build one provider per mode and report what each resolved to."""
    builders = {
        "standard": standard_mode_provider,
        "detached": detached_mode_provider,
        "serverless": serverless_mode_provider,
    }

    requested = sys.argv[1:] or ["standard"]
    for name in requested:
        if name not in builders:
            logger.error("Unknown mode %r; choose from %s", name, list(builders))
            return 2

        logger.info("--- %s mode ---", name)
        provider = None
        try:
            # Construction calls initialize(), which creates real AWS resources
            # (a launch template, and an IAM role when auto-creating one).
            provider = builders[name]()
            logger.info("provider_id: %s", provider.provider_id)
            for resource_type, entries in provider.list_resources().items():
                logger.info("  %s: %d", resource_type, len(entries))
        except KeyError as exc:
            logger.error("Missing required environment variable: %s", exc)
            return 2
        except Exception:
            logger.exception("Could not build the %s provider", name)
            return 1
        finally:
            if provider is not None:
                provider.shutdown()

    return 0


if __name__ == "__main__":
    sys.exit(main())

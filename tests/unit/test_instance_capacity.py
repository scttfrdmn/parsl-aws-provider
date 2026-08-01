"""Unit tests for instance-capacity lookup.

Parsl's ``ExecutionProvider`` declares ``cores_per_node``/``mem_per_node``, and
``HighThroughputExecutor`` sizes its worker count from them — when both are
``None`` it falls back to a single worker per node regardless of how large the
instance is. ``describe_instance_capacity`` fills them in from EC2, and must
degrade to ``(None, None)`` rather than raise: it runs during ``__init__``, so an
unreachable EC2 must not stop the provider from constructing.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from moto import mock_aws

from parsl_ephemeral_aws.utils.aws import describe_instance_capacity


pytestmark = pytest.mark.unit


class TestDescribeInstanceCapacity:
    """Capacity lookup against moto and against failing clients."""

    @mock_aws
    def test_returns_vcpus_and_memory_in_gb(self):
        """t3.micro is 2 vCPU / 1 GiB; memory is converted from MiB."""
        session = boto3.Session(region_name="us-east-1")

        vcpus, memory_gb = describe_instance_capacity(session, "t3.micro")

        assert vcpus == 2
        assert memory_gb == 1.0

    @mock_aws
    def test_larger_instance_type(self):
        """A multi-GiB type converts without integer truncation."""
        session = boto3.Session(region_name="us-east-1")

        vcpus, memory_gb = describe_instance_capacity(session, "m5.xlarge")

        assert vcpus == 4
        assert memory_gb == 16.0

    @mock_aws
    def test_unknown_instance_type_returns_none(self):
        """An invalid type yields (None, None), matching the base default."""
        session = boto3.Session(region_name="us-east-1")

        assert describe_instance_capacity(session, "not-a-real-type") == (None, None)

    def test_client_error_returns_none(self):
        """An API failure is an absent hint, not an error."""
        session = MagicMock()
        session.client.return_value.describe_instance_types.side_effect = ClientError(
            {"Error": {"Code": "UnauthorizedOperation", "Message": "denied"}},
            "DescribeInstanceTypes",
        )

        assert describe_instance_capacity(session, "t3.micro") == (None, None)

    def test_missing_credentials_returns_none(self):
        """No credentials during __init__ must not stop construction."""
        session = MagicMock()
        session.client.side_effect = NoCredentialsError()

        assert describe_instance_capacity(session, "t3.micro") == (None, None)

    def test_empty_response_returns_none(self):
        """An empty InstanceTypes list must not IndexError."""
        session = MagicMock()
        session.client.return_value.describe_instance_types.return_value = {
            "InstanceTypes": []
        }

        assert describe_instance_capacity(session, "t3.micro") == (None, None)

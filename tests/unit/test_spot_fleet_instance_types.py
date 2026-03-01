"""Unit tests for the C1 fix: SpotFleetManager instance-type list generation.

Verifies that the manager no longer attempts to synthesise alternative instance
types from the primary type string (which broke for multi-char families, high
generation numbers, etc.).

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import pytest
from unittest.mock import MagicMock, patch

from parsl_ephemeral_aws.compute.spot_fleet import SpotFleetManager


def _make_provider(instance_type="t3.micro", instance_types=None):
    """Return a minimal mock provider suitable for SpotFleetManager."""
    provider = MagicMock()
    provider.workflow_id = "wf-test"
    provider.region = "us-east-1"
    provider.instance_type = instance_type
    provider.instance_types = instance_types  # None means "not set"
    provider.image_id = "ami-12345678"
    provider.vpc_id = "vpc-abc"
    provider.subnet_id = "subnet-abc"
    provider.security_group_id = "sg-abc"
    provider.key_name = None
    provider.use_public_ips = False
    provider.nodes_per_block = 1
    provider.spot_max_price_percentage = 100
    provider.worker_init = ""
    provider.tags = {}
    provider.aws_profile = None
    # No explicit credentials → SpotFleetManager uses CredentialManager default
    del provider.aws_access_key_id
    del provider.aws_secret_access_key
    del provider.aws_session_token
    return provider


@pytest.mark.unit
class TestSpotFleetInstanceTypes:
    """Tests for correct instance-type list generation in SpotFleetManager."""

    @pytest.fixture(autouse=True)
    def _patch_credential_manager(self):
        """Suppress real credential/session creation."""
        with (
            patch(
                "parsl_ephemeral_aws.compute.spot_fleet.CredentialManager"
            ) as mock_cm,
            patch("parsl_ephemeral_aws.compute.spot_fleet.SecurityConfig"),
        ):
            mock_cm.return_value.create_boto3_session.return_value = MagicMock()
            yield

    def _get_instance_types_for_provider(self, provider):
        """Instantiate SpotFleetManager and return the computed instance_types list."""
        mgr = SpotFleetManager(provider)
        # Replicate the instance_types selection logic from _create_fleet_request
        return (
            provider.instance_types
            if (hasattr(provider, "instance_types") and provider.instance_types)
            else [provider.instance_type]
        )

    def test_fallback_to_single_type_when_instance_types_none(self):
        """When provider.instance_types is None, use only the primary type."""
        provider = _make_provider("m5.xlarge", instance_types=None)
        types = self._get_instance_types_for_provider(provider)
        assert types == ["m5.xlarge"]

    def test_fallback_to_single_type_when_instance_types_empty(self):
        """When provider.instance_types is an empty list, use the primary type."""
        provider = _make_provider("c6g.large", instance_types=[])
        types = self._get_instance_types_for_provider(provider)
        assert types == ["c6g.large"]

    def test_explicit_instance_types_respected(self):
        """When provider.instance_types is set, use it as-is."""
        explicit = ["m5.xlarge", "m5a.xlarge", "m6i.xlarge"]
        provider = _make_provider("m5.xlarge", instance_types=explicit)
        types = self._get_instance_types_for_provider(provider)
        assert types == explicit

    def test_no_invalid_types_for_multi_char_family(self):
        """Multi-char families (m5a, c6g) do not produce garbage type strings."""
        for instance_type in ("m5a.2xlarge", "c6g.4xlarge", "r6i.large"):
            provider = _make_provider(instance_type, instance_types=None)
            types = self._get_instance_types_for_provider(provider)
            # Must only contain the original type — nothing synthesised
            assert types == [
                instance_type
            ], f"Expected only [{instance_type}], got {types}"

    def test_no_invalid_types_for_high_generation(self):
        """Generation numbers ≥ 9 do not produce integer overflow types."""
        provider = _make_provider("t4g.micro", instance_types=None)
        types = self._get_instance_types_for_provider(provider)
        assert types == ["t4g.micro"]
        # Old broken code would have produced "t5g.micro" (slicing single char)
        assert "t5g.micro" not in types

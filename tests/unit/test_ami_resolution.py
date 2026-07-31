"""Unit tests for AMI resolution and architecture selection (#84).

Hardcoding region->AMI pairs cannot work. The table this replaces was stamped
2026-03-01, and by 2026-07-30 every one of its 21 entries was unusable -- 9
carried a DeprecationTime, 6 returned InvalidAMIID.NotFound, 2 were structurally
malformed, and the remainder were unreachable. AWS's public SSM aliases are
repointed at each AL2023 release, so they are the only maintenance-free source.

Architecture matters just as much: an x86_64 AMI on a Graviton instance type
fails to launch, and nothing in this package distinguished the two before #84.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from moto import mock_aws

from parsl_ephemeral_aws.constants import (
    AMI_SSM_PARAMETER_TEMPLATE,
    ARCHITECTURE_ARM64,
    ARCHITECTURE_X86_64,
    DEFAULT_AMI_MAPPING,
)
from parsl_ephemeral_aws.exceptions import AMINotFoundError
from parsl_ephemeral_aws.utils.aws import (
    architecture_for_instance_type,
    get_default_ami,
)


pytestmark = pytest.mark.unit


class TestArchitectureForInstanceType:
    """Graviton detection from the instance-type family suffix.

    The classification was validated against ``describe_instance_types`` for all
    1,346 types AWS offers in us-east-1 (396 arm64, 950 x86_64) with zero
    mistakes; these cases are representative samples of that sweep.
    """

    @pytest.mark.parametrize(
        "instance_type",
        [
            "c7g.xlarge",  # current Graviton compute
            "m8g.large",  # newest Graviton generation
            "r7gd.4xlarge",  # Graviton with local NVMe
            "c8gn.medium",  # Graviton network-optimised
            "t4g.nano",  # Graviton burstable
            "x2gd.medium",  # Graviton memory-optimised
            "im4gn.large",  # Graviton storage-optimised
            "a1.medium",  # original Graviton, no "g" suffix
        ],
    )
    def test_graviton_families_are_arm64(self, instance_type):
        assert architecture_for_instance_type(instance_type) == ARCHITECTURE_ARM64

    @pytest.mark.parametrize(
        "instance_type",
        [
            "t3.micro",  # the package default
            "m5.xlarge",
            "c6i.large",  # Intel
            "m6a.2xlarge",  # AMD -- "a" suffix, not "g"
            "r5n.large",  # network-optimised x86
            "m5dn.xlarge",  # multiple suffix letters, none "g"
            "i3en.large",
            "p4d.24xlarge",  # GPU, still x86_64 host
        ],
    )
    def test_x86_families_are_x86_64(self, instance_type):
        assert architecture_for_instance_type(instance_type) == ARCHITECTURE_X86_64

    def test_g_in_prefix_is_not_graviton(self):
        """g5 is an x86_64 GPU family: the "g" is the prefix, not the suffix.

        This is the case a naive substring search gets wrong.
        """
        assert architecture_for_instance_type("g5.xlarge") == ARCHITECTURE_X86_64
        assert architecture_for_instance_type("g6e.2xlarge") == ARCHITECTURE_X86_64

    def test_case_is_normalised(self):
        assert architecture_for_instance_type("C7G.XLARGE") == ARCHITECTURE_ARM64

    def test_bare_family_without_size(self):
        """Callers sometimes pass just the family."""
        assert architecture_for_instance_type("c7g") == ARCHITECTURE_ARM64
        assert architecture_for_instance_type("m5") == ARCHITECTURE_X86_64

    def test_unparseable_family_falls_back_to_x86(self):
        """An unrecognised shape keeps the pre-#84 behaviour rather than raising."""
        assert architecture_for_instance_type("nonsense") == ARCHITECTURE_X86_64
        assert architecture_for_instance_type("") == ARCHITECTURE_X86_64

    def test_mac_metal_is_not_claimed_as_arm64(self):
        """mac*.metal is arm64_mac and needs a macOS AMI, not an AL2023 arm64 one.

        Returning x86_64 is no more wrong than arm64 would be -- either way the
        caller must pass image_id explicitly -- but it must not silently select
        an AL2023 arm64 image that cannot boot a Mac instance.
        """
        assert architecture_for_instance_type("mac2-m2.metal") == ARCHITECTURE_X86_64


class TestGetDefaultAmiFromSSM:
    """SSM is the primary source; the offline table is only a fallback."""

    @mock_aws
    def test_resolves_x86_64_from_ssm(self):
        session = boto3.Session(region_name="us-east-1")

        ami = get_default_ami("us-east-1", ARCHITECTURE_X86_64, session=session)

        assert ami.startswith("ami-")
        # Not the stale table entry: this came from the live parameter.
        assert ami != DEFAULT_AMI_MAPPING["us-east-1"]

    @mock_aws
    def test_resolves_arm64_from_ssm(self):
        session = boto3.Session(region_name="us-east-1")

        ami = get_default_ami("us-east-1", ARCHITECTURE_ARM64, session=session)

        assert ami.startswith("ami-")

    @mock_aws
    def test_arm64_and_x86_differ(self):
        """The two architectures must not resolve to the same image."""
        session = boto3.Session(region_name="us-east-1")

        x86 = get_default_ami("us-east-1", ARCHITECTURE_X86_64, session=session)
        arm = get_default_ami("us-east-1", ARCHITECTURE_ARM64, session=session)

        assert x86 != arm

    @mock_aws
    def test_default_architecture_is_x86_64(self):
        session = boto3.Session(region_name="us-east-1")

        assert get_default_ami("us-east-1", session=session) == get_default_ami(
            "us-east-1", ARCHITECTURE_X86_64, session=session
        )

    @mock_aws
    def test_resolves_in_a_region_absent_from_the_table(self):
        """The old lookup raised AMINotFoundError for any unlisted region.

        me-central-1 is in no version of DEFAULT_AMI_MAPPING, so this can only
        succeed via SSM.
        """
        assert "me-central-1" not in DEFAULT_AMI_MAPPING
        session = boto3.Session(region_name="me-central-1")

        assert get_default_ami("me-central-1", session=session).startswith("ami-")

    def test_queries_the_kernel_default_alias(self):
        """The version-independent alias, not a pinned kernel version.

        Pinning 6.1/6.12/6.18 would just become the next stale constant.
        """
        session = MagicMock()
        session.client.return_value.get_parameter.return_value = {
            "Parameter": {"Value": "ami-abc123"}
        }

        assert get_default_ami("us-east-1", session=session) == "ami-abc123"

        name = session.client.return_value.get_parameter.call_args.kwargs["Name"]
        assert name == (
            "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
        )
        assert "kernel-6." not in name

    def test_uses_the_supplied_session(self):
        """The caller's profile and endpoint must be honoured, not a bare default."""
        session = MagicMock()
        session.client.return_value.get_parameter.return_value = {
            "Parameter": {"Value": "ami-fromsession"}
        }

        assert get_default_ami("eu-west-1", session=session) == "ami-fromsession"
        session.client.assert_called_once_with("ssm", region_name="eu-west-1")

    def test_arm64_parameter_name(self):
        session = MagicMock()
        session.client.return_value.get_parameter.return_value = {
            "Parameter": {"Value": "ami-arm"}
        }

        get_default_ami("us-east-1", ARCHITECTURE_ARM64, session=session)

        assert session.client.return_value.get_parameter.call_args.kwargs[
            "Name"
        ] == AMI_SSM_PARAMETER_TEMPLATE.format(architecture="arm64")


class TestGetDefaultAmiFallback:
    """Behaviour when SSM cannot be reached."""

    def _failing_session(self, exc=None):
        session = MagicMock()
        session.client.return_value.get_parameter.side_effect = exc or ClientError(
            {"Error": {"Code": "ParameterNotFound", "Message": "nope"}},
            "GetParameter",
        )
        return session

    def test_falls_back_to_offline_table(self):
        """Offline test runs against moto/substrate must still get an AMI."""
        session = self._failing_session()

        ami = get_default_ami("us-east-1", session=session)

        assert ami == DEFAULT_AMI_MAPPING["us-east-1"]

    def test_falls_back_when_credentials_are_absent(self):
        session = self._failing_session(NoCredentialsError())

        assert (
            get_default_ami("eu-west-1", session=session)
            == (DEFAULT_AMI_MAPPING["eu-west-1"])
        )

    def test_arm64_refuses_the_x86_only_table(self):
        """Handing an x86_64 AMI to a Graviton instance is an opaque failure.

        Better to raise here, where the message can say why, than to let EC2
        reject the launch minutes later.
        """
        session = self._failing_session()

        with pytest.raises(AMINotFoundError, match="arm64"):
            get_default_ami("us-east-1", ARCHITECTURE_ARM64, session=session)

    def test_unknown_region_raises_with_the_ssm_cause(self):
        session = self._failing_session()

        with pytest.raises(AMINotFoundError) as excinfo:
            get_default_ami("me-central-1", session=session)

        # The message must name the real cause, not just "no AMI found".
        assert "ParameterNotFound" in str(excinfo.value)

    def test_rejects_an_unsupported_architecture(self):
        with pytest.raises(AMINotFoundError, match="Unsupported architecture"):
            get_default_ami("us-east-1", "sparc64")

    def test_fallback_table_entries_are_well_formed(self):
        """Two entries in the previous table were structurally invalid AMI IDs.

        ``InvalidAMIID.Malformed`` from EC2 means they could never have worked in
        any region, so shape is worth asserting even though freshness cannot be.
        """
        assert DEFAULT_AMI_MAPPING, "fallback table must not be empty"
        for region, ami in DEFAULT_AMI_MAPPING.items():
            body = ami.removeprefix("ami-")
            assert ami.startswith("ami-"), f"{region}: {ami}"
            assert len(body) == 17, f"{region}: {ami} has {len(body)} hex chars"
            assert all(c in "0123456789abcdef" for c in body), f"{region}: {ami}"

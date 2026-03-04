"""Unit tests for GlobusComputeProvider.

Verifies config generation for standard, spot, and container variants, as
well as the minimum_iam_policy() helper.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import os
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from parsl_ephemeral_aws import GlobusComputeProvider
from parsl_ephemeral_aws.globus_compute import _PROVIDER_TYPE
from parsl_ephemeral_aws.provider import EphemeralAWSProvider
from parsl_ephemeral_aws.state.file import FileStateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider(tmp_path, **extra_kwargs) -> GlobusComputeProvider:
    """Return a GlobusComputeProvider with all AWS interactions mocked out."""
    provider_id = f"test-{uuid.uuid4().hex[:8]}"
    state_file = str(tmp_path / f"{provider_id}.json")
    state_store = FileStateStore(file_path=state_file, provider_id=provider_id)

    mode_mock = MagicMock()
    mode_mock.submit_job.return_value = f"resource-{uuid.uuid4().hex[:8]}"
    mode_mock.get_job_status.return_value = {}
    mode_mock.cancel_jobs.return_value = {}
    mode_mock.cleanup_resources.return_value = None
    mode_mock.cleanup_infrastructure.return_value = None
    mode_mock.list_resources.return_value = {}

    with (
        patch("parsl_ephemeral_aws.provider.create_session") as mock_session,
        patch.object(
            EphemeralAWSProvider,
            "_initialize_state_store",
            return_value=state_store,
        ),
        patch.object(
            EphemeralAWSProvider,
            "_initialize_operating_mode",
            return_value=mode_mock,
        ),
    ):
        mock_session.return_value = MagicMock()
        provider = GlobusComputeProvider(
            provider_id=provider_id,
            region="us-east-1",
            image_id="ami-12345678",
            instance_type="t3.micro",
            mode="standard",
            vpc_id="vpc-test00001",
            subnet_id="subnet-test001",
            security_group_id="sg-test00001",
            **extra_kwargs,
        )

    return provider


# ---------------------------------------------------------------------------
# TestGlobusComputeProviderImport
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGlobusComputeProviderImport:
    """Verify the public import path works."""

    def test_importable_from_package(self):
        """``from parsl_ephemeral_aws import GlobusComputeProvider`` works."""
        # The import at the top of this file already validates this; an
        # explicit assertion makes the intent clear.
        assert GlobusComputeProvider is not None

    def test_is_subclass_of_ephemeral_aws_provider(self, tmp_path):
        """GlobusComputeProvider is a subclass of EphemeralAWSProvider."""
        assert issubclass(GlobusComputeProvider, EphemeralAWSProvider)


# ---------------------------------------------------------------------------
# TestGlobusComputeProviderConstruction
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGlobusComputeProviderConstruction:
    """Verify constructor stores the new attributes correctly."""

    def test_default_attributes(self, tmp_path):
        provider = _make_provider(tmp_path)
        assert provider.endpoint_id is None
        assert provider.container_image is None
        assert provider.display_name == "Ephemeral AWS Endpoint"

    def test_custom_attributes(self, tmp_path):
        ep_id = str(uuid.uuid4())
        provider = _make_provider(
            tmp_path,
            endpoint_id=ep_id,
            container_image="python:3.11-slim",
            display_name="My Endpoint",
        )
        assert provider.endpoint_id == ep_id
        assert provider.container_image == "python:3.11-slim"
        assert provider.display_name == "My Endpoint"

    def test_inherits_standard_params(self, tmp_path):
        """EphemeralAWSProvider params are still accessible."""
        provider = _make_provider(
            tmp_path,
            use_spot=True,
            max_blocks=20,
            status_polling_interval=30,
        )
        assert provider.use_spot is True
        assert provider.max_blocks == 20
        assert provider.status_polling_interval == 30


# ---------------------------------------------------------------------------
# TestGenerateEndpointConfig
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGenerateEndpointConfig:
    """Verify generate_endpoint_config() writes a correct config.yaml."""

    def test_creates_directory_and_file(self, tmp_path):
        provider = _make_provider(tmp_path)
        endpoint_dir = str(tmp_path / "my_endpoint")

        result_path = provider.generate_endpoint_config(endpoint_dir)

        assert os.path.isdir(endpoint_dir)
        assert os.path.isfile(result_path)
        assert result_path == str(tmp_path / "my_endpoint" / "config.yaml")

    def test_returns_absolute_path(self, tmp_path):
        provider = _make_provider(tmp_path)
        result_path = provider.generate_endpoint_config(str(tmp_path / "ep"))
        assert os.path.isabs(result_path)

    def test_config_contains_display_name(self, tmp_path):
        provider = _make_provider(tmp_path, display_name="Test Endpoint")
        config_path = provider.generate_endpoint_config(str(tmp_path / "ep"))
        content = Path(config_path).read_text()
        assert "display_name: Test Endpoint" in content

    def test_config_contains_engine_type(self, tmp_path):
        provider = _make_provider(tmp_path)
        config_path = provider.generate_endpoint_config(str(tmp_path / "ep"))
        content = Path(config_path).read_text()
        assert "type: GlobusComputeEngine" in content

    def test_config_contains_provider_type(self, tmp_path):
        provider = _make_provider(tmp_path)
        config_path = provider.generate_endpoint_config(str(tmp_path / "ep"))
        content = Path(config_path).read_text()
        assert f"type: {_PROVIDER_TYPE}" in content

    def test_config_contains_region(self, tmp_path):
        provider = _make_provider(tmp_path)
        config_path = provider.generate_endpoint_config(str(tmp_path / "ep"))
        content = Path(config_path).read_text()
        assert "region: us-east-1" in content

    def test_config_contains_instance_type(self, tmp_path):
        provider = _make_provider(tmp_path)
        config_path = provider.generate_endpoint_config(str(tmp_path / "ep"))
        content = Path(config_path).read_text()
        assert "instance_type: t3.micro" in content

    def test_config_contains_mode(self, tmp_path):
        provider = _make_provider(tmp_path)
        config_path = provider.generate_endpoint_config(str(tmp_path / "ep"))
        content = Path(config_path).read_text()
        assert "mode: standard" in content

    def test_config_encrypted_flag(self, tmp_path):
        provider = _make_provider(tmp_path)
        config_path = provider.generate_endpoint_config(str(tmp_path / "ep"))
        content = Path(config_path).read_text()
        assert "encrypted: true" in content

    def test_existing_directory_is_ok(self, tmp_path):
        """Calling generate_endpoint_config twice does not raise."""
        provider = _make_provider(tmp_path)
        ep_dir = str(tmp_path / "ep")
        provider.generate_endpoint_config(ep_dir)
        # Second call should overwrite without error
        provider.generate_endpoint_config(ep_dir)

    def test_todo_placeholder_when_no_endpoint_id(self, tmp_path):
        """When endpoint_id is None the config includes a TODO reminder."""
        provider = _make_provider(tmp_path)
        assert provider.endpoint_id is None
        config_path = provider.generate_endpoint_config(str(tmp_path / "ep"))
        content = Path(config_path).read_text()
        assert "TODO" in content

    def test_endpoint_id_written_when_set(self, tmp_path):
        ep_id = str(uuid.uuid4())
        provider = _make_provider(tmp_path, endpoint_id=ep_id)
        config_path = provider.generate_endpoint_config(str(tmp_path / "ep"))
        content = Path(config_path).read_text()
        assert ep_id in content
        assert "TODO" not in content


# ---------------------------------------------------------------------------
# TestGenerateEndpointConfigSpot
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGenerateEndpointConfigSpot:
    """Config generation with use_spot=True."""

    def test_use_spot_true_in_config(self, tmp_path):
        provider = _make_provider(tmp_path, use_spot=True)
        config_path = provider.generate_endpoint_config(str(tmp_path / "ep"))
        content = Path(config_path).read_text()
        assert "use_spot: true" in content

    def test_spot_interruption_handling_in_config(self, tmp_path):
        provider = _make_provider(
            tmp_path, use_spot=True, spot_interruption_handling=True
        )
        config_path = provider.generate_endpoint_config(str(tmp_path / "ep"))
        content = Path(config_path).read_text()
        assert "spot_interruption_handling: true" in content

    def test_spot_interruption_handling_absent_when_no_spot(self, tmp_path):
        """spot_interruption_handling line omitted when use_spot=False."""
        provider = _make_provider(tmp_path, use_spot=False)
        config_path = provider.generate_endpoint_config(str(tmp_path / "ep"))
        content = Path(config_path).read_text()
        assert "spot_interruption_handling" not in content


# ---------------------------------------------------------------------------
# TestGenerateEndpointConfigContainer
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGenerateEndpointConfigContainer:
    """Config generation with container_image set."""

    def test_container_type_docker_in_config(self, tmp_path):
        provider = _make_provider(tmp_path, container_image="python:3.11-slim")
        config_path = provider.generate_endpoint_config(str(tmp_path / "ep"))
        content = Path(config_path).read_text()
        assert "container_type: docker" in content

    def test_container_uri_in_config(self, tmp_path):
        provider = _make_provider(tmp_path, container_image="python:3.11-slim")
        config_path = provider.generate_endpoint_config(str(tmp_path / "ep"))
        content = Path(config_path).read_text()
        assert "container_uri: python:3.11-slim" in content

    def test_container_image_in_provider_params(self, tmp_path):
        """container_image also appears in the provider sub-block."""
        provider = _make_provider(tmp_path, container_image="python:3.11-slim")
        config_path = provider.generate_endpoint_config(str(tmp_path / "ep"))
        content = Path(config_path).read_text()
        # Should appear at least twice: engine.container_uri + provider.container_image
        assert content.count("python:3.11-slim") >= 2

    def test_no_container_section_without_image(self, tmp_path):
        """When no container_image is set there is no container_type line."""
        provider = _make_provider(tmp_path)
        config_path = provider.generate_endpoint_config(str(tmp_path / "ep"))
        content = Path(config_path).read_text()
        assert "container_type" not in content

    def test_ecr_image_uri_preserved(self, tmp_path):
        ecr_uri = "123456789.dkr.ecr.us-east-1.amazonaws.com/my-image:latest"
        provider = _make_provider(tmp_path, container_image=ecr_uri)
        config_path = provider.generate_endpoint_config(str(tmp_path / "ep"))
        content = Path(config_path).read_text()
        assert ecr_uri in content


# ---------------------------------------------------------------------------
# TestMinimumIamPolicy
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMinimumIamPolicy:
    """Verify minimum_iam_policy() returns a well-formed IAM policy document."""

    def test_returns_dict(self):
        policy = GlobusComputeProvider.minimum_iam_policy()
        assert isinstance(policy, dict)

    def test_version_field(self):
        policy = GlobusComputeProvider.minimum_iam_policy()
        assert policy["Version"] == "2012-10-17"

    def test_has_statements(self):
        policy = GlobusComputeProvider.minimum_iam_policy()
        assert "Statement" in policy
        assert len(policy["Statement"]) > 0

    def test_ec2_statement_present(self):
        policy = GlobusComputeProvider.minimum_iam_policy()
        sids = {s["Sid"] for s in policy["Statement"]}
        assert "EC2Management" in sids

    def test_ssm_statement_present(self):
        policy = GlobusComputeProvider.minimum_iam_policy()
        sids = {s["Sid"] for s in policy["Statement"]}
        assert "SSMTunneling" in sids

    def test_iam_statement_present(self):
        policy = GlobusComputeProvider.minimum_iam_policy()
        sids = {s["Sid"] for s in policy["Statement"]}
        assert "IAMInstanceProfile" in sids

    def test_ecr_absent_by_default(self):
        policy = GlobusComputeProvider.minimum_iam_policy()
        sids = {s["Sid"] for s in policy["Statement"]}
        assert "ECRContainerImages" not in sids

    def test_ecr_present_when_requested(self):
        policy = GlobusComputeProvider.minimum_iam_policy(include_ecr=True)
        sids = {s["Sid"] for s in policy["Statement"]}
        assert "ECRContainerImages" in sids

    def test_all_effects_are_allow(self):
        policy = GlobusComputeProvider.minimum_iam_policy(include_ecr=True)
        for stmt in policy["Statement"]:
            assert stmt["Effect"] == "Allow"

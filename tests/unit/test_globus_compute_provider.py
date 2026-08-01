"""Unit tests for GlobusComputeProvider.

Verifies config generation for standard, spot, and container variants, the
``parsl.providers`` registration and ``config.py`` shim that make a generated
config loadable (#87), and the minimum_iam_policy() helper.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import os
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import parsl.providers
import pytest

from parsl_aws_provider import GlobusComputeProvider
from parsl_aws_provider.globus_compute import (
    _CONFIG_PY_SHIM,
    _PROVIDER_TYPE,
    _register_with_parsl_providers,
)
from parsl_aws_provider.provider import EphemeralAWSProvider
from parsl_aws_provider.state.file import FileStateStore


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
        patch("parsl_aws_provider.provider.create_session") as mock_session,
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


def _all_actions_list(policy) -> list:
    """Every action in the policy, with duplicates preserved."""
    return [action for stmt in policy["Statement"] for action in stmt["Action"]]


def _all_actions(policy) -> set:
    """Every action in the policy, flattened across statements."""
    return set(_all_actions_list(policy))


# ---------------------------------------------------------------------------
# TestGlobusComputeProviderImport
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGlobusComputeProviderImport:
    """Verify the public import path works."""

    def test_importable_from_package(self):
        """``from parsl_aws_provider import GlobusComputeProvider`` works."""
        # The import at the top of this file already validates this; an
        # explicit assertion makes the intent clear.
        assert GlobusComputeProvider is not None

    def test_is_subclass_of_ephemeral_aws_provider(self, tmp_path):
        """GlobusComputeProvider is a subclass of EphemeralAWSProvider."""
        assert issubclass(GlobusComputeProvider, EphemeralAWSProvider)


# ---------------------------------------------------------------------------
# TestParslProvidersRegistration
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParslProvidersRegistration:
    """Verify the #87 fix: the class is resolvable the way Globus looks it up.

    ``ProviderDispatcher.build_instance`` does
    ``getattr(parsl.providers, type_name, None)`` and raises when the result is
    ``None``, so these assertions mirror that call exactly rather than testing
    a proxy for it.
    """

    def test_class_is_attribute_of_parsl_providers(self):
        """Importing the package puts the class on ``parsl.providers``."""
        # The import at the top of this file has already run the registration.
        assert getattr(parsl.providers, _PROVIDER_TYPE, None) is GlobusComputeProvider

    def test_provider_type_is_a_bare_name(self):
        """The type key has no dots -- ``getattr`` cannot walk them (#87)."""
        assert "." not in _PROVIDER_TYPE

    def test_listed_in_parsl_providers_all(self):
        """``__all__`` lists it, so ``import *`` and error messages include it."""
        assert _PROVIDER_TYPE in parsl.providers.__all__

    def test_registration_is_idempotent(self):
        """Re-registering does not duplicate the ``__all__`` entry."""
        before = list(parsl.providers.__all__)
        _register_with_parsl_providers()
        assert parsl.providers.__all__.count(_PROVIDER_TYPE) == 1
        assert parsl.providers.__all__ == before

    def test_registration_restores_a_removed_attribute(self):
        """Calling it again re-assigns the attribute if something removed it."""
        delattr(parsl.providers, _PROVIDER_TYPE)
        assert getattr(parsl.providers, _PROVIDER_TYPE, None) is None
        try:
            _register_with_parsl_providers()
            assert (
                getattr(parsl.providers, _PROVIDER_TYPE, None) is GlobusComputeProvider
            )
        finally:
            # Leave the module as the rest of the suite expects it.
            _register_with_parsl_providers()


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
        """False by default, and reflects the constructor argument (#138).

        This asserted ``true`` while the value was hardcoded, which is how a
        config no EC2 worker could load kept passing its own test: the worker is
        handed a ``--cert_dir`` under the endpoint host's ``run_dir`` and dies
        ``FileNotFoundError`` before registering (#62).
        """
        provider = _make_provider(tmp_path)
        config_path = provider.generate_endpoint_config(str(tmp_path / "ep"))
        assert "encrypted: false" in Path(config_path).read_text()

        explicit = _make_provider(tmp_path, encrypted=True)
        config_path = explicit.generate_endpoint_config(str(tmp_path / "ep2"))
        assert "encrypted: true" in Path(config_path).read_text()

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

    def test_network_ids_written(self, tmp_path):
        """The IDs #69 made required appear in the provider block.

        Without these the config parses and then dies in the constructor with
        ``vpc_id, subnet_id, and security_group_id are required``.
        """
        provider = _make_provider(tmp_path)
        content = Path(
            provider.generate_endpoint_config(str(tmp_path / "ep"))
        ).read_text()
        assert "vpc_id: vpc-test00001" in content
        assert "subnet_id: subnet-test001" in content
        assert "security_group_id: sg-test00001" in content

    def test_writes_config_py_shim(self, tmp_path):
        """``config.py`` is written alongside ``config.yaml`` (#87)."""
        provider = _make_provider(tmp_path)
        provider.generate_endpoint_config(str(tmp_path / "ep"))
        assert (tmp_path / "ep" / "config.py").is_file()

    def test_shim_imports_the_package(self, tmp_path):
        """The shim's whole purpose is the import that registers the class."""
        provider = _make_provider(tmp_path)
        provider.generate_endpoint_config(str(tmp_path / "ep"))
        shim = (tmp_path / "ep" / "config.py").read_text()
        assert "import parsl_aws_provider" in shim
        assert "load_config_yaml" in shim

    def test_shim_defines_module_level_config(self, tmp_path):
        """``_load_config_py`` reads a module-level ``config``; compile the shim.

        Compiling proves the generated file is syntactically valid without
        executing it (execution would need globus-compute-endpoint installed).
        """
        provider = _make_provider(tmp_path)
        provider.generate_endpoint_config(str(tmp_path / "ep"))
        shim_path = tmp_path / "ep" / "config.py"
        compile(shim_path.read_text(), str(shim_path), "exec")
        assert "\nconfig = " in _CONFIG_PY_SHIM

    def test_returned_path_is_the_yaml_not_the_shim(self, tmp_path):
        """The return value stays the file a caller would edit."""
        provider = _make_provider(tmp_path)
        result = provider.generate_endpoint_config(str(tmp_path / "ep"))
        assert result.endswith("config.yaml")


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

    def test_spot_interruption_statement_present(self):
        """The #86 EventBridge -> SQS warning path needs its own grants."""
        policy = GlobusComputeProvider.minimum_iam_policy()
        sids = {s["Sid"] for s in policy["Statement"]}
        assert "SpotInterruptionWarning" in sids

    def test_network_creation_actions_absent(self):
        """#69 made the network caller-supplied, so no create/delete grants.

        Granting these would let the provider destroy resources it does not own
        -- the same hazard class as the serverless SG deletion (#100).
        """
        actions = _all_actions(
            GlobusComputeProvider.minimum_iam_policy(include_ecr=True)
        )
        for action in (
            "ec2:CreateVpc",
            "ec2:DeleteVpc",
            "ec2:CreateSubnet",
            "ec2:DeleteSubnet",
            "ec2:CreateSecurityGroup",
            "ec2:DeleteSecurityGroup",
            "ec2:CreateNatGateway",
            "ec2:DeleteNatGateway",
            "ec2:CreateInternetGateway",
            "ec2:AllocateAddress",
        ):
            assert action not in actions

    def test_spot_fleet_actions_absent(self):
        """Spot Fleet was replaced by EC2 Fleet in #86."""
        actions = _all_actions(GlobusComputeProvider.minimum_iam_policy())
        assert "ec2:RequestSpotFleet" not in actions
        assert "ec2:CancelSpotFleetRequests" not in actions
        assert "ec2:DescribeSpotFleetRequests" not in actions
        assert "ec2:CreateFleet" in actions

    def test_iam_delete_actions_absent(self):
        """No teardown grants while the provider performs no teardown (#132)."""
        actions = _all_actions(GlobusComputeProvider.minimum_iam_policy())
        for action in (
            "iam:DeleteRole",
            "iam:DeleteInstanceProfile",
            "iam:RemoveRoleFromInstanceProfile",
            "iam:DetachRolePolicy",
        ):
            assert action not in actions

    def test_launch_template_actions_present(self):
        """Every launch path goes through a launch template since #85."""
        actions = _all_actions(GlobusComputeProvider.minimum_iam_policy())
        assert "ec2:CreateLaunchTemplate" in actions
        assert "ec2:DeleteLaunchTemplate" in actions

    def test_ssm_get_parameter_present(self):
        """AMI resolution moved to AWS's public SSM parameters in #82."""
        actions = _all_actions(GlobusComputeProvider.minimum_iam_policy())
        assert "ssm:GetParameter" in actions

    def test_no_duplicate_actions(self):
        """A duplicated action means a list was pasted into two statements."""
        actions = _all_actions_list(
            GlobusComputeProvider.minimum_iam_policy(include_ecr=True)
        )
        assert len(actions) == len(set(actions))

    def test_every_statement_is_well_formed(self):
        policy = GlobusComputeProvider.minimum_iam_policy(include_ecr=True)
        for stmt in policy["Statement"]:
            assert set(stmt) == {"Sid", "Effect", "Action", "Resource"}
            assert isinstance(stmt["Action"], list)
            assert stmt["Action"], f"{stmt['Sid']} has an empty action list"
            assert all(":" in action for action in stmt["Action"])

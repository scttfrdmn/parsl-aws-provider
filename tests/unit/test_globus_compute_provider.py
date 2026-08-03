"""Unit tests for EphemeralComputeProvider.

Verifies config generation for standard, spot, and container variants, the
``parsl.providers`` registration (#87) and the ``sitecustomize`` bootstrap that
makes it reach the exec'd user-endpoint process (#196), and the
minimum_iam_policy() helper.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import ast
import os
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import parsl.providers
import pytest
import yaml

from parsl_ephemeral_provider import EphemeralComputeProvider
from parsl_ephemeral_provider.globus_compute import (
    _BOOTSTRAP_DIRNAME,
    _PROVIDER_TYPE,
    _register_with_parsl_providers,
)
from parsl_ephemeral_provider.provider import EphemeralProvider
from parsl_ephemeral_provider.state.file import FileStateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider(tmp_path, **extra_kwargs) -> EphemeralComputeProvider:
    """Return a EphemeralComputeProvider with all AWS interactions mocked out."""
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
        patch("parsl_ephemeral_provider.provider.create_session") as mock_session,
        patch.object(
            EphemeralProvider,
            "_initialize_state_store",
            return_value=state_store,
        ),
        patch.object(
            EphemeralProvider,
            "_initialize_operating_mode",
            return_value=mode_mock,
        ),
    ):
        mock_session.return_value = MagicMock()
        provider = EphemeralComputeProvider(
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
# TestEphemeralComputeProviderImport
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEphemeralComputeProviderImport:
    """Verify the public import path works."""

    def test_importable_from_package(self):
        """``from parsl_ephemeral_provider import EphemeralComputeProvider`` works."""
        # The import at the top of this file already validates this; an
        # explicit assertion makes the intent clear.
        assert EphemeralComputeProvider is not None

    def test_is_subclass_of_ephemeral_aws_provider(self, tmp_path):
        """EphemeralComputeProvider is a subclass of EphemeralProvider."""
        assert issubclass(EphemeralComputeProvider, EphemeralProvider)


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
        assert (
            getattr(parsl.providers, _PROVIDER_TYPE, None) is EphemeralComputeProvider
        )

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
                getattr(parsl.providers, _PROVIDER_TYPE, None)
                is EphemeralComputeProvider
            )
        finally:
            # Leave the module as the rest of the suite expects it.
            _register_with_parsl_providers()


# ---------------------------------------------------------------------------
# TestEphemeralComputeProviderConstruction
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEphemeralComputeProviderConstruction:
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
        """EphemeralProvider params are still accessible."""
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
    """Verify generate_endpoint_config() writes a startable four-file layout.

    Before #196 everything went into one ``config.yaml``, engine block included.
    That single key is what ``load_config_yaml()`` classifies on: present means
    ``UserEndpointConfig``, and ``start`` refuses anything that is not a
    ``ManagerEndpointConfig``. So the output could never be started, and each
    attempt to debug it leaked another IAM pair. The engine block now lives in
    ``user_config_template.yaml.j2``, which is what these tests read.
    """

    def test_creates_directory_and_returns_the_template(self, tmp_path):
        provider = _make_provider(tmp_path)
        endpoint_dir = str(tmp_path / "my_endpoint")

        result_path = provider.generate_endpoint_config(endpoint_dir)

        assert os.path.isdir(endpoint_dir)
        assert os.path.isfile(result_path)
        assert result_path == str(
            tmp_path / "my_endpoint" / "user_config_template.yaml.j2"
        )

    def test_returns_absolute_path(self, tmp_path):
        provider = _make_provider(tmp_path)
        result_path = provider.generate_endpoint_config(str(tmp_path / "ep"))
        assert os.path.isabs(result_path)

    def test_writes_all_four_files(self, tmp_path):
        """Every one of them is load-bearing; a missing one is a broken endpoint."""
        provider = _make_provider(tmp_path)
        provider.generate_endpoint_config(str(tmp_path / "ep"))
        ep = tmp_path / "ep"

        assert (ep / "config.yaml").is_file()
        assert (ep / "user_config_template.yaml.j2").is_file()
        assert (ep / "user_environment.yaml").is_file()
        assert (ep / _BOOTSTRAP_DIRNAME / "sitecustomize.py").is_file()

    def test_manager_config_has_no_engine_key(self, tmp_path):
        """The #196 blocker, asserted at its narrowest.

        ``load_config_yaml`` does ``config_dict.pop("engine", None)`` and picks
        ``ManagerEndpointConfig`` only when the result is ``None``. One key
        decides whether the endpoint can start at all.
        """
        provider = _make_provider(tmp_path)
        provider.generate_endpoint_config(str(tmp_path / "ep"))
        loaded = yaml.safe_load((tmp_path / "ep" / "config.yaml").read_text())

        assert "engine" not in loaded

    def test_manager_config_omits_the_template_path(self, tmp_path):
        """``user_config_template_path`` must not be emitted.

        Its setter resolves the value against the *process* working directory and
        raises ``ValueError`` if the result does not exist, so naming the file
        relatively breaks ``start`` from anywhere but the endpoint directory.
        Omitting it lets ``Endpoint.user_config_template_path()`` fall back to
        ``endpoint_dir / "user_config_template.yaml.j2"`` -- the file we wrote.
        """
        provider = _make_provider(tmp_path)
        provider.generate_endpoint_config(str(tmp_path / "ep"))
        loaded = yaml.safe_load((tmp_path / "ep" / "config.yaml").read_text())

        assert "user_config_template_path" not in loaded

    def test_manager_config_contains_display_name(self, tmp_path):
        provider = _make_provider(tmp_path, display_name="Test Endpoint")
        provider.generate_endpoint_config(str(tmp_path / "ep"))
        content = (tmp_path / "ep" / "config.yaml").read_text()
        assert "display_name: Test Endpoint" in content

    def test_template_contains_engine_type(self, tmp_path):
        provider = _make_provider(tmp_path)
        content = Path(
            provider.generate_endpoint_config(str(tmp_path / "ep"))
        ).read_text()
        assert "type: GlobusComputeEngine" in content

    def test_template_contains_provider_type(self, tmp_path):
        provider = _make_provider(tmp_path)
        content = Path(
            provider.generate_endpoint_config(str(tmp_path / "ep"))
        ).read_text()
        assert f"type: {_PROVIDER_TYPE}" in content

    def test_template_contains_region(self, tmp_path):
        provider = _make_provider(tmp_path)
        content = Path(
            provider.generate_endpoint_config(str(tmp_path / "ep"))
        ).read_text()
        assert "region: us-east-1" in content

    def test_template_contains_instance_type(self, tmp_path):
        provider = _make_provider(tmp_path)
        content = Path(
            provider.generate_endpoint_config(str(tmp_path / "ep"))
        ).read_text()
        assert "instance_type: t3.micro" in content

    def test_template_contains_mode(self, tmp_path):
        provider = _make_provider(tmp_path)
        content = Path(
            provider.generate_endpoint_config(str(tmp_path / "ep"))
        ).read_text()
        assert "mode: standard" in content

    def test_template_encrypted_flag(self, tmp_path):
        """False by default, and reflects the constructor argument (#138).

        This asserted ``true`` while the value was hardcoded, which is how a
        config no EC2 worker could load kept passing its own test: the worker is
        handed a ``--cert_dir`` under the endpoint host's ``run_dir`` and dies
        ``FileNotFoundError`` before registering (#62).
        """
        provider = _make_provider(tmp_path)
        path = provider.generate_endpoint_config(str(tmp_path / "ep"))
        assert "encrypted: false" in Path(path).read_text()

        explicit = _make_provider(tmp_path, encrypted=True)
        path = explicit.generate_endpoint_config(str(tmp_path / "ep2"))
        assert "encrypted: true" in Path(path).read_text()

    def test_existing_directory_is_ok(self, tmp_path):
        """Calling generate_endpoint_config twice does not raise."""
        provider = _make_provider(tmp_path)
        ep_dir = str(tmp_path / "ep")
        provider.generate_endpoint_config(ep_dir)
        # Second call should overwrite without error
        provider.generate_endpoint_config(ep_dir)

    def test_endpoint_id_is_never_a_top_level_config_key(self, tmp_path):
        """``BaseConfig`` rejects ``endpoint_id`` -- it is not a Globus config field.

        It stays legal where it is emitted, nested under ``engine.provider``, because
        there it binds to a ``EphemeralComputeProvider`` kwarg. At the top level of
        either file it would raise ``Unexpected keyword argument``, which is exactly
        what the old output's ``TODO`` instructed the reader to do.
        """
        ep_id = str(uuid.uuid4())
        provider = _make_provider(tmp_path, endpoint_id=ep_id)
        provider.generate_endpoint_config(str(tmp_path / "ep"))

        for name in ("config.yaml", "user_config_template.yaml.j2"):
            loaded = yaml.safe_load((tmp_path / "ep" / name).read_text())
            assert "endpoint_id" not in loaded

        template = yaml.safe_load(
            (tmp_path / "ep" / "user_config_template.yaml.j2").read_text()
        )
        assert template["engine"]["provider"]["endpoint_id"] == ep_id

    def test_endpoint_id_is_surfaced_as_a_start_flag(self, tmp_path):
        """It is reachable by the manager too, via ``--endpoint-uuid``.

        The manager writes the UUID to ``endpoint.json``; that flag is the only way
        to supply a pre-existing one, so ``config.yaml`` says so rather than
        pretending there is a key for it.
        """
        ep_id = str(uuid.uuid4())
        provider = _make_provider(tmp_path, endpoint_id=ep_id)
        provider.generate_endpoint_config(str(tmp_path / "ep"))
        content = (tmp_path / "ep" / "config.yaml").read_text()

        assert ep_id in content
        assert "--endpoint-uuid" in content

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

    def test_bootstrap_imports_the_package(self, tmp_path):
        """The whole purpose of the bootstrap is the registering import.

        The manager forks and ``execvpe``s a fresh interpreter for the user
        endpoint, which reads its config from stdin -- so nothing in this package
        is imported there and the bare ``EphemeralComputeProvider`` name does not
        resolve. ``sitecustomize`` runs during ``site`` initialisation, before any
        user code, which is early enough.
        """
        provider = _make_provider(tmp_path)
        provider.generate_endpoint_config(str(tmp_path / "ep"))
        bootstrap = tmp_path / "ep" / _BOOTSTRAP_DIRNAME / "sitecustomize.py"

        assert "import parsl_ephemeral_provider" in bootstrap.read_text()
        compile(bootstrap.read_text(), str(bootstrap), "exec")

    def test_bootstrap_does_not_re_raise(self, tmp_path):
        """A missing package must not break every interpreter on the PYTHONPATH.

        ``sitecustomize`` is imported by *any* process that inherits the path.
        Raising there would take out unrelated commands; the endpoint failing
        with "not a valid provider" is the narrower blast radius.
        """
        provider = _make_provider(tmp_path)
        provider.generate_endpoint_config(str(tmp_path / "ep"))
        source = (tmp_path / "ep" / _BOOTSTRAP_DIRNAME / "sitecustomize.py").read_text()

        # Parsed rather than grepped: the file's own comment says "re-raised".
        tree = ast.parse(source)
        assert not [n for n in ast.walk(tree) if isinstance(n, ast.Raise)]
        assert "file=sys.stderr" in source

    def test_user_environment_points_at_the_bootstrap(self, tmp_path):
        """The only seam into the exec'd child is ``user_environment.yaml``.

        The manager reads it and merges it into ``env`` immediately before
        ``os.execvpe``. The path must be absolute: the child's working directory
        is not the endpoint directory.
        """
        provider = _make_provider(tmp_path)
        provider.generate_endpoint_config(str(tmp_path / "ep"))
        loaded = yaml.safe_load((tmp_path / "ep" / "user_environment.yaml").read_text())

        expected = tmp_path.resolve() / "ep" / _BOOTSTRAP_DIRNAME
        assert loaded["PYTHONPATH"] == str(expected)
        assert os.path.isabs(loaded["PYTHONPATH"])

    def test_a_stale_config_py_is_removed(self, tmp_path):
        """``get_config`` prefers ``config.py``, so leaving one keeps the bug.

        Anyone regenerating a directory built before #196 would otherwise get the
        fix on disk and the old unstartable shape at load time.
        """
        ep = tmp_path / "ep"
        ep.mkdir()
        stale = ep / "config.py"
        stale.write_text("config = None\n")

        provider = _make_provider(tmp_path)
        provider.generate_endpoint_config(str(ep))

        assert not stale.exists()


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
        policy = EphemeralComputeProvider.minimum_iam_policy()
        assert isinstance(policy, dict)

    def test_version_field(self):
        policy = EphemeralComputeProvider.minimum_iam_policy()
        assert policy["Version"] == "2012-10-17"

    def test_has_statements(self):
        policy = EphemeralComputeProvider.minimum_iam_policy()
        assert "Statement" in policy
        assert len(policy["Statement"]) > 0

    def test_ec2_statement_present(self):
        policy = EphemeralComputeProvider.minimum_iam_policy()
        sids = {s["Sid"] for s in policy["Statement"]}
        assert "EC2Management" in sids

    def test_ssm_statement_present(self):
        policy = EphemeralComputeProvider.minimum_iam_policy()
        sids = {s["Sid"] for s in policy["Statement"]}
        assert "SSMCommandsAndParameters" in sids

    def test_iam_statement_present(self):
        policy = EphemeralComputeProvider.minimum_iam_policy()
        sids = {s["Sid"] for s in policy["Statement"]}
        assert "IAMInstanceProfile" in sids

    def test_ecr_absent_by_default(self):
        policy = EphemeralComputeProvider.minimum_iam_policy()
        sids = {s["Sid"] for s in policy["Statement"]}
        assert "ECRContainerImages" not in sids

    def test_ecr_present_when_requested(self):
        policy = EphemeralComputeProvider.minimum_iam_policy(include_ecr=True)
        sids = {s["Sid"] for s in policy["Statement"]}
        assert "ECRContainerImages" in sids

    def test_all_effects_are_allow(self):
        policy = EphemeralComputeProvider.minimum_iam_policy(include_ecr=True)
        for stmt in policy["Statement"]:
            assert stmt["Effect"] == "Allow"

    def test_spot_interruption_statement_present(self):
        """The #86 EventBridge -> SQS warning path needs its own grants."""
        policy = EphemeralComputeProvider.minimum_iam_policy()
        sids = {s["Sid"] for s in policy["Statement"]}
        assert "SpotInterruptionWarning" in sids

    def test_network_creation_actions_absent(self):
        """#69 made the network caller-supplied, so no create/delete grants.

        Granting these would let the provider destroy resources it does not own
        -- the same hazard class as the serverless SG deletion (#100).
        """
        actions = _all_actions(
            EphemeralComputeProvider.minimum_iam_policy(include_ecr=True)
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
        actions = _all_actions(EphemeralComputeProvider.minimum_iam_policy())
        assert "ec2:RequestSpotFleet" not in actions
        assert "ec2:CancelSpotFleetRequests" not in actions
        assert "ec2:DescribeSpotFleetRequests" not in actions
        assert "ec2:CreateFleet" in actions

    def test_iam_delete_actions_present(self):
        """The teardown #132 added must be permitted, or the leak returns (#195).

        This test asserted the *opposite* until #195 -- it pinned "no teardown
        grants while the provider performs no teardown (#132)", which stopped
        being true when v0.8.0's #132 fix shipped the teardown. So CI stayed
        green over a policy that silently reverted that fix: cleanup logs rather
        than raises, so the AccessDenied never surfaced and the roles simply
        accumulated, 94 of them in a real account.
        """
        actions = _all_actions(EphemeralComputeProvider.minimum_iam_policy())
        for action in (
            "iam:RemoveRoleFromInstanceProfile",
            "iam:DeleteInstanceProfile",
            "iam:ListAttachedRolePolicies",
            "iam:DetachRolePolicy",
            "iam:DeleteRole",
        ):
            assert action in actions

    def test_session_validation_present(self):
        """create_session() calls this before anything else (#195).

        Omitting it failed the user at ``EphemeralProvider(...)`` itself --
        the first AWS call the package makes, on the construction path.
        """
        actions = _all_actions(EphemeralComputeProvider.minimum_iam_policy())
        assert "sts:GetCallerIdentity" in actions

    def test_parameter_store_write_actions_present(self):
        """Detached mode needs Parameter Store state, so writes must be granted.

        Both deletes appear because they are distinct IAM actions and the
        backend calls both: delete_parameter() per key and delete_parameters()
        for the batched cleanup.
        """
        actions = _all_actions(EphemeralComputeProvider.minimum_iam_policy())
        for action in ("ssm:PutParameter", "ssm:DeleteParameter"):
            assert action in actions
        assert "ssm:DeleteParameters" in actions

    def test_unused_session_manager_actions_absent(self):
        """There is no SSH-over-SSM tunnel in this package (#195).

        Five session actions were granted for "Session Manager tunnels to reach
        workers in a private subnet". No such transport exists -- nothing calls
        any of them, and the bastion is an autonomous orchestrator rather than a
        network tunnel. StartSession in particular is a shell on the instance.
        """
        actions = _all_actions(
            EphemeralComputeProvider.minimum_iam_policy(include_ecr=True)
        )
        for action in (
            "ssm:StartSession",
            "ssm:TerminateSession",
            "ssm:ResumeSession",
            "ssm:DescribeSessions",
            "ssm:GetConnectionStatus",
        ):
            assert action not in actions

    def test_every_granted_action_has_a_call_site(self):
        """The property that keeps this honest, rather than a curated list.

        Hand-maintained policies drift: #195 found this one granting five
        actions nothing called while omitting nine the code did. Deriving the
        check from the package's own source means a newly granted action must
        point at real code, and a newly *called* API is not silently missing --
        the companion test below covers that direction.

        boto3 method names are snake_case of the IAM action, with a handful of
        irregular pairs, so the mapping is computed rather than listed.
        """
        import re
        from pathlib import Path

        pkg = Path(EphemeralComputeProvider.__module__.split(".")[0])
        source = "\n".join(
            p.read_text() for p in Path(pkg.name).rglob("*.py") if p.is_file()
        )

        def boto_name(action: str) -> str:
            return re.sub(r"(?<!^)(?=[A-Z])", "_", action.split(":", 1)[1]).lower()

        # Actions with no single boto3 call of their own name.
        exempt = {
            # Granted on the resource, not called: RunInstances/CreateFleet
            # perform the pass, and CreateTags covers TagSpecifications.
            "iam:PassRole",
            # Read-only existence checks reached via waiters and describe_*.
            "ec2:DescribeTags",
            # Required implicitly by put_rule(Tags=...) rather than by a
            # tag_resource() call of its own -- IAM authorizes the tagging half
            # of a create-with-tags separately, so omitting this fails put_rule
            # itself whenever additional_tags is set.
            "events:TagResource",
        }

        missing = []
        for action in _all_actions(
            EphemeralComputeProvider.minimum_iam_policy(include_ecr=True)
        ):
            if action in exempt or action.startswith("ecr:"):
                continue
            if f".{boto_name(action)}(" not in source:
                missing.append(action)

        assert not missing, f"granted with no call site in the package: {missing}"

    def test_iam_and_sts_calls_are_all_granted(self):
        """The direction #195 actually failed in: called but not granted.

        Scoped to IAM and STS rather than every service, because those are where
        a missing grant is both silent and expensive -- the teardown logs instead
        of raising, so AccessDenied leaks a standing privileged principal, and
        the STS check fails construction outright. Serverless and detached modes
        add calls this policy deliberately does not cover, so a whole-package
        sweep would assert against a scope this method never claimed.
        """
        import re
        from pathlib import Path

        pkg = Path(EphemeralComputeProvider.__module__.split(".")[0]).name
        source = "\n".join(
            p.read_text() for p in Path(pkg).rglob("*.py") if p.is_file()
        )
        granted = _all_actions(EphemeralComputeProvider.minimum_iam_policy())

        # The calls this policy's scope reaches: instance-profile creation and
        # teardown, plus the session check. Derived from the client each is made
        # on, so a call added to either path shows up here.
        expected = {
            "iam": [
                "create_role",
                "get_role",
                "attach_role_policy",
                "create_instance_profile",
                "get_instance_profile",
                "add_role_to_instance_profile",
                "remove_role_from_instance_profile",
                "delete_instance_profile",
                "list_attached_role_policies",
                "detach_role_policy",
                "delete_role",
            ],
            "sts": ["get_caller_identity"],
        }

        ungranted = []
        for service, methods in expected.items():
            for method in methods:
                if f".{method}(" not in source:
                    continue  # not called; nothing to grant
                action = f"{service}:" + "".join(
                    part.title() for part in method.split("_")
                )
                # PassRole is authorized on RunInstances, not by a method call.
                action = re.sub(r"^iam:Sts", "sts:", action)
                if action not in granted:
                    ungranted.append(action)

        assert not ungranted, f"called by the package but not granted: {ungranted}"

    def test_launch_template_actions_present(self):
        """Every launch path goes through a launch template since #85."""
        actions = _all_actions(EphemeralComputeProvider.minimum_iam_policy())
        assert "ec2:CreateLaunchTemplate" in actions
        assert "ec2:DeleteLaunchTemplate" in actions

    def test_ssm_get_parameter_present(self):
        """AMI resolution moved to AWS's public SSM parameters in #82."""
        actions = _all_actions(EphemeralComputeProvider.minimum_iam_policy())
        assert "ssm:GetParameter" in actions

    def test_no_duplicate_actions(self):
        """A duplicated action means a list was pasted into two statements."""
        actions = _all_actions_list(
            EphemeralComputeProvider.minimum_iam_policy(include_ecr=True)
        )
        assert len(actions) == len(set(actions))

    def test_every_statement_is_well_formed(self):
        policy = EphemeralComputeProvider.minimum_iam_policy(include_ecr=True)
        for stmt in policy["Statement"]:
            assert set(stmt) == {"Sid", "Effect", "Action", "Resource"}
            assert isinstance(stmt["Action"], list)
            assert stmt["Action"], f"{stmt['Sid']} has an empty action list"
            assert all(":" in action for action in stmt["Action"])

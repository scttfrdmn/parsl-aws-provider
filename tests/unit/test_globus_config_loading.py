"""Regression test for #87: a generated endpoint config must actually load.

Every other test in `test_globus_compute_provider.py` asserts on the *text* of
the generated files, which is exactly the blind spot that let #87 ship: the old
`config.yaml` was well-formed, internally consistent, and unloadable. The only
assertion that would have caught it is the one made here -- hand the generated
directory to `globus_compute_endpoint`'s own `get_config()` and see what comes
back.

These tests are skipped when `globus-compute-endpoint` is not installed
(`uv sync --extra globus`), so they do not gate a default `uv sync` run. They
need no Globus credentials and touch no network, which is why they live in
`tests/unit/` rather than behind the `globus` marker in `tests/aws/`.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from parsl_ephemeral_aws import GlobusComputeProvider
from parsl_ephemeral_aws.provider import EphemeralAWSProvider
from parsl_ephemeral_aws.state.file import FileStateStore

get_config = pytest.importorskip(
    "globus_compute_endpoint.endpoint.config.utils",
    reason="globus-compute-endpoint not installed (uv sync --extra globus)",
).get_config

pytestmark = pytest.mark.unit


VPC_ID = "vpc-0123456789abcdef0"
SUBNET_ID = "subnet-0123456789abcdef0"
SG_ID = "sg-0123456789abcdef0"


def _make_provider(tmp_path, **extra_kwargs) -> GlobusComputeProvider:
    """Build a provider with every AWS interaction mocked out.

    `_initialize_operating_mode` is patched because `provider.__init__` calls
    `initialize()` unconditionally, and `StandardMode.initialize()` creates a
    real launch template and IAM instance profile.
    """
    provider_id = f"test-{uuid.uuid4().hex[:8]}"
    state_store = FileStateStore(
        file_path=str(tmp_path / f"{provider_id}.json"), provider_id=provider_id
    )

    with (
        patch("parsl_ephemeral_aws.provider.create_session") as mock_session,
        patch.object(
            EphemeralAWSProvider, "_initialize_state_store", return_value=state_store
        ),
        patch.object(
            EphemeralAWSProvider,
            "_initialize_operating_mode",
            return_value=MagicMock(),
        ),
    ):
        mock_session.return_value = MagicMock()
        return GlobusComputeProvider(
            provider_id=provider_id,
            region="us-east-1",
            image_id="ami-0123456789abcdef0",
            instance_type="t3.micro",
            mode="standard",
            vpc_id=VPC_ID,
            subnet_id=SUBNET_ID,
            security_group_id=SG_ID,
            **extra_kwargs,
        )


def _load(endpoint_dir: Path):
    """Load a generated endpoint directory through Globus Compute's own loader.

    The loader really does construct the provider named in the YAML, so the AWS
    session has to be mocked for the duration -- `EphemeralAWSProvider.__init__`
    calls `GetCallerIdentity`, which fails against the synthetic credentials the
    non-`aws` suites run under. Everything under test happens before that point:
    the dispatcher's `getattr(parsl.providers, ...)` lookup, the YAML parse, and
    the binding of the YAML keys to constructor kwargs.
    """
    with (
        patch("parsl_ephemeral_aws.provider.create_session") as mock_session,
        patch.object(
            EphemeralAWSProvider,
            "_initialize_operating_mode",
            return_value=MagicMock(),
        ),
    ):
        mock_session.return_value = MagicMock()
        config = get_config(endpoint_dir)

    # An engine holds a live ZMQ-bound HighThroughputEngine; shut it down so the
    # test does not leak sockets or a process into the rest of the session.
    engine = getattr(config, "engine", None)
    if engine is not None:
        engine.shutdown()
    return config


class TestGeneratedConfigLoads:
    """The generated pair loads, and the provider is the class we registered."""

    def test_config_loads(self, tmp_path):
        provider = _make_provider(tmp_path, endpoint_id=str(uuid.uuid4()))
        provider.generate_endpoint_config(str(tmp_path / "ep"))

        config = _load(tmp_path / "ep")

        assert config.engine is not None
        assert isinstance(config.engine.provider, GlobusComputeProvider)

    def test_provider_receives_network_ids(self, tmp_path):
        """The constructor rejects a provider without them, so this also proves
        the YAML carries all three -- omitting any one raises during the load."""
        provider = _make_provider(tmp_path)
        provider.generate_endpoint_config(str(tmp_path / "ep"))

        loaded = _load(tmp_path / "ep").engine.provider

        assert loaded.vpc_id == VPC_ID
        assert loaded.subnet_id == SUBNET_ID
        assert loaded.security_group_id == SG_ID

    def test_provider_params_round_trip(self, tmp_path):
        provider = _make_provider(
            tmp_path,
            display_name="Round Trip Endpoint",
            max_blocks=7,
            use_spot=True,
        )
        provider.generate_endpoint_config(str(tmp_path / "ep"))

        config = _load(tmp_path / "ep")

        assert config.display_name == "Round Trip Endpoint"
        assert config.engine.provider.region == "us-east-1"
        assert config.engine.provider.instance_type == "t3.micro"
        assert config.engine.provider.max_blocks == 7
        assert config.engine.provider.use_spot is True

    def test_yaml_alone_does_not_load(self, tmp_path):
        """Negative control: without the shim the load fails as it did in #87.

        This is what makes the passing tests above meaningful -- it shows the
        shim is load-bearing and not incidental. `get_config()` prefers
        `config.py`, so deleting it falls back to the bare `config.yaml`, which
        is the pre-fix state: nothing has imported this package, so
        `getattr(parsl.providers, "GlobusComputeProvider", None)` is None.
        """
        import parsl.providers

        provider = _make_provider(tmp_path)
        provider.generate_endpoint_config(str(tmp_path / "ep"))
        (tmp_path / "ep" / "config.py").unlink()

        # Un-register for the duration: this process has already imported the
        # package, whereas the endpoint daemon never does.
        delattr(parsl.providers, "GlobusComputeProvider")
        try:
            with pytest.raises(Exception, match="not a valid provider"):
                _load(tmp_path / "ep")
        finally:
            from parsl_ephemeral_aws.globus_compute import (
                _register_with_parsl_providers,
            )

            _register_with_parsl_providers()


class TestMultiUserLimitation:
    """Pin the known #133 gap so a future change to it is a deliberate one.

    Multi-user endpoints render `user_config_template.yaml.j2` to a string and
    call `load_config_yaml()` on it in a forked process -- `get_config()` is
    never reached, so the `config.py` shim does not apply. This test asserts the
    *mechanism*, not the failure: if upstream adds dotted-path resolution, or we
    ship a `.pth`, this is the test that should start failing and be rewritten.
    """

    def test_load_config_yaml_needs_prior_registration(self, tmp_path):
        import parsl.providers
        from globus_compute_endpoint.endpoint.config.utils import load_config_yaml

        from parsl_ephemeral_aws.globus_compute import _register_with_parsl_providers

        rendered = (
            "engine:\n"
            "  type: GlobusComputeEngine\n"
            "  provider:\n"
            "    type: GlobusComputeProvider\n"
            "    region: us-east-1\n"
            f"    vpc_id: {VPC_ID}\n"
            f"    subnet_id: {SUBNET_ID}\n"
            f"    security_group_id: {SG_ID}\n"
        )

        delattr(parsl.providers, "GlobusComputeProvider")
        try:
            with pytest.raises(Exception, match="not a valid provider"):
                load_config_yaml(rendered)
        finally:
            _register_with_parsl_providers()

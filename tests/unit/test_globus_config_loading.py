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

from parsl_aws_provider import GlobusComputeProvider
from parsl_aws_provider.provider import EphemeralAWSProvider
from parsl_aws_provider.state.file import FileStateStore

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
        patch("parsl_aws_provider.provider.create_session") as mock_session,
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
        patch("parsl_aws_provider.provider.create_session") as mock_session,
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
            from parsl_aws_provider.globus_compute import (
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

        from parsl_aws_provider.globus_compute import _register_with_parsl_providers

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


class TestEveryParameterSurvivesTheRoundTrip:
    """#138: the generator emitted 15 of 52 parameters and dropped the rest.

    `test_provider_params_round_trip` above checks four of them, which is how a
    hand-picked emitter list stayed wrong for so long -- the four it checked were
    all in the list. These tests assert on the parameters that were *missing*,
    and the first one is the check the issue names as the one that would have
    caught all three consequences.
    """

    def test_worker_init_survives(self, tmp_path):
        """Consequence 1, the one that broke every endpoint.

        A Globus worker's launch command is rewritten to
        `globus-compute-endpoint python-exec ...`, so that binary must be on the
        worker's PATH and only `worker_init` puts it there. The generator dropped
        it, so the reconstructed provider fell back to
        `EphemeralAWSProvider.DEFAULT_WORKER_INIT` -- which installs `parsl` and
        not `globus-compute-endpoint`, making every worker command "command not
        found".
        """
        worker_init = (
            "dnf install -y python3.11\npip3.11 install globus-compute-endpoint\n"
        )
        provider = _make_provider(tmp_path, worker_init=worker_init)
        provider.generate_endpoint_config(str(tmp_path / "ep"))

        loaded = _load(tmp_path / "ep").engine.provider

        assert loaded.worker_init == worker_init

    def test_default_worker_init_installs_globus_compute_endpoint(self, tmp_path):
        """A caller who sets nothing still gets a worker that can start.

        The subclass overrides the inherited default, and the value is emitted
        unconditionally so it travels with the config rather than being resolved
        again on load. Asserting the *capability* (the binary is installed), not
        the exact script, so tightening the script does not break this.
        """
        provider = _make_provider(tmp_path)
        provider.generate_endpoint_config(str(tmp_path / "ep"))

        loaded = _load(tmp_path / "ep").engine.provider

        assert "globus-compute-endpoint" in loaded.worker_init

    def test_encrypted_defaults_false_and_is_overridable(self, tmp_path):
        """Consequence 2: `encrypted: true` was hardcoded and cannot work.

        HighThroughputExecutor writes CurveZMQ certificates under its own
        `run_dir` on the endpoint host and hands that path to workers as
        `--cert_dir`; an EC2 worker has no such directory and dies
        `FileNotFoundError` (#62). The capability existed -- a hand-edited
        `encrypted: false` loads fine -- it was simply unreachable.
        """
        provider = _make_provider(tmp_path)
        provider.generate_endpoint_config(str(tmp_path / "ep"))
        assert _load(tmp_path / "ep").engine.encrypted is False

        explicit = _make_provider(tmp_path, encrypted=True)
        explicit.generate_endpoint_config(str(tmp_path / "ep2"))
        assert _load(tmp_path / "ep2").engine.encrypted is True

    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param(
                {"additional_tags": {"Project": "x", "Owner": "y"}}, id="tags"
            ),
            pytest.param(
                {"state_store_type": "s3", "s3_bucket": "b"}, id="state-backend"
            ),
            pytest.param(
                {"use_spot_fleet": True, "instance_types": ["t3.medium", "t3.large"]},
                id="fleet",
            ),
            # auto_create_instance_profile is not decoration: the constructor
            # rejects warm_pool_size > 0 without it, since SSM SendCommand needs
            # IAM permissions on the instance.
            pytest.param(
                {
                    "warm_pool_size": 2,
                    "warm_pool_ttl": 300,
                    "auto_create_instance_profile": True,
                },
                id="warm-pool",
            ),
            pytest.param({"baked_ami_id": "ami-0bakedbakedbaked0"}, id="baked-ami"),
            pytest.param({"key_name": "mykey", "use_public_ips": False}, id="access"),
            # max_idle_time is deprecated and ignored (#194), but it is still
            # persisted and forwarded, so it must still survive the round trip --
            # that is exactly what this class asserts. The warning is expected.
            pytest.param(
                {"auto_shutdown": False, "max_idle_time": 900},
                id="idle",
                marks=pytest.mark.filterwarnings(
                    "ignore:max_idle_time is deprecated:DeprecationWarning"
                ),
            ),
            pytest.param({"profile_name": "aws"}, id="profile"),
        ],
    )
    def test_dropped_parameter_groups_survive(self, tmp_path, kwargs):
        """Consequence 3: the 37 silently-dropped parameters, by group.

        Each group is one thing a user could configure and then find missing --
        no cost allocation, the wrong state backend, an unreachable fleet path.
        A dict and a list are in here on purpose: those need real YAML rendering
        rather than `str()`, which is where a naive emitter breaks.
        """
        provider = _make_provider(tmp_path, **kwargs)
        provider.generate_endpoint_config(str(tmp_path / "ep"))

        loaded = _load(tmp_path / "ep").engine.provider

        for name, expected in kwargs.items():
            actual = getattr(loaded, name)
            # state_store_type normalises to an Enum whose .value is the string.
            actual = getattr(actual, "value", actual)
            assert actual == expected, f"{name}: {actual!r} != {expected!r}"

    def test_subclass_own_params_survive(self, tmp_path):
        """The four `GlobusComputeProvider` params, which the signature loop misses.

        `_provider_params_yaml` walks `EphemeralAWSProvider.__init__`, so these
        need naming individually -- and they land in three different places:
        `display_name` at the top level, `encrypted` and `container_uri` on the
        engine, `endpoint_id` and `container_image` on the provider. Asserting
        after a real load is what proves each one reached a key its consumer
        actually reads.
        """
        endpoint_id = str(uuid.uuid4())
        provider = _make_provider(
            tmp_path,
            endpoint_id=endpoint_id,
            container_image="python:3.11-slim",
            display_name="Subclass Params",
        )
        provider.generate_endpoint_config(str(tmp_path / "ep"))

        config = _load(tmp_path / "ep")

        assert config.display_name == "Subclass Params"
        assert config.engine.container_uri == "python:3.11-slim"
        assert config.engine.provider.endpoint_id == endpoint_id
        assert config.engine.provider.container_image == "python:3.11-slim"

    def test_resolved_ami_is_not_pinned_into_the_config(self, tmp_path):
        """The emitter keys on what the caller passed, not on what differs.

        `image_id` defaults to None and is then filled from SSM with the current
        Amazon Linux 2023 AMI (#84). A "differs from the default" rule would
        write today's AMI into the file and freeze it there, which is the
        staleness #84 removed. `_make_provider` passes `image_id`, so this builds
        a provider without it.
        """
        provider_id = f"test-{uuid.uuid4().hex[:8]}"
        with (
            patch("parsl_aws_provider.provider.create_session") as mock_session,
            patch.object(
                EphemeralAWSProvider,
                "_initialize_state_store",
                return_value=FileStateStore(
                    file_path=str(tmp_path / f"{provider_id}.json"),
                    provider_id=provider_id,
                ),
            ),
            patch.object(
                EphemeralAWSProvider,
                "_initialize_operating_mode",
                return_value=MagicMock(),
            ),
        ):
            mock_session.return_value = MagicMock()
            provider = GlobusComputeProvider(
                provider_id=provider_id,
                region="us-east-1",
                mode="standard",
                vpc_id=VPC_ID,
                subnet_id=SUBNET_ID,
                security_group_id=SG_ID,
            )

        assert "image_id" not in provider._build_config_yaml()

    def test_explicit_image_id_is_emitted(self, tmp_path):
        """The other half: an AMI the caller chose must survive.

        Without this, "don't pin the resolved AMI" could be implemented by
        dropping `image_id` altogether and still pass the test above.
        """
        provider = _make_provider(tmp_path)  # passes image_id
        provider.generate_endpoint_config(str(tmp_path / "ep"))

        loaded = _load(tmp_path / "ep").engine.provider

        assert loaded.image_id == "ami-0123456789abcdef0"

    @pytest.mark.filterwarnings(
        # The sample below sets every accepted parameter, including the
        # deprecated max_idle_time (#194), which is the point: a deprecated
        # option must still be emitted or a loaded config would silently differ.
        "ignore:max_idle_time is deprecated:DeprecationWarning"
    )
    def test_emitted_set_is_derived_from_the_signature(self, tmp_path):
        """The property that keeps this fixed: no hand-maintained list.

        #138 happened because the emitter named parameters one by one and fell
        behind. Passing every parameter the constructor accepts and asserting
        each appears is what makes a newly added option a passing case for free
        rather than a silent omission.
        """
        import inspect

        signature = inspect.signature(EphemeralAWSProvider.__init__)
        from parsl_aws_provider.globus_compute import _SKIP_PARAMS

        # Values chosen only to be non-default and type-plausible; the assertion
        # is about presence, not about what a sane configuration looks like.
        # instance_type and image_id are absent because _make_provider passes
        # them, and region/mode/network because they are its positional shape.
        # one_shot is handled separately below -- the constructor rejects it
        # alongside warm_pool_size, and both belong in the covered set.
        sample = {
            "max_blocks": 4,
            "min_blocks": 1,
            "init_blocks": 1,
            "key_name": "k",
            "profile_name": "aws",
            "state_file_path": "s.json",
            "s3_key": "k.json",
            "parameter_store_path": "/p/x",
            "spot_max_price": "0.05",
            "spot_allocation_strategy": "capacity-optimized",
            "spot_interruption_handling": True,
            "auto_shutdown": False,
            "max_idle_time": 900,
            "bastion_instance_type": "t3.small",
            "memory_size": 2048,
            "timeout": 600,
            "use_public_ips": False,
            "custom_ami": True,
            "iam_instance_profile_arn": "arn:aws:iam::1:instance-profile/p",
            "auto_create_instance_profile": True,
            "status_polling_interval": 30,
            "waiter_delay": 10,
            "waiter_max_attempts": 30,
            "warm_pool_size": 1,
            "warm_pool_ttl": 300,
            "use_spot": True,
        }
        provider = _make_provider(tmp_path, **sample)
        yaml_text = provider._build_config_yaml()

        missing = [
            name
            for name in signature.parameters
            if name in sample
            and name not in _SKIP_PARAMS
            and f"    {name}:" not in yaml_text
        ]
        assert not missing, f"passed but not emitted: {missing}"

        one_shot = _make_provider(
            tmp_path, one_shot=True, auto_create_instance_profile=True
        )
        assert "    one_shot:" in one_shot._build_config_yaml()

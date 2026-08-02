"""Regression tests for #87 and #196: a generated config must actually start.

Every other test in `test_globus_compute_provider.py` asserts on the *text* of
the generated files, which is exactly the blind spot that let both issues ship:
the old `config.yaml` was well-formed, internally consistent, loadable -- and
unstartable. The only assertions that catch that are the ones made here: hand the
generated directory to `globus_compute_endpoint`'s own loader and check the
*class* that comes back, then follow the second half of the path the daemon
actually takes.

That path has two stages, and a test that stops after the first proves nothing:

1. `get_config(endpoint_dir)` reads `config.yaml`. `start` accepts the result only
   if it is a `ManagerEndpointConfig`, which `load_config_yaml` returns only when
   there is no top-level `engine:` key. That is #196.
2. The manager renders `user_config_template.yaml.j2`, forks, and `execvpe`s a
   *fresh interpreter* that reads the rendered config from stdin. Nothing in that
   child ever imports this package, so `getattr(parsl.providers, ...)` fails
   there even though it succeeds in-process. The `sitecustomize` bootstrap on
   `PYTHONPATH` is what fixes it, and only a subprocess can test it -- this
   process has already imported the package.

These tests are skipped when `globus-compute-endpoint` is not installed
(`uv sync --extra globus`), so they do not gate a default `uv sync` run. They
need no Globus credentials and touch no network, which is why they live in
`tests/unit/` rather than behind the `globus` marker in `tests/aws/`.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import os
import pwd
import subprocess
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from parsl_aws_provider import GlobusComputeProvider
from parsl_aws_provider.provider import EphemeralAWSProvider
from parsl_aws_provider.state.file import FileStateStore

_config_utils = pytest.importorskip(
    "globus_compute_endpoint.endpoint.config.utils",
    reason="globus-compute-endpoint not installed (uv sync --extra globus)",
)
get_config = _config_utils.get_config
load_user_config_template = _config_utils.load_user_config_template
render_config_user_template = _config_utils.render_config_user_template

from globus_compute_endpoint.endpoint.config import (  # noqa: E402
    ManagerEndpointConfig,
    UserEndpointConfig,
)
from globus_compute_endpoint.endpoint.identity_mapper import (  # noqa: E402
    MappedPosixIdentity,
)

pytestmark = pytest.mark.unit


VPC_ID = "vpc-0123456789abcdef0"
SUBNET_ID = "subnet-0123456789abcdef0"
SG_ID = "sg-0123456789abcdef0"

# Run in the exec'd child. Deliberately never names `parsl_aws_provider`:
# `patch("parsl_aws_provider.provider.create_session")` would *import* the
# package by name, which is the very thing the negative control has to rule out.
# Patching botocore instead keeps the AWS calls in the constructor mocked without
# touching the import under test.
_CHILD_LOADER = r"""
import json, sys
from unittest.mock import MagicMock, patch
from globus_compute_endpoint.endpoint.config.utils import load_config_yaml

out = {"sitecustomize_ran": "sitecustomize" in sys.modules,
       "package_imported": "parsl_aws_provider" in sys.modules}
with patch("boto3.Session", MagicMock()), patch("botocore.session.Session", MagicMock()):
    try:
        cfg = load_config_yaml(sys.stdin.read())
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    else:
        out["config_class"] = type(cfg).__name__
        out["provider_class"] = type(cfg.engine.provider).__name__
        out["region"] = cfg.engine.provider.region
        out["encrypted"] = cfg.engine.encrypted
        cfg.engine.shutdown()
print("RESULT " + json.dumps(out))
"""


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


def _load_manager(endpoint_dir: Path) -> ManagerEndpointConfig:
    """Load `config.yaml` the way `start` does, via Globus Compute's loader.

    No AWS mocking is needed and none is done, which is itself the point: since
    #196 moved the `engine:` block out of this file, loading it constructs no
    provider and so reaches no AWS API. Before the fix, every rejected load
    created an IAM role and instance profile on the way to failing.
    """
    return get_config(endpoint_dir)


def _render(endpoint_dir: Path) -> str:
    """Render `user_config_template.yaml.j2` as the endpoint manager would."""
    template_path = endpoint_dir / "user_config_template.yaml.j2"
    return render_config_user_template(
        parent_config=_load_manager(endpoint_dir),
        user_config_template=load_user_config_template(template_path),
        user_config_template_path=template_path,
        mapped_identity=MappedPosixIdentity(
            local_user_record=pwd.getpwuid(os.getuid()),
            matched_identity=uuid.UUID(int=0),
            globus_identity_candidates=[],
        ),
    )


def _load_in_child(endpoint_dir: Path, *, with_bootstrap: bool = True) -> dict:
    """Load the rendered template in a fresh interpreter, as the manager does.

    `endpoint_manager` forks and `execvpe`s `globus-compute-endpoint
    _start-user-endpoint`, which reads its config from stdin; the only seam into
    that child's environment is `user_environment.yaml`, which the manager merges
    into `env` immediately before the exec. This reproduces that shape exactly.

    A subprocess is not incidental here. This process imported
    `parsl_aws_provider` at module scope, so `parsl.providers` already carries the
    class and any in-process assertion is answering the wrong question.
    """
    env = dict(os.environ)
    env.pop("AWS_PROFILE", None)  # the child must not resolve a named profile
    if with_bootstrap:
        data = yaml.safe_load((endpoint_dir / "user_environment.yaml").read_text())
        env.update({k: str(v) for k, v in (data or {}).items()})
    else:
        env.pop("PYTHONPATH", None)

    completed = subprocess.run(
        [sys.executable, "-c", _CHILD_LOADER],
        input=_render(endpoint_dir),
        env=env,
        # The child really constructs the provider, and `state_file_path` defaults
        # to a *relative* `ephemeral_aws_state.json`, so without this the state
        # document lands in whatever cwd the test runner happened to have -- the
        # checkout -- which `_no_default_state_file_left_behind` fails on. It is
        # also the truer shape: the endpoint manager's exec'd child inherits the
        # manager's working directory, never this repository.
        cwd=str(endpoint_dir.parent),
        capture_output=True,
        text=True,
        timeout=180,
    )
    for line in completed.stdout.splitlines():
        if line.startswith("RESULT "):
            import json

            return json.loads(line[len("RESULT ") :])
    raise AssertionError(
        f"child produced no result\nstdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr[-2000:]}"
    )


def _load(endpoint_dir: Path):
    """Load the rendered user config in-process, for parameter round-tripping.

    The provider really is constructed, so the AWS session has to be mocked --
    `EphemeralAWSProvider.__init__` calls `GetCallerIdentity`, which fails against
    the synthetic credentials the non-`aws` suites run under. Everything the
    parameter tests care about happens before that: the dispatcher's
    `getattr(parsl.providers, ...)` lookup, the YAML parse, and the binding of the
    YAML keys to constructor kwargs.
    """
    rendered = _render(endpoint_dir)
    with (
        patch("parsl_aws_provider.provider.create_session") as mock_session,
        patch.object(
            EphemeralAWSProvider,
            "_initialize_operating_mode",
            return_value=MagicMock(),
        ),
    ):
        mock_session.return_value = MagicMock()
        config = _config_utils.load_config_yaml(rendered)

    # An engine holds a live ZMQ-bound HighThroughputEngine; shut it down so the
    # test does not leak sockets or a process into the rest of the session.
    engine = getattr(config, "engine", None)
    if engine is not None:
        engine.shutdown()
    return config


class TestManagerConfigIsStartable:
    """#196: `start` accepts one class, and the old output was the other one.

    `load_config_yaml` pops `engine` and returns `ManagerEndpointConfig` when it is
    absent, `UserEndpointConfig` when present. `_start_endpoint_manager` then
    refuses anything that is not the former, and `start` routes there
    unconditionally. So the whole blocker reduces to which class comes back --
    which is what these assert, rather than the text that produced it.
    """

    def test_manager_config_is_the_class_start_accepts(self, tmp_path):
        provider = _make_provider(tmp_path)
        provider.generate_endpoint_config(str(tmp_path / "ep"))

        config = _load_manager(tmp_path / "ep")

        assert isinstance(config, ManagerEndpointConfig)
        assert not isinstance(config, UserEndpointConfig)

    def test_display_name_survives_to_the_manager(self, tmp_path):
        """The one value the manager config carries has to arrive."""
        provider = _make_provider(tmp_path, display_name="Manager Named")
        provider.generate_endpoint_config(str(tmp_path / "ep"))

        assert _load_manager(tmp_path / "ep").display_name == "Manager Named"

    def test_template_path_resolves_from_a_foreign_cwd(self, tmp_path, monkeypatch):
        """Why `user_config_template_path` is deliberately not emitted.

        Its setter resolves the value against the process working directory and
        raises if the result does not exist, so emitting a relative path makes
        `start` work from the endpoint directory and fail from anywhere else.
        Omitting it lets `Endpoint.user_config_template_path()` derive the path
        from the endpoint directory, which is correct from any cwd.
        """
        provider = _make_provider(tmp_path)
        provider.generate_endpoint_config(str(tmp_path / "ep"))

        monkeypatch.chdir(tmp_path.parent)
        config = _load_manager(tmp_path / "ep")

        assert config.user_config_template_path is None

    def test_loading_the_manager_config_creates_no_provider(self, tmp_path):
        """The IAM leak, fixed structurally rather than by better teardown.

        Before #196 the `engine:` block sat in `config.yaml`, so *every* load --
        including the ones that went on to be rejected -- constructed a provider,
        and `EphemeralAWSProvider.__init__` calls `initialize()`, which creates an
        IAM role and instance profile. A rejected config now creates nothing
        because nothing is constructed: note that this test mocks no AWS at all
        and still passes.
        """
        provider = _make_provider(tmp_path)
        provider.generate_endpoint_config(str(tmp_path / "ep"))

        config = _load_manager(tmp_path / "ep")

        assert getattr(config, "engine", None) is None


class TestUserEndpointLoadsInAForkedInterpreter:
    """The second half of #196: the fix must reach the process that execs.

    Moving the engine block to the template is only half a fix. The template is
    loaded by a fresh interpreter that reads its config from stdin, so the
    `config.py` shim -- which only `get_config()` honours -- can never run there.
    Without the `sitecustomize` bootstrap this change would push every endpoint
    into the #133 failure instead of the #196 one.
    """

    def test_rendered_template_loads_in_the_child(self, tmp_path):
        provider = _make_provider(tmp_path)
        provider.generate_endpoint_config(str(tmp_path / "ep"))

        result = _load_in_child(tmp_path / "ep")

        assert "error" not in result, result.get("error")
        assert result["config_class"] == "UserEndpointConfig"
        assert result["provider_class"] == "GlobusComputeProvider"
        assert result["region"] == "us-east-1"
        assert result["encrypted"] is False

    def test_the_package_is_imported_before_any_user_code(self, tmp_path):
        """`sitecustomize` runs during `site` initialisation, which is why this works.

        A later hook would be too late: the dispatcher's `getattr` happens while
        the config is being parsed, before anything the endpoint owner controls.
        """
        provider = _make_provider(tmp_path)
        provider.generate_endpoint_config(str(tmp_path / "ep"))

        result = _load_in_child(tmp_path / "ep")

        assert result["sitecustomize_ran"] is True
        assert result["package_imported"] is True

    def test_without_the_bootstrap_the_child_cannot_resolve_the_provider(
        self, tmp_path
    ):
        """Negative control: the bootstrap is load-bearing, not incidental.

        This is the #133 failure, and it is what the generated output would hit if
        `user_environment.yaml` were dropped as an implementation detail.
        `ProviderDispatcher` does `getattr(parsl.providers, type_name, None)` and
        `parsl.providers` has no module `__getattr__`, so a name nothing
        registered simply is not there.
        """
        provider = _make_provider(tmp_path)
        provider.generate_endpoint_config(str(tmp_path / "ep"))

        result = _load_in_child(tmp_path / "ep", with_bootstrap=False)

        assert result["package_imported"] is False
        assert "not a valid provider" in result.get("error", "")


class TestGeneratedConfigLoads:
    """The rendered template loads, and the provider is the class we registered."""

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

        assert _load_manager(tmp_path / "ep").display_name == "Round Trip Endpoint"

        engine = _load(tmp_path / "ep").engine
        assert engine.provider.region == "us-east-1"
        assert engine.provider.instance_type == "t3.micro"
        assert engine.provider.max_blocks == 7
        assert engine.provider.use_spot is True


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
        need naming individually -- and since #196 they land in three different
        places across *two* files: `display_name` in the manager `config.yaml`,
        `encrypted` and `container_uri` on the engine in the template, and
        `container_image` on the provider. Asserting after a real load is what
        proves each one reached a key its consumer actually reads.

        `endpoint_id` is the exception and is checked separately below: it is not a
        config key in any file, because `BaseConfig` rejects it.
        """
        provider = _make_provider(
            tmp_path,
            endpoint_id=str(uuid.uuid4()),
            container_image="python:3.11-slim",
            display_name="Subclass Params",
        )
        provider.generate_endpoint_config(str(tmp_path / "ep"))

        assert _load_manager(tmp_path / "ep").display_name == "Subclass Params"

        engine = _load(tmp_path / "ep").engine
        assert engine.container_uri == "python:3.11-slim"
        assert engine.provider.container_image == "python:3.11-slim"

    def test_endpoint_id_reaches_the_provider_but_not_the_manager(self, tmp_path):
        """Both halves, each asserted after a real load.

        Nested under `engine.provider` it binds to a `GlobusComputeProvider` kwarg
        and survives. At the top level of `config.yaml` it would raise `Unexpected
        keyword argument` from `BaseConfig` -- which is what the old output's `TODO`
        told the reader to do. The manager writes the real UUID to `endpoint.json`;
        the way to supply one is `start --endpoint-uuid`.
        """
        endpoint_id = str(uuid.uuid4())
        provider = _make_provider(tmp_path, endpoint_id=endpoint_id)
        provider.generate_endpoint_config(str(tmp_path / "ep"))

        assert _load(tmp_path / "ep").engine.provider.endpoint_id == endpoint_id

        # Reachable in the manager config as guidance, not as a key -- so the load
        # that would have failed succeeds.
        manager_text = (tmp_path / "ep" / "config.yaml").read_text()
        assert endpoint_id in manager_text
        assert "endpoint_id" not in yaml.safe_load(manager_text)
        assert isinstance(_load_manager(tmp_path / "ep"), ManagerEndpointConfig)

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

        assert "image_id" not in provider._build_user_config_template()

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
        yaml_text = provider._build_user_config_template()

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
        assert "    one_shot:" in one_shot._build_user_config_template()

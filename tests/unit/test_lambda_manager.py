"""Unit tests for LambdaManager code generation and credential configuration.

Every existing test patches ``_generate_lambda_code`` with a stub return value,
so the real body had never run — and it raised ``ValueError`` on every call.
These tests execute it.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import io
import zipfile
from types import SimpleNamespace

import pytest

from parsl_ephemeral_aws.compute.lambda_func import LambdaManager
from parsl_ephemeral_aws.security.credential_manager import CredentialConfiguration


pytestmark = pytest.mark.unit


def _manager(**provider_attrs):
    """Build a LambdaManager with only the attributes under test.

    ``__init__`` pulls in the audit logger and credential manager, neither of
    which is relevant here, so it is bypassed deliberately.
    """
    attrs = {"workflow_id": "test-workflow", "region": "us-east-1"}
    attrs.update(provider_attrs)

    manager = object.__new__(LambdaManager)
    manager.provider = SimpleNamespace(**attrs)
    return manager


def _handler_source(command):
    """Generate the Lambda payload and return the handler module source."""
    payload = _manager()._generate_lambda_code(command)
    return zipfile.ZipFile(io.BytesIO(payload)).read("handler.py").decode()


class TestGeneratedLambdaCode:
    """The handler is generated from a template; it has to be valid Python."""

    def test_generated_handler_compiles(self):
        """Regression: the template was an f-string, so its dict braces were
        read as replacement fields and every call raised ValueError."""
        compile(_handler_source("echo hello"), "handler.py", "exec")

    def test_generated_handler_runs_the_command(self):
        """The handler must actually execute the baked-in command."""
        namespace = {}
        exec(_handler_source("echo hello"), namespace)  # noqa: S102

        response = namespace["main"]({}, None)

        assert response["statusCode"] == 200
        assert response["returncode"] == 0
        assert response["stdout"].strip() == "hello"

    def test_nonzero_exit_reports_500(self):
        """A failing command must be distinguishable from a successful one."""
        namespace = {}
        exec(_handler_source("exit 3"), namespace)  # noqa: S102

        response = namespace["main"]({}, None)

        assert response["statusCode"] == 500
        assert response["returncode"] == 3

    def test_event_command_overrides_the_baked_in_default(self):
        """The event payload takes precedence over the compiled-in command."""
        namespace = {}
        exec(_handler_source("echo baked"), namespace)  # noqa: S102

        response = namespace["main"]({"command": "echo from-event"}, None)

        assert response["stdout"].strip() == "from-event"

    @pytest.mark.parametrize(
        "command",
        [
            "echo hello",
            "python -c \"print('hi')\"",
            "echo 'single'",
            'echo "double"',
            "echo a\\nb",
        ],
    )
    def test_quoting_in_the_command_survives(self, command):
        """The command is embedded as a JSON literal, so quotes must round-trip."""
        namespace = {}
        exec(_handler_source(command), namespace)  # noqa: S102

        assert namespace["DEFAULT_COMMAND"] == command

    def test_payload_is_a_zip_containing_handler_py(self):
        """Lambda expects a ZIP whose handler module matches the configured name."""
        payload = _manager()._generate_lambda_code("echo hello")

        assert zipfile.ZipFile(io.BytesIO(payload)).namelist() == ["handler.py"]


class TestCredentialConfiguration:
    """The credential config used field names the dataclass does not have."""

    def test_credential_config_is_constructible(self):
        """Regression: aws_access_key_id/aws_secret_access_key/aws_session_token
        are not fields on CredentialConfiguration, so this raised TypeError."""
        manager = _manager()
        manager.security_config = SimpleNamespace(
            environment=SimpleNamespace(value="dev")
        )

        config = manager._create_credential_config_from_provider()

        assert isinstance(config, CredentialConfiguration)

    def test_no_profile_is_invented_when_the_provider_has_none(self):
        """It previously defaulted to a profile literally named "aws"."""
        manager = _manager()
        manager.security_config = SimpleNamespace(
            environment=SimpleNamespace(value="dev")
        )

        config = manager._create_credential_config_from_provider()

        assert config.use_profile is None

    def test_production_environment_disables_ambient_credentials(self):
        """Matches the behaviour of the other three compute managers."""
        manager = _manager(aws_profile="aws", aws_access_key_id="AKIA0000000000000000")
        manager.security_config = SimpleNamespace(
            environment=SimpleNamespace(value="production")
        )

        config = manager._create_credential_config_from_provider()

        assert config.use_environment_variables is False
        assert config.use_profile is None

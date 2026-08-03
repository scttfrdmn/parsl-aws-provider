"""Shared test helpers for constructing compute managers against mocks.

These live outside ``conftest.py`` deliberately: they are plain functions, not
fixtures, so tests can call them mid-body with per-case overrides rather than
taking them as parameters.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

from unittest.mock import MagicMock, patch

from parsl_ephemeral_provider.constants import DEFAULT_SPOT_ALLOCATION_STRATEGY


def mock_provider(**overrides):
    """Build a mock provider that satisfies every compute manager's ``__init__``.

    All four managers are ``__init__(self, provider)`` and read a fixed set of
    attributes off it -- ``workflow_id``/``region`` for audit events, the four
    ``aws_*`` credential fields, and the security fields that
    ``SecurityConfig``/``CredentialConfig`` require. A bare ``MagicMock`` is not
    enough, for two reasons:

    * ``SecurityConfig``/``CredentialConfig`` read several attributes as
      *values*, so a MagicMock where a CIDR string or a bool belongs raises
      during construction.
    * the audit logger ``json.dumps``es its event metadata, and a MagicMock
      attribute reaching it fails with ``TypeError: Object of type Mock is not
      JSON serializable`` -- surfacing as ``ResourceCreationError: Credential
      initialization failed``, which names neither the attribute nor the cause.

    Parameters
    ----------
    **overrides
        Attributes to set after the defaults, e.g. ``session=my_session`` or
        ``use_spot_instances=True``.
    """
    provider = MagicMock()
    provider.workflow_id = "test-workflow"
    provider.region = "us-east-1"
    provider.aws_access_key_id = None
    provider.aws_secret_access_key = None
    provider.aws_session_token = None
    provider.aws_profile = None
    # Read as values by SecurityConfig / CredentialConfig, not just passed along.
    provider.security_config = None
    provider.security_environment = "dev"
    provider.vpc_cidr = "10.0.0.0/16"
    provider.admin_cidr_blocks = None
    provider.strict_security_mode = None
    provider.role_arn = None
    provider.vpc_id = "vpc-12345"
    provider.subnet_id = "subnet-12345"
    provider.security_group_id = "sg-12345"
    provider.subnet_ids = ["subnet-12345"]
    provider.use_spot_instances = False
    provider.nodes_per_block = 1
    provider.tags = {}
    # Read as a value and normalised to the camelCase spelling RequestSpotFleet
    # accepts (#84); a MagicMock here fails inside the normaliser rather than at
    # the API, so the real default has to be present.
    provider.spot_allocation_strategy = DEFAULT_SPOT_ALLOCATION_STRATEGY
    # Every manager now resolves its session through resolve_manager_session,
    # which prefers ``provider.session`` and only falls back to the credential
    # manager when there is none (#117). A bare MagicMock always *has* a
    # ``.session``, so a test that stubs CredentialManager alone hands the
    # manager an auto-created mock instead of the client it configured. Callers
    # pass ``session=`` to control it.
    provider.session = None
    for name, value in overrides.items():
        setattr(provider, name, value)
    return provider


def make_manager(manager_cls, module, client=None, **provider_overrides):
    """Construct *manager_cls* against a mocked session, returning it.

    Patches ``CredentialManager`` in the manager's own module so no real
    credential resolution or boto3 session is attempted; every ``client()`` and
    ``resource()`` call on the session returns the same MagicMock, so the tests
    can set ``side_effect`` on whichever client attribute they name.

    Parameters
    ----------
    manager_cls : type
        The manager to construct, e.g. ``ECSManager``.
    module : str
        Its module name under ``parsl_ephemeral_provider.compute``, e.g. ``"ecs"`` --
        needed because ``CredentialManager`` must be patched where it is *used*.
    client : Optional[MagicMock]
        Pre-configured client to hand back from every ``session.client()`` call.
        Pass one when the test needs a call to fail during ``__init__``.
    **provider_overrides
        Forwarded to :func:`mock_provider`.
    """
    provider = mock_provider(**provider_overrides)
    if client is None:
        client = MagicMock()
    session = MagicMock()
    session.client.return_value = client
    session.resource.return_value = MagicMock()
    session.region_name = "us-east-1"

    with patch(
        f"parsl_ephemeral_provider.compute.{module}.CredentialManager"
    ) as mock_cm:
        mock_cm.return_value.create_boto3_session.return_value = session
        return manager_cls(provider=provider)

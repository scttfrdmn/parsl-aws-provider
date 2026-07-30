"""Unit tests for compute-manager session resolution.

``resolve_manager_session`` is the single place where all four compute managers
decide which boto3 session to operate through. It was extracted because every
manager went straight to ``credential_manager.create_boto3_session()``, throwing
away ``provider.session`` — so an explicitly configured session (role
credentials, a chosen profile, a LocalStack endpoint) was silently replaced by
one built from ambient environment credentials, possibly in a different account
(#117).

Only one existing test caught that, and only incidentally: it asserted on a
client the manager happened to reach through the injected session. These tests
pin the precedence rule itself.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

from unittest.mock import MagicMock

import pytest

from parsl_ephemeral_aws.constants import DEFAULT_REGION
from parsl_ephemeral_aws.utils.aws import resolve_manager_session

pytestmark = pytest.mark.unit


def test_the_providers_own_session_wins():
    """A provider that carries a session has it used verbatim.

    The identity check matters: returning an equivalent-but-different session
    would still lose an ``endpoint_url`` or a set of temporary credentials.
    """
    provider_session = MagicMock(name="provider_session")
    provider = MagicMock(session=provider_session, region="eu-west-2")
    credential_manager = MagicMock()

    resolved = resolve_manager_session(provider, credential_manager)

    assert resolved is provider_session
    credential_manager.create_boto3_session.assert_not_called()


def test_the_credential_manager_is_the_fallback():
    """With no session on the provider, one is built from its region."""
    built_session = MagicMock(name="built_session")
    credential_manager = MagicMock()
    credential_manager.create_boto3_session.return_value = built_session
    provider = MagicMock(session=None, region="eu-west-2")

    resolved = resolve_manager_session(provider, credential_manager)

    assert resolved is built_session
    credential_manager.create_boto3_session.assert_called_once_with(region="eu-west-2")


def test_a_provider_without_a_session_attribute_falls_back():
    """The lookup is a ``getattr``, so a mode or provider missing the attribute
    is a fallback, not an ``AttributeError``.

    ``ServerlessMode`` passes itself to ``LambdaManager``/``ECSManager``, and
    operating modes are not guaranteed to expose the provider's full surface.
    """

    class SessionlessProvider:
        region = "ap-southeast-2"

    credential_manager = MagicMock()

    resolve_manager_session(SessionlessProvider(), credential_manager)

    credential_manager.create_boto3_session.assert_called_once_with(
        region="ap-southeast-2"
    )


@pytest.mark.parametrize("region", [None, ""])
def test_a_missing_region_falls_back_to_the_default(region):
    """``create_boto3_session`` is never called with a falsy region.

    boto3 resolves ``region_name=None`` from the ambient environment, so a
    provider whose region was never set would land wherever ``AWS_DEFAULT_REGION``
    points — a different region than the one the rest of the provider tags and
    queries.
    """
    credential_manager = MagicMock()
    provider = MagicMock(session=None, region=region)

    resolve_manager_session(provider, credential_manager)

    credential_manager.create_boto3_session.assert_called_once_with(
        region=DEFAULT_REGION
    )

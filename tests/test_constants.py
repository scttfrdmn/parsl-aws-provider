"""Tests for the constants module.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import pytest

from parsl_ephemeral_aws import constants

pytestmark = pytest.mark.unit


def test_default_region():
    """Test that the default region is set."""
    assert constants.DEFAULT_REGION == "us-east-1"


def test_default_instance_type():
    """Test that the default instance type is set."""
    assert constants.DEFAULT_INSTANCE_TYPE == "t3.micro"


def test_tag_values():
    """The tag keys are bare names, not prefixed ones.

    This asserted ``parsl-ephemeral-workflow-id`` and friends, which no commit
    has ever produced. ``TAG_PREFIX`` prefixes resource *names* (see
    ``spot_fleet.py``'s IAM role); the tag *keys* are plain, and ``TAG_NAME`` is
    EC2's reserved ``Name`` key.
    """
    assert constants.TAG_PREFIX == "parsl-ephemeral"
    assert constants.TAG_NAME == "Name"
    assert constants.TAG_WORKFLOW_ID == "WorkflowId"
    assert constants.TAG_BLOCK_ID == "BlockId"
    assert constants.TAG_JOB_ID == "JobId"


def test_managed_marker_is_not_the_name_key():
    """The provider-managed marker must not collide with ``Name`` (#109).

    Resources are tagged with both a descriptive ``Name`` and a marker meaning
    "this provider created it". While the marker key *was* ``TAG_NAME``, every
    such tag list sent ``Name`` twice and EC2 rejected the whole request with
    ``Duplicate tag key 'Name' specified.``
    """
    assert constants.TAG_MANAGED == "ParslEphemeralManaged"
    assert constants.TAG_MANAGED != constants.TAG_NAME

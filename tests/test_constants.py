"""Tests for the constants module.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

from parsl_ephemeral_aws import constants


def test_default_region():
    """Test that the default region is set."""
    assert constants.DEFAULT_REGION == 'us-east-1'


def test_default_instance_type():
    """Test that the default instance type is set."""
    assert constants.DEFAULT_INSTANCE_TYPE == 't3.medium'


def test_tag_values():
    """Test that tag values are correctly formatted."""
    assert constants.TAG_PREFIX == 'parsl-ephemeral'
    assert constants.TAG_NAME == 'parsl-ephemeral-resource'
    assert constants.TAG_WORKFLOW_ID == 'parsl-ephemeral-workflow-id'
    assert constants.TAG_BLOCK_ID == 'parsl-ephemeral-block-id'
    assert constants.TAG_JOB_ID == 'parsl-ephemeral-job-id'
"""Unit tests for the SpotFleetManager class.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import unittest
from unittest.mock import patch, MagicMock
from botocore.exceptions import ClientError
import logging

import pytest

from parsl_aws_provider.compute.spot_fleet import SpotFleetManager
from parsl_aws_provider.constants import (
    EC2_FLEET_DEFAULT_ALLOCATION_STRATEGY,
    TAG_PREFIX,
)
from parsl_aws_provider.exceptions import SpotFleetThrottlingError

pytestmark = pytest.mark.unit


class _SimpleProvider:
    """A provider stand-in that answers only the attributes it is given.

    Mirrors the anonymous type StandardMode builds for SpotFleetManager
    (``standard.py:220``). Unlike a MagicMock it raises ``AttributeError`` for
    anything it was not given, so the manager's ``getattr(..., default)`` reads
    fall through to their defaults exactly as they do in production.
    """

    def __init__(self, **attrs):
        for name, value in attrs.items():
            setattr(self, name, value)


class TestSpotFleetManager(unittest.TestCase):
    """Test suite for the SpotFleetManager class."""

    # The resolved network every fleet launches into. Supplied by the caller
    # since #69; the manager creates none of it.
    NETWORK = {
        "vpc_id": "vpc-12345678",
        "subnet_id": "subnet-12345678",
        "security_group_id": "sg-12345678",
    }

    def setUp(self):
        """Set up the test environment."""
        # A plain object, not a MagicMock. SpotFleetManager reads its optional
        # security settings with getattr(provider, name, <default>) --
        # `vpc_cidr`, `security_environment`, `admin_cidr_blocks`,
        # `strict_security_mode` -- and a MagicMock answers every one of them
        # with a MagicMock, so the default never applies and SecurityConfig
        # raises `ValueError: Invalid VPC CIDR: <MagicMock ...>` during
        # construction. That was every failure in this file.
        #
        # The single live caller (StandardMode, standard.py:220) builds exactly
        # this shape: a bare namespace carrying only the attributes below, none
        # of them security settings. Modelling it means the getattr defaults are
        # exercised the way they are in production, instead of being masked.
        self.mock_provider = _SimpleProvider(
            workflow_id="test-workflow-id",
            region="us-east-1",
            aws_access_key_id="test_access_key",
            aws_secret_access_key="test_secret_key",
            aws_session_token=None,
            aws_profile=None,
            vpc_id=None,
            subnet_id=None,
            security_group_id=None,
            image_id="ami-12345678",
            instance_type="t2.micro",
            instance_types=[],
            key_name=None,
            use_public_ips=True,
            nodes_per_block=1,
            tags={"ProjectTag": "TestProject"},
            spot_max_price_percentage=100,
            worker_init="echo 'Worker init script'",
        )

        # Disable logging during tests
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        """Clean up after tests."""
        # Re-enable logging
        logging.disable(logging.NOTSET)

    @patch("parsl_aws_provider.compute.spot_fleet.CredentialManager")
    def test_initialization(self, mock_credential_manager_cls):
        """Test SpotFleetManager initialization.

        The session is obtained from ``CredentialManager.create_boto3_session()``,
        not by handing the provider's keys straight to ``boto3.Session`` -- the
        manager derives a ``CredentialConfiguration`` from the provider and lets
        the credential manager resolve it (which is what applies sanitization and
        token refresh). This test used to assert the direct-``boto3.Session``
        construction, a path that no longer exists.
        """
        mock_session = MagicMock()
        mock_ec2_client = MagicMock()
        mock_ec2_resource = MagicMock()

        mock_session.client.return_value = mock_ec2_client
        mock_session.resource.return_value = mock_ec2_resource
        mock_manager = mock_credential_manager_cls.return_value
        mock_manager.create_boto3_session.return_value = mock_session

        # Instantiate SpotFleetManager
        manager = SpotFleetManager(self.mock_provider)

        # The provider's region must reach the session, not a default.
        mock_manager.create_boto3_session.assert_called_once_with(region="us-east-1")

        # And the config handed to CredentialManager must be the one derived from
        # the provider: the provider carries explicit keys, so environment-variable
        # credentials are enabled and the (absent) profile is passed through.
        credential_config = mock_credential_manager_cls.call_args.args[0]
        self.assertTrue(credential_config.use_environment_variables)
        self.assertEqual(credential_config.use_profile, self.mock_provider.aws_profile)

        mock_session.client.assert_called_with("ec2")
        mock_session.resource.assert_called_with("ec2")
        self.assertIs(manager.ec2_client, mock_ec2_client)
        self.assertIs(manager.ec2_resource, mock_ec2_resource)

        # Verify instance variables
        self.assertEqual(manager.provider, self.mock_provider)
        self.assertEqual(manager.vpc_id, None)
        self.assertEqual(manager.subnet_id, None)
        self.assertEqual(manager.security_group_id, None)
        self.assertEqual(manager.iam_fleet_role_arn, None)
        self.assertEqual(manager.fleet_requests, {})
        self.assertEqual(manager.instances, {})
        self.assertEqual(manager.blocks, {})

    @patch("boto3.Session")
    def test_generate_user_data(self, mock_session_cls):
        """Test generation of user data script."""
        # Configure mocks
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_session.client.return_value = MagicMock()
        mock_session.resource.return_value = MagicMock()

        # Instantiate SpotFleetManager
        manager = SpotFleetManager(self.mock_provider)

        # Generate user data
        user_data = manager._generate_user_data()

        # Verify user data contains expected content
        self.assertIn("#!/bin/bash", user_data)
        self.assertIn("Starting Parsl worker setup for test-workflow-id", user_data)
        self.assertIn("Worker init script", user_data)

    # The two tests that used to sit here covered ``_get_iam_fleet_role()``,
    # which is gone with the API it served: ``CreateFleet`` has no
    # ``IamFleetRole`` member, so an EC2 Fleet needs no service role at all
    # (#86). Nothing creates the role any more, so there is nothing to assert
    # about creating it. What *is* still worth asserting is that a fleet is
    # requested without one, and that a role named by a pre-#86 state document is
    # still cleaned up -- covered by
    # ``test_fleet_request_carries_no_iam_fleet_role`` and
    # ``test_legacy_iam_fleet_role_is_still_cleaned_up`` below.

    @patch("parsl_aws_provider.compute.spot_fleet.CredentialManager")
    def test_throttling_error_handling(self, mock_credential_manager_cls):
        """A throttled ``CreateFleet`` must surface as SpotFleetThrottlingError.

        Retargeted from ``request_spot_fleet`` to ``create_fleet`` (#86). The
        translation happens in ``_translate_fleet_error``, and reaching it
        requires the ``ClientError`` to arrive at ``_create_fleet`` unwrapped --
        ``create_ec2_fleet`` used to wrap it in ``ResourceCreationError``, which
        made the whole error branch, its audit log, and its launch-template
        cleanup unreachable.
        """
        mock_ec2_client = self._mock_ec2(mock_credential_manager_cls)
        mock_ec2_client.create_fleet.side_effect = ClientError(
            {
                "Error": {
                    "Code": "RequestLimitExceeded",
                    "Message": "Request limit exceeded",
                },
                "ResponseMetadata": {"RetryAfter": 30},
            },
            "CreateFleet",
        )

        manager = SpotFleetManager(self.mock_provider)

        with self.assertRaises(SpotFleetThrottlingError) as context:
            manager._create_fleet("block-123", self.NETWORK, 1)

        self.assertIn("AWS throttled the fleet request", str(context.exception))
        self.assertEqual(context.exception.operation, "create_fleet")
        self.assertEqual(context.exception.retry_after, 30)

        # The per-block launch template must not outlive the failed fleet it was
        # built for; nothing else would ever reclaim it.
        mock_ec2_client.delete_launch_template.assert_called_once()
        self.assertEqual(manager.launch_templates, {})

    @patch("parsl_aws_provider.compute.spot_fleet.CredentialManager")
    def test_no_duplicate_tag_keys_in_fleet_request(self, mock_credential_manager_cls):
        """No TagSpecification may repeat a tag key (#109).

        EC2 rejects the whole request outright -- verified against real AWS for
        both ``request_spot_fleet`` ("Duplicate tag key 'Name' specified") and
        ``run_instances``. The marker tag used to be emitted as ``TAG_NAME``,
        which *is* the string ``"Name"``, so the tag lists carried ``Name``
        twice. moto accepts duplicates and keeps the last value, so nothing
        caught it.
        """
        mock_ec2_client = self._mock_ec2(mock_credential_manager_cls)

        SpotFleetManager(self.mock_provider)._create_fleet("block-123", self.NETWORK, 1)

        # Instance tags travel in the launch *template* (#85) and the fleet's own
        # tags in CreateFleet; both forms must be duplicate-free.
        tag_specs = list(
            mock_ec2_client.create_fleet.call_args.kwargs.get("TagSpecifications", [])
        )
        for call in mock_ec2_client.create_launch_template.call_args_list:
            tag_specs.extend(call.kwargs.get("TagSpecifications", []))
            tag_specs.extend(
                call.kwargs["LaunchTemplateData"].get("TagSpecifications", [])
            )
        self.assertTrue(tag_specs)
        for spec in tag_specs:
            keys = [tag["Key"] for tag in spec["Tags"]]
            self.assertCountEqual(
                keys,
                set(keys),
                f"duplicate tag key in {spec['ResourceType']} spec: {keys}",
            )
            # And the descriptive Name must survive, not be overwritten by the
            # marker's "true" -- which is what a duplicate key did on the
            # services that tolerate them.
            name = next(tag["Value"] for tag in spec["Tags"] if tag["Key"] == "Name")
            self.assertTrue(name.startswith(TAG_PREFIX))

    @patch("parsl_aws_provider.compute.spot_fleet.CredentialManager")
    def test_fleet_request_carries_no_iam_fleet_role(self, mock_credential_manager_cls):
        """``CreateFleet`` has no ``IamFleetRole``, so none may be sent (#86).

        EC2 rejects an unknown member outright, so sending the parameter the
        legacy ``RequestSpotFleet`` required would fail every launch. No IAM call
        should be made at all on this path either -- the role the old API needed
        simply is not part of it.
        """
        mock_ec2_client = self._mock_ec2(mock_credential_manager_cls)
        mock_session = (
            mock_credential_manager_cls.return_value.create_boto3_session.return_value
        )

        manager = SpotFleetManager(self.mock_provider)
        mock_session.client.reset_mock()
        manager._create_fleet("block-123", self.NETWORK, 1)

        kwargs = mock_ec2_client.create_fleet.call_args.kwargs
        self.assertNotIn("IamFleetRole", kwargs)
        self.assertNotIn("SpotFleetRequestConfig", kwargs)
        self.assertEqual(manager.iam_fleet_role_arn, None)
        # And no IAM client was even asked for.
        self.assertNotIn(
            "iam", [call.args[0] for call in mock_session.client.call_args_list]
        )
        # The legacy API must not be called either.
        mock_ec2_client.request_spot_fleet.assert_not_called()

    @patch("parsl_aws_provider.compute.spot_fleet.CredentialManager")
    @patch("time.sleep", return_value=None)  # skip the propagation waits
    def test_legacy_iam_fleet_role_is_still_cleaned_up(
        self, mock_sleep, mock_credential_manager_cls
    ):
        """A role named by a pre-#86 state document must still be deleted.

        Nothing creates one now, but a workflow resumed across the upgrade
        carries the ARN in its state, and dropping the cleanup with the creation
        would leak the role permanently.
        """
        mock_ec2_client = self._mock_ec2(mock_credential_manager_cls)
        mock_session = (
            mock_credential_manager_cls.return_value.create_boto3_session.return_value
        )
        mock_iam_client = MagicMock()
        mock_session.client.side_effect = lambda service, **kwargs: {
            "ec2": mock_ec2_client,
            "iam": mock_iam_client,
        }[service]

        manager = SpotFleetManager(self.mock_provider)
        manager.iam_fleet_role_arn = (
            "arn:aws:iam::123456789012:role/parsl-ephemeral-spot-fleet-role-test-wor"
        )

        manager.cleanup_all_resources()

        role_name = "parsl-ephemeral-spot-fleet-role-test-wor"
        mock_iam_client.detach_role_policy.assert_called_with(
            RoleName=role_name,
            PolicyArn=(
                "arn:aws:iam::aws:policy/service-role/AmazonEC2SpotFleetTaggingRole"
            ),
        )
        mock_iam_client.delete_role.assert_called_with(RoleName=role_name)

    def _mock_ec2(self, mock_credential_manager_cls):
        """Wire a mock EC2 client through the credential manager, and return it.

        ``create_launch_template`` is answered because a template is mandatory on
        this path: ``CreateFleet`` has no ``LaunchSpecifications`` member, so
        unlike the legacy API there is no inline-launch fallback (#86).
        """
        mock_ec2_client = MagicMock()
        mock_ec2_client.create_launch_template.return_value = {
            "LaunchTemplate": {
                "LaunchTemplateId": "lt-123",
                "LatestVersionNumber": 1,
            }
        }
        mock_ec2_client.create_fleet.return_value = {
            "FleetId": "fleet-123",
            "Instances": [{"InstanceIds": ["i-123"]}],
        }
        mock_ec2_client.describe_spot_price_history.return_value = {
            "SpotPriceHistory": [{"SpotPrice": "0.01"}]
        }
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ec2_client
        mock_session.resource.return_value = MagicMock()
        mock_credential_manager_cls.return_value.create_boto3_session.return_value = (
            mock_session
        )
        return mock_ec2_client

    def _fleet_spot_options(self, mock_credential_manager_cls, provider):
        """Drive ``_create_fleet`` and return the ``SpotOptions`` it sent."""
        mock_ec2_client = self._mock_ec2(mock_credential_manager_cls)
        SpotFleetManager(provider)._create_fleet("block-123", self.NETWORK, 1)
        return mock_ec2_client.create_fleet.call_args.kwargs["SpotOptions"]

    @patch("parsl_aws_provider.compute.spot_fleet.CredentialManager")
    def test_configured_allocation_strategy_reaches_the_api(
        self, mock_credential_manager_cls
    ):
        """The provider's ``spot_allocation_strategy`` must actually be sent (#84).

        It was hardcoded to ``lowestPrice`` here, so the configured value was
        never read at all -- which is why this asserts on the request boto3
        received rather than on the helper that converts the spelling.
        """
        self.mock_provider.spot_allocation_strategy = "capacity-optimized"

        spot_options = self._fleet_spot_options(
            mock_credential_manager_cls, self.mock_provider
        )

        # Kebab-case: the only spelling CreateFleet accepts. It rejects the
        # camelCase form RequestSpotFleet demands, and vice versa -- verified
        # against real EC2, and neither DryRun nor TotalTargetCapacity=0 catches
        # the wrong one.
        self.assertEqual(spot_options["AllocationStrategy"], "capacity-optimized")

    @patch("parsl_aws_provider.compute.spot_fleet.CredentialManager")
    def test_allocation_strategy_defaults_when_provider_omits_it(
        self, mock_credential_manager_cls
    ):
        """StandardMode's provider stand-in may not carry the attribute at all.

        ``_SimpleProvider`` raises ``AttributeError`` for anything it was not
        given, so this exercises the ``getattr(..., default)`` fall-through the
        way production does.
        """
        self.assertFalse(hasattr(self.mock_provider, "spot_allocation_strategy"))

        spot_options = self._fleet_spot_options(
            mock_credential_manager_cls, self.mock_provider
        )

        self.assertEqual(
            spot_options["AllocationStrategy"],
            EC2_FLEET_DEFAULT_ALLOCATION_STRATEGY,
        )
        self.assertNotEqual(spot_options["AllocationStrategy"], "lowest-price")


if __name__ == "__main__":
    unittest.main()

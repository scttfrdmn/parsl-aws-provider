"""Unit tests for Spot Fleet cleanup functionality.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import unittest
from unittest.mock import MagicMock, patch
import boto3
from botocore.exceptions import ClientError

from parsl_ephemeral_aws.compute.spot_fleet_cleanup import (
    cleanup_spot_fleet_role,
    cleanup_all_spot_fleet_resources,
)


class TestSpotFleetCleanup(unittest.TestCase):
    """Test suite for Spot Fleet cleanup functionality."""

    def setUp(self):
        """Set up test environment."""
        self.mock_session = MagicMock(spec=boto3.Session)
        self.mock_iam = MagicMock()
        self.mock_ec2 = MagicMock()

        # Configure session to return mock clients
        self.mock_session.client.side_effect = lambda service_name: {
            "iam": self.mock_iam,
            "ec2": self.mock_ec2,
        }[service_name]

        # Configure mock IAM client
        self.mock_iam.get_role.return_value = {
            "Role": {
                "RoleName": "test-role",
                "Arn": "arn:aws:iam::123456789012:role/test-role",
            }
        }

        self.mock_iam.list_attached_role_policies.return_value = {
            "AttachedPolicies": [
                {
                    "PolicyName": "SpotFleetTaggingPolicy",
                    "PolicyArn": "arn:aws:iam::aws:policy/service-role/AmazonEC2SpotFleetTaggingRole",
                }
            ]
        }

    def test_cleanup_spot_fleet_role_success(self):
        """Test successful cleanup of Spot Fleet IAM role."""
        # Call function
        result = cleanup_spot_fleet_role(
            session=self.mock_session,
            role_name="test-role",
            wait_for_detachment=False,  # Don't wait in tests
        )

        # Verify result
        self.assertTrue(result)

        # Verify client calls
        self.mock_iam.get_role.assert_called_once_with(RoleName="test-role")
        self.mock_iam.list_attached_role_policies.assert_called_once_with(
            RoleName="test-role"
        )
        self.mock_iam.detach_role_policy.assert_called_once_with(
            RoleName="test-role",
            PolicyArn="arn:aws:iam::aws:policy/service-role/AmazonEC2SpotFleetTaggingRole",
        )
        self.mock_iam.delete_role.assert_called_once_with(RoleName="test-role")

    def test_cleanup_spot_fleet_role_not_found(self):
        """Test cleanup when role doesn't exist."""
        # Configure mock to raise NoSuchEntity error
        self.mock_iam.get_role.side_effect = ClientError(
            {"Error": {"Code": "NoSuchEntity", "Message": "Role not found"}}, "GetRole"
        )

        # Call function
        result = cleanup_spot_fleet_role(
            session=self.mock_session, role_name="test-role"
        )

        # Verify result
        self.assertTrue(result)

        # Verify client calls
        self.mock_iam.get_role.assert_called_once_with(RoleName="test-role")
        self.mock_iam.list_attached_role_policies.assert_not_called()
        self.mock_iam.detach_role_policy.assert_not_called()
        self.mock_iam.delete_role.assert_not_called()

    def test_cleanup_spot_fleet_role_delete_conflict(self):
        """Test cleanup when role is still in use."""
        # Configure mock to succeed until delete_role
        self.mock_iam.delete_role.side_effect = ClientError(
            {"Error": {"Code": "DeleteConflict", "Message": "Role is in use"}},
            "DeleteRole",
        )

        # Call function with fewer retries for testing
        result = cleanup_spot_fleet_role(
            session=self.mock_session,
            role_name="test-role",
            wait_for_detachment=False,
            max_attempts=2,
            delay_seconds=0,  # Don't wait in tests
        )

        # Verify result
        self.assertFalse(result)

        # Verify client calls
        self.assertEqual(self.mock_iam.get_role.call_count, 2)
        self.assertEqual(self.mock_iam.list_attached_role_policies.call_count, 2)
        self.assertEqual(self.mock_iam.detach_role_policy.call_count, 2)
        self.assertEqual(self.mock_iam.delete_role.call_count, 2)

    def test_cleanup_all_spot_fleet_resources(self):
        """Test cleanup of all Spot Fleet resources."""
        # Configure mock EC2 client for paginator
        mock_paginator = MagicMock()
        self.mock_ec2.get_paginator.return_value = mock_paginator

        # Configure paginator to return Spot Fleet requests
        mock_paginator.paginate.return_value = [
            {
                "SpotFleetRequestConfigs": [
                    {
                        "SpotFleetRequestId": "sfr-12345",
                        "SpotFleetRequestState": "active",
                    },
                    {
                        "SpotFleetRequestId": "sfr-67890",
                        "SpotFleetRequestState": "active",
                    },
                ]
            }
        ]

        # Configure describe_tags to identify requests from our workflow
        self.mock_ec2.describe_tags.side_effect = [
            {"Tags": [{"Key": "WorkflowId", "Value": "test-workflow"}]},
            {"Tags": [{"Key": "OtherWorkflow", "Value": "other-workflow"}]},
        ]

        # Configure cancel_spot_fleet_requests response
        self.mock_ec2.cancel_spot_fleet_requests.return_value = {
            "SuccessfulFleetRequests": [
                {
                    "SpotFleetRequestId": "sfr-12345",
                    "CurrentSpotFleetRequestState": "cancelled_terminating",
                    "PreviousSpotFleetRequestState": "active",
                }
            ],
            "UnsuccessfulFleetRequests": [],
        }

        # Configure IAM paginator for roles
        mock_iam_paginator = MagicMock()
        self.mock_iam.get_paginator.return_value = mock_iam_paginator

        # Configure IAM paginator to return roles
        mock_iam_paginator.paginate.return_value = [
            {
                "Roles": [
                    {
                        "RoleName": "parsl-aws-spot-fleet-role-test-work",
                        "Arn": "arn:aws:iam::123456789012:role/parsl-aws-spot-fleet-role-test-work",
                    },
                    {
                        "RoleName": "other-role",
                        "Arn": "arn:aws:iam::123456789012:role/other-role",
                    },
                ]
            }
        ]

        # Call the function with patch for cleanup_spot_fleet_role
        with patch(
            "parsl_ephemeral_aws.compute.spot_fleet_cleanup.cleanup_spot_fleet_role"
        ) as mock_cleanup_role:
            mock_cleanup_role.return_value = True

            result = cleanup_all_spot_fleet_resources(
                session=self.mock_session,
                workflow_id="test-workflow",
                cancel_active_requests=True,
                cleanup_iam_roles=True,
            )

        # Verify result
        self.assertEqual(len(result["cancelled_requests"]), 1)
        self.assertEqual(result["cancelled_requests"][0], "sfr-12345")
        self.assertEqual(len(result["cleaned_roles"]), 1)
        self.assertEqual(
            result["cleaned_roles"][0], "parsl-aws-spot-fleet-role-test-work"
        )
        self.assertEqual(len(result["errors"]), 0)

        # Verify client calls
        self.mock_ec2.get_paginator.assert_called_once_with(
            "describe_spot_fleet_requests"
        )
        self.mock_ec2.describe_tags.assert_called_with(
            Filters=[{"Name": "resource-id", "Values": ["sfr-67890"]}]
        )
        self.mock_ec2.cancel_spot_fleet_requests.assert_called_once_with(
            SpotFleetRequestIds=["sfr-12345"], TerminateInstances=True
        )
        self.mock_iam.get_paginator.assert_called_once_with("list_roles")

        # Verify cleanup_spot_fleet_role was called
        mock_cleanup_role.assert_called_once_with(
            self.mock_session, "parsl-aws-spot-fleet-role-test-work"
        )


if __name__ == "__main__":
    unittest.main()

"""Unit tests for Spot Fleet cleanup functionality.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import unittest
from unittest.mock import MagicMock, call, patch
import boto3
import pytest
from botocore.exceptions import ClientError

from parsl_ephemeral_provider.compute.spot_fleet_cleanup import (
    cleanup_spot_fleet_role,
    cleanup_all_spot_fleet_resources,
)

pytestmark = pytest.mark.unit


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
        """Test cleanup of all fleet resources, EC2 Fleet and legacy alike.

        The sweep now covers two generations of API (#86), so the paginator has
        to be dispatched by operation rather than answering every call with the
        same pages. It makes three calls: ``describe_instances`` once per
        workflow-tag key -- ``describe_instances`` ANDs its filters, so
        ``WorkflowId`` and ``ParslWorkflowId`` cannot be asked for together --
        and then ``describe_spot_fleet_requests`` for anything predating #86.

        An EC2 Fleet is found through its *instances*, never through
        ``describe_fleets``: an ``instant`` fleet does not appear in a
        fleet-level listing at all unless its ID is already known, and a tag
        filter does not find it either (both verified against real EC2). The
        ``aws:ec2:fleet-id`` tag EC2 stamps on every fleet-launched instance is
        the only route, which is why the mock returns reservations carrying it.
        """
        # Configure mock EC2 paginators, dispatched by operation.
        instances_paginator = MagicMock()
        instances_paginator.paginate.return_value = [
            {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-11111111",
                                "Tags": [
                                    {"Key": "WorkflowId", "Value": "test-workflow"},
                                    {"Key": "aws:ec2:fleet-id", "Value": "fleet-abc"},
                                ],
                            },
                            # Two instances from one fleet must yield one
                            # deletion, not two.
                            {
                                "InstanceId": "i-22222222",
                                "Tags": [
                                    {"Key": "WorkflowId", "Value": "test-workflow"},
                                    {"Key": "aws:ec2:fleet-id", "Value": "fleet-abc"},
                                ],
                            },
                        ]
                    }
                ]
            }
        ]

        legacy_paginator = MagicMock()
        legacy_paginator.paginate.return_value = [
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

        self.mock_ec2.get_paginator.side_effect = lambda operation: {
            "describe_instances": instances_paginator,
            "describe_spot_fleet_requests": legacy_paginator,
        }[operation]

        self.mock_ec2.delete_fleets.return_value = {
            "SuccessfulFleetDeletions": [
                {
                    "FleetId": "fleet-abc",
                    "CurrentFleetState": "deleted_terminating",
                    "PreviousFleetState": "active",
                }
            ],
            "UnsuccessfulFleetDeletions": [],
        }

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
            "parsl_ephemeral_provider.compute.spot_fleet_cleanup.cleanup_spot_fleet_role"
        ) as mock_cleanup_role:
            mock_cleanup_role.return_value = True

            result = cleanup_all_spot_fleet_resources(
                session=self.mock_session,
                workflow_id="test-workflow",
                cancel_active_requests=True,
                cleanup_iam_roles=True,
            )

        # Verify result. The fleet is reported once even though two of its
        # instances carried its tag, and both tag-key passes saw both.
        self.assertEqual(result["deleted_fleets"], ["fleet-abc"])
        self.assertEqual(len(result["cancelled_requests"]), 1)
        self.assertEqual(result["cancelled_requests"][0], "sfr-12345")
        self.assertEqual(len(result["cleaned_roles"]), 1)
        self.assertEqual(
            result["cleaned_roles"][0], "parsl-aws-spot-fleet-role-test-work"
        )
        self.assertEqual(len(result["errors"]), 0)

        # Verify client calls. Both workflow tag keys must be swept -- resources
        # created before the key was renamed carry the other one, and missing a
        # pass leaks every fleet tagged with it.
        self.assertEqual(
            self.mock_ec2.get_paginator.call_args_list,
            [
                call("describe_instances"),
                call("describe_instances"),
                call("describe_spot_fleet_requests"),
            ],
        )
        self.assertEqual(
            [
                c.kwargs["Filters"][0]
                for c in instances_paginator.paginate.call_args_list
            ],
            [
                {"Name": "tag:WorkflowId", "Values": ["test-workflow"]},
                {"Name": "tag:ParslWorkflowId", "Values": ["test-workflow"]},
            ],
        )
        # Every pass also requires the fleet-id tag to be present, so a
        # non-fleet instance of the same workflow is not mistaken for one.
        for paginate_call in instances_paginator.paginate.call_args_list:
            self.assertEqual(
                paginate_call.kwargs["Filters"][1],
                {"Name": "tag-key", "Values": ["aws:ec2:fleet-id"]},
            )

        # Deleting an instant fleet always terminates its instances; AWS rejects
        # NoTerminateInstances for the type, so the flag is not optional.
        self.mock_ec2.delete_fleets.assert_called_once_with(
            FleetIds=["fleet-abc"], TerminateInstances=True
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

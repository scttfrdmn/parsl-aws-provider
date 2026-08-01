"""Unit tests for the SpotFleet functionality in ServerlessMode.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import base64
import pytest
from unittest.mock import MagicMock, patch
import boto3
import time

from parsl_aws_provider.exceptions import OperatingModeError
from parsl_aws_provider.modes.serverless import ServerlessMode
from parsl_aws_provider.constants import (
    RESOURCE_TYPE_SPOT_FLEET,
    WORKER_TYPE_LAMBDA,
    WORKER_TYPE_ECS,
    STATUS_COMPLETED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_FAILED,
    STATUS_CANCELLED,
)

pytestmark = pytest.mark.unit


class TestServerlessModeSpotFleet:
    """Tests for the SpotFleet functionality in ServerlessMode class."""

    @pytest.fixture
    def mock_session(self):
        """Create a mock boto3 session."""
        session = MagicMock(spec=boto3.Session)
        session.region_name = "us-east-1"
        return session

    @pytest.fixture
    def mock_state_store(self):
        """Create a mock state store."""
        store = MagicMock()
        store.load_state.return_value = None  # Default to no state
        return store

    @pytest.fixture
    def mock_cf_client(self):
        """Create a mock CloudFormation client."""
        client = MagicMock()

        # Mock create_stack
        client.create_stack.return_value = {"StackId": "stack-12345"}

        # Mock describe_stacks
        client.describe_stacks.return_value = {
            "Stacks": [
                {
                    "StackId": "stack-12345",
                    "StackName": "parsl-ecs-12345",
                    "StackStatus": "CREATE_COMPLETE",
                    "Outputs": [
                        {"OutputKey": "SpotFleetRequestId", "OutputValue": "sfr-12345"}
                    ],
                }
            ]
        }

        return client

    @pytest.fixture
    def mock_ec2_client(self):
        """Create a mock EC2 client, answering the EC2 Fleet API (#86).

        The legacy ``describe_spot_fleet_requests`` /
        ``cancel_spot_fleet_requests`` answers this fixture used to carry are
        gone with the API: the mode now calls ``CreateFleet``, ``DescribeFleets``
        and ``DeleteFleets``. They are left unstubbed deliberately, so a
        reintroduced call shows up as a bare MagicMock rather than quietly
        passing.

        ``create_launch_template`` has to be answered because the template is
        mandatory -- ``CreateFleet`` has no ``LaunchSpecifications`` member, and
        ``Overrides`` cannot carry ``UserData``, so the per-job command has
        nowhere else to live.
        """
        client = MagicMock()

        # Mock describe_vpcs / describe_subnets / describe_security_groups, which
        # _verify_resources() reads. Nothing creates any of the three since #69.
        client.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-12345"}]}
        client.describe_subnets.return_value = {
            "Subnets": [{"SubnetId": "subnet-12345"}]
        }
        client.describe_security_groups.return_value = {
            "SecurityGroups": [{"GroupId": "sg-12345"}]
        }

        client.create_launch_template.return_value = {
            "LaunchTemplate": {
                "LaunchTemplateId": "lt-12345",
                "LatestVersionNumber": 1,
            }
        }

        # An instant fleet returns its instance IDs synchronously, which is why
        # this fleet type was chosen: the block knows its instances without
        # polling, and the fleet ID no longer has to be recovered from stack
        # outputs.
        client.create_fleet.return_value = {
            "FleetId": "fleet-12345",
            "Instances": [{"InstanceIds": ["i-11111111", "i-22222222"]}],
        }

        client.describe_fleets.return_value = {
            "Fleets": [{"FleetId": "fleet-12345", "FleetState": "active"}]
        }

        client.delete_fleets.return_value = {
            "SuccessfulFleetDeletions": [
                {
                    "FleetId": "fleet-12345",
                    "CurrentFleetState": "deleted_terminating",
                    "PreviousFleetState": "active",
                }
            ],
            "UnsuccessfulFleetDeletions": [],
        }

        # An instant fleet's live instances are found only by filtering
        # describe_instances on the aws:ec2:fleet-id tag: describe_fleet_instances
        # refuses the fleet type outright, and describe_fleets' own Instances list
        # reflects the original launch and never drops terminated instances.
        instances_paginator = MagicMock()
        instances_paginator.paginate.return_value = [
            {
                "Reservations": [
                    {
                        "Instances": [
                            {"InstanceId": "i-11111111"},
                            {"InstanceId": "i-22222222"},
                        ]
                    }
                ]
            }
        ]
        client.get_paginator.return_value = instances_paginator

        client.describe_spot_price_history.return_value = {
            "SpotPriceHistory": [{"SpotPrice": "0.01"}]
        }

        return client

    @pytest.fixture
    def mock_ssm_client(self):
        """Answer the public AL2023 AMI alias the fleet resolves its image from.

        ``image_id`` is unset on this mode, so ``_resolve_fleet_image_id()`` goes
        to SSM (#83). Answering it here keeps the resolved AMI a real string that
        the launch-template assertions can check, instead of a MagicMock repr.
        """
        client = MagicMock()
        client.get_parameter.return_value = {
            "Parameter": {"Value": "ami-0abcdef1234567890"}
        }
        return client

    @pytest.fixture
    def serverless_mode_with_spot_fleet(
        self,
        mock_session,
        mock_state_store,
        mock_ec2_client,
        mock_cf_client,
        mock_ssm_client,
    ):
        """Create a ServerlessMode instance with SpotFleet enabled."""

        # Configure session to return mock clients
        def get_client(service_name, **kwargs):
            if service_name == "ec2":
                return mock_ec2_client
            elif service_name == "cloudformation":
                return mock_cf_client
            elif service_name == "ssm":
                return mock_ssm_client
            return MagicMock()

        mock_session.client.side_effect = get_client

        # Create mode instance with SpotFleet enabled
        mode = ServerlessMode(
            provider_id="test-provider",
            session=mock_session,
            state_store=mock_state_store,
            worker_type=WORKER_TYPE_ECS,
            use_spot_fleet=True,
            instance_types=["t3.small", "t3.medium", "m5.small"],
            nodes_per_block=2,
            spot_max_price_percentage=80,
            vpc_id="vpc-12345",
            subnet_id="subnet-12345",
            security_group_id="sg-12345",
        )

        return mode

    def test_init_with_spot_fleet(self, serverless_mode_with_spot_fleet):
        """Test initialization of ServerlessMode with SpotFleet options."""
        # Verify SpotFleet specific attributes
        assert serverless_mode_with_spot_fleet.use_spot_fleet is True
        assert len(serverless_mode_with_spot_fleet.instance_types) == 3
        assert "t3.small" in serverless_mode_with_spot_fleet.instance_types
        assert "t3.medium" in serverless_mode_with_spot_fleet.instance_types
        assert "m5.small" in serverless_mode_with_spot_fleet.instance_types
        assert serverless_mode_with_spot_fleet.nodes_per_block == 2
        assert serverless_mode_with_spot_fleet.spot_max_price_percentage == 80

    def test_default_instance_types(self, mock_session, mock_state_store):
        """Test that default instance types are provided if not specified."""
        # Create mode with SpotFleet but no instance types specified. ECS needs
        # the network IDs (#69) — its awsvpcConfiguration is mandatory.
        mode = ServerlessMode(
            provider_id="test-provider",
            session=mock_session,
            state_store=mock_state_store,
            worker_type=WORKER_TYPE_ECS,
            use_spot_fleet=True,
            nodes_per_block=2,
            vpc_id="vpc-12345",
            subnet_id="subnet-12345",
            security_group_id="sg-12345",
        )

        # Verify default instance types are set
        assert mode.use_spot_fleet is True
        assert len(mode.instance_types) > 0
        assert "t3.small" in mode.instance_types
        assert "m5.large" in mode.instance_types

    def test_submit_job_with_spot_fleet(
        self, serverless_mode_with_spot_fleet, mock_cf_client, mock_ec2_client
    ):
        """A fleet-backed submit calls CreateFleet directly, with no stack (#86).

        This used to assert on ``create_stack`` parameters
        (``UseSpotFleet``/``UseSpot``/``NodesPerBlock``/``SpotMaxPricePercentage``/
        ``InstanceTypes``), all of which fed an ``AWS::EC2::SpotFleet`` resource in
        ``ecs_worker.yml``. CloudFormation cannot build this fleet at all: the
        ``Overrides`` list is variable-length, ``Fn::ForEach`` expands to a map
        rather than a list, an out-of-range ``!Select`` fails validation even
        inside an untaken ``!If`` branch, and padding the slots by repeating a
        type is rejected by EC2 as ``InvalidFleetConfig: duplicate instance
        pools`` -- which a ``DryRun`` does *not* catch. Deploying the stack also
        created an ECS cluster, task definition, two IAM roles and a log group
        that an EC2 fleet never touches.

        So the assertions move to the API the mode now calls, and the fact that no
        stack is created is itself asserted: ``get_job_status()`` and
        ``cleanup_resources()`` both branch on the *absence* of ``stack_name``.
        """
        # Setup for job submission
        serverless_mode_with_spot_fleet.initialized = True
        serverless_mode_with_spot_fleet.ecs_manager = MagicMock()

        resource_id = serverless_mode_with_spot_fleet.submit_job(
            "job-1", "echo hello", 2
        )

        # No stack, and no ECS machinery -- the fleet is created directly.
        mock_cf_client.create_stack.assert_not_called()

        # The launch template is mandatory, and the per-job command travels in it
        # because a fleet override cannot carry UserData.
        mock_ec2_client.create_launch_template.assert_called_once()
        template_kwargs = mock_ec2_client.create_launch_template.call_args.kwargs
        assert template_kwargs["LaunchTemplateName"].endswith("-ecs-job-1")
        template_data = template_kwargs["LaunchTemplateData"]
        assert template_data["ImageId"] == "ami-0abcdef1234567890"
        # An instance that shuts itself down after its one command must terminate,
        # not stop and leave a billed EBS volume behind.
        assert template_data["InstanceInitiatedShutdownBehavior"] == "terminate"
        assert template_data["MetadataOptions"]["HttpTokens"] == "required"
        user_data = base64.b64decode(template_data["UserData"]).decode()
        assert "echo hello" in user_data
        assert "shutdown -h now" in user_data

        # The fleet itself: one override per instance type, all spot, and the
        # kebab-case allocation strategy CreateFleet requires -- it rejects the
        # camelCase spelling RequestSpotFleet demanded.
        mock_ec2_client.create_fleet.assert_called_once()
        fleet_kwargs = mock_ec2_client.create_fleet.call_args.kwargs
        assert fleet_kwargs["Type"] == "instant"
        assert fleet_kwargs["TargetCapacitySpecification"] == {
            "TotalTargetCapacity": 2,
            "DefaultTargetCapacityType": "spot",
        }
        assert fleet_kwargs["SpotOptions"]["AllocationStrategy"] == (
            "price-capacity-optimized"
        )
        overrides = fleet_kwargs["LaunchTemplateConfigs"][0]["Overrides"]
        assert [o["InstanceType"] for o in overrides] == [
            "t3.small",
            "t3.medium",
            "m5.small",
        ]
        # The template is referenced by pinned version, never $Latest: the fleet
        # must launch the definition just built, not one a concurrent provider
        # added afterwards.
        spec = fleet_kwargs["LaunchTemplateConfigs"][0]["LaunchTemplateSpecification"]
        assert spec == {"LaunchTemplateId": "lt-12345", "Version": "1"}
        # spot_max_price_percentage=80 of the 3x-spot on-demand proxy (0.01),
        # times the two nodes MaxTotalPrice covers.
        assert fleet_kwargs["SpotOptions"]["MaxTotalPrice"] == str(0.03 * 0.8 * 2)

        # Verify resource tracking
        assert resource_id in serverless_mode_with_spot_fleet.resources
        resource = serverless_mode_with_spot_fleet.resources[resource_id]
        assert resource["job_id"] == "job-1"
        assert resource["worker_type"] == WORKER_TYPE_ECS
        assert resource["status"] == STATUS_PENDING
        assert resource["use_spot_fleet"] is True
        assert resource["resource_type"] == RESOURCE_TYPE_SPOT_FLEET
        assert resource["fleet_request_id"] == "fleet-12345"
        # Both IDs are needed to reclaim the job: the fleet holds the instances,
        # and the template is a per-job resource only this record names.
        assert resource["launch_template_id"] == "lt-12345"
        assert resource["instance_ids"] == ["i-11111111", "i-22222222"]
        assert "stack_name" not in resource

        # Verify state was saved
        serverless_mode_with_spot_fleet.state_store.save_state.assert_called()

    def test_submit_job_fails_when_the_fleet_is_unfilled(
        self, serverless_mode_with_spot_fleet, mock_ec2_client
    ):
        """An instant fleet that filled nothing is a failed submit, not a pending one.

        ``CreateFleet`` reports a pool it could not fill *inline*, in ``Errors``,
        rather than failing the call -- so an empty fleet is a 200 response. Left
        unchecked the block would sit PENDING forever waiting for instances that
        an instant fleet will never make another attempt to launch.
        """
        serverless_mode_with_spot_fleet.initialized = True
        serverless_mode_with_spot_fleet.ecs_manager = MagicMock()
        mock_ec2_client.create_fleet.return_value = {
            "FleetId": "fleet-12345",
            "Instances": [],
            "Errors": [
                {
                    "ErrorCode": "InsufficientInstanceCapacity",
                    "ErrorMessage": "There is no Spot capacity available.",
                }
            ],
        }

        with pytest.raises(OperatingModeError, match="Failed to submit job job-1"):
            serverless_mode_with_spot_fleet.submit_job("job-1", "echo hello", 2)

        # The empty fleet is deleted rather than left as a resource that holds
        # nothing and will never grow, and the per-job template goes with it.
        #
        # The fleet is deleted twice and that is deliberate rather than
        # coincidental: ``_create_job_fleet`` drops it inline, and the record it
        # already annotated then routes the failed submit through
        # ``cleanup_resources() -> _reclaim_fleet()``, which drops it again along
        # with the template. Asserting a count here would assert the coincidence.
        # Deleting twice is safe -- verified against real EC2, where the second
        # DeleteFleets also reports ``deleted_terminating`` -- and belt-and-braces
        # is the right trade for instances that would otherwise bill untracked.
        mock_ec2_client.delete_fleets.assert_called_with(
            FleetIds=["fleet-12345"], TerminateInstances=True
        )
        # The template goes only through the submit-failure path: the unfilled
        # raise sits *after* the try block whose handler deletes it inline.
        mock_ec2_client.delete_launch_template.assert_called_once_with(
            LaunchTemplateId="lt-12345"
        )
        # And the record is gone, so nothing later re-reports the dead job.
        assert serverless_mode_with_spot_fleet.resources == {}

    def test_submit_job_without_a_subnet_creates_no_template(
        self, serverless_mode_with_spot_fleet, mock_ec2_client
    ):
        """A missing subnet has to be refused before anything is created.

        Every fleet override names the subnet to launch into, so an unset one
        reaches ``CreateFleet`` as a null and is refused there -- by which point
        the launch template exists and has to be cleaned up on the way out. The
        ECS guard in ``submit_job`` covers the Fargate path, but the fleet path
        returns before reaching it.
        """
        serverless_mode_with_spot_fleet.initialized = True
        serverless_mode_with_spot_fleet.ecs_manager = MagicMock()
        serverless_mode_with_spot_fleet.subnet_id = None

        with pytest.raises(OperatingModeError, match="Failed to submit job job-1"):
            serverless_mode_with_spot_fleet.submit_job("job-1", "echo hello", 2)

        mock_ec2_client.create_launch_template.assert_not_called()
        mock_ec2_client.create_fleet.assert_not_called()

    def test_get_spot_fleet_status(
        self, serverless_mode_with_spot_fleet, mock_ec2_client
    ):
        """Status now comes from ``FleetState`` plus the fleet's live instances (#86).

        The capacity comparison this replaced cannot decide an instant fleet's
        status at all: the fleet does not maintain capacity, so ``FleetState``
        stays ``active`` for its whole life and the counters keep reporting the
        original launch however many instances have since died. It was also the
        site of #114, where ``FulfilledCapacity`` was read from the wrong nesting
        level, came back 0 forever, and left every block PENDING.
        """
        mode = serverless_mode_with_spot_fleet

        def set_state(state):
            mock_ec2_client.describe_fleets.return_value = {
                "Fleets": [{"FleetId": "fleet-12345", "FleetState": state}]
            }

        def set_instances(instance_ids):
            mock_ec2_client.get_paginator.return_value.paginate.return_value = [
                {
                    "Reservations": [
                        {"Instances": [{"InstanceId": i} for i in instance_ids]}
                    ]
                }
            ]

        # Active with live instances -> RUNNING.
        assert mode._get_spot_fleet_status("fleet-12345") == STATUS_RUNNING
        mock_ec2_client.describe_fleets.assert_called_with(FleetIds=["fleet-12345"])
        # The ID is always passed: AWS returns instant fleets from DescribeFleets
        # only when asked for by ID, so an unfiltered call finds nothing.
        mock_ec2_client.describe_spot_fleet_requests.assert_not_called()

        # Active with nothing left running -> terminal, so Parsl frees the block.
        # An instant fleet makes no further launch attempts, so there is nothing
        # to wait for.
        set_instances([])
        assert mode._get_spot_fleet_status("fleet-12345") == STATUS_COMPLETED
        # ...and the instances are found through the fleet-id tag EC2 stamps on
        # them, the only route that works for this fleet type.
        paginate_kwargs = (
            mock_ec2_client.get_paginator.return_value.paginate.call_args.kwargs
        )
        assert {
            "Name": "tag:aws:ec2:fleet-id",
            "Values": ["fleet-12345"],
        } in paginate_kwargs["Filters"]

        set_instances(["i-11111111"])
        for state, expected in [
            ("submitted", STATUS_PENDING),
            ("modifying", STATUS_PENDING),
            # Deleting but the instances have not gone yet, so the workers are
            # still live.
            ("deleted_running", STATUS_RUNNING),
            ("deleted", STATUS_CANCELLED),
            ("deleted_terminating", STATUS_CANCELLED),
            ("failed", STATUS_FAILED),
        ]:
            set_state(state)
            assert mode._get_spot_fleet_status("fleet-12345") == expected, state

        # A fleet EC2 has forgotten is terminal, not unknown: its instances are
        # long gone, so reporting UNKNOWN would leave the block held forever.
        mock_ec2_client.describe_fleets.return_value = {"Fleets": []}
        assert mode._get_spot_fleet_status("fleet-12345") == STATUS_COMPLETED

    def test_get_job_status_for_spot_fleet(
        self, serverless_mode_with_spot_fleet, mock_cf_client, mock_ec2_client
    ):
        """A fleet-backed job's status is read from the fleet, never a stack (#86).

        The fleet ID no longer has to be recovered by polling stack outputs -- it
        comes back from ``CreateFleet`` itself, so the record carries it from the
        moment of submit. That closes a real window: registration for interruption
        handling used to wait up to three minutes for CREATE_COMPLETE, during which
        an interruption went unhandled.

        The stack branch has to stay unreached rather than merely unused: a
        fleet-backed record has no ``stack_name``, so falling through to it would
        report UNKNOWN and leave the instances running.
        """
        # Setup resources as _create_job_fleet leaves them: fleet and template
        # IDs, and no stack name.
        resource_id = "serverless-ecs-job-1"
        serverless_mode_with_spot_fleet.resources = {
            resource_id: {
                "job_id": "job-1",
                "worker_type": WORKER_TYPE_ECS,
                "status": STATUS_PENDING,
                "created_at": time.time() - 60,
                "use_spot_fleet": True,
                "resource_type": RESOURCE_TYPE_SPOT_FLEET,
                "fleet_request_id": "fleet-12345",
                "launch_template_id": "lt-12345",
            }
        }

        status = serverless_mode_with_spot_fleet.get_job_status([resource_id])

        mock_cf_client.describe_stacks.assert_not_called()
        mock_ec2_client.describe_fleets.assert_called_with(FleetIds=["fleet-12345"])

        # Verify status result and resource tracking updates
        assert status[resource_id] == STATUS_RUNNING
        assert (
            serverless_mode_with_spot_fleet.resources[resource_id]["status"]
            == STATUS_RUNNING
        )

    def test_cancel_spot_fleet_job(
        self, serverless_mode_with_spot_fleet, mock_cf_client, mock_ec2_client
    ):
        """Cancelling a fleet-backed job deletes the fleet and its template (#86).

        Deleting the fleet *is* the cancellation, and it always terminates the
        instances: AWS rejects ``NoTerminateInstances`` for an instant fleet, since
        "a deleted instant fleet with running instances is not supported". The
        template is deleted afterwards -- harmless to instances already launched
        from it, but leaving it behind strands a per-job resource that only the
        orphan sweep could find.
        """
        # Setup resources as _create_job_fleet leaves them.
        resource_id = "serverless-ecs-job-1"
        serverless_mode_with_spot_fleet.resources = {
            resource_id: {
                "job_id": "job-1",
                "worker_type": WORKER_TYPE_ECS,
                "status": STATUS_RUNNING,
                "use_spot_fleet": True,
                "fleet_request_id": "fleet-12345",
                "launch_template_id": "lt-12345",
                "resource_type": RESOURCE_TYPE_SPOT_FLEET,
            }
        }

        # Cancel job
        status = serverless_mode_with_spot_fleet.cancel_jobs([resource_id])

        mock_ec2_client.delete_fleets.assert_called_once_with(
            FleetIds=["fleet-12345"], TerminateInstances=True
        )
        mock_ec2_client.delete_launch_template.assert_called_once_with(
            LaunchTemplateId="lt-12345"
        )
        # There is no stack to delete, and attempting one would raise from
        # CloudFormation on a real account.
        mock_cf_client.delete_stack.assert_not_called()
        mock_ec2_client.cancel_spot_fleet_requests.assert_not_called()

        # Verify status result
        assert status[resource_id] == STATUS_CANCELLED
        assert (
            serverless_mode_with_spot_fleet.resources[resource_id]["status"]
            == STATUS_CANCELLED
        )

    @patch(
        "parsl_aws_provider.compute.spot_fleet_cleanup.cleanup_all_spot_fleet_resources"
    )
    def test_cleanup_infrastructure_with_spot_fleet(
        self, mock_cleanup_spot_fleet, serverless_mode_with_spot_fleet
    ):
        """Test infrastructure cleanup with SpotFleet resources."""
        # Setup resources
        serverless_mode_with_spot_fleet.vpc_id = "vpc-12345"
        serverless_mode_with_spot_fleet.subnet_id = "subnet-12345"
        serverless_mode_with_spot_fleet.security_group_id = "sg-12345"
        serverless_mode_with_spot_fleet.initialized = True

        # Mock successful cleanup
        mock_cleanup_spot_fleet.return_value = {
            "cancelled_requests": ["sfr-12345"],
            "cleaned_roles": ["parsl-aws-spot-fleet-role-test"],
            "errors": [],
        }

        # Cleanup infrastructure
        serverless_mode_with_spot_fleet.cleanup_infrastructure()

        # Verify SpotFleet cleanup was called
        mock_cleanup_spot_fleet.assert_called_once_with(
            session=serverless_mode_with_spot_fleet.session,
            workflow_id=serverless_mode_with_spot_fleet.provider_id,
            cancel_active_requests=True,
            cleanup_iam_roles=True,
        )

        # The caller's network survives cleanup and stays configured, so a
        # subsequent initialize() can reuse it (#69).
        assert serverless_mode_with_spot_fleet.vpc_id == "vpc-12345"
        assert serverless_mode_with_spot_fleet.subnet_id == "subnet-12345"
        assert serverless_mode_with_spot_fleet.security_group_id == "sg-12345"

        assert serverless_mode_with_spot_fleet.initialized is False

    def test_list_resources_with_spot_fleet(self, serverless_mode_with_spot_fleet):
        """Test listing resources including SpotFleet resources."""
        # Setup resources including SpotFleet
        serverless_mode_with_spot_fleet.vpc_id = "vpc-12345"
        serverless_mode_with_spot_fleet.subnet_id = "subnet-12345"
        serverless_mode_with_spot_fleet.security_group_id = "sg-12345"

        lambda_id = "serverless-lambda-job-1"
        spot_fleet_id = "serverless-spot-fleet-job-2"
        serverless_mode_with_spot_fleet.resources = {
            lambda_id: {
                "job_id": "job-1",
                "worker_type": WORKER_TYPE_LAMBDA,
                "stack_name": "parsl-lambda-job1",
                "status": STATUS_RUNNING,
                "created_at": time.time(),
            },
            spot_fleet_id: {
                "job_id": "job-2",
                "worker_type": WORKER_TYPE_ECS,
                "stack_name": "parsl-ecs-job2",
                "status": STATUS_RUNNING,
                "created_at": time.time(),
                "use_spot_fleet": True,
                "fleet_request_id": "sfr-12345",
                "resource_type": RESOURCE_TYPE_SPOT_FLEET,
            },
        }

        # List resources
        resources = serverless_mode_with_spot_fleet.list_resources()

        # Verify resource categories
        assert "lambda_functions" in resources
        assert "ecs_tasks" in resources  # Standard ECS resources
        assert "spot_fleet_requests" in resources  # SpotFleet resources
        assert "vpc" in resources
        assert "subnet" in resources
        assert "security_group" in resources

        # Verify counts
        assert len(resources["lambda_functions"]) == 1
        assert len(resources["ecs_tasks"]) == 0  # No standard ECS tasks, only SpotFleet
        assert len(resources["spot_fleet_requests"]) == 1

        # Verify Lambda resource
        assert resources["lambda_functions"][0]["id"] == lambda_id
        assert resources["lambda_functions"][0]["job_id"] == "job-1"
        assert resources["lambda_functions"][0]["status"] == STATUS_RUNNING

        # Verify SpotFleet resource
        assert resources["spot_fleet_requests"][0]["id"] == spot_fleet_id
        assert resources["spot_fleet_requests"][0]["job_id"] == "job-2"
        assert resources["spot_fleet_requests"][0]["status"] == STATUS_RUNNING
        assert resources["spot_fleet_requests"][0]["fleet_request_id"] == "sfr-12345"

    def test_load_state_with_spot_fleet(
        self, serverless_mode_with_spot_fleet, mock_state_store
    ):
        """Test loading state with SpotFleet information."""
        # Setup mock state with SpotFleet data
        mock_state = {
            "resources": {
                "spot-fleet-job-1": {
                    "job_id": "spot-job-1",
                    "status": STATUS_RUNNING,
                    "worker_type": WORKER_TYPE_ECS,
                    "resource_type": RESOURCE_TYPE_SPOT_FLEET,
                    "fleet_request_id": "sfr-12345",
                    "use_spot_fleet": True,
                }
            },
            "provider_id": "test-provider",
            "mode": "ServerlessMode",
            "vpc_id": "vpc-12345",
            "subnet_id": "subnet-12345",
            "security_group_id": "sg-12345",
            "initialized": True,
            "worker_type": WORKER_TYPE_ECS,
            "use_spot_fleet": True,
            "instance_types": ["t3.small", "t3.medium", "m5.small"],
            "nodes_per_block": 2,
            "spot_max_price_percentage": 80,
        }
        mock_state_store.load_state.return_value = mock_state

        # Load state
        result = serverless_mode_with_spot_fleet.load_state()

        # Verify state was loaded
        assert result is True
        assert serverless_mode_with_spot_fleet.resources == mock_state["resources"]
        assert (
            serverless_mode_with_spot_fleet.use_spot_fleet
            == mock_state["use_spot_fleet"]
        )
        assert (
            serverless_mode_with_spot_fleet.instance_types
            == mock_state["instance_types"]
        )
        assert (
            serverless_mode_with_spot_fleet.nodes_per_block
            == mock_state["nodes_per_block"]
        )
        assert (
            serverless_mode_with_spot_fleet.spot_max_price_percentage
            == mock_state["spot_max_price_percentage"]
        )

        # Verify SpotFleet resource data is loaded correctly
        spot_resource = serverless_mode_with_spot_fleet.resources.get(
            "spot-fleet-job-1"
        )
        assert spot_resource is not None
        assert spot_resource["resource_type"] == RESOURCE_TYPE_SPOT_FLEET
        assert spot_resource["fleet_request_id"] == "sfr-12345"
        assert spot_resource["use_spot_fleet"] is True

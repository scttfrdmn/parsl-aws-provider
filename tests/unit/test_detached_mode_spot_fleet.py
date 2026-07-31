"""Unit tests for the SpotFleet functionality in DetachedMode.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import pytest
from unittest.mock import MagicMock, patch
import boto3
import json

from parsl_ephemeral_aws.modes.detached import DetachedMode
from parsl_ephemeral_aws.constants import (
    RESOURCE_TYPE_SPOT_FLEET,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_CANCELED,
)

pytestmark = pytest.mark.unit


class TestDetachedModeSpotFleet:
    """Tests for the SpotFleet functionality in DetachedMode class."""

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
    def mock_ec2_client(self):
        """Create a mock EC2 client."""
        client = MagicMock()

        # Mock create_vpc
        client.create_vpc.return_value = {"Vpc": {"VpcId": "vpc-12345"}}

        # Mock create_subnet
        client.create_subnet.return_value = {"Subnet": {"SubnetId": "subnet-12345"}}

        # Mock create_security_group
        client.create_security_group.return_value = {"GroupId": "sg-12345"}

        # Mock run_instances (for bastion host)
        client.run_instances.return_value = {
            "Instances": [
                {
                    "InstanceId": "i-bastion",
                    "State": {"Name": "pending"},
                    "PrivateIpAddress": "10.0.0.1",
                    "PublicIpAddress": "54.123.456.789",
                }
            ]
        }

        # Mock describe_instances
        client.describe_instances.return_value = {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-bastion",
                            "State": {"Name": "running"},
                            "PrivateIpAddress": "10.0.0.1",
                            "PublicIpAddress": "54.123.456.789",
                        }
                    ]
                }
            ]
        }

        # Mock cancel_spot_fleet_requests
        client.cancel_spot_fleet_requests.return_value = {
            "SuccessfulFleetRequests": [
                {
                    "SpotFleetRequestId": "sfr-12345",
                    "CurrentSpotFleetRequestState": "cancelled_terminating",
                    "PreviousSpotFleetRequestState": "active",
                }
            ],
            "UnsuccessfulFleetRequests": [],
        }

        # Mock describe_spot_fleet_requests
        client.describe_spot_fleet_requests.return_value = {
            "SpotFleetRequestConfigs": [
                {
                    "SpotFleetRequestId": "sfr-12345",
                    "SpotFleetRequestState": "active",
                    "ActivityStatus": "fulfilled",
                }
            ]
        }

        # Mock describe_spot_fleet_instances
        client.describe_spot_fleet_instances.return_value = {
            "ActiveInstances": [
                {
                    "InstanceId": "i-spot1",
                    "InstanceType": "t3.micro",
                    "SpotInstanceRequestId": "sir-12345",
                },
                {
                    "InstanceId": "i-spot2",
                    "InstanceType": "t3.micro",
                    "SpotInstanceRequestId": "sir-67890",
                },
            ]
        }

        return client

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
                    "StackName": "parsl-bastion-12345",
                    "StackStatus": "CREATE_COMPLETE",
                    "Outputs": [
                        {"OutputKey": "BastionHostId", "OutputValue": "i-bastion"}
                    ],
                }
            ]
        }

        return client

    @pytest.fixture
    def mock_ssm_client(self):
        """Create a mock SSM client."""
        client = MagicMock()

        # Mock get_parameter for regular job
        client.get_parameter.return_value = {
            "Parameter": {
                "Name": "/parsl/workflows/test-workflow/status/job-1",
                "Value": json.dumps(
                    {"status": STATUS_RUNNING, "instance_id": "i-worker"}
                ),
            }
        }

        return client

    @pytest.fixture
    def mock_iam_client(self):
        """Create a mock IAM client."""
        client = MagicMock()

        # Mock get_role success
        client.get_role.return_value = {
            "Role": {
                "RoleName": "parsl-aws-spot-fleet-role-test-work",
                "Arn": "arn:aws:iam::123456789012:role/parsl-aws-spot-fleet-role-test-work",
            }
        }

        return client

    @pytest.fixture
    def detached_mode_with_spot_fleet(
        self,
        mock_session,
        mock_state_store,
        mock_ec2_client,
        mock_cf_client,
        mock_ssm_client,
        mock_iam_client,
    ):
        """Create a DetachedMode instance with SpotFleet enabled."""

        # Configure session to return mock clients
        def get_client(service_name, **kwargs):
            if service_name == "ec2":
                return mock_ec2_client
            elif service_name == "cloudformation":
                return mock_cf_client
            elif service_name == "ssm":
                return mock_ssm_client
            elif service_name == "iam":
                return mock_iam_client
            return MagicMock()

        mock_session.client.side_effect = get_client

        # Create mode instance with SpotFleet enabled
        mode = DetachedMode(
            provider_id="test-provider",
            session=mock_session,
            state_store=mock_state_store,
            workflow_id="test-workflow",
            bastion_instance_type="t3.micro",
            instance_type="t3.small",
            image_id="ami-12345678",
            region="us-east-1",
            use_spot_fleet=True,
            instance_types=["t3.small", "t3.medium", "m5.small"],
            nodes_per_block=2,
            spot_max_price_percentage=80,
            vpc_id="vpc-12345",
            subnet_id="subnet-12345",
            security_group_id="sg-12345",
        )

        return mode

    def test_init_with_spot_fleet(self, detached_mode_with_spot_fleet):
        """Test initialization of DetachedMode with SpotFleet options."""
        # Verify SpotFleet specific attributes
        assert detached_mode_with_spot_fleet.use_spot_fleet is True
        assert len(detached_mode_with_spot_fleet.instance_types) == 3
        assert "t3.small" in detached_mode_with_spot_fleet.instance_types
        assert "t3.medium" in detached_mode_with_spot_fleet.instance_types
        assert "m5.small" in detached_mode_with_spot_fleet.instance_types
        assert detached_mode_with_spot_fleet.nodes_per_block == 2
        assert detached_mode_with_spot_fleet.spot_max_price_percentage == 80

    @patch("parsl_ephemeral_aws.modes.detached.get_default_ami")
    @patch("parsl_ephemeral_aws.modes.detached.get_cf_template")
    def test_initialize_with_spot_fleet(
        self,
        mock_get_cf_template,
        mock_get_default_ami,
        detached_mode_with_spot_fleet,
        mock_ec2_client,
        mock_cf_client,
    ):
        """The bastion stack must carry no fleet parameters at all.

        This used to assert the opposite -- that ``UseSpotFleet``,
        ``NodesPerBlock``, ``SpotMaxPricePercentage`` and ``InstanceTypes`` were
        sent. ``bastion.yml`` declares none of them, and CloudFormation rejects an
        undeclared parameter outright: "ValidationError: Parameters:
        [UseSpotFleet] do not exist in the template". Since ``bastion_host_type``
        defaults to ``"cloudformation"``, that made the default bastion path fail
        on every initialize.

        They were dropped rather than added to the template because all four
        describe a *worker fleet* and the bastion is a single host; the fleet
        settings reach the workers through the manager script's environment. So
        the assertion is inverted, and the network parameters the stack does need
        are checked in their place -- an empty ``Parameters`` list would satisfy a
        purely negative test.
        """
        # Setup mocks
        mock_get_default_ami.return_value = "ami-default"
        mock_get_cf_template.return_value = "CloudFormation Template"
        detached_mode_with_spot_fleet.bastion_host_type = "cloudformation"

        # Call initialize
        detached_mode_with_spot_fleet.initialize()

        # Verify infrastructure was created
        assert detached_mode_with_spot_fleet.vpc_id == "vpc-12345"
        assert detached_mode_with_spot_fleet.subnet_id == "subnet-12345"
        assert detached_mode_with_spot_fleet.security_group_id == "sg-12345"
        assert detached_mode_with_spot_fleet.bastion_id == "stack-12345"
        assert detached_mode_with_spot_fleet.initialized is True

        assert mock_cf_client.create_stack.call_args_list
        for call_args in mock_cf_client.create_stack.call_args_list:
            cf_params = call_args[1].get("Parameters", [])
            param_dict = {p["ParameterKey"]: p["ParameterValue"] for p in cf_params}

            for fleet_param in (
                "UseSpotFleet",
                "NodesPerBlock",
                "SpotMaxPricePercentage",
                "InstanceTypes",
            ):
                assert fleet_param not in param_dict

            # What the bastion genuinely needs, so this is not vacuously true.
            assert param_dict["VpcId"] == "vpc-12345"
            assert param_dict["SubnetId"] == "subnet-12345"
            assert param_dict["SecurityGroupId"] == "sg-12345"
            assert param_dict["WorkflowId"] == "test-workflow"

    def test_submit_job_with_spot_fleet(
        self, detached_mode_with_spot_fleet, mock_ssm_client
    ):
        """Test job submission with SpotFleet options."""
        # Setup mode as initialized
        detached_mode_with_spot_fleet.initialized = True
        detached_mode_with_spot_fleet.vpc_id = "vpc-12345"
        detached_mode_with_spot_fleet.subnet_id = "subnet-12345"
        detached_mode_with_spot_fleet.security_group_id = "sg-12345"
        detached_mode_with_spot_fleet.bastion_id = "i-bastion"

        # Submit a job
        command = "echo hello"
        resource_id = detached_mode_with_spot_fleet.submit_job("job-1", command, 1)

        # Verify SSM parameters were created
        assert mock_ssm_client.put_parameter.call_count == 2  # Job command and status

        # Verify job data includes SpotFleet options
        for call_args in mock_ssm_client.put_parameter.call_args_list:
            # Check if this is the job data parameter
            if "/parsl/workflows/test-workflow/jobs/job-1" in call_args[1]["Name"]:
                job_data = json.loads(call_args[1]["Value"])

                # Verify SpotFleet options are included
                assert job_data["use_spot_fleet"] is True
                assert len(job_data["instance_types"]) == 3
                assert "t3.small" in job_data["instance_types"]
                assert "t3.medium" in job_data["instance_types"]
                assert "m5.small" in job_data["instance_types"]
                assert job_data["nodes_per_block"] == 2
                assert job_data["spot_max_price_percentage"] == 80

    def test_get_job_status_for_spot_fleet(
        self, detached_mode_with_spot_fleet, mock_ssm_client
    ):
        """Test getting job status for a SpotFleet job."""
        # Setup mock resources
        job_id = "spot-job-1"
        resource_id = f"spot-fleet-{job_id}"
        detached_mode_with_spot_fleet.resources = {
            resource_id: {"job_id": job_id, "status": STATUS_PENDING}
        }

        # Mock SSM get_parameter response for a SpotFleet job
        mock_ssm_client.get_parameter.return_value = {
            "Parameter": {
                "Value": json.dumps(
                    {
                        "status": STATUS_RUNNING,
                        "instance_id": "i-spot1",
                        "fleet_request_id": "sfr-12345",
                        "resource_type": RESOURCE_TYPE_SPOT_FLEET,
                        "all_instance_ids": ["i-spot1", "i-spot2"],
                    }
                )
            }
        }

        # Get status
        status = detached_mode_with_spot_fleet.get_job_status([resource_id])

        # Verify SSM call
        mock_ssm_client.get_parameter.assert_called_with(
            Name=f"/parsl/workflows/test-workflow/status/{job_id}"
        )

        # Verify status result
        assert status[resource_id] == STATUS_RUNNING

        # Verify resource was updated with SpotFleet information
        assert (
            detached_mode_with_spot_fleet.resources[resource_id]["status"]
            == STATUS_RUNNING
        )
        assert (
            detached_mode_with_spot_fleet.resources[resource_id]["fleet_request_id"]
            == "sfr-12345"
        )
        assert (
            detached_mode_with_spot_fleet.resources[resource_id]["resource_type"]
            == RESOURCE_TYPE_SPOT_FLEET
        )
        assert detached_mode_with_spot_fleet.resources[resource_id][
            "all_instance_ids"
        ] == ["i-spot1", "i-spot2"]

    def test_cancel_spot_fleet_job(
        self, detached_mode_with_spot_fleet, mock_ssm_client
    ):
        """Test canceling a SpotFleet job."""
        # Setup mock resource with SpotFleet details
        resource_id = "spot-fleet-job-1"
        detached_mode_with_spot_fleet.resources = {
            resource_id: {
                "job_id": "spot-job-1",
                "status": STATUS_RUNNING,
                "resource_type": RESOURCE_TYPE_SPOT_FLEET,
                "fleet_request_id": "sfr-12345",
                "all_instance_ids": ["i-spot1", "i-spot2"],
            }
        }

        # Cancel the job
        status = detached_mode_with_spot_fleet.cancel_jobs([resource_id])

        # Verify SSM put_parameter was called for the cancel request
        mock_ssm_client.put_parameter.assert_called_once()
        args, kwargs = mock_ssm_client.put_parameter.call_args
        assert kwargs["Name"] == "/parsl/workflows/test-workflow/cancel"

        # Verify the cancel request data contains the SpotFleet information
        cancel_data = json.loads(kwargs["Value"])
        assert "spot-job-1" in cancel_data["job_ids"]
        assert "spot_fleet_jobs" in cancel_data
        assert cancel_data["spot_fleet_jobs"]["spot-job-1"] == "sfr-12345"

        # Verify status results
        assert status[resource_id] == STATUS_CANCELED

        # Verify resource was updated
        assert (
            detached_mode_with_spot_fleet.resources[resource_id]["status"]
            == STATUS_CANCELED
        )

    def test_cleanup_spot_fleet_resources(
        self, detached_mode_with_spot_fleet, mock_ec2_client, mock_ssm_client
    ):
        """Cleanup must delete the fleet through the EC2 Fleet API (#86).

        The fleet a job holds is now created by ``CreateFleet``, so
        ``CancelSpotFleetRequests`` cannot reach it -- it would answer
        ``InvalidSpotFleetRequestId.NotFound`` and the instances would keep
        running and billing. ``DeleteFleets`` is the counterpart, and
        ``TerminateInstances=True`` is not optional: it is a required member, and
        omitting it leaves the instances behind.
        """
        # Setup mock resource with SpotFleet details
        resource_id = "spot-fleet-job-1"
        detached_mode_with_spot_fleet.resources = {
            resource_id: {
                "job_id": "spot-job-1",
                "status": STATUS_RUNNING,
                "resource_type": RESOURCE_TYPE_SPOT_FLEET,
                "fleet_request_id": "sfr-12345",
                "all_instance_ids": ["i-spot1", "i-spot2"],
            }
        }

        # Cleanup the resource
        detached_mode_with_spot_fleet.cleanup_resources([resource_id])

        mock_ec2_client.delete_fleets.assert_called_once_with(
            FleetIds=["sfr-12345"], TerminateInstances=True
        )
        mock_ec2_client.cancel_spot_fleet_requests.assert_not_called()

        # Verify SSM parameters were deleted
        mock_ssm_client.delete_parameter.assert_any_call(
            Name="/parsl/workflows/test-workflow/jobs/spot-job-1"
        )
        mock_ssm_client.delete_parameter.assert_any_call(
            Name="/parsl/workflows/test-workflow/status/spot-job-1"
        )

        # Verify resource was removed from tracking
        assert resource_id not in detached_mode_with_spot_fleet.resources

        # Verify state was saved
        detached_mode_with_spot_fleet.state_store.save_state.assert_called()

    def test_bastion_script_includes_spot_fleet_support(
        self, detached_mode_with_spot_fleet
    ):
        """The bastion script must carry the EC2 Fleet helpers, not the old ones.

        Two of the three functions this asserted on are gone with the legacy API
        (#86). ``get_spot_fleet_role()`` provisioned the ``IamFleetRole`` that
        ``RequestSpotFleet`` requires; ``CreateFleet`` has no such member at all,
        so the role -- and the IAM permissions to create it -- are no longer
        needed. ``wait_for_fleet_instances()`` polled
        ``describe_spot_fleet_instances``; an ``instant`` fleet returns its
        instance IDs in the ``CreateFleet`` response, so there is nothing to wait
        for, and ``get_fleet_instance_ids()`` looks them up by the reserved
        ``aws:ec2:fleet-id`` tag -- the only way to find an instant fleet's
        instances, since ``DescribeFleetInstances`` refuses them outright.

        Checked against the generated text rather than the module source because
        the bastion cannot import from this package: the script is assembled by
        string substitution and runs standalone on the instance, where a missing
        helper surfaces only as a ``NameError`` in that instance's log.
        """
        # Generate the bastion manager script
        manager_script = detached_mode_with_spot_fleet._get_bastion_manager_script()

        assert "def launch_spot_fleet(" in manager_script
        assert "def get_fleet_instance_ids(" in manager_script
        assert "def delete_launch_template(" in manager_script
        assert "RESOURCE_TYPE_SPOT_FLEET" in manager_script
        assert "USE_SPOT_FLEET" in manager_script

        assert "get_spot_fleet_role" not in manager_script
        assert "wait_for_fleet_instances" not in manager_script

        # Scoped to the two ways the role could be *passed* rather than a bare
        # substring check: the script's own header explains why CreateFleet has
        # no IamFleetRole member, and that explanation should not fail this.
        assert "'IamFleetRole'" not in manager_script
        assert "IamFleetRole=" not in manager_script

    def test_load_state_with_spot_fleet(
        self, detached_mode_with_spot_fleet, mock_state_store
    ):
        """Test loading state with SpotFleet information."""
        # Setup mock state with SpotFleet data
        mock_state = {
            "resources": {
                "spot-fleet-job-1": {
                    "job_id": "spot-job-1",
                    "status": STATUS_RUNNING,
                    "resource_type": RESOURCE_TYPE_SPOT_FLEET,
                    "fleet_request_id": "sfr-12345",
                    "all_instance_ids": ["i-spot1", "i-spot2"],
                }
            },
            "provider_id": "test-provider",
            "mode": "DetachedMode",
            "vpc_id": "vpc-12345",
            "subnet_id": "subnet-12345",
            "security_group_id": "sg-12345",
            "bastion_id": "i-bastion",
            "initialized": True,
            "workflow_id": "test-workflow",
            "bastion_host_type": "direct",
            "use_spot_fleet": True,
            "instance_types": ["t3.small", "t3.medium", "m5.small"],
            "nodes_per_block": 2,
            "spot_max_price_percentage": 80,
        }
        mock_state_store.load_state.return_value = mock_state

        # Load state
        result = detached_mode_with_spot_fleet.load_state()

        # Verify state was loaded
        assert result is True
        assert detached_mode_with_spot_fleet.resources == mock_state["resources"]
        assert (
            detached_mode_with_spot_fleet.use_spot_fleet == mock_state["use_spot_fleet"]
        )
        assert (
            detached_mode_with_spot_fleet.instance_types == mock_state["instance_types"]
        )
        assert (
            detached_mode_with_spot_fleet.nodes_per_block
            == mock_state["nodes_per_block"]
        )
        assert (
            detached_mode_with_spot_fleet.spot_max_price_percentage
            == mock_state["spot_max_price_percentage"]
        )

        # Verify SpotFleet resource data is loaded correctly
        spot_resource = detached_mode_with_spot_fleet.resources.get("spot-fleet-job-1")
        assert spot_resource is not None
        assert spot_resource["resource_type"] == RESOURCE_TYPE_SPOT_FLEET
        assert spot_resource["fleet_request_id"] == "sfr-12345"
        assert spot_resource["all_instance_ids"] == ["i-spot1", "i-spot2"]

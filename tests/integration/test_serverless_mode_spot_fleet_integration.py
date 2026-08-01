"""Integration tests for ServerlessMode with SpotFleet functionality.

These run the mode against moto, which intercepts the HTTP layer, so the real
CloudFormation templates are deployed and the real ``describe_*`` response shapes
are exercised. Nothing on the mode is stubbed.

The template's spot fleet branch is one deliberate omission: moto's
``AWS::EC2::SpotFleet`` handler reads
``SpotFleetRequestConfigData["LaunchSpecifications"]`` unconditionally
(``moto/ec2/models/spot_requests.py``), while ``ecs_worker.yml`` -- like current
AWS guidance -- declares ``LaunchTemplateConfigs``. Deploying that branch dies
inside moto with ``KeyError: 'LaunchSpecifications'``, a simulator gap rather
than a package defect, so ``_get_spot_fleet_status()`` is exercised against a
directly created fleet request instead.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import io
import json
import uuid
import zipfile
from unittest.mock import patch

import boto3
import pytest

try:
    # moto 5 replaced the per-service decorators (mock_ec2, mock_iam, ...) with a
    # single mock_aws.
    from moto import mock_aws

    MOTO_AVAILABLE = True
except ImportError:
    MOTO_AVAILABLE = False

from parsl_aws_provider.modes.serverless import ServerlessMode
from parsl_aws_provider.constants import (
    RESOURCE_TYPE_ECS_TASK,
    RESOURCE_TYPE_LAMBDA_FUNCTION,
    WORKER_TYPE_ECS,
    WORKER_TYPE_LAMBDA,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_PENDING,
    STATUS_RUNNING,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not MOTO_AVAILABLE, reason="moto not installed"),
]


class MockStateStore:
    """Keyed state store, matching the real stores' two-argument interface."""

    def __init__(self):
        self.states = {}

    def save_state(self, state_key, state_data):
        self.states[state_key] = state_data
        return True

    def load_state(self, state_key):
        return self.states.get(state_key)

    def delete_state(self, state_key):
        self.states.pop(state_key, None)


@pytest.fixture(autouse=True)
def moto():
    """Activate moto around each test *and* around its fixtures.

    ``@mock_aws`` as a class decorator wraps only the ``test_*`` methods, so a
    fixture that provisions a VPC runs outside the mock and hits real AWS. An
    autouse fixture is entered before the fixtures that depend on it, so the
    whole graph is covered.
    """
    with mock_aws():
        yield


class TestServerlessModeSpotFleetIntegration:
    """Integration tests for ServerlessMode against moto."""

    @pytest.fixture
    def aws_session(self):
        """A real boto3 session; moto intercepts its calls."""
        return boto3.Session(region_name="us-east-1")

    @pytest.fixture
    def network(self, aws_session):
        """Pre-provision the VPC, subnet, and security group.

        Since #69 no mode creates network resources -- all three IDs are
        caller-supplied and required for the ECS worker type.
        """
        ec2 = aws_session.client("ec2")
        vpc_id = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
        subnet_id = ec2.create_subnet(VpcId=vpc_id, CidrBlock="10.0.0.0/24")["Subnet"][
            "SubnetId"
        ]
        security_group_id = ec2.create_security_group(
            GroupName=f"parsl-test-{uuid.uuid4().hex[:8]}",
            Description="Test security group",
            VpcId=vpc_id,
        )["GroupId"]
        return {
            "vpc_id": vpc_id,
            "subnet_id": subnet_id,
            "security_group_id": security_group_id,
        }

    def _mode(self, aws_session, **overrides):
        """Build a ServerlessMode with a unique provider ID."""
        kwargs = {
            "provider_id": f"test-{uuid.uuid4()}",
            "session": aws_session,
            "state_store": MockStateStore(),
            "worker_type": WORKER_TYPE_ECS,
            "instance_types": ["t3.small", "t3.medium"],
            "nodes_per_block": 2,
            "spot_max_price_percentage": 80,
        }
        kwargs.update(overrides)
        return ServerlessMode(**kwargs)

    @pytest.fixture
    def serverless_mode(self, aws_session, network):
        """An ECS-mode instance wired to the pre-provisioned network."""
        return self._mode(aws_session, **network)

    def test_initialize_adopts_the_supplied_network(self, serverless_mode, network):
        """Initialization adopts the caller's network and keeps fleet settings."""
        serverless_mode.initialize()

        assert serverless_mode.initialized is True
        assert serverless_mode.vpc_id == network["vpc_id"]
        assert serverless_mode.subnet_id == network["subnet_id"]
        assert serverless_mode.security_group_id == network["security_group_id"]

        assert len(serverless_mode.instance_types) == 2
        assert serverless_mode.nodes_per_block == 2
        assert serverless_mode.spot_max_price_percentage == 80

    def test_submit_ecs_job_deploys_the_packaged_template(self, serverless_mode):
        """An ECS submit deploys the real ecs_worker.yml and tracks the stack.

        Two defects meet here. The tracking record used to be created *after*
        dispatch, while both submit helpers end by updating it -- so every submit
        raised `KeyError` from inside a blanket `except`, leaving a live stack
        with no tracking record to clean it up (#115). And the template was read
        off a `__file__`-relative path rather than through `get_cf_template()`,
        so it could not be found in an installed package (#113).
        """
        serverless_mode.initialize()

        resource_id = serverless_mode.submit_job("test-job-1", "echo hello", 2)

        resource = serverless_mode.resources[resource_id]
        assert resource["job_id"] == "test-job-1"
        assert resource["command"] == "echo hello"
        assert resource["status"] == STATUS_PENDING
        assert resource["resource_type"] == RESOURCE_TYPE_ECS_TASK

        # The stack really exists, and carries the outputs get_job_status reads.
        stack = serverless_mode.cf_client.describe_stacks(
            StackName=resource["stack_name"]
        )["Stacks"][0]
        assert stack["StackStatus"] == "CREATE_COMPLETE"
        outputs = {out["OutputKey"] for out in stack["Outputs"]}
        assert {"ClusterName", "ServiceName"} <= outputs

    def test_submit_lambda_job_stages_a_real_zip_in_s3(self, aws_session):
        """A Lambda submit stages an intact archive and deploys the function.

        The deployment package used to be latin1-decoded into the
        `CodeZipContent` CloudFormation parameter. That is neither the base64 the
        template documents nor legal XML, so CloudFormation's own DescribeStacks
        echo came back unparseable and the job reported UNKNOWN forever (#116).
        """
        mode = self._mode(aws_session, worker_type=WORKER_TYPE_LAMBDA)
        mode.initialize()

        resource_id = mode.submit_job("test-job-lambda", "echo hi", 1)
        resource = mode.resources[resource_id]

        assert resource["resource_type"] == RESOURCE_TYPE_LAMBDA_FUNCTION

        # The staged object is a valid zip holding the generated handler.
        body = (
            aws_session.client("s3")
            .get_object(Bucket=resource["code_bucket"], Key=resource["code_key"])[
                "Body"
            ]
            .read()
        )
        assert zipfile.ZipFile(io.BytesIO(body)).namelist() == ["handler.py"]

        # The status query parses -- it could not while the parameter carried
        # XML-illegal control characters.
        assert mode.get_job_status([resource_id])[resource_id] == STATUS_PENDING

        # And the function was really deployed from that archive.
        function = aws_session.client("lambda").get_function(
            FunctionName="parsl-lambda-test-job-lambda"
        )
        assert function["Configuration"]["CodeSize"] == len(body)

    def test_cleanup_removes_the_staged_lambda_code(self, aws_session):
        """Cleaning up a Lambda job deletes its staged package and bucket."""
        mode = self._mode(aws_session, worker_type=WORKER_TYPE_LAMBDA)
        mode.initialize()
        resource_id = mode.submit_job("test-job-lambda-2", "echo hi", 1)
        bucket = mode.resources[resource_id]["code_bucket"]
        key = mode.resources[resource_id]["code_key"]

        mode.cleanup_resources([resource_id])

        s3 = aws_session.client("s3")
        with pytest.raises(s3.exceptions.ClientError):
            s3.head_object(Bucket=bucket, Key=key)

        # The bucket was created by the mode, so the mode removes it.
        mode.cleanup_infrastructure()
        assert bucket not in [b["Name"] for b in s3.list_buckets()["Buckets"]]

    def test_a_caller_supplied_bucket_is_reused_and_kept(self, aws_session):
        """A configured lambda_code_bucket is staged into, never deleted.

        The parameter used to be ``checkpoint_bucket``, whose checkpointing half
        #137 removed; the staging override it also carried is real, so it kept the
        behaviour under a name that describes it.
        """
        s3 = aws_session.client("s3")
        s3.create_bucket(Bucket="parsl-test-caller-bucket")

        mode = self._mode(
            aws_session,
            worker_type=WORKER_TYPE_LAMBDA,
            lambda_code_bucket="parsl-test-caller-bucket",
        )
        mode.initialize()
        resource_id = mode.submit_job("test-job-lambda-3", "echo hi", 1)

        assert mode.resources[resource_id]["code_bucket"] == "parsl-test-caller-bucket"

        mode.cleanup_resources([resource_id])
        mode.cleanup_infrastructure()

        # The caller's bucket survives: only a bucket the mode created is deleted.
        assert "parsl-test-caller-bucket" in [
            b["Name"] for b in s3.list_buckets()["Buckets"]
        ]

    def test_a_failed_submit_leaves_nothing_behind(self, serverless_mode):
        """A submit that fails mid-flight does not leak a stack or a record."""
        serverless_mode.initialize()

        with patch.object(
            serverless_mode.cf_client,
            "create_stack",
            side_effect=RuntimeError("boom"),
        ):
            with pytest.raises(Exception, match="Failed to submit job"):
                serverless_mode.submit_job("test-job-fail", "echo hello", 1)

        # Tracking is created before dispatch now, so the failure path has to
        # remove it again -- otherwise a phantom job would be polled forever.
        assert serverless_mode.resources == {}

    def test_an_active_fleet_with_live_instances_reports_running(
        self, serverless_mode, network
    ):
        """An active fleet still holding instances reports RUNNING.

        The status comes from the *instances*, not from capacity counters. An
        instant fleet does not maintain capacity, so its ``FleetState`` stays
        ``active`` for the fleet's whole life regardless of what became of the
        instances, and the counters only ever reflect the original launch.

        This is the site of #114, though not in the way the old test described.
        That version read `FulfilledCapacity` from the wrong nesting level -- it
        lives inside `SpotFleetRequestConfig`, beside `TargetCapacity` -- so the
        0 default made `0 >= target` false and a fully provisioned fleet reported
        PENDING forever, never terminal, so Parsl polled it and never freed the
        block. The capacity comparison is gone entirely now; instance state
        replaced it.

        The old test also created its fleet with ``request_spot_fleet``, the
        legacy API #86 removed. ``_get_spot_fleet_status`` calls
        ``describe_fleets``, which knows nothing about an ``sfr-`` request, so it
        took the "EC2 has forgotten this fleet" branch and returned COMPLETED --
        the assertion was against an API the code no longer calls.
        """
        serverless_mode.initialize()
        ec2 = serverless_mode.session.client("ec2")
        template = ec2.create_launch_template(
            LaunchTemplateName="parsl-test-fleet-template",
            LaunchTemplateData={
                "ImageId": "ami-12345678",
                "InstanceType": "t3.small",
            },
        )["LaunchTemplate"]
        fleet = ec2.create_fleet(
            Type="instant",
            LaunchTemplateConfigs=[
                {
                    "LaunchTemplateSpecification": {
                        "LaunchTemplateId": template["LaunchTemplateId"],
                        "Version": str(template["LatestVersionNumber"]),
                    },
                    "Overrides": [
                        {
                            "InstanceType": "t3.small",
                            "SubnetId": network["subnet_id"],
                        }
                    ],
                }
            ],
            TargetCapacitySpecification={
                "TotalTargetCapacity": 2,
                "DefaultTargetCapacityType": "spot",
            },
        )
        fleet_id = fleet["FleetId"]

        # Real EC2 tags every fleet-launched instance with `aws:ec2:fleet-id`,
        # and that tag is the only supported way to enumerate an instant fleet's
        # instances: DescribeFleetInstances rejects this fleet type outright, and
        # DescribeFleets reports the original launch without dropping instances
        # that have since terminated. Neither moto nor substrate applies it
        # (substrate#443), so it is set here -- otherwise the lookup comes back
        # empty and a running fleet reports COMPLETED for want of a tag rather
        # than for any behaviour of the code under test.
        instance_ids = [i for g in fleet["Instances"] for i in g["InstanceIds"]]
        assert instance_ids, "fleet launched nothing; nothing to assert about"
        ec2.create_tags(
            Resources=instance_ids,
            Tags=[{"Key": "aws:ec2:fleet-id", "Value": fleet_id}],
        )

        assert serverless_mode._get_spot_fleet_status(fleet_id) == STATUS_RUNNING

    def test_a_fleet_whose_instances_are_gone_reports_completed(
        self, serverless_mode, network
    ):
        """A terminal status, so Parsl can free the block.

        The counterpart to the test above, and the reason instance state is what
        decides: the fleet below stays ``active`` throughout, so anything reading
        ``FleetState`` alone would report it RUNNING forever.
        """
        serverless_mode.initialize()
        ec2 = serverless_mode.session.client("ec2")
        template = ec2.create_launch_template(
            LaunchTemplateName="parsl-test-fleet-template-2",
            LaunchTemplateData={
                "ImageId": "ami-12345678",
                "InstanceType": "t3.small",
            },
        )["LaunchTemplate"]
        fleet = ec2.create_fleet(
            Type="instant",
            LaunchTemplateConfigs=[
                {
                    "LaunchTemplateSpecification": {
                        "LaunchTemplateId": template["LaunchTemplateId"],
                        "Version": str(template["LatestVersionNumber"]),
                    },
                    "Overrides": [
                        {
                            "InstanceType": "t3.small",
                            "SubnetId": network["subnet_id"],
                        }
                    ],
                }
            ],
            TargetCapacitySpecification={
                "TotalTargetCapacity": 1,
                "DefaultTargetCapacityType": "spot",
            },
        )
        fleet_id = fleet["FleetId"]
        instance_ids = [i for g in fleet["Instances"] for i in g["InstanceIds"]]
        ec2.create_tags(  # see substrate#443, above
            Resources=instance_ids,
            Tags=[{"Key": "aws:ec2:fleet-id", "Value": fleet_id}],
        )

        ec2.terminate_instances(InstanceIds=instance_ids)

        assert (
            ec2.describe_fleets(FleetIds=[fleet_id])["Fleets"][0]["FleetState"]
            == "active"
        )
        assert serverless_mode._get_spot_fleet_status(fleet_id) == STATUS_COMPLETED

    def test_cancel_job_deletes_the_stack(self, serverless_mode):
        """Cancelling a job marks it CANCELLED and deletes its stack."""
        serverless_mode.initialize()
        resource_id = serverless_mode.submit_job("test-job-2", "echo hello", 2)
        stack_name = serverless_mode.resources[resource_id]["stack_name"]

        cancel_results = serverless_mode.cancel_jobs([resource_id])

        assert cancel_results[resource_id] == STATUS_CANCELLED
        assert serverless_mode.resources[resource_id]["status"] == STATUS_CANCELLED

        # A deleted stack resolves by StackId only, so a lookup by name raises.
        cf = serverless_mode.cf_client
        with pytest.raises(cf.exceptions.ClientError, match="does not exist"):
            cf.describe_stacks(StackName=stack_name)

    def test_list_resources_groups_by_worker_type(self, serverless_mode):
        """Submitted jobs are listed under their worker type."""
        serverless_mode.initialize()
        resource_id = serverless_mode.submit_job("test-job-3", "echo hello", 2)

        resources = serverless_mode.list_resources()

        assert len(resources["ecs_tasks"]) == 1
        assert resources["ecs_tasks"][0]["id"] == resource_id
        assert resources["ecs_tasks"][0]["job_id"] == "test-job-3"

    def test_cleanup_resources_drops_tracking(self, serverless_mode):
        """Cleaning up a resource deletes its stack and drops the tracking."""
        serverless_mode.initialize()
        resource_id = serverless_mode.submit_job("test-job-4", "echo hello", 2)
        stack_name = serverless_mode.resources[resource_id]["stack_name"]

        serverless_mode.cleanup_resources([resource_id])

        assert resource_id not in serverless_mode.resources
        assert serverless_mode.list_resources()["ecs_tasks"] == []

        cf = serverless_mode.cf_client
        with pytest.raises(cf.exceptions.ClientError, match="does not exist"):
            cf.describe_stacks(StackName=stack_name)

    @patch(
        "parsl_aws_provider.compute.spot_fleet_cleanup.cleanup_all_spot_fleet_resources"
    )
    def test_cleanup_infrastructure_spares_the_callers_network(
        self, mock_cleanup_spot_fleet, aws_session, network
    ):
        """Infrastructure cleanup releases fleet resources, not the network."""
        mode = self._mode(aws_session, use_spot_fleet=True, **network)
        mode.initialize()
        mode.resources["placeholder"] = {
            "id": "placeholder",
            "job_id": "test-job-5",
            "worker_type": WORKER_TYPE_ECS,
            "status": STATUS_PENDING,
        }

        mock_cleanup_spot_fleet.return_value = {
            "cancelled_requests": ["sfr-12345678"],
            "cleaned_roles": ["parsl-aws-spot-fleet-role-test"],
            "errors": [],
        }

        mode.cleanup_infrastructure()

        mock_cleanup_spot_fleet.assert_called_once()
        assert not mode.resources
        assert mode.initialized is False

        # The VPC, subnet, and security group belong to the caller and must
        # survive: nulling them is what #74 removed, and deleting the security
        # group is what #70 removed.
        assert mode.vpc_id == network["vpc_id"]
        assert mode.subnet_id == network["subnet_id"]
        assert mode.security_group_id == network["security_group_id"]
        assert aws_session.client("ec2").describe_security_groups(
            GroupIds=[network["security_group_id"]]
        )["SecurityGroups"]

    def test_ecs_template_parameters_match_the_template(self, serverless_mode, network):
        """Every parameter the mode sends is one ecs_worker.yml declares.

        A stale or misspelled ParameterKey is rejected by CloudFormation at
        create time, so this deploys the real template with the full parameter
        set the spot fleet path would send.
        """
        from parsl_aws_provider.utils.aws import get_cf_template

        cf = serverless_mode.cf_client
        params = {
            "ClusterName": "parsl-test-cluster",
            "TaskFamily": "parsl-test-task",
            "Command": "echo hello",
            "VpcId": network["vpc_id"],
            "SubnetIds": network["subnet_id"],
            "SecurityGroupIds": network["security_group_id"],
            "AssignPublicIp": "ENABLED",
            "WorkflowId": "wf-1",
            "JobId": "job-1",
            "TaskCount": "1",
            "UseSpot": "false",
            "UseSpotFleet": "false",
            "InstanceTypes": json.dumps(["t3.small"]),
            "NodesPerBlock": "2",
            "SpotMaxPricePercentage": "80",
        }
        cf.create_stack(
            StackName="parsl-ecs-parameter-check",
            TemplateBody=get_cf_template("ecs_worker.yml"),
            Parameters=[
                {"ParameterKey": key, "ParameterValue": value}
                for key, value in params.items()
            ],
            Capabilities=["CAPABILITY_IAM"],
        )

        stack = cf.describe_stacks(StackName="parsl-ecs-parameter-check")["Stacks"][0]
        assert stack["StackStatus"] == "CREATE_COMPLETE"

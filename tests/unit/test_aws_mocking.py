"""Unit tests that drive the resource managers against moto-mocked AWS.

Unlike the rest of ``tests/unit``, these tests do not mock the boto3 client:
moto intercepts the HTTP layer, so the managers issue real API calls against a
simulated AWS. That makes this the only place in the unit suite where a request
shape is validated rather than asserted against a MagicMock.

Two things about the file's history are worth stating, because both hid bugs:

1. The import guard caught an ``ImportError`` for moto 4's per-service
   decorators (``mock_ec2``, ``mock_s3``, ...), which moto 5 removed in favour of
   a single ``mock_aws``. moto was installed and working the whole time, so the
   guard silently skipped all 12 tests on every run.
2. Underneath the skips, the tests called constructors and methods that no
   commit has ever produced -- ``create_instances()``,
   ``LambdaManager.invoke_lambda_function()``, and so on. Every manager takes
   ``__init__(self, provider)`` and reads its configuration off that object.
   These tests now use the real surface.

The ``VPCManager``, ``SecurityGroupManager`` and ``EC2Manager`` classes went with
#90: nothing in the package imported them, so their tests were coverage of dead
code. The managers that remain here are the ones the modes actually route
through.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import json
import os
import uuid
from types import SimpleNamespace

import boto3
import pytest

from moto import mock_aws

from parsl_ephemeral_provider.compute.ecs import ECSManager
from parsl_ephemeral_provider.compute.lambda_func import LambdaManager
from parsl_ephemeral_provider.compute.spot_fleet import SpotFleetManager
from parsl_ephemeral_provider.constants import (
    STATUS_CANCELLED,
    TAG_MANAGED,
    TAG_WORKFLOW_ID,
)
from parsl_ephemeral_provider.state.parameter_store import ParameterStoreState
from parsl_ephemeral_provider.state.s3 import S3State

pytestmark = pytest.mark.unit


@pytest.fixture
def region():
    """AWS region for testing."""
    return "us-east-1"


@pytest.fixture
def aws_credentials(monkeypatch, region):
    """Synthetic credentials so boto3 never reaches a real credential chain.

    The AWS-managed-policy support these tests also need
    (``MOTO_IAM_LOAD_MANAGED_POLICIES``) is set session-wide in
    ``tests/conftest.py``; without it every ``attach_role_policy`` against an
    ``arn:aws:iam::aws:policy/...`` ARN fails with ``NoSuchEntity``.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", region)


@pytest.fixture
def moto_session(aws_credentials, region):
    """A boto3 session for asserting against AWS state directly."""
    return boto3.Session(region_name=region)


def make_provider(**overrides):
    """Build the provider object the managers read their configuration from.

    A ``SimpleNamespace``, not a ``MagicMock``: the managers read several settings
    with ``getattr(provider, name, <default>)`` -- ``vpc_cidr``,
    ``security_environment``, ``admin_cidr_blocks``, ``strict_security_mode`` --
    and a MagicMock answers every one of them, so the default never applies and
    ``SecurityConfig`` raises ``ValueError: Invalid VPC CIDR: <MagicMock ...>``
    during construction. Only the attributes listed here exist, matching what the
    operating modes actually pass.
    """
    attrs = {
        "workflow_id": f"wf-{uuid.uuid4().hex[:8]}",
        "region": "us-east-1",
        "aws_access_key_id": "testing",
        "aws_secret_access_key": "testing",
        "aws_session_token": None,
        "aws_profile": None,
        "tags": {"TestTag": "TestValue"},
        "vpc_id": None,
        "subnet_id": None,
        "subnet_ids": None,
        "security_group_id": None,
        "image_id": "ami-12345678",
        "instance_type": "t3.micro",
        "instance_types": [],
        "key_name": None,
        "use_public_ips": True,
        "nodes_per_block": 1,
        "use_spot_instances": False,
        "worker_init": "echo 'worker init'",
        "spot_max_price_percentage": 100,
        "lambda_timeout": 300,
        "lambda_memory": 1024,
        "ecs_task_cpu": 1024,
        "ecs_task_memory": 2048,
        "ecs_container_image": None,
    }
    attrs.update(overrides)
    return SimpleNamespace(**attrs)


def provision_network(session):
    """Create the pre-provisioned VPC/subnet/SG the managers now require.

    Since #69 the provider does not create network resources; the caller supplies
    all three IDs. These tests mirror that by building them with plain boto3.
    """
    ec2 = session.client("ec2")
    vpc_id = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    subnet_id = ec2.create_subnet(
        VpcId=vpc_id, CidrBlock="10.0.1.0/24", AvailabilityZone="us-east-1a"
    )["Subnet"]["SubnetId"]
    sg_id = ec2.create_security_group(
        GroupName=f"test-sg-{uuid.uuid4().hex[:8]}",
        Description="Pre-provisioned test security group",
        VpcId=vpc_id,
    )["GroupId"]
    return vpc_id, subnet_id, sg_id


def tag_dict(tags):
    """Normalize a boto3 tag list to a dict, rejecting duplicate keys.

    A duplicate key is a bug, not a shape variation: EC2 rejects duplicate tag
    keys outright on ``RunInstances``/``CreateSecurityGroup``/``RequestSpotFleet``
    (#109), and moto tolerates them, so an assertion has to look for them.
    """
    keys = [tag["Key"] for tag in tags]
    assert len(keys) == len(set(keys)), f"duplicate tag key: {keys}"
    return {tag["Key"]: tag["Value"] for tag in tags}


class TestSpotFleetWithMoto:
    """``SpotFleetManager`` requests fleets against pre-provisioned network."""

    @mock_aws
    def test_fleet_block_lifecycle(self, moto_session):
        """``create_blocks`` creates an EC2 Fleet; ``terminate_block`` deletes it.

        Retargeted from the legacy Spot Fleet API onto ``CreateFleet`` (#86). The
        two assertions that went with the old API are gone for good reasons
        rather than convenience:

        * ``describe_spot_fleet_requests`` cannot see this fleet at all -- the two
          APIs keep separate registries, so the call returns nothing and indexing
          ``[0]`` is the ``IndexError`` this test failed with.
        * the IAM-role assertions had nothing left to assert. ``CreateFleet`` has
          no ``IamFleetRole`` member, so no service role is created; that is now
          pinned negatively here, and the surviving cleanup of a role named by a
          pre-#86 state document is covered in ``test_spot_fleet.py``.

        Kept under moto because this is the only unit test where the
        ``CreateFleet`` request *shape* is validated by something other than a
        MagicMock -- a launch template really has to exist, and be referenced by
        ID and version, for the call to succeed.

        ``time.sleep`` is no longer patched: the 10-second IAM propagation wait
        belonged to the role fetch that is gone.
        """
        vpc_id, subnet_id, sg_id = provision_network(moto_session)
        # A real AMI: moto validates the ID at launch, and the fleet resolves its
        # image through the launch template, so a placeholder would fail the call.
        image_id = moto_session.client("ec2").describe_images()["Images"][0]["ImageId"]
        manager = SpotFleetManager(
            make_provider(
                vpc_id=vpc_id,
                subnet_id=subnet_id,
                security_group_id=sg_id,
                image_id=image_id,
                instance_types=["t3.micro", "t3.small"],
            )
        )
        ec2 = moto_session.client("ec2")

        blocks = manager.create_blocks(1)

        block_id, block = next(iter(blocks.items()))
        fleet_id = block["fleet_request_id"]
        assert fleet_id.startswith("fleet-")

        fleet = ec2.describe_fleets(FleetIds=[fleet_id])["Fleets"][0]
        assert fleet["FleetState"] == "active"
        # Type "instant" is what makes the instance IDs available synchronously,
        # which every caller here relies on.
        assert fleet["Type"] == "instant"

        # The instances came back from the create call itself rather than from a
        # polling loop, and the block records them.
        assert block["instance_ids"]
        instances = ec2.describe_instances(InstanceIds=block["instance_ids"])
        instance = instances["Reservations"][0]["Instances"][0]
        tags = tag_dict(instance["Tags"])
        assert tags[TAG_MANAGED] == "true"
        assert tags[TAG_WORKFLOW_ID] == manager.provider.workflow_id

        assert manager.get_block_status(block_id) in ("PENDING", "RUNNING")

        # No IAM role was created for the fleet, and none was asked for.
        assert manager.iam_fleet_role_arn is None
        iam = moto_session.client("iam")
        assert iam.list_roles()["Roles"] == []

        manager.terminate_block(block_id)
        assert manager.blocks[block_id]["status"] == STATUS_CANCELLED
        assert ec2.describe_fleets(FleetIds=[fleet_id])["Fleets"][0][
            "FleetState"
        ].startswith("deleted")

    @mock_aws
    def test_missing_network_is_rejected(self, moto_session):
        """Without pre-provisioned IDs the manager refuses to create blocks."""
        from parsl_ephemeral_provider.exceptions import ResourceCreationError

        manager = SpotFleetManager(make_provider())

        with pytest.raises(ResourceCreationError, match="pre-provisioned"):
            manager.create_blocks(1)


class TestLambdaWithMoto:
    """``LambdaManager`` creates and deletes functions and their roles."""

    @mock_aws
    def test_function_and_role_lifecycle(self, moto_session):
        """``_create_lambda_function`` then ``cleanup_all_resources``.

        There is no ``create_lambda_function``/``delete_lambda_function``; the
        public surface is ``submit_job``/``get_job_status``/
        ``cleanup_all_resources``. ``submit_job`` cannot run under moto -- moto
        *executes* the packaged handler, so an async invoke returns a
        ``FunctionError`` -- so the creation half is driven directly.
        """
        manager = LambdaManager(make_provider())
        lambda_client = moto_session.client("lambda")
        iam = moto_session.client("iam")

        function_name = manager._create_lambda_function("job-1", "echo hello")

        function = lambda_client.get_function(FunctionName=function_name)
        assert function["Configuration"]["MemorySize"] == 1024
        assert function["Configuration"]["Timeout"] == 300
        assert function["Tags"][TAG_MANAGED] == "true"
        assert function["Tags"][TAG_WORKFLOW_ID] == manager.provider.workflow_id

        # The execution role carries the AWS-managed basic execution policy.
        (role_name,) = manager.role_names
        attached = iam.list_attached_role_policies(RoleName=role_name)[
            "AttachedPolicies"
        ]
        assert [p["PolicyName"] for p in attached] == ["AWSLambdaBasicExecutionRole"]

        manager.cleanup_all_resources()

        assert manager.function_names == set()
        assert manager.role_names == set()
        with pytest.raises(lambda_client.exceptions.ResourceNotFoundException):
            lambda_client.get_function(FunctionName=function_name)

    @mock_aws
    def test_execution_role_creation_is_idempotent(self, moto_session):
        """Two calls reuse one role rather than failing on EntityAlreadyExists."""
        manager = LambdaManager(make_provider())

        first = manager._create_lambda_execution_role()
        second = manager._create_lambda_execution_role()

        assert first == second
        assert len(manager.role_names) == 1

    @mock_aws
    def test_timed_out_job_reaches_a_terminal_status(self, moto_session):
        """A job past its timeout must not be stuck reporting UNKNOWN (#111).

        The timeout branch interpolated a ``job_id`` that is not in scope, so it
        raised ``NameError``, which the blanket ``except`` converted to
        ``"UNKNOWN"`` -- a non-terminal status, so the job was polled forever and
        its stored status never advanced.
        """
        import time

        manager = LambdaManager(make_provider(lambda_timeout=1))
        manager.jobs["job-1"] = {
            "id": "job-1",
            "function_name": "fn",
            "request_id": "rid",
            "status": "PENDING",
            "submitted_at": time.time() - 3600,
        }

        assert manager.get_job_status("fn", "rid") == "COMPLETED"
        assert manager.jobs["job-1"]["status"] == "COMPLETED"


class TestECSWithMoto:
    """``ECSManager`` sets up the cluster, task definition, and network."""

    @mock_aws
    def test_cluster_is_created_during_init(self, moto_session):
        """``__init__`` calls ``_get_or_create_cluster``; there is no
        ``create_cluster``/``delete_cluster``."""
        vpc_id, subnet_id, _ = provision_network(moto_session)
        manager = ECSManager(make_provider(vpc_id=vpc_id, subnet_id=subnet_id))
        ecs = moto_session.client("ecs")

        cluster = ecs.describe_clusters(clusters=[manager.cluster_name])["clusters"][0]
        assert cluster["status"] == "ACTIVE"
        assert manager.cluster_name in manager.clusters

        # A second manager on the same workflow adopts the existing cluster.
        again = ECSManager(
            make_provider(
                workflow_id=manager.provider.workflow_id,
                vpc_id=vpc_id,
                subnet_id=subnet_id,
            )
        )
        assert again.cluster_name == manager.cluster_name

    @mock_aws
    def test_task_definition_registration(self, moto_session):
        """``_register_task_definition(job_id, command)`` builds the Fargate
        task; there is no ``register_task_definition(family=, ...)``."""
        vpc_id, subnet_id, _ = provision_network(moto_session)
        manager = ECSManager(make_provider(vpc_id=vpc_id, subnet_id=subnet_id))
        ecs = moto_session.client("ecs")
        logs = moto_session.client("logs")

        task_definition_arn = manager._register_task_definition("job-1", "echo hello")

        task_definition = ecs.describe_task_definition(
            taskDefinition=task_definition_arn
        )["taskDefinition"]
        assert task_definition["networkMode"] == "awsvpc"
        assert task_definition["requiresCompatibilities"] == ["FARGATE"]

        (container,) = task_definition["containerDefinitions"]
        assert container["command"] == ["/bin/sh", "-c", "echo hello"]
        assert container["cpu"] == 1024
        assert container["memory"] == 2048

        # The log group is created ahead of the task: Fargate tasks fail
        # immediately if the awslogs driver cannot write.
        log_group_name = container["logConfiguration"]["options"]["awslogs-group"]
        assert log_group_name in manager.log_groups
        assert [
            g["logGroupName"]
            for g in logs.describe_log_groups(logGroupNamePrefix=log_group_name)[
                "logGroups"
            ]
        ] == [log_group_name]

    @mock_aws
    def test_network_resolution_honours_supplied_ids(self, moto_session):
        """The caller's VPC and subnet are used verbatim; the SG is created."""
        vpc_id, subnet_id, _ = provision_network(moto_session)
        manager = ECSManager(make_provider(vpc_id=vpc_id, subnet_id=subnet_id))
        ec2 = moto_session.client("ec2")

        network = manager._get_or_create_network_resources()

        assert network["vpc_id"] == vpc_id
        assert network["subnet_ids"] == [subnet_id]

        group = ec2.describe_security_groups(GroupIds=[network["security_group_id"]])[
            "SecurityGroups"
        ][0]
        assert group["VpcId"] == vpc_id
        # EC2 attaches allow-all-outbound itself; re-authorizing it raises
        # InvalidPermission.Duplicate and used to abort this branch (#110).
        assert group["IpPermissionsEgress"] == [
            {
                "IpProtocol": "-1",
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                "Ipv6Ranges": [],
                "PrefixListIds": [],
                "UserIdGroupPairs": [],
            }
        ]

    @mock_aws
    def test_cleanup_removes_cluster_and_role(self, moto_session):
        """Cleanup drops the cluster, task definitions, and execution role."""
        vpc_id, subnet_id, _ = provision_network(moto_session)
        manager = ECSManager(make_provider(vpc_id=vpc_id, subnet_id=subnet_id))
        ecs = moto_session.client("ecs")
        cluster_name = manager.cluster_name

        manager._register_task_definition("job-1", "echo hello")
        manager.cleanup_all_resources()

        clusters = ecs.describe_clusters(clusters=[cluster_name])["clusters"]
        assert not clusters or clusters[0]["status"] == "INACTIVE"
        assert manager.role_names == set()


class TestParameterStoreWithMoto:
    """``ParameterStoreState`` round-trips keyed state documents."""

    @mock_aws
    def test_parameter_store_lifecycle(self, moto_session):
        """Save, load, list, and delete a keyed state document."""
        provider = make_provider()
        prefix = f"/parsl/test/{uuid.uuid4().hex[:8]}"
        store = ParameterStoreState(provider=provider, prefix=prefix)

        state = {
            "provider_info": {"id": "test-provider", "region": "us-east-1"},
            "resources": {"resource-1": {"id": "r-1", "status": "running"}},
        }

        store.save_state("test-state", state)

        assert store.load_state("test-state") == state

        states = store.list_states("")
        assert any(key.endswith("test-state") for key in states)

        store.delete_state("test-state")
        assert store.load_state("test-state") is None

    @mock_aws
    def test_keys_are_namespaced(self, moto_session):
        """Two keys are separate documents, not one clobbering the other.

        The provider writes ``"provider"`` and the operating mode writes
        ``"mode"``; before the state layer was keyed both went to the same slot
        and each full overwrite destroyed the other's fields.
        """
        store = ParameterStoreState(
            provider=make_provider(), prefix=f"/parsl/test/{uuid.uuid4().hex[:8]}"
        )

        store.save_state("provider", {"job_map": {"job-1": "block-1"}})
        store.save_state("mode", {"baked_ami_id": "ami-baked"})

        assert store.load_state("provider") == {"job_map": {"job-1": "block-1"}}
        assert store.load_state("mode") == {"baked_ami_id": "ami-baked"}


class TestS3StateWithMoto:
    """``S3State`` round-trips keyed state documents."""

    @mock_aws
    def test_s3_state_lifecycle(self, moto_session):
        """Save, load, list, and delete a keyed state document."""
        bucket_name = f"test-bucket-{uuid.uuid4().hex[:8]}"
        moto_session.client("s3").create_bucket(Bucket=bucket_name)

        store = S3State(
            provider=make_provider(),
            bucket_name=bucket_name,
            key_prefix=f"parsl/test/{uuid.uuid4().hex[:8]}",
        )

        state = {
            "provider_info": {"id": "test-provider", "region": "us-east-1"},
            "resources": {"resource-1": {"id": "r-1", "status": "running"}},
        }

        store.save_state("test-state", state)

        assert store.load_state("test-state") == state

        states = store.list_states("")
        assert any(key.endswith("test-state") for key in states)

        store.delete_state("test-state")
        assert store.load_state("test-state") is None

    @mock_aws
    def test_bucket_is_created_when_requested(self, moto_session):
        """``create_bucket_if_not_exists`` provisions a missing bucket."""
        bucket_name = f"test-bucket-{uuid.uuid4().hex[:8]}"

        store = S3State(
            provider=make_provider(),
            bucket_name=bucket_name,
            create_bucket_if_not_exists=True,
        )
        store.save_state("test-state", {"ok": True})

        s3 = moto_session.client("s3")
        assert bucket_name in [b["Name"] for b in s3.list_buckets()["Buckets"]]


class TestCloudFormationWithMoto:
    """CloudFormation is the substrate for detached and serverless modes."""

    @mock_aws
    def test_stack_lifecycle(self, moto_session):
        """A stack can be created, described, and deleted.

        Note this needs ``openapi-spec-validator``: moto's CloudFormation parser
        imports its API Gateway backend, which imports that package
        unconditionally. Without it every CloudFormation call raises
        ``ModuleNotFoundError`` -- which is why it is a declared test dependency
        rather than something moto pulls in.
        """
        cf = moto_session.client("cloudformation")
        s3 = moto_session.client("s3")
        stack_name = f"test-stack-{uuid.uuid4().hex[:8]}"
        bucket_name = f"cf-bucket-{uuid.uuid4().hex[:8]}"
        template = {
            "AWSTemplateFormatVersion": "2010-09-09",
            "Resources": {
                "TestBucket": {
                    "Type": "AWS::S3::Bucket",
                    "Properties": {"BucketName": bucket_name},
                }
            },
            "Outputs": {"BucketName": {"Value": {"Ref": "TestBucket"}}},
        }

        stack_id = cf.create_stack(
            StackName=stack_name,
            TemplateBody=json.dumps(template),
            Capabilities=["CAPABILITY_IAM"],
        )["StackId"]

        stack = cf.describe_stacks(StackName=stack_name)["Stacks"][0]
        assert stack["StackName"] == stack_name
        assert stack["StackStatus"] == "CREATE_COMPLETE"
        assert stack["Outputs"] == [
            {"OutputKey": "BucketName", "OutputValue": bucket_name}
        ]
        # The templated resource really exists, not just the stack record.
        assert bucket_name in [b["Name"] for b in s3.list_buckets()["Buckets"]]

        cf.delete_stack(StackName=stack_name)

        # A deleted stack is addressable only by ID -- by name it is gone, on
        # real AWS and under moto alike.
        assert (
            cf.describe_stacks(StackName=stack_id)["Stacks"][0]["StackStatus"]
            == "DELETE_COMPLETE"
        )
        with pytest.raises(cf.exceptions.ClientError, match="does not exist"):
            cf.describe_stacks(StackName=stack_name)


def test_moto_is_installed_and_serves_managed_policies():
    """A canary for the guard this file used to carry.

    The old ``try: from moto import mock_ec2 / except ImportError`` skipped all
    12 tests here on every run, reporting "Moto library not available" while moto
    was installed and working. If moto ever really goes missing, the import at
    the top of this module fails loudly instead.
    """
    assert mock_aws is not None
    assert os.environ.get("MOTO_IAM_LOAD_MANAGED_POLICIES") == "true"

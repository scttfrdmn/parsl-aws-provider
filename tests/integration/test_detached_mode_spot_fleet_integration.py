"""Integration tests for the SpotFleet functionality in DetachedMode.

These run the mode against [substrate](https://github.com/scttfrdmn/substrate),
the emulator this suite standardised on in #125, rather than moto (#183). The
switch is not like-for-like: it *adds* coverage.

The old moto version passed ``bastion_host_type="direct"``, because since #86
``bastion.yml`` puts ``ImageId`` in a launch template and has the
``AWS::EC2::Instance`` reference it -- one template serving both the on-demand and
the spot bastion. moto's CloudFormation handler reads ``properties["ImageId"]``
directly (``moto/ec2/models/instances.py:400``) and resolves no launch template,
so it raises ``KeyError: 'ImageId'`` on a stack real CloudFormation accepts.
Substrate deploys that template end to end as of ``0.87.1``
([substrate#516](https://github.com/scttfrdmn/substrate/issues/516),
[#517](https://github.com/scttfrdmn/substrate/issues/517)), so these tests now
exercise the **default** ``cloudformation`` bastion path -- the one users get --
instead of the fallback chosen to work around a simulator gap.

The spot-fleet lifecycle is likewise real here: ``test_cleanup_deletes_the_fleet``
creates an actual ``CreateFleet`` fleet and asserts cleanup deletes it. The moto
version patched ``boto3.client`` and asserted against a ``MagicMock``'s
``cancel_spot_fleet_requests`` -- an API the provider stopped calling in #86, so
that assertion could not have failed however the code behaved.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import json
import uuid

import pytest

from parsl_ephemeral_provider.modes.detached import DetachedMode
from parsl_ephemeral_provider.constants import (
    RESOURCE_TYPE_EC2,
    RESOURCE_TYPE_SPOT_FLEET,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_CANCELED,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.substrate,
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


class TestDetachedModeSpotFleetIntegration:
    """Integration tests for DetachedMode SpotFleet functionality."""

    @pytest.fixture
    def image_id(self, substrate_session):
        """A registered AMI, rather than a made-up ``ami-`` string.

        ``bastion.yml`` passes it into a launch template that the bastion instance
        resolves, so it has to be an image the emulator will actually serve.
        """
        return substrate_session.client("ec2").register_image(
            Name=f"parsl-test-ami-{uuid.uuid4().hex[:8]}",
            RootDeviceName="/dev/sda1",
            BlockDeviceMappings=[{"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": 8}}],
        )["ImageId"]

    @pytest.fixture
    def workflow_id(self):
        """Unique per test, and unique **in its first eight characters**.

        Two reasons it has to be unique. SSM parameter paths are keyed on it, and
        emulator state lives for the server process's lifetime, so a fixed value
        meets the previous run's parameters rather than starting clean.

        The eight-character part is the subtle one: the bastion stack is named
        ``parsl-bastion-{workflow_id[:8]}`` (``modes/detached.py:458``), so an id
        like ``test-workflow-<random>`` truncates to ``test-wor`` for every test in
        the file and the second ``initialize()`` dies on ``AlreadyExists``. The
        random part therefore leads.

        That truncation is a real provider constraint, not a test artefact: two
        concurrent workflows whose ids share a prefix collide on the same stack.
        Worth knowing before it is met in an account rather than an emulator.
        """
        return f"{uuid.uuid4().hex[:8]}-test-workflow"

    @pytest.fixture
    def detached_mode(
        self, substrate_session, substrate_network, image_id, workflow_id
    ):
        """A spot-fleet DetachedMode on the default CloudFormation bastion path.

        No ``create_vpc``: #69 removed the parameter -- every mode now requires the
        three network IDs, which ``substrate_network`` provisions -- and the mode's
        ``**kwargs`` swallowed it silently rather than rejecting it, so it read as
        configuration while doing nothing.

        No ``region`` either. The mode would merely store it, while its clients
        come from the session, which follows ``AWS_TEST_REGION``. Substrate
        partitions state by region, so naming a different one here is how you get
        a ``NotFound`` for a resource that was just created successfully.
        """
        mode = DetachedMode(
            provider_id=f"test-provider-{uuid.uuid4().hex[:8]}",
            session=substrate_session,
            state_store=MockStateStore(),
            workflow_id=workflow_id,
            bastion_instance_type="t3.micro",
            instance_type="t3.micro",
            image_id=image_id,
            vpc_id=substrate_network["vpc_id"],
            subnet_id=substrate_network["subnet_id"],
            security_group_id=substrate_network["security_group_id"],
            use_spot_fleet=True,
            instance_types=["t3.micro", "t3.small", "m5.large"],
            nodes_per_block=2,
            spot_max_price_percentage=80,
        )
        try:
            yield mode
        finally:
            # `preserve_bastion` defaults to True, so cleanup alone keeps the
            # stack -- and its IAM role, which `bastion.yml` deliberately does
            # not name, outlives it. substrate gives an unnamed resource its
            # logical ID verbatim (substrate#560), and `BastionHostRole` is
            # account-global, so the next test in the file met
            # `EntityAlreadyExists`.
            #
            # This was invisible until substrate 0.92.0. Delete-by-ARN silently
            # no-op'd (substrate#544) and DeleteStack left the stack's resources
            # standing (substrate#518), so no teardown here removed anything and
            # the collision was attributed entirely to #560. Both are fixed now,
            # which makes the fixture's own omission the operative cause.
            mode.preserve_bastion = False
            mode.cleanup_infrastructure()

    def test_initialize_deploys_the_bastion_stack(self, detached_mode):
        """The default bastion path deploys ``bastion.yml`` and reports its stack.

        This is the case moto could not run at all, and the reason the file moved:
        the template's ``AWS::EC2::Instance`` resolves its ``ImageId`` through an
        ``AWS::EC2::LaunchTemplate``, which moto's CloudFormation handler does not
        do.
        """
        detached_mode.initialize()

        assert detached_mode.initialized is True
        assert detached_mode.bastion_id
        # A stack ARN, not an `i-`: the bastion is stack-managed on this path.
        assert ":stack/" in detached_mode.bastion_id

    def test_submit_job_with_spot_fleet(self, detached_mode, workflow_id):
        """A submitted job hands the bastion its full spot-fleet configuration.

        The real ``bastion.yml`` is deployed rather than stubbed: patching
        ``get_cf_template`` out is what let #112 -- it raised
        ``ModuleNotFoundError`` on every call, and the templates never shipped in
        the wheel -- go unnoticed at all four of its call sites.
        """
        detached_mode.initialize()

        job_id = "test-job-1"
        resource_id = detached_mode.submit_job(job_id, "echo 'Hello, world!'", 1)

        assert resource_id in detached_mode.resources
        assert detached_mode.resources[resource_id]["job_id"] == job_id
        assert detached_mode.resources[resource_id]["status"] == STATUS_PENDING

        ssm = detached_mode.session.client("ssm")
        job_param = ssm.get_parameter(
            Name=f"/parsl/workflows/{workflow_id}/jobs/{job_id}"
        )
        status_param = ssm.get_parameter(
            Name=f"/parsl/workflows/{workflow_id}/status/{job_id}"
        )

        # The bastion polls these parameters and is the thing that launches the
        # fleet, so anything the fleet needs has to be in the job document.
        job_data = json.loads(job_param["Parameter"]["Value"])
        assert job_data["use_spot_fleet"] is True
        assert job_data["instance_types"] == ["t3.micro", "t3.small", "m5.large"]
        assert job_data["nodes_per_block"] == 2
        assert job_data["spot_max_price_percentage"] == 80

        assert (
            json.loads(status_param["Parameter"]["Value"])["status"] == STATUS_PENDING
        )

    def test_get_job_status_adopts_the_bastions_fleet_details(
        self, detached_mode, workflow_id
    ):
        """Status is read from SSM and the fleet details are copied into tracking.

        The bastion, not the provider, launches the fleet, so the fleet ID and its
        instance list only reach the provider through the status parameter.
        """
        detached_mode.initialize()
        job_id = "test-job-2"
        status_data = {
            "status": STATUS_RUNNING,
            "instance_id": "i-spot1",
            "fleet_request_id": "sfr-12345",
            "resource_type": RESOURCE_TYPE_SPOT_FLEET,
            "all_instance_ids": ["i-spot1", "i-spot2"],
        }
        detached_mode.session.client("ssm").put_parameter(
            Name=f"/parsl/workflows/{workflow_id}/status/{job_id}",
            Value=json.dumps(status_data),
            Type="String",
        )

        resource_id = f"spot-fleet-{job_id}"
        detached_mode.resources = {
            resource_id: {
                "job_id": job_id,
                "status": STATUS_PENDING,
                "type": RESOURCE_TYPE_EC2,
            }
        }

        status = detached_mode.get_job_status([resource_id])

        assert status[resource_id] == STATUS_RUNNING
        resource = detached_mode.resources[resource_id]
        assert resource["status"] == STATUS_RUNNING
        assert resource["fleet_request_id"] == "sfr-12345"
        assert resource["resource_type"] == RESOURCE_TYPE_SPOT_FLEET
        assert resource["all_instance_ids"] == ["i-spot1", "i-spot2"]

    def test_cancel_job_with_spot_fleet(self, detached_mode, workflow_id):
        """Cancelling writes the fleet IDs the bastion needs to tear down."""
        detached_mode.initialize()
        job_id = "test-job-3"
        detached_mode.session.client("ssm").put_parameter(
            Name=f"/parsl/workflows/{workflow_id}/jobs/{job_id}",
            Value=json.dumps({"command": 'echo "test"', "use_spot_fleet": True}),
            Type="String",
        )

        resource_id = f"spot-fleet-{job_id}"
        detached_mode.resources = {
            resource_id: {
                "job_id": job_id,
                "status": STATUS_RUNNING,
                "resource_type": RESOURCE_TYPE_SPOT_FLEET,
                "fleet_request_id": "sfr-12345",
                "all_instance_ids": ["i-spot1", "i-spot2"],
            }
        }

        status = detached_mode.cancel_jobs([resource_id])

        assert status[resource_id] == STATUS_CANCELED
        assert detached_mode.resources[resource_id]["status"] == STATUS_CANCELED

        cancel_param = detached_mode.session.client("ssm").get_parameter(
            Name=f"/parsl/workflows/{workflow_id}/cancel"
        )
        cancel_data = json.loads(cancel_param["Parameter"]["Value"])
        assert job_id in cancel_data["job_ids"]
        assert cancel_data["spot_fleet_jobs"][job_id] == "sfr-12345"

    def test_cleanup_deletes_the_fleet_and_its_ssm_records(
        self, detached_mode, workflow_id, substrate_network
    ):
        """Cleanup deletes the real fleet, then drops its SSM parameters.

        The fleet here is a genuine ``CreateFleet`` fleet, so the deletion is
        observable. The moto version patched ``boto3.client`` and asserted a
        ``MagicMock`` had received ``cancel_spot_fleet_requests`` -- the legacy API
        #86 removed, which ``cleanup_resources`` no longer calls, so the assertion
        held no matter what the code did. Deleting a fleet is also not optional
        for an instant fleet: it is what terminates the instances.
        """
        detached_mode.initialize()
        ec2 = detached_mode.session.client("ec2")

        template = ec2.create_launch_template(
            LaunchTemplateName=f"parsl-test-fleet-{uuid.uuid4().hex[:8]}",
            LaunchTemplateData={
                "ImageId": detached_mode.image_id,
                "InstanceType": "t3.micro",
            },
        )["LaunchTemplate"]
        fleet_id = ec2.create_fleet(
            Type="instant",
            LaunchTemplateConfigs=[
                {
                    "LaunchTemplateSpecification": {
                        "LaunchTemplateId": template["LaunchTemplateId"],
                        "Version": str(template["LatestVersionNumber"]),
                    },
                    "Overrides": [
                        {
                            "InstanceType": "t3.micro",
                            "SubnetId": substrate_network["subnet_id"],
                        }
                    ],
                }
            ],
            TargetCapacitySpecification={
                "TotalTargetCapacity": 1,
                "DefaultTargetCapacityType": "spot",
            },
        )["FleetId"]

        job_id = "test-job-4"
        ssm = detached_mode.session.client("ssm")
        for suffix, value in (
            ("jobs", {"command": "echo test"}),
            ("status", {"status": STATUS_RUNNING}),
        ):
            ssm.put_parameter(
                Name=f"/parsl/workflows/{workflow_id}/{suffix}/{job_id}",
                Value=json.dumps(value),
                Type="String",
            )

        resource_id = f"spot-fleet-{job_id}"
        detached_mode.resources = {
            resource_id: {
                "job_id": job_id,
                "status": STATUS_RUNNING,
                "resource_type": RESOURCE_TYPE_SPOT_FLEET,
                "fleet_request_id": fleet_id,
            }
        }

        detached_mode.cleanup_resources([resource_id])

        assert resource_id not in detached_mode.resources

        fleet = ec2.describe_fleets(FleetIds=[fleet_id])["Fleets"][0]
        assert fleet["FleetState"].startswith("deleted")

        for suffix in ("jobs", "status"):
            with pytest.raises(ssm.exceptions.ParameterNotFound):
                ssm.get_parameter(
                    Name=f"/parsl/workflows/{workflow_id}/{suffix}/{job_id}"
                )

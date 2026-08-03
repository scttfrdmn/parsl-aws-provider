"""Integration tests for operating modes using substrate.

These drive each mode's real lifecycle -- initialize, submit, status, cancel,
cleanup -- against the emulator, with no mocking of the mode itself.

Two things had kept the whole file from running. ``FileStateStore(file_path=...)``
omitted the required ``provider_id``, so every test errored during fixture setup;
and the assertions described the pre-#69 provider, which created its own VPC,
subnet and security group. It no longer does: the caller supplies them, so
``initialize()`` verifies rather than creates, and ``cleanup_infrastructure()``
leaves them alone -- deleting a caller's network would be the same class of bug as
the serverless security-group deletion fixed in #100. The old
``assert mode.vpc_id is None`` after cleanup asserted the opposite.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import os
import uuid

import pytest

from parsl_ephemeral_provider.modes.detached import DetachedMode
from parsl_ephemeral_provider.modes.serverless import ServerlessMode
from parsl_ephemeral_provider.modes.standard import StandardMode
from parsl_ephemeral_provider.state.file import FileStateStore
from tests.substrate_support import is_substrate_available

pytestmark = pytest.mark.skipif(
    not is_substrate_available(),
    reason="substrate not available - start with 'make substrate-up'",
)


@pytest.fixture
def provider_id():
    """Generate a unique provider ID for tests."""
    return f"test-provider-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def temp_state_store(tmp_path, provider_id):
    """Create a temporary state store for testing.

    ``provider_id`` is required (``state/file.py``): it is the key the document is
    written under, so a store without one cannot address its own state.
    """
    return FileStateStore(
        file_path=str(tmp_path / "state.json"), provider_id=provider_id
    )


@pytest.mark.integration
@pytest.mark.substrate
class TestStandardModeSubstrate:
    """Integration tests for StandardMode using substrate."""

    def test_initialize_and_cleanup(
        self, substrate_session, substrate_network, temp_state_store, provider_id
    ):
        """initialize() verifies the caller's network and builds a launch template."""
        mode = StandardMode(
            provider_id=provider_id,
            session=substrate_session,
            state_store=temp_state_store,
            region="us-east-1",
            instance_type="t3.micro",
            image_id="ami-12345678",
            vpc_id=substrate_network["vpc_id"],
            subnet_id=substrate_network["subnet_id"],
            security_group_id=substrate_network["security_group_id"],
        )

        try:
            mode.initialize()

            assert mode.initialized is True
            # The launch template is the mode's own resource, unlike the network.
            assert mode._launch_template_id is not None

            ec2 = substrate_session.client("ec2")
            templates = ec2.describe_launch_templates(
                LaunchTemplateIds=[mode._launch_template_id]
            )
            assert len(templates["LaunchTemplates"]) == 1

            assert os.path.exists(temp_state_store.file_path)

        finally:
            mode.cleanup_infrastructure()

        assert mode.initialized is False
        # The network belongs to the caller and must survive cleanup (#69).
        assert mode.vpc_id == substrate_network["vpc_id"]
        ec2 = substrate_session.client("ec2")
        assert len(ec2.describe_vpcs(VpcIds=[mode.vpc_id])["Vpcs"]) == 1

    def test_submit_job_and_status(
        self, substrate_session, substrate_network, temp_state_store, provider_id
    ):
        """A submitted job launches a tracked instance and can be cancelled."""
        mode = StandardMode(
            provider_id=provider_id,
            session=substrate_session,
            state_store=temp_state_store,
            region="us-east-1",
            instance_type="t3.micro",
            image_id="ami-12345678",
            vpc_id=substrate_network["vpc_id"],
            subnet_id=substrate_network["subnet_id"],
            security_group_id=substrate_network["security_group_id"],
        )

        try:
            mode.initialize()

            job_id = f"test-job-{uuid.uuid4().hex[:8]}"
            resource_id = mode.submit_job(job_id, "echo hello", 1)

            assert resource_id in mode.resources
            assert mode.resources[resource_id]["job_id"] == job_id

            assert mode.get_job_status([resource_id])[resource_id] == "RUNNING"
            assert mode.cancel_jobs([resource_id])[resource_id] == "CANCELED"

            mode.cleanup_resources([resource_id])
            assert resource_id not in mode.resources

        finally:
            mode.cleanup_infrastructure()


@pytest.mark.integration
@pytest.mark.substrate
class TestDetachedModeSubstrate:
    """Integration tests for DetachedMode using substrate.

    ``bastion_host_type="direct"`` throughout: the CloudFormation bastion is not
    reachable here, since substrate serves no ``cloudformation`` endpoint. That
    path is covered against real AWS in ``tests/aws/``.
    """

    def _mode(self, session, network, store, provider_id, **overrides):
        return DetachedMode(
            provider_id=provider_id,
            session=session,
            state_store=store,
            region="us-east-1",
            instance_type="t3.micro",
            image_id="ami-12345678",
            workflow_id=f"test-workflow-{uuid.uuid4().hex[:8]}",
            bastion_instance_type="t3.micro",
            bastion_host_type="direct",
            vpc_id=network["vpc_id"],
            subnet_id=network["subnet_id"],
            security_group_id=network["security_group_id"],
            **overrides,
        )

    def test_initialize_and_cleanup(
        self, substrate_session, substrate_network, temp_state_store, provider_id
    ):
        """A direct-mode bastion launches with no key pair, and is torn down.

        No ``key_name`` is passed, which is the ordinary configuration -- SSM is
        how you reach the bastion, and it needs no key. That combination used to
        fail botocore's parameter validation before any API call, so this mode
        could not start at all (#158).
        """
        mode = self._mode(
            substrate_session, substrate_network, temp_state_store, provider_id
        )

        try:
            mode.initialize()

            assert mode.initialized is True
            assert mode.bastion_id is not None

            ec2 = substrate_session.client("ec2")
            reservations = ec2.describe_instances(InstanceIds=[mode.bastion_id])
            assert len(reservations["Reservations"]) == 1

        finally:
            mode.preserve_bastion = False
            mode.cleanup_infrastructure()

        assert mode.initialized is False
        assert mode.bastion_id is None
        # Again, the caller's network outlives the mode.
        assert mode.vpc_id == substrate_network["vpc_id"]

    def test_submit_job_and_status(
        self, substrate_session, substrate_network, temp_state_store, provider_id
    ):
        """The bastion records the job in SSM Parameter Store when submitted."""
        mode = self._mode(
            substrate_session, substrate_network, temp_state_store, provider_id
        )

        try:
            mode.initialize()

            job_id = f"test-job-{uuid.uuid4().hex[:8]}"
            resource_id = mode.submit_job(job_id, "echo hello", 1)

            assert resource_id in mode.resources
            assert mode.resources[resource_id]["job_id"] == job_id

            # Jobs are handed to the bastion through Parameter Store, so the
            # parameter existing is what proves the dispatch happened.
            ssm = substrate_session.client("ssm")
            prefix = f"/parsl/workflows/{mode.workflow_id}"
            described = ssm.describe_parameters(
                ParameterFilters=[
                    {"Key": "Name", "Option": "BeginsWith", "Values": [prefix]}
                ]
            )
            assert described["Parameters"], f"no SSM parameters under {prefix}"

            assert resource_id in mode.get_job_status([resource_id])
            assert resource_id in mode.cancel_jobs([resource_id])

        finally:
            mode.preserve_bastion = False
            mode.cleanup_infrastructure()


@pytest.mark.integration
@pytest.mark.substrate
class TestServerlessModeSubstrate:
    """Integration tests for ServerlessMode using substrate."""

    def test_lambda_mode_needs_no_network(
        self, substrate_session, temp_state_store, provider_id
    ):
        """Lambda-only serverless initializes without any caller network.

        Functions run in the Lambda-managed VPC, which is why the provider's
        network guard exempts this combination.
        """
        mode = ServerlessMode(
            provider_id=provider_id,
            session=substrate_session,
            state_store=temp_state_store,
            region="us-east-1",
            worker_type="lambda",
            lambda_memory=128,
            lambda_timeout=30,
        )

        try:
            mode.initialize()

            assert mode.initialized is True
            assert mode.vpc_id is None
            assert mode.subnet_id is None
            assert mode.security_group_id is None

        finally:
            mode.cleanup_infrastructure()

        assert mode.initialized is False

    def test_ecs_mode_uses_the_supplied_network(
        self, substrate_session, substrate_network, temp_state_store, provider_id
    ):
        """ECS/Fargate tasks run in the caller's subnet, so the IDs are kept."""
        mode = ServerlessMode(
            provider_id=provider_id,
            session=substrate_session,
            state_store=temp_state_store,
            region="us-east-1",
            worker_type="ecs",
            ecs_task_cpu=256,
            ecs_task_memory=512,
            ecs_container_image="python:3.12-slim",
            vpc_id=substrate_network["vpc_id"],
            subnet_id=substrate_network["subnet_id"],
            security_group_id=substrate_network["security_group_id"],
        )

        try:
            mode.initialize()

            assert mode.initialized is True
            assert mode.vpc_id == substrate_network["vpc_id"]
            assert mode.subnet_id == substrate_network["subnet_id"]
            assert mode.security_group_id == substrate_network["security_group_id"]

        finally:
            mode.cleanup_infrastructure()

    def test_submit_lambda_job(
        self,
        substrate_session,
        temp_state_store,
        provider_id,
        requires_cloudformation,
    ):
        """Submitting a Lambda job provisions its function through a stack.

        Skipped rather than failed while the emulator serves no CloudFormation:
        ``_submit_lambda_job`` deploys the worker as a stack, so this exercises the
        emulator's gap, not the provider. Covered for real in ``tests/aws/``.
        """
        mode = ServerlessMode(
            provider_id=provider_id,
            session=substrate_session,
            state_store=temp_state_store,
            region="us-east-1",
            worker_type="lambda",
            lambda_memory=128,
            lambda_timeout=30,
        )

        try:
            mode.initialize()

            job_id = f"test-job-{uuid.uuid4().hex[:8]}"
            resource_id = mode.submit_job(job_id, "echo hello", 1)

            assert resource_id in mode.resources
            assert mode.resources[resource_id]["job_id"] == job_id
            assert mode.resources[resource_id]["worker_type"] == "lambda"

            assert resource_id in mode.get_job_status([resource_id])
            assert resource_id in mode.cancel_jobs([resource_id])

            mode.cleanup_resources([resource_id])
            assert resource_id not in mode.resources

        finally:
            mode.cleanup_infrastructure()

"""Integration tests for state persistence in workflow scenarios.

Each test runs a real mode against the emulator, saves, builds a *second* mode
object from the same state store, and asserts the second one knows what the first
one created. That second object is the whole point: it stands in for a restarted
driver, and anything it fails to recover is a resource nothing will ever clean up.

The three mode round-trips here used to patch ``_create_vpc``, ``_create_subnet``,
``_create_security_group``, ``_create_ec2_instance``, ``_create_bastion_host``,
``_create_ssm_parameter`` and five ``_delete_*`` counterparts. None of those
methods exists on any mode, so ``patch.object`` raised ``AttributeError`` before
reaching an assertion -- they described the pre-#69 design, where a mode created
its own network. The serverless one also called ``mock_open()`` without importing
it.

Because the mocks never applied, the assertions behind them were never checked,
and two of them were wrong in a way that matters:

* ``cleanup_infrastructure()`` was expected to leave ``vpc_id``/``subnet_id``/
  ``security_group_id`` as ``None``. Since #69 those IDs belong to the *caller*;
  the mode neither creates nor deletes them, and nulling them would make a
  resumed provider unusable. Verified: they survive cleanup.
* The state file was expected to be gone or empty after cleanup. Cleanup *saves*
  -- ``initialized: false`` with an empty ``resources`` map -- because deleting
  the document is the provider's job at shutdown (``delete_state``), and a
  cleanup that erased it would strand any resource the pass could not release.

What is asserted instead is what each mode actually persists: the tracking map,
the launch template (#85), the bastion (#111), and the two ownership flags that
decide whether cleanup may delete a shared resource (#132, #100).

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import json
import os
import time
import uuid
from unittest.mock import patch

import pytest

from parsl_ephemeral_provider.modes.detached import DetachedMode
from parsl_ephemeral_provider.modes.serverless import ServerlessMode
from parsl_ephemeral_provider.modes.standard import StandardMode
from parsl_ephemeral_provider.state.file import FileStateStore
from tests.substrate_support import is_substrate_available

# A marker only *selects* tests; the skipif is what makes a plain
# `pytest tests/integration` skip rather than error when nothing is listening.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.substrate,
    pytest.mark.skipif(
        not is_substrate_available(),
        reason="substrate not available - start with 'make substrate-up'",
    ),
]


@pytest.fixture
def provider_id():
    """The identity a resumed provider is recognized by.

    ``load_state`` ignores any document whose ``provider_id`` differs, so both
    halves of a round-trip must share this and each test must have its own.
    """
    return f"test-provider-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def state_store(tmp_path, provider_id):
    """A real file-backed store in the test's own directory.

    Real, not mocked: the point of these tests is that a document written by one
    object can be read by another, and a MagicMock store would satisfy every
    assertion while serializing nothing. ``state/file.py`` also takes an
    ``fcntl.flock`` on the handle, which a mock cannot provide -- it raises
    "fileno() returned a non-integer".
    """
    return FileStateStore(
        file_path=str(tmp_path / "state.json"), provider_id=provider_id
    )


def _saved_mode_state(state_store):
    """Read the mode's section of the state document straight off disk.

    Used where a claim is about the document rather than the restored object --
    the two can disagree, which is the failure these tests exist to catch.
    """
    with open(state_store.file_path) as handle:
        return json.load(handle)["_states"]["mode"]


class TestStandardModeStatePersistence:
    """StandardMode: what a restarted driver recovers, and what it must not lose."""

    def _build(self, session, state_store, provider_id, network, **overrides):
        kwargs = dict(
            provider_id=provider_id,
            session=session,
            state_store=state_store,
            region=session.region_name,
            instance_type="t3.micro",
            image_id="ami-12345678",
            vpc_id=network["vpc_id"],
            subnet_id=network["subnet_id"],
            security_group_id=network["security_group_id"],
        )
        kwargs.update(overrides)
        return StandardMode(**kwargs)

    def test_resources_and_the_launch_template_survive_a_restart(
        self, substrate_session, substrate_network, state_store, provider_id
    ):
        """A second mode object recovers the tracking map and the template.

        The template ID is the load-bearing part: a resumed provider that did not
        recover it built a second template and leaked the first, since nothing
        else records it (#85). The instances are real -- ``submit_job`` launches
        them in the emulator -- so a document that describes them wrongly shows
        up here rather than in production.
        """
        mode = self._build(
            substrate_session, state_store, provider_id, substrate_network
        )
        mode.initialize()
        resource_ids = [
            mode.submit_job(f"job-{i}", f"echo 'Job {i}'", 1) for i in range(3)
        ]
        assert len(mode.resources) == 3
        mode.save_state()

        resumed = self._build(
            substrate_session, state_store, provider_id, substrate_network
        )
        try:
            assert resumed.load_state() is True

            assert resumed.initialized
            assert set(resumed.resources) == set(resource_ids)
            for index, resource_id in enumerate(resource_ids):
                assert resumed.resources[resource_id]["job_id"] == f"job-{index}"
            # Recovered, not rebuilt: same template, and it still exists.
            assert resumed._launch_template_id == mode._launch_template_id
            assert resumed._launch_template_version is not None
            substrate_session.client("ec2").describe_launch_templates(
                LaunchTemplateIds=[resumed._launch_template_id]
            )
        finally:
            resumed.cleanup_infrastructure()

    def test_cleanup_releases_what_the_mode_made_and_keeps_the_rest(
        self, substrate_session, substrate_network, state_store, provider_id
    ):
        """The caller's network outlives the mode; the mode's template does not.

        Asserted from the *resumed* object, so ownership is shown to survive
        persistence: a provider restarted after a crash must still be able to
        clean up, and must still know which resources are not its to delete.
        """
        mode = self._build(
            substrate_session, state_store, provider_id, substrate_network
        )
        mode.initialize()
        mode.submit_job("job-0", "echo hello", 1)
        mode.save_state()
        template_id = mode._launch_template_id

        resumed = self._build(
            substrate_session, state_store, provider_id, substrate_network
        )
        resumed.load_state()
        resumed.cleanup_infrastructure()

        assert not resumed.initialized
        assert resumed.resources == {}
        assert resumed._launch_template_id is None

        ec2 = substrate_session.client("ec2")
        templates = ec2.describe_launch_templates()["LaunchTemplates"]
        assert template_id not in [t["LaunchTemplateId"] for t in templates]

        # The three IDs were supplied by the caller (#69). The mode did not
        # create them, so it must not delete them or forget them.
        assert resumed.vpc_id == substrate_network["vpc_id"]
        assert resumed.subnet_id == substrate_network["subnet_id"]
        assert resumed.security_group_id == substrate_network["security_group_id"]
        ec2.describe_vpcs(VpcIds=[substrate_network["vpc_id"]])
        ec2.describe_subnets(SubnetIds=[substrate_network["subnet_id"]])

        # Cleanup saves rather than deletes: the document now says "nothing
        # running", which is what a resumed provider needs to read. Deleting it
        # is the provider's job at shutdown.
        saved = _saved_mode_state(state_store)
        assert saved["initialized"] is False
        assert saved["resources"] == {}
        assert saved["launch_template_id"] is None

    def test_another_providers_document_is_not_adopted(
        self, substrate_session, substrate_network, state_store, provider_id
    ):
        """State is keyed by provider ID, so one provider cannot inherit another's.

        Without this guard a second provider sharing a state file would adopt
        resources it did not create and terminate them on its own shutdown.
        """
        mode = self._build(
            substrate_session, state_store, provider_id, substrate_network
        )
        mode.initialize()
        try:
            mode.submit_job("job-0", "echo hello", 1)
            mode.save_state()

            stranger = self._build(
                substrate_session,
                state_store,
                "a-different-provider",
                substrate_network,
            )

            assert stranger.load_state() is False
            assert stranger.resources == {}
            assert not stranger.initialized
        finally:
            mode.cleanup_infrastructure()


class TestDetachedModeStatePersistence:
    """DetachedMode: the bastion and the workflow it serves must both come back."""

    def _build(self, session, state_store, provider_id, workflow_id, network):
        return DetachedMode(
            provider_id=provider_id,
            session=session,
            state_store=state_store,
            region=session.region_name,
            instance_type="t3.micro",
            image_id="ami-12345678",
            workflow_id=workflow_id,
            # Not the "cloudformation" default: that path needs an endpoint
            # substrate does not serve. The direct path uses run_instances, so
            # the bastion here is a real instance whose fate can be checked.
            bastion_host_type="direct",
            bastion_instance_type="t3.micro",
            preserve_bastion=False,
            vpc_id=network["vpc_id"],
            subnet_id=network["subnet_id"],
            security_group_id=network["security_group_id"],
        )

    def test_the_bastion_and_workflow_id_survive_a_restart(
        self, substrate_session, substrate_network, state_store, provider_id
    ):
        """Losing either one orphans a running instance.

        The bastion is a long-lived instance the driver does not hold a handle
        to, and the workflow ID is the SSM prefix its work orders live under -- a
        resumed provider that recovered neither would launch a second bastion and
        bill for the first forever.

        Both attributes are cleared on the resumed object before loading, so a
        constructor value cannot masquerade as a restored one.
        """
        workflow_id = f"test-workflow-{uuid.uuid4().hex[:8]}"
        mode = self._build(
            substrate_session, state_store, provider_id, workflow_id, substrate_network
        )
        mode.initialize()
        assert mode.bastion_id is not None

        resource_ids = [
            mode.submit_job(f"job-{i}", f"echo 'Job {i}'", 1) for i in range(3)
        ]
        # The bastion is tracked alongside the jobs, so it is cleaned up with them.
        assert len(mode.resources) == len(resource_ids) + 1
        mode.save_state()

        # The work orders the bastion polls are in SSM, keyed by workflow ID --
        # which is why losing that ID is as bad as losing the instance.
        ssm = substrate_session.client("ssm")
        job_id = mode.resources[resource_ids[0]]["job_id"]
        order = json.loads(
            ssm.get_parameter(Name=f"/parsl/workflows/{workflow_id}/jobs/{job_id}")[
                "Parameter"
            ]["Value"]
        )
        assert order["command"] == "echo 'Job 0'"

        resumed = self._build(
            substrate_session, state_store, provider_id, workflow_id, substrate_network
        )
        resumed.bastion_id = None
        resumed.workflow_id = "never-restored"

        assert resumed.load_state() is True
        assert resumed.initialized
        assert resumed.bastion_id == mode.bastion_id
        assert resumed.workflow_id == workflow_id
        assert set(resumed.resources) == set(mode.resources)

        resumed.cleanup_infrastructure()

        assert not resumed.initialized
        assert resumed.bastion_id is None
        assert resumed.resources == {}
        # preserve_bastion=False, so the instance really goes.
        described = substrate_session.client("ec2").describe_instances(
            InstanceIds=[mode.bastion_id]
        )
        state = described["Reservations"][0]["Instances"][0]["State"]["Name"]
        assert state in ("shutting-down", "terminated")
        # And the caller's network is untouched, as in standard mode.
        assert resumed.vpc_id == substrate_network["vpc_id"]


class TestServerlessModeStatePersistence:
    """ServerlessMode: the code bucket, and who is allowed to delete it."""

    def _build(self, session, state_store, provider_id, **overrides):
        kwargs = dict(
            provider_id=provider_id,
            session=session,
            state_store=state_store,
            region=session.region_name,
            # Lambda needs none of the three network IDs, so this is also the one
            # mode that can be exercised without a VPC.
            worker_type="lambda",
            lambda_memory=128,
            lambda_timeout=30,
        )
        kwargs.update(overrides)
        return ServerlessMode(**kwargs)

    def test_a_bucket_the_mode_created_is_deleted_after_a_restart(
        self, substrate_session, state_store, provider_id
    ):
        """Ownership has to survive persistence or the bucket leaks.

        ``_owns_lambda_code_bucket`` is set at creation time, in memory. A driver
        that restarted between the create and the shutdown would clean up with
        the flag back at its ``False`` default and leave the bucket standing --
        so the flag is persisted, and this asserts the resumed object acts on it.

        ``_submit_lambda_job`` is patched out because dispatch goes through
        CloudFormation, which substrate does not serve. The tracking record is
        written by ``submit_job`` before dispatch (#115), which is the part under
        test here.
        """
        mode = self._build(substrate_session, state_store, provider_id)
        mode.initialize()

        with patch.object(mode, "_submit_lambda_job"):
            resource_ids = [
                mode.submit_job(f"job-{i}", f"echo 'Job {i}'", 1) for i in range(3)
            ]
        bucket = mode._ensure_lambda_code_bucket()
        assert mode._owns_lambda_code_bucket is True
        mode.save_state()

        resumed = self._build(substrate_session, state_store, provider_id)
        assert resumed._lambda_code_bucket is None
        assert resumed._owns_lambda_code_bucket is False

        assert resumed.load_state() is True
        assert resumed.initialized
        assert set(resumed.resources) == set(resource_ids)
        for index, resource_id in enumerate(resource_ids):
            record = resumed.resources[resource_id]
            assert record["job_id"] == f"job-{index}"
            # The routing decision is in the document because it determines
            # whether the network IDs were required at all (#118).
            assert record["worker_type"] == "lambda"
        assert resumed._lambda_code_bucket == bucket
        assert resumed._owns_lambda_code_bucket is True

        resumed.cleanup_infrastructure()

        assert not resumed.initialized
        assert resumed.resources == {}
        buckets = [
            b["Name"] for b in substrate_session.client("s3").list_buckets()["Buckets"]
        ]
        assert bucket not in buckets

    def test_a_bucket_the_caller_supplied_survives_cleanup(
        self, substrate_session, state_store, provider_id
    ):
        """The other side of the gate, and the reason it exists.

        A caller-supplied bucket may hold more than this provider's code, and
        deleting it would destroy data the provider never owned -- the same
        hazard class as the shared security group deleted in #100. Checked
        through a restart, because that is where ownership is easiest to lose.
        """
        s3 = substrate_session.client("s3")
        bucket = f"caller-owned-{uuid.uuid4().hex[:8]}"
        s3.create_bucket(
            Bucket=bucket,
            CreateBucketConfiguration={
                "LocationConstraint": substrate_session.region_name
            },
        )
        try:
            mode = self._build(
                substrate_session, state_store, provider_id, lambda_code_bucket=bucket
            )
            mode.initialize()
            assert mode._ensure_lambda_code_bucket() == bucket
            assert mode._owns_lambda_code_bucket is False
            mode.save_state()

            resumed = self._build(
                substrate_session, state_store, provider_id, lambda_code_bucket=bucket
            )
            assert resumed.load_state() is True
            assert resumed._owns_lambda_code_bucket is False

            resumed.cleanup_infrastructure()

            assert bucket in [b["Name"] for b in s3.list_buckets()["Buckets"]]
        finally:
            s3.delete_bucket(Bucket=bucket)


class TestInterruptionPersistence:
    """A reclaim marker must outlive the driver that recorded it."""

    def test_an_interruption_survives_a_restart(
        self, substrate_session, substrate_network, state_store, provider_id
    ):
        """An interrupted block must still read as interrupted after a restart.

        A reclaim is precisely the event most likely to be followed by the driver
        going away, so a marker held only in memory would be lost exactly when it
        matters. If the restored block came back RUNNING, the provider would keep
        waiting on capacity AWS already took, and if it came back COMPLETED --
        which is what ``EC2_STATUS_MAPPING`` makes of a ``shutting-down``
        instance -- its tasks would be silently dropped instead of re-run (#137).

        This used to test a checkpoint/recovery API that #137 deleted: its entry
        point was ``register_task(task_id, instance_id)``, and a Parsl provider is
        never told a task ID. What replaces it is the response that *is*
        implementable at this layer -- mark the block, let Parsl's own ``retries``
        re-run the tasks -- checked across the persistence boundary this file
        exists to cover.
        """
        instance_id = f"i-spot-{uuid.uuid4().hex[:12]}"

        def build_mode():
            return StandardMode(
                provider_id=provider_id,
                session=substrate_session,
                state_store=state_store,
                region=substrate_session.region_name,
                instance_type="t3.micro",
                image_id="ami-12345678",
                vpc_id=substrate_network["vpc_id"],
                subnet_id=substrate_network["subnet_id"],
                security_group_id=substrate_network["security_group_id"],
                use_spot=True,
                spot_interruption_handling=True,
            )

        mode = build_mode()
        try:
            mode.resources[instance_id] = {
                "type": "ec2",
                "job_id": f"test-job-{uuid.uuid4().hex[:8]}",
                "status": "RUNNING",
                "is_spot": True,
                "created_at": time.time(),
            }
            # The real registration path, as used by every spot launch.
            mode._register_spot_instance(instance_id)

            # Drive the reclaim through the queue the monitoring thread drains,
            # rather than reaching for the handler directly -- substrate emulates
            # neither EventBridge nor a spot ``InstanceLifecycle``, so this is the
            # only seam that can originate one here.
            mode.spot_interruption_monitor.event_queue.put(
                (
                    "instance",
                    instance_id,
                    {"InstanceId": instance_id, "InstanceAction": "terminate"},
                )
            )
            mode.spot_interruption_monitor._process_interruption_events()

            assert mode.resources[instance_id]["status"] == "INTERRUPTED"
            mode.save_state()
        finally:
            if mode.spot_interruption_monitor:
                mode.spot_interruption_monitor.stop_monitoring()

        mode2 = build_mode()
        try:
            assert mode2.load_state() is True

            assert mode2.resources[instance_id]["status"] == "INTERRUPTED"
            # And the reason came back with it, so a FAILED block is diagnosable
            # without going to CloudTrail.
            assert mode2.resources[instance_id]["interruption_event"] == {
                "InstanceId": instance_id,
                "InstanceAction": "terminate",
            }
            # Still sticky on the far side: get_job_status must not re-derive a
            # healthy-looking status from the instance EC2 is tearing down.
            assert mode2.get_job_status([instance_id])[instance_id] == "INTERRUPTED"
        finally:
            if mode2.spot_interruption_monitor:
                mode2.spot_interruption_monitor.stop_monitoring()


def test_the_state_file_lives_where_the_caller_put_it(state_store, tmp_path):
    """A sanity check on the fixture the whole file depends on.

    If the store wrote somewhere else, every round-trip above would still pass by
    reading back an object it never persisted.
    """
    state_store.save_state("mode", {"provider_id": state_store.provider_id})

    assert os.path.dirname(state_store.file_path) == str(tmp_path)
    assert os.path.exists(state_store.file_path)

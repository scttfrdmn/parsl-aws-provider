"""Integration tests for the spot interruption response, across all three modes.

Detection is covered in ``test_spot_interruption_substrate.py``; this file covers
what happens *after* a reclaim is detected — the mode marking the block so the
provider reports FAILED and Parsl re-runs its tasks. That path is per-mode, and
the wiring differs in each: StandardMode registers each spot instance as it is
created, DetachedMode re-registers them from state after a restart, and
ServerlessMode registers a fleet whose ID is not its own resource key.

Interruptions are driven by putting an event on ``monitor.event_queue`` and
calling ``_process_interruption_events()``. That is the seam the monitoring thread
itself uses, and it is the only workable one here: substrate does not emulate
``events``, and it never sets ``InstanceLifecycle``, so neither the EventBridge
warning nor the EC2-state poll can originate an interruption against the
emulator. Everything downstream of the queue is the real code.

The checkpoint/recovery API the previous version of this file tested was deleted
in #137. It could not work at this layer: its entry point was
``register_task(task_id, instance_id)``, and a Parsl provider is never told a
task ID — it is handed a command and returns a block ID.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import time
import uuid

import pytest

from parsl_ephemeral_aws.compute.spot_interruption import SpotInterruptionMonitor
from parsl_ephemeral_aws.constants import (
    RESOURCE_TYPE_EC2,
    RESOURCE_TYPE_SPOT_FLEET,
    STATUS_INTERRUPTED,
    STATUS_RUNNING,
)
from parsl_ephemeral_aws.modes.detached import DetachedMode
from parsl_ephemeral_aws.modes.serverless import ServerlessMode
from parsl_ephemeral_aws.modes.standard import StandardMode
from parsl_ephemeral_aws.state.file import FileStateStore
from tests.substrate_support import cleanup_substrate_vpc, setup_substrate_vpc

pytestmark = [pytest.mark.integration, pytest.mark.substrate]


@pytest.fixture
def provider_id():
    """A unique provider ID, so concurrent runs cannot collide on state keys."""
    return f"test-provider-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def state_store(tmp_path, provider_id):
    """A file-backed store, which is what makes the restart test possible."""
    return FileStateStore(
        file_path=str(tmp_path / "state.json"), provider_id=provider_id
    )


@pytest.fixture
def network(substrate_session):
    """Pre-provisioned VPC/subnet/SG — required of every mode since #69."""
    ids = setup_substrate_vpc()
    yield ids
    cleanup_substrate_vpc(ids["vpc_id"])


def _interrupt_instance(monitor, instance_id):
    """Deliver an instance reclaim through the queue the monitor thread drains."""
    monitor.event_queue.put(
        (
            "instance",
            instance_id,
            {
                "InstanceId": instance_id,
                "InstanceAction": "terminate",
                "NoticeTime": time.time(),
                "Source": "eventbridge",
            },
        )
    )
    monitor._process_interruption_events()


def _interrupt_fleet(monitor, fleet_id, instance_ids):
    """Deliver a fleet reclaim through the queue the monitor thread drains."""
    monitor.event_queue.put(
        (
            "fleet",
            fleet_id,
            instance_ids,
            {
                "FleetRequestId": fleet_id,
                "InstanceAction": "terminate",
                "NoticeTime": time.time(),
                "Source": "eventbridge",
            },
        )
    )
    monitor._process_interruption_events()


class TestStandardModeInterruption:
    """StandardMode registers each spot instance as it creates it."""

    @pytest.fixture
    def mode(self, substrate_session, state_store, provider_id, network):
        mode = StandardMode(
            provider_id=provider_id,
            session=substrate_session,
            state_store=state_store,
            region="us-east-1",
            instance_type="t3.micro",
            image_id="ami-12345678",
            vpc_id=network["vpc_id"],
            subnet_id=network["subnet_id"],
            security_group_id=network["security_group_id"],
            use_spot=True,
            spot_interruption_handling=True,
        )
        yield mode
        if mode.spot_interruption_monitor:
            mode.spot_interruption_monitor.stop_monitoring()

    def test_a_reclaim_marks_the_block_the_provider_reports_on(self, mode):
        """The whole point of #137, through a real mode rather than a stub.

        Before this, the reclaim was *invisible* rather than merely unhandled:
        EC2 reports a reclaimed instance as ``shutting-down``, which
        ``EC2_STATUS_MAPPING`` renders COMPLETED, so the block reported success
        and its tasks were silently dropped instead of being re-run.
        """
        instance_id = f"i-{uuid.uuid4().hex[:16]}"
        mode.resources[instance_id] = {
            "type": RESOURCE_TYPE_EC2,
            "job_id": "job-1",
            "status": STATUS_RUNNING,
            "is_spot": True,
        }

        # The real registration call, made by _create_spot_instance on every
        # spot launch — not a hand-wired handler.
        mode._register_spot_instance(instance_id)
        assert instance_id in mode.spot_interruption_monitor.instance_handlers

        _interrupt_instance(mode.spot_interruption_monitor, instance_id)

        assert mode.resources[instance_id]["status"] == STATUS_INTERRUPTED
        # And it survives the next poll, which is what makes the marker mean
        # anything: get_job_status must not re-derive COMPLETED from the
        # shutting-down instance.
        assert mode.get_job_status([instance_id])[instance_id] == STATUS_INTERRUPTED

    def test_an_untracked_instance_does_not_break_the_monitor(self, mode):
        """A warning can arrive for an instance the mode no longer tracks.

        The monitor polls on its own thread, so a job that finished and was
        cleaned up between the poll and the dispatch is routine. Raising here
        would kill the monitoring thread and take every *other* instance's
        interruption handling down with it.
        """
        instance_id = f"i-{uuid.uuid4().hex[:16]}"
        mode._register_spot_instance(instance_id)

        _interrupt_instance(mode.spot_interruption_monitor, instance_id)

        assert instance_id not in mode.resources


class TestDetachedModeInterruption:
    """DetachedMode's registrations have to survive a restart.

    Its whole reason to exist is that the driver can go away and come back, so a
    registration held only in the monitor's in-memory dict would be lost exactly
    when it is most needed.
    """

    def _mode(self, session, state_store, provider_id, network, workflow_id):
        return DetachedMode(
            provider_id=provider_id,
            session=session,
            state_store=state_store,
            region="us-east-1",
            instance_type="t3.micro",
            image_id="ami-12345678",
            vpc_id=network["vpc_id"],
            subnet_id=network["subnet_id"],
            security_group_id=network["security_group_id"],
            workflow_id=workflow_id,
            bastion_instance_type="t3.micro",
            bastion_host_type="direct",
            use_spot=True,
            spot_interruption_handling=True,
        )

    def test_registrations_are_rebuilt_from_state_after_a_restart(
        self, substrate_session, state_store, provider_id, network
    ):
        instance_id = f"i-{uuid.uuid4().hex[:16]}"
        workflow_id = f"test-workflow-{uuid.uuid4().hex[:8]}"

        first = self._mode(
            substrate_session, state_store, provider_id, network, workflow_id
        )
        try:
            first.resources[instance_id] = {
                "type": RESOURCE_TYPE_EC2,
                "job_id": "job-1",
                "status": STATUS_RUNNING,
                "is_spot": True,
            }
            first.save_state()
        finally:
            if first.spot_interruption_monitor:
                first.spot_interruption_monitor.stop_monitoring()

        second = self._mode(
            substrate_session, state_store, provider_id, network, workflow_id
        )
        try:
            assert second.load_state() is True

            # Re-registered from the persisted resource record, without the
            # instance ever being launched again.
            assert instance_id in second.spot_interruption_monitor.instance_handlers

            _interrupt_instance(second.spot_interruption_monitor, instance_id)

            assert second.resources[instance_id]["status"] == STATUS_INTERRUPTED
            # This mode reads status from a document the worker writes, so a
            # reclaimed instance cannot report its own reclaim — it just stops
            # updating. Re-deriving would overwrite the marker with a stale
            # RUNNING.
            assert (
                second.get_job_status([instance_id])[instance_id] == STATUS_INTERRUPTED
            )
        finally:
            if second.spot_interruption_monitor:
                second.spot_interruption_monitor.stop_monitoring()


class TestServerlessModeFleetInterruption:
    """A fleet ID is never the resource key, in any mode.

    ServerlessMode keys a fleet-backed job by ``serverless-<job_id>`` and records
    the fleet as a ``fleet_request_id`` *field*, so the direct
    ``resources[fleet_id]`` lookup ``handle_fleet_interruption`` used to do missed
    every single time — the block Parsl holds kept reporting healthy straight
    through the reclaim. This is that gap, closed and pinned.
    """

    @pytest.fixture
    def mode(self, substrate_session, state_store, provider_id, network):
        mode = ServerlessMode(
            provider_id=provider_id,
            session=substrate_session,
            state_store=state_store,
            region="us-east-1",
            worker_type="ecs",
            vpc_id=network["vpc_id"],
            subnet_id=network["subnet_id"],
            security_group_id=network["security_group_id"],
            use_spot=True,
            use_spot_fleet=True,
            spot_interruption_handling=True,
            instance_types=["t3.micro", "t3.small"],
        )
        yield mode
        if mode.spot_interruption_monitor:
            mode.spot_interruption_monitor.stop_monitoring()

    def test_a_fleet_reclaim_reaches_the_block_that_records_the_fleet(self, mode):
        fleet_id = f"fleet-{uuid.uuid4().hex[:12]}"
        resource_id = "serverless-job-1"
        instance_ids = [f"i-{uuid.uuid4().hex[:16]}" for _ in range(2)]

        mode.resources[resource_id] = {
            "job_id": "job-1",
            "resource_type": RESOURCE_TYPE_SPOT_FLEET,
            "use_spot_fleet": True,
            "fleet_request_id": fleet_id,
            "instance_ids": instance_ids,
            "status": STATUS_RUNNING,
        }
        mode.spot_interruption_monitor.register_fleet(
            fleet_id, mode.handle_fleet_interruption
        )

        _interrupt_fleet(mode.spot_interruption_monitor, fleet_id, instance_ids[:1])

        assert mode.resources[resource_id]["status"] == STATUS_INTERRUPTED
        # A reclaimed fleet still reports itself active, so re-deriving status
        # from it would hand back a healthy-looking answer on the next poll.
        assert mode.get_job_status([resource_id])[resource_id] == STATUS_INTERRUPTED

    def test_the_monitor_is_started_by_construction(self, mode):
        """This mode starts monitoring in ``__init__``, unlike StandardMode.

        Worth pinning: a monitor nobody started detects nothing, and the
        difference between the two modes is easy to lose in a refactor.
        """
        assert isinstance(mode.spot_interruption_monitor, SpotInterruptionMonitor)
        assert mode.spot_interruption_monitor.monitoring_thread.is_alive()

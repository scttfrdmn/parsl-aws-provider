"""Unit tests for spot interruption detection and the response to it.

Two halves, matching the split in the package: ``SpotInterruptionMonitor``
detects that AWS is reclaiming capacity and calls the registered handler, and
``OperatingMode.handle_*_interruption`` decides what that means -- mark the
block interrupted so the provider reports FAILED and Parsl re-runs the tasks.

The checkpoint/recovery API these tests used to cover was deleted in #137. It
could not work at this layer: its entry point was
``register_task(task_id, instance_id)``, and a Parsl provider is never told a
task ID.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import pytest
import boto3
import threading
import queue
from unittest.mock import MagicMock, patch

from parsl_ephemeral_aws.compute.spot_interruption import SpotInterruptionMonitor
from parsl_ephemeral_aws.constants import STATUS_INTERRUPTED, STATUS_RUNNING
from parsl_ephemeral_aws.modes.base import OperatingMode

pytestmark = pytest.mark.unit


class TestSpotInterruptionMonitor:
    """Tests for the SpotInterruptionMonitor class."""

    @pytest.fixture
    def mock_session(self):
        """Create a mock boto3 session."""
        session = MagicMock(spec=boto3.Session)
        return session

    @pytest.fixture
    def mock_ec2_client(self):
        """Create a mock EC2 client."""
        client = MagicMock()

        # Mock describe_instances response. Detection keys off two real,
        # observable fields: an ``InstanceLifecycle`` of "spot" and a state of
        # "shutting-down"/"stopping". The previous fixture used
        # "marked-for-termination" — not an EC2 instance state at all — and
        # omitted InstanceLifecycle, so neither half of the condition could match
        # and no interruption was ever detected.
        client.describe_instances.return_value = {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-test1",
                            "InstanceLifecycle": "spot",
                            "State": {"Name": "running"},
                        },
                        {
                            "InstanceId": "i-test2",
                            "InstanceLifecycle": "spot",
                            "State": {"Name": "shutting-down"},
                        },
                    ]
                }
            ]
        }

        # A fleet's instances are found by filtering describe_instances on the
        # aws:ec2:fleet-id tag, not by describe_spot_fleet_instances, which
        # refuses an EC2 Fleet of type instant outright with ``Unsupported``
        # (#86, verified against real EC2). The paginator is what
        # get_ec2_fleet_instance_ids drives.
        fleet_paginator = MagicMock()
        fleet_paginator.paginate.return_value = [
            {
                "Reservations": [
                    {
                        "Instances": [
                            {"InstanceId": "i-test1"},
                            {"InstanceId": "i-test2"},
                        ]
                    }
                ]
            }
        ]
        client.get_paginator.return_value = fleet_paginator

        return client

    @pytest.fixture
    def mock_cloudwatch_client(self):
        """Create a mock CloudWatch client."""
        client = MagicMock()
        return client

    @pytest.fixture
    def monitor(self, mock_session):
        """Create a SpotInterruptionMonitor instance."""
        return SpotInterruptionMonitor(
            session=mock_session,
            check_interval=1,  # Short interval for testing
            lead_time=10,
        )

    def test_init(self, monitor, mock_session):
        """Test initialization of SpotInterruptionMonitor."""
        assert monitor.session == mock_session
        assert monitor.check_interval == 1
        assert monitor.lead_time == 10
        assert monitor.instance_handlers == {}
        assert monitor.fleet_handlers == {}
        assert monitor.monitoring_thread is None
        assert isinstance(monitor.stop_event, threading.Event)
        assert isinstance(monitor.event_queue, queue.Queue)

    def test_register_instance(self, monitor):
        """Test registering a spot instance for monitoring."""
        handler = MagicMock()

        monitor.register_instance("i-test1", handler)

        assert "i-test1" in monitor.instance_handlers
        assert monitor.instance_handlers["i-test1"] == handler

    def test_register_fleet(self, monitor):
        """Test registering a spot fleet for monitoring."""
        handler = MagicMock()

        monitor.register_fleet("fleet-test1", handler)

        assert "fleet-test1" in monitor.fleet_handlers
        assert monitor.fleet_handlers["fleet-test1"] == handler

    def test_deregister_instance(self, monitor):
        """Test deregistering a spot instance."""
        handler = MagicMock()

        monitor.register_instance("i-test1", handler)
        assert "i-test1" in monitor.instance_handlers

        monitor.deregister_instance("i-test1")
        assert "i-test1" not in monitor.instance_handlers

    def test_deregister_fleet(self, monitor):
        """Test deregistering a spot fleet."""
        handler = MagicMock()

        monitor.register_fleet("fleet-test1", handler)
        assert "fleet-test1" in monitor.fleet_handlers

        monitor.deregister_fleet("fleet-test1")
        assert "fleet-test1" not in monitor.fleet_handlers

    @patch("threading.Thread")
    def test_start_monitoring(self, mock_thread, monitor):
        """Test starting the monitoring thread."""
        monitor.start_monitoring()

        mock_thread.assert_called_once()
        assert mock_thread.return_value.start.call_count == 1
        assert monitor.monitoring_thread is not None

    @patch("threading.Thread")
    def test_stop_monitoring(self, mock_thread, monitor):
        """Test stopping the monitoring thread."""
        # Setup a mock thread
        mock_thread_instance = MagicMock()
        mock_thread.return_value = mock_thread_instance

        # Start monitoring
        monitor.start_monitoring()

        # Stop monitoring
        monitor.stop_monitoring()

        assert monitor.stop_event.is_set()
        assert mock_thread_instance.join.call_count == 1

    @patch("threading.Thread")
    def test_check_instance_interruptions(
        self, mock_thread, monitor, mock_ec2_client, mock_cloudwatch_client
    ):
        """Test checking for instance interruptions."""
        # Register an instance handler
        handler = MagicMock()
        monitor.register_instance("i-test2", handler)

        # Call the method directly
        monitor._check_instance_interruptions(mock_ec2_client, mock_cloudwatch_client)

        # Check if an event was queued for the interrupted instance
        assert not monitor.event_queue.empty()
        event_type, instance_id, event_details = monitor.event_queue.get()

        assert event_type == "instance"
        assert instance_id == "i-test2"
        assert event_details["InstanceId"] == "i-test2"
        assert event_details["InstanceAction"] == "terminate"

    @patch("threading.Thread")
    def test_check_fleet_interruptions(self, mock_thread, monitor, mock_ec2_client):
        """A fleet's instances are found by tag, not by the Spot Fleet API (#86).

        ``describe_spot_fleet_instances`` used to be the route, and this test
        asserted on it. It cannot be: it rejects an EC2 Fleet of type instant
        with ``Unsupported`` -- "Describe fleet instances is not supported by
        this type of fleet" -- verified against real EC2. The reserved
        ``aws:ec2:fleet-id`` tag EC2 stamps on every fleet-launched instance is
        the only workable route.
        """
        handler = MagicMock()
        monitor.register_fleet("fleet-test1", handler)

        monitor._check_fleet_interruptions(mock_ec2_client)

        # The legacy call must not be made at all -- it would raise.
        mock_ec2_client.describe_spot_fleet_instances.assert_not_called()

        mock_ec2_client.get_paginator.assert_called_with("describe_instances")
        paginate_kwargs = (
            mock_ec2_client.get_paginator.return_value.paginate.call_args.kwargs
        )
        assert {
            "Name": "tag:aws:ec2:fleet-id",
            "Values": ["fleet-test1"],
        } in paginate_kwargs["Filters"]

        # And the instances the tag search found are then described for state.
        mock_ec2_client.describe_instances.assert_called_with(
            InstanceIds=["i-test1", "i-test2"]
        )

        # i-test2 is shutting-down in the fixture, so the fleet handler is due an
        # event -- naming only that instance, not the whole fleet.
        assert not monitor.event_queue.empty()
        event_type, fleet_id, instance_ids, event_details = monitor.event_queue.get()
        assert event_type == "fleet"
        assert fleet_id == "fleet-test1"
        assert instance_ids == ["i-test2"]
        # Marked as the post-facto track: this instance is already dying, so a
        # handler must not assume it has the two minutes a warning would give.
        assert event_details["Source"] == "ec2-state"

    @patch("threading.Thread")
    def test_process_interruption_events(self, mock_thread, monitor):
        """Test processing interruption events."""
        # Setup handlers
        instance_handler = MagicMock()
        fleet_handler = MagicMock()

        monitor.register_instance("i-test1", instance_handler)
        monitor.register_fleet("fleet-test1", fleet_handler)

        # Add events to the queue
        instance_event = ("instance", "i-test1", {"InstanceId": "i-test1"})
        fleet_event = (
            "fleet",
            "fleet-test1",
            ["i-test1", "i-test2"],
            {"FleetRequestId": "fleet-test1"},
        )

        monitor.event_queue.put(instance_event)
        monitor.event_queue.put(fleet_event)

        # Process events
        monitor._process_interruption_events()

        # Verify handlers were called
        instance_handler.assert_called_once_with("i-test1", {"InstanceId": "i-test1"})
        fleet_handler.assert_called_once_with(
            "fleet-test1", ["i-test1", "i-test2"], {"FleetRequestId": "fleet-test1"}
        )


class _StubMode(OperatingMode):
    """Minimal concrete mode: the interruption response lives on the base class.

    Testing it here rather than through StandardMode keeps the assertions on the
    one thing under test. The per-mode half -- that ``get_job_status`` does not
    overwrite the marker on the next poll -- is covered in each mode's own tests.
    """

    def initialize(self):
        self.initialized = True

    def submit_job(self, job_id, command, tasks_per_node=1, job_name=None):
        raise NotImplementedError

    def get_job_status(self, resource_ids):
        return {rid: self.resources[rid]["status"] for rid in resource_ids}

    def cancel_jobs(self, resource_ids):
        raise NotImplementedError

    def cleanup_resources(self, resource_ids):
        raise NotImplementedError

    def cleanup_infrastructure(self):
        pass

    def list_resources(self):
        return {"instances": []}

    def cleanup_all(self):
        pass


class TestInterruptionResponse:
    """``OperatingMode.handle_*_interruption`` — the response to a reclaim (#137).

    Before this existed, an interruption was *invisible* rather than merely
    unhandled: the reclaimed instance went to "shutting-down", which
    EC2_STATUS_MAPPING renders COMPLETED, so the block reported success and its
    tasks were silently dropped.
    """

    @pytest.fixture
    def mode(self):
        mode = _StubMode(
            provider_id="test-provider",
            session=MagicMock(spec=boto3.Session),
            state_store=MagicMock(),
            vpc_id="vpc-12345",
            subnet_id="subnet-12345",
            security_group_id="sg-12345",
            use_spot=True,
            spot_interruption_handling=True,
        )
        mode.resources = {
            "i-test1": {"status": STATUS_RUNNING},
            "i-test2": {"status": STATUS_RUNNING},
            "fleet-test1": {"status": STATUS_RUNNING},
        }
        return mode

    def test_instance_interruption_marks_the_block(self, mode):
        event = {"InstanceId": "i-test1", "InstanceAction": "terminate"}

        mode.handle_instance_interruption("i-test1", event)

        assert mode.resources["i-test1"]["status"] == STATUS_INTERRUPTED
        # The event is kept so a FAILED block is diagnosable without going back
        # to CloudTrail for the reason.
        assert mode.resources["i-test1"]["interruption_event"] == event
        # Only the named instance: a reclaim is per-instance, not per-provider.
        assert mode.resources["i-test2"]["status"] == STATUS_RUNNING

    def test_untracked_instance_is_ignored(self, mode):
        """A warning can arrive for an instance we no longer track.

        The monitor polls on its own thread, so a job that finished and was
        cleaned up between the poll and the dispatch is normal, not an error --
        this must not raise into the monitoring thread.
        """
        mode.handle_instance_interruption("i-unknown", {"InstanceAction": "terminate"})

        assert "i-unknown" not in mode.resources

    def test_fleet_interruption_marks_fleet_and_named_instances(self, mode):
        event = {"FleetRequestId": "fleet-test1", "Source": "eventbridge"}

        mode.handle_fleet_interruption("fleet-test1", ["i-test1"], event)

        # The fleet block is what Parsl holds a job ID for, so it is the one that
        # must go FAILED; the instances are marked too so status is consistent.
        assert mode.resources["fleet-test1"]["status"] == STATUS_INTERRUPTED
        assert mode.resources["fleet-test1"]["interruption_event"] == event
        assert mode.resources["i-test1"]["status"] == STATUS_INTERRUPTED
        # A fleet reclaim names the affected instances; the rest keep running.
        assert mode.resources["i-test2"]["status"] == STATUS_RUNNING

    def test_untracked_fleet_still_marks_its_instances(self, mode):
        """The fleet ID may not be a tracked resource in every mode.

        DetachedMode tracks the bastion-side job rather than the fleet, so the
        instance loop has to run whether or not the fleet ID resolves.
        """
        mode.handle_fleet_interruption("fleet-unknown", ["i-test2"], {})

        assert mode.resources["i-test2"]["status"] == STATUS_INTERRUPTED

    def test_fleet_is_found_through_the_block_that_records_it(self, mode):
        """The fleet ID is a *field*, not the resource key, in every real mode.

        StandardMode keys a fleet block by block ID and the other two by
        ``serverless-<job_id>``, recording the fleet as ``fleet_request_id``. A
        direct ``resources[fleet_id]`` lookup therefore missed every time, so the
        block Parsl holds kept reporting healthy through the reclaim — the
        original bug, surviving in the fleet path only.
        """
        mode.resources["block-7"] = {
            "status": STATUS_RUNNING,
            "fleet_request_id": "fleet-abc",
        }

        mode.handle_fleet_interruption(
            "fleet-abc", ["i-test1"], {"Source": "ec2-state"}
        )

        assert mode.resources["block-7"]["status"] == STATUS_INTERRUPTED
        assert mode.resources["block-7"]["interruption_event"] == {
            "Source": "ec2-state"
        }
        # A block backed by a different fleet is untouched.
        assert mode.resources["fleet-test1"]["status"] == STATUS_RUNNING

    def test_monitor_dispatches_to_the_mode(self, mode):
        """The two halves connect: what the monitor calls is what the mode does.

        Registering the *bound method* is the whole wiring. This used to point at
        a handler object whose task mapping nothing ever populated, so every
        interruption logged "No registered tasks found" and did nothing.
        """
        monitor = SpotInterruptionMonitor(session=MagicMock(spec=boto3.Session))
        monitor.register_instance("i-test1", mode.handle_instance_interruption)
        monitor.event_queue.put(
            ("instance", "i-test1", {"InstanceAction": "terminate"})
        )

        monitor._process_interruption_events()

        assert mode.resources["i-test1"]["status"] == STATUS_INTERRUPTED

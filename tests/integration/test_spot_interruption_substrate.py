"""Integration tests for spot interruption detection against substrate.

The monitor is exercised against a real endpoint rather than MagicMocks, so every
call it makes has to be a shape the emulator will actually answer: SQS
receive/delete, ``describe_instances``, tag-filtered pagination.

The ``substrate_session`` fixture comes from ``tests/conftest.py`` deliberately
rather than being redefined here. It wraps ``session.client`` to bind
``endpoint_url``; ``get_substrate_session()`` alone returns a plain session with
synthetic credentials and no endpoint, so clients built from it reach *real* AWS
and fail on authentication.

Two substrate limitations shape what is testable, both verified against the
running emulator rather than assumed:

* ``events`` is not emulated -- ``PutRule`` returns ``501 service not emulated:
  awsevents``. That makes the degradation path in
  :meth:`test_the_notifier_degrades_when_eventbridge_is_absent` testable for
  real. The warning path itself is still covered end to end by wiring a queue
  directly, since the monitor only ever reads warnings through SQS.
* ``InstanceLifecycle`` is never set, even for ``InstanceMarketOptions``
  ``MarketType: spot``, so the EC2-state track cannot fire here at all -- it
  requires ``InstanceLifecycle == "spot"``. That branch stays with the mocked
  unit tests; here the instances are real and the queue is the input.

The checkpoint/recovery API these tests used to cover was deleted in #137: its
entry point was ``register_task(task_id, instance_id)``, and a Parsl provider is
never told a task ID.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import json
import uuid

import pytest

from parsl_aws_provider.compute.spot_interruption import SpotInterruptionMonitor

pytestmark = [pytest.mark.integration, pytest.mark.substrate]


@pytest.fixture
def warning_queue(substrate_session):
    """An SQS queue standing in for the one the EventBridge rule delivers to.

    Substrate cannot create the rule, but the monitor reads warnings only through
    SQS, so a queue wired in directly exercises the identical code path with the
    identical message shape.
    """
    sqs = substrate_session.client("sqs")
    url = sqs.create_queue(QueueName=f"spot-warning-{uuid.uuid4().hex[:8]}")["QueueUrl"]

    yield url

    try:
        sqs.delete_queue(QueueUrl=url)
    except Exception as exc:  # pragma: no cover - cleanup only
        print(f"Error cleaning up queue {url}: {exc}")


def _warning_body(instance_id, action="terminate"):
    """Build the EventBridge envelope AWS delivers for a reclaim warning."""
    return json.dumps(
        {
            "source": "aws.ec2",
            "detail-type": "EC2 Spot Instance Interruption Warning",
            "detail": {"instance-id": instance_id, "instance-action": action},
        }
    )


class TestSpotInterruptionSubstrate:
    """Detection driven against substrate's AWS endpoint."""

    def test_the_notifier_degrades_when_eventbridge_is_absent(self, substrate_session):
        """A notifier that cannot be created must not fail the workflow.

        Every mode starts the monitor during ``initialize()``, so raising here
        would take down a provider that could otherwise run perfectly well --
        just without advance warning. Substrate's missing ``events`` service
        makes this the realistic failure; an IAM policy lacking
        ``events:PutRule`` produces the same shape.
        """
        monitor = SpotInterruptionMonitor(
            session=substrate_session, check_interval=1, provider_id="degrade-test"
        )

        monitor.start_monitoring()
        try:
            assert monitor.monitoring_thread.is_alive()
            # Running on the EC2-state track alone, with no notifier behind it.
            assert monitor.warning_rule_name is None
            assert monitor.warning_queue_url is None
        finally:
            monitor.stop_monitoring()

        assert monitor.monitoring_thread is None

    def test_a_queued_warning_reaches_the_instance_handler(
        self, substrate_session, warning_queue
    ):
        """The warning path, end to end over real SQS.

        This is the track that matters: it fires while the instance is still
        running, the only point at which anything can be done about the reclaim.
        """
        monitor = SpotInterruptionMonitor(session=substrate_session, check_interval=1)
        monitor.warning_queue_url = warning_queue

        seen = []
        instance_id = f"i-{uuid.uuid4().hex[:16]}"
        monitor.register_instance(
            instance_id, lambda iid, event: seen.append((iid, event))
        )

        sqs = substrate_session.client("sqs")
        sqs.send_message(QueueUrl=warning_queue, MessageBody=_warning_body(instance_id))

        monitor._poll_warning_queue(sqs, substrate_session.client("ec2"))
        monitor._process_interruption_events()

        assert len(seen) == 1
        handled_id, event = seen[0]
        assert handled_id == instance_id
        assert event["InstanceAction"] == "terminate"
        # Marks this as the advance track, so a handler can tell it still has
        # roughly two minutes rather than none.
        assert event["Source"] == "eventbridge"

    def test_a_warning_for_an_unowned_instance_is_dropped(
        self, substrate_session, warning_queue
    ):
        """The rule matches the whole account and region, not our instances.

        Their IDs are not known until launch, so the pattern cannot be scoped.
        Warnings for instances this monitor does not track are therefore the
        common case, and must be dropped silently rather than raising into the
        monitoring thread.
        """
        monitor = SpotInterruptionMonitor(session=substrate_session, check_interval=1)
        monitor.warning_queue_url = warning_queue

        seen = []
        monitor.register_instance("i-ours", lambda iid, event: seen.append(iid))

        sqs = substrate_session.client("sqs")
        sqs.send_message(
            QueueUrl=warning_queue, MessageBody=_warning_body("i-somebody-elses")
        )

        monitor._poll_warning_queue(sqs, substrate_session.client("ec2"))
        monitor._process_interruption_events()

        assert seen == []
        # And the message is gone: deletion happens before parsing, so an unowned
        # warning cannot be redelivered on every poll for the whole retention
        # period.
        remaining = sqs.receive_message(QueueUrl=warning_queue, MaxNumberOfMessages=10)
        assert remaining.get("Messages", []) == []

    def test_an_unparseable_warning_is_deleted_not_retried(
        self, substrate_session, warning_queue
    ):
        """A malformed body must not wedge the queue."""
        monitor = SpotInterruptionMonitor(session=substrate_session, check_interval=1)
        monitor.warning_queue_url = warning_queue

        sqs = substrate_session.client("sqs")
        sqs.send_message(QueueUrl=warning_queue, MessageBody="not json at all")

        monitor._poll_warning_queue(sqs, substrate_session.client("ec2"))

        assert monitor.event_queue.empty()
        remaining = sqs.receive_message(QueueUrl=warning_queue, MaxNumberOfMessages=10)
        assert remaining.get("Messages", []) == []

    def test_a_fleet_instance_warning_reaches_the_fleet_handler(
        self, substrate_session, warning_queue
    ):
        """A warning names an instance; the handler owed it may own the fleet.

        The link is the reserved ``aws:ec2:fleet-id`` tag EC2 stamps on every
        fleet-launched instance -- the only workable route, since
        ``describe_fleet_instances`` rejects an instant fleet outright (#86).
        Substrate serves both ``create_tags`` and the tag filter, so the lookup
        is exercised for real here rather than mocked.
        """
        ec2 = substrate_session.client("ec2")
        fleet_id = f"fleet-{uuid.uuid4().hex[:12]}"
        instance_id = ec2.run_instances(
            ImageId="ami-12345678", MinCount=1, MaxCount=1, InstanceType="t3.micro"
        )["Instances"][0]["InstanceId"]
        ec2.create_tags(
            Resources=[instance_id],
            Tags=[{"Key": "aws:ec2:fleet-id", "Value": fleet_id}],
        )

        monitor = SpotInterruptionMonitor(session=substrate_session, check_interval=1)
        monitor.warning_queue_url = warning_queue

        seen = []
        monitor.register_fleet(
            fleet_id, lambda fid, iids, event: seen.append((fid, iids, event))
        )

        sqs = substrate_session.client("sqs")
        sqs.send_message(QueueUrl=warning_queue, MessageBody=_warning_body(instance_id))

        try:
            monitor._poll_warning_queue(sqs, ec2)
            monitor._process_interruption_events()

            assert len(seen) == 1
            handled_fleet, instance_ids, event = seen[0]
            assert handled_fleet == fleet_id
            # Only the warned instance, not the whole fleet: a reclaim is
            # per-instance, and taking the rest down would be gratuitous.
            assert instance_ids == [instance_id]
            assert event["FleetRequestId"] == fleet_id
        finally:
            ec2.terminate_instances(InstanceIds=[instance_id])

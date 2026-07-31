"""Unit tests for the EventBridge spot-interruption warning notifier (#86).

The EC2-state poll that predated this could only ever report an interruption
post-facto: an interrupted instance is first observable at ``shutting-down``,
which is after the reclaim and far too late to checkpoint. An ``instant`` fleet
gets no Capacity Rebalance either -- ``CreateFleet`` rejects
``SpotOptions.MaintenanceStrategies`` for that type -- so an EventBridge rule
delivering to SQS, polled by the driver, is what supplies the two-minute
warning.

The mechanism was verified end to end against real AWS with a Fault Injection
Simulator experiment (``aws:ec2:send-spot-instance-interruptions``): the warning
reached the queue 15.2s after the experiment started, with the instance still
``running``. What these tests pin is the wiring around it -- the parts a live
probe proves once but that silently rot afterwards:

* the queue policy is set *before* the target is added, so no window exists in
  which EventBridge has a target it cannot deliver to;
* ``put_targets`` sends no ``RoleArn``, because SQS delivery is authorised by the
  queue's own resource policy and passing a role is not how it works;
* ``remove_targets`` precedes ``delete_rule``, because EventBridge refuses to
  delete a rule that still has a target;
* each message is deleted *before* it is parsed, since the rule matches every
  spot interruption in the account and an unparseable or unowned message would
  otherwise redeliver on every poll for the whole retention period.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from parsl_ephemeral_aws.compute.spot_interruption import SpotInterruptionMonitor
from parsl_ephemeral_aws.constants import (
    SPOT_INTERRUPTION_EVENT_DETAIL_TYPE,
    SPOT_INTERRUPTION_EVENT_SOURCE,
    SPOT_INTERRUPTION_QUEUE_RETENTION_SECONDS,
    SPOT_INTERRUPTION_QUEUE_WAIT_SECONDS,
    SPOT_INTERRUPTION_RULE_NAME_PREFIX,
    TAG_MANAGED,
)
from parsl_ephemeral_aws.exceptions import ResourceCreationError
from parsl_ephemeral_aws.utils.aws import (
    create_spot_interruption_notifier,
    delete_spot_interruption_notifier,
)

pytestmark = pytest.mark.unit


QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/942542972736/parsl-spot-warning-abc"
QUEUE_ARN = "arn:aws:sqs:us-east-1:942542972736:parsl-spot-warning-abc"
RULE_ARN = "arn:aws:events:us-east-1:942542972736:rule/parsl-spot-warning-abc"


def _clients():
    """Return ``(recorder, events, sqs)`` sharing one call log.

    Ordering is half of what this module tests, and it is ordering *between* two
    clients -- the queue policy on SQS against the target on EventBridge. Two
    independent mocks cannot express that, so both are attached to a parent whose
    ``mock_calls`` interleaves them.
    """
    recorder = MagicMock()
    events = MagicMock()
    sqs = MagicMock()
    recorder.attach_mock(events, "events")
    recorder.attach_mock(sqs, "sqs")

    sqs.create_queue.return_value = {"QueueUrl": QUEUE_URL}
    sqs.get_queue_attributes.return_value = {"Attributes": {"QueueArn": QUEUE_ARN}}
    sqs.set_queue_attributes.return_value = {}
    events.put_rule.return_value = {"RuleArn": RULE_ARN}
    events.put_targets.return_value = {"FailedEntryCount": 0}

    return recorder, events, sqs


def _call_names(recorder):
    """The bare method names from *recorder*, in the order they were called."""
    return [name for name, _, _ in recorder.mock_calls]


class TestCreateSpotInterruptionNotifier:
    """The rule, the queue, and the wiring between them."""

    def test_returns_the_rule_name_queue_url_and_arn(self):
        _, events, sqs = _clients()

        result = create_spot_interruption_notifier(
            events, sqs, "parsl-spot-warning-abc"
        )

        assert result == ("parsl-spot-warning-abc", QUEUE_URL, QUEUE_ARN)

    def test_the_queue_policy_is_set_before_the_target_is_added(self):
        """Otherwise EventBridge briefly has a target it cannot deliver to.

        The window is short but it is exactly the window that matters: a warning
        arriving in it is dropped by SQS as unauthorised, and a dropped warning
        is not retried -- the two minutes elapse and the instance is gone. This
        is also the ordering that cannot be caught by a live probe, which will
        almost always create the rule long before any interruption fires.
        """
        recorder, events, sqs = _clients()

        create_spot_interruption_notifier(events, sqs, "parsl-spot-warning-abc")

        names = _call_names(recorder)
        assert names.index("sqs.set_queue_attributes") < names.index(
            "events.put_targets"
        )

    def test_the_queue_exists_before_the_rule_names_it(self):
        """The policy needs the queue ARN and the rule ARN, so both come first."""
        recorder, events, sqs = _clients()

        create_spot_interruption_notifier(events, sqs, "parsl-spot-warning-abc")

        names = _call_names(recorder)
        assert names.index("sqs.create_queue") < names.index("sqs.get_queue_attributes")
        assert names.index("events.put_rule") < names.index("sqs.set_queue_attributes")

    def test_put_targets_sends_no_role_arn(self):
        """SQS is authorised by the queue policy, not by a role EventBridge assumes.

        Most target types take a ``RoleArn``; SQS does not, and supplying one
        would be a role created, and leaked, for nothing.
        """
        _, events, sqs = _clients()

        create_spot_interruption_notifier(events, sqs, "parsl-spot-warning-abc")

        kwargs = events.put_targets.call_args.kwargs
        assert "RoleArn" not in kwargs
        assert kwargs["Rule"] == "parsl-spot-warning-abc"
        assert kwargs["Targets"] == [
            {"Id": "parsl-spot-warning-queue", "Arn": QUEUE_ARN}
        ]

    def test_the_event_pattern_matches_only_the_interruption_warning(self):
        """A wider pattern would deliver unrelated EC2 events to be parsed."""
        _, events, sqs = _clients()

        create_spot_interruption_notifier(events, sqs, "parsl-spot-warning-abc")

        pattern = json.loads(events.put_rule.call_args.kwargs["EventPattern"])
        assert pattern == {
            "source": [SPOT_INTERRUPTION_EVENT_SOURCE],
            "detail-type": [SPOT_INTERRUPTION_EVENT_DETAIL_TYPE],
        }
        assert events.put_rule.call_args.kwargs["State"] == "ENABLED"

    def test_the_queue_policy_is_scoped_to_this_rule(self):
        """``aws:SourceArn`` is what stops any other rule posting to the queue.

        Without the condition the policy grants ``events.amazonaws.com`` at
        large, which is every EventBridge rule in every account -- a queue any
        stranger can inject a fabricated interruption warning into, and this
        monitor's handlers act on those warnings by checkpointing and
        rescheduling.
        """
        _, events, sqs = _clients()

        create_spot_interruption_notifier(events, sqs, "parsl-spot-warning-abc")

        policy = json.loads(
            sqs.set_queue_attributes.call_args.kwargs["Attributes"]["Policy"]
        )
        statement = policy["Statement"][0]
        assert statement["Effect"] == "Allow"
        assert statement["Principal"] == {"Service": "events.amazonaws.com"}
        assert statement["Action"] == "sqs:SendMessage"
        assert statement["Resource"] == QUEUE_ARN
        assert statement["Condition"] == {"ArnEquals": {"aws:SourceArn": RULE_ARN}}

    def test_the_queue_retention_is_short(self):
        """A warning is worthless once its two minutes are up.

        Default SQS retention is four days. Replaying a stale warning against a
        long-dead instance would have the handler checkpoint and reschedule work
        that already finished or already failed, so the retention is cut to the
        window in which the message can still be acted on.
        """
        _, events, sqs = _clients()

        create_spot_interruption_notifier(events, sqs, "parsl-spot-warning-abc")

        attributes = sqs.create_queue.call_args.kwargs["Attributes"]
        assert attributes["MessageRetentionPeriod"] == str(
            SPOT_INTERRUPTION_QUEUE_RETENTION_SECONDS
        )
        assert SPOT_INTERRUPTION_QUEUE_RETENTION_SECONDS <= 3600

    def test_tags_reach_the_rule_when_supplied(self):
        _, events, sqs = _clients()

        create_spot_interruption_notifier(
            events,
            sqs,
            "parsl-spot-warning-abc",
            tags=[{"Key": TAG_MANAGED, "Value": "true"}],
        )

        assert events.put_rule.call_args.kwargs["Tags"] == [
            {"Key": TAG_MANAGED, "Value": "true"}
        ]

    def test_no_tags_key_is_sent_when_none_are_given(self):
        """``Tags=[]`` is not the same as omitting it -- EventBridge rejects it.

        ``put_rule`` declares ``Tags`` with a minimum length of 1, so an empty
        list is a ``ValidationException`` rather than a no-op.
        """
        _, events, sqs = _clients()

        create_spot_interruption_notifier(events, sqs, "parsl-spot-warning-abc")

        assert "Tags" not in events.put_rule.call_args.kwargs

    def test_a_refused_target_raises(self):
        """``put_targets`` reports failure in the body, not by raising.

        ``FailedEntryCount`` is the only signal, so an unchecked call returns
        successfully having wired nothing -- the rule exists, the queue exists,
        and no warning ever arrives. Nothing downstream would notice, because
        the absence of interruption warnings is indistinguishable from the
        absence of interruptions.
        """
        _, events, sqs = _clients()
        events.put_targets.return_value = {
            "FailedEntryCount": 1,
            "FailedEntries": [
                {
                    "TargetId": "parsl-spot-warning-queue",
                    "ErrorCode": "AccessDeniedException",
                    "ErrorMessage": "not authorized",
                }
            ],
        }

        with pytest.raises(ResourceCreationError) as excinfo:
            create_spot_interruption_notifier(events, sqs, "parsl-spot-warning-abc")

        assert "AccessDeniedException" in str(excinfo.value)

    def test_a_client_error_is_wrapped(self):
        """The caller degrades on ``ResourceCreationError``, not on ClientError."""
        _, events, sqs = _clients()
        sqs.create_queue.side_effect = ClientError(
            {
                "Error": {
                    "Code": "QueueDeletedRecently",
                    "Message": "You must wait 60 seconds after deleting a queue",
                }
            },
            "CreateQueue",
        )

        with pytest.raises(ResourceCreationError) as excinfo:
            create_spot_interruption_notifier(events, sqs, "parsl-spot-warning-abc")

        assert "QueueDeletedRecently" in str(excinfo.value)


class TestDeleteSpotInterruptionNotifier:
    """Cleanup, which runs from paths that must not be stopped by a failure."""

    def test_targets_are_removed_before_the_rule_is_deleted(self):
        """EventBridge refuses to delete a rule that still has a target.

        Reversing these two leaves the rule behind on every teardown, still
        matching every spot interruption in the account, and pointing at a queue
        that is about to be deleted.
        """
        recorder, events, sqs = _clients()

        delete_spot_interruption_notifier(
            events, sqs, "parsl-spot-warning-abc", QUEUE_URL
        )

        names = _call_names(recorder)
        assert names.index("events.remove_targets") < names.index("events.delete_rule")
        assert events.remove_targets.call_args.kwargs["Ids"] == [
            "parsl-spot-warning-queue"
        ]

    def test_everything_is_deleted(self):
        _, events, sqs = _clients()

        delete_spot_interruption_notifier(
            events, sqs, "parsl-spot-warning-abc", QUEUE_URL
        )

        events.delete_rule.assert_called_once_with(Name="parsl-spot-warning-abc")
        sqs.delete_queue.assert_called_once_with(QueueUrl=QUEUE_URL)

    def test_a_failed_rule_deletion_does_not_strand_the_queue(self):
        """Each step is independent; the queue costs nothing but leaks a name.

        SQS refuses to recreate a queue for 60 seconds after deletion, so a
        stranded queue also blocks the next provider that picks the same
        provider-ID prefix.
        """
        _, events, sqs = _clients()
        events.delete_rule.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}},
            "DeleteRule",
        )

        delete_spot_interruption_notifier(
            events, sqs, "parsl-spot-warning-abc", QUEUE_URL
        )

        sqs.delete_queue.assert_called_once_with(QueueUrl=QUEUE_URL)

    def test_an_already_absent_rule_is_not_an_error(self):
        """Cleanup runs from ``except`` handlers, where gone is the goal."""
        _, events, sqs = _clients()
        events.remove_targets.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}},
            "RemoveTargets",
        )
        sqs.delete_queue.side_effect = ClientError(
            {
                "Error": {
                    "Code": "AWS.SimpleQueueService.NonExistentQueue",
                    "Message": "gone",
                }
            },
            "DeleteQueue",
        )

        delete_spot_interruption_notifier(
            events, sqs, "parsl-spot-warning-abc", QUEUE_URL
        )

    def test_nothing_is_called_for_a_notifier_that_was_never_created(self):
        """The monitor degrades to the EC2 poll, leaving both names None."""
        _, events, sqs = _clients()

        delete_spot_interruption_notifier(events, sqs, None, None)

        events.remove_targets.assert_not_called()
        events.delete_rule.assert_not_called()
        sqs.delete_queue.assert_not_called()


class TestMonitorNotifierLifecycle:
    """When the monitor creates and destroys the notifier."""

    @pytest.fixture
    def session(self):
        """A session whose ``client()`` returns a distinct mock per service."""
        clients = {
            "events": MagicMock(name="events"),
            "sqs": MagicMock(name="sqs"),
            "ec2": MagicMock(name="ec2"),
            "cloudwatch": MagicMock(name="cloudwatch"),
        }
        session = MagicMock()
        session.client.side_effect = lambda service, **kw: clients.setdefault(
            service, MagicMock(name=service)
        )
        session._clients = clients
        return session

    def test_construction_makes_no_aws_calls(self, session):
        """Every mode builds a monitor in ``__init__``, before initialize().

        Creating the rule there would mean a construction that can fail on an
        IAM permission, and -- worse -- a rule and queue created by a provider
        that then raises, with no ``stop_monitoring`` ever reached to remove
        them.
        """
        monitor = SpotInterruptionMonitor(session, provider_id="abcdef123456")

        session.client.assert_not_called()
        assert monitor.warning_rule_name is None
        assert monitor.warning_queue_url is None

    def test_the_notifier_name_is_derived_from_the_provider_id(self, session):
        """Two providers in one account must not share a queue.

        They would each drain warnings the other needed: SQS delivers a message
        to one consumer, and this poll deletes on receipt.
        """
        monitor = SpotInterruptionMonitor(session, provider_id="abcdef123456")

        assert (
            monitor._notifier_name == f"{SPOT_INTERRUPTION_RULE_NAME_PREFIX}-abcdef12"
        )

    def test_a_missing_provider_id_still_yields_a_unique_name(self, session):
        first = SpotInterruptionMonitor(session)
        second = SpotInterruptionMonitor(session)

        assert first._notifier_name != second._notifier_name

    @patch("threading.Thread")
    def test_start_monitoring_creates_the_notifier(self, _thread, session):
        monitor = SpotInterruptionMonitor(session, provider_id="abcdef123456")

        with patch(
            "parsl_ephemeral_aws.compute.spot_interruption."
            "create_spot_interruption_notifier",
            return_value=(monitor._notifier_name, QUEUE_URL, QUEUE_ARN),
        ) as create:
            monitor.start_monitoring()

        create.assert_called_once()
        assert create.call_args.args[2] == monitor._notifier_name
        assert create.call_args.kwargs["tags"] == [
            {"Key": TAG_MANAGED, "Value": "true"}
        ]
        assert monitor.warning_queue_url == QUEUE_URL

    @patch("threading.Thread")
    def test_a_failed_notifier_degrades_rather_than_failing_the_workflow(
        self, _thread, session
    ):
        """Losing the advance warning is worse than the poll, not fatal.

        A caller whose IAM policy grants no ``events``/``sqs`` still gets a
        working provider; what it loses is the lead time to checkpoint. Raising
        here would make the two new permissions mandatory for every spot
        workflow, including ones that do no checkpointing at all.
        """
        monitor = SpotInterruptionMonitor(session, provider_id="abcdef123456")

        with patch(
            "parsl_ephemeral_aws.compute.spot_interruption."
            "create_spot_interruption_notifier",
            side_effect=ResourceCreationError("AccessDenied"),
        ):
            monitor.start_monitoring()

        assert monitor.warning_queue_url is None
        # The thread still started: the EC2-state poll is the fallback track.
        assert monitor.monitoring_thread is not None

    @patch("threading.Thread")
    def test_the_notifier_can_be_disabled(self, _thread, session):
        monitor = SpotInterruptionMonitor(
            session, provider_id="abcdef123456", use_event_bridge=False
        )

        with patch(
            "parsl_ephemeral_aws.compute.spot_interruption."
            "create_spot_interruption_notifier",
        ) as create:
            monitor.start_monitoring()

        create.assert_not_called()
        assert monitor.warning_queue_url is None

    @patch("threading.Thread")
    def test_restarting_does_not_create_a_second_notifier(self, thread, session):
        """A monitor whose thread died is restartable; its queue is not remade.

        Creating a second one would leak the first, and it would leak it under a
        name nothing tracks -- the attribute holding it has just been
        overwritten.
        """
        thread.return_value.is_alive.return_value = False
        monitor = SpotInterruptionMonitor(session, provider_id="abcdef123456")

        with patch(
            "parsl_ephemeral_aws.compute.spot_interruption."
            "create_spot_interruption_notifier",
            return_value=(monitor._notifier_name, QUEUE_URL, QUEUE_ARN),
        ) as create:
            monitor.start_monitoring()
            monitor.start_monitoring()

        assert create.call_count == 1

    @patch("threading.Thread")
    def test_stop_monitoring_deletes_the_notifier(self, _thread, session):
        monitor = SpotInterruptionMonitor(session, provider_id="abcdef123456")
        monitor.warning_rule_name = monitor._notifier_name
        monitor.warning_queue_url = QUEUE_URL

        with patch(
            "parsl_ephemeral_aws.compute.spot_interruption."
            "delete_spot_interruption_notifier",
        ) as delete:
            monitor.stop_monitoring()

        delete.assert_called_once()
        assert delete.call_args.args[2] == monitor._notifier_name
        assert delete.call_args.args[3] == QUEUE_URL
        assert monitor.warning_rule_name is None
        assert monitor.warning_queue_url is None

    def test_the_notifier_is_deleted_even_when_no_thread_was_running(self, session):
        """The rule outlives the thread, so cleanup cannot be gated on it.

        ``stop_monitoring`` returns early when there is no live thread. If the
        teardown sat after that return, a monitor whose thread had already died
        -- or one stopped twice -- would leave the rule and queue behind for
        good: nothing else in the package deletes them.
        """
        monitor = SpotInterruptionMonitor(session, provider_id="abcdef123456")
        monitor.warning_rule_name = monitor._notifier_name
        monitor.warning_queue_url = QUEUE_URL
        assert monitor.monitoring_thread is None

        with patch(
            "parsl_ephemeral_aws.compute.spot_interruption."
            "delete_spot_interruption_notifier",
        ) as delete:
            monitor.stop_monitoring()

        delete.assert_called_once()

    def test_stopping_a_monitor_with_no_notifier_calls_nothing(self, session):
        monitor = SpotInterruptionMonitor(session, provider_id="abcdef123456")

        with patch(
            "parsl_ephemeral_aws.compute.spot_interruption."
            "delete_spot_interruption_notifier",
        ) as delete:
            monitor.stop_monitoring()

        delete.assert_not_called()


def _warning_message(instance_id="i-abc123", action="terminate", receipt="rh-1"):
    """An EventBridge envelope in the shape a real warning arrived in.

    Captured from the FIS probe rather than invented: ``detail`` carries
    ``instance-id`` and ``instance-action``, and the envelope's own
    ``detail-type`` is the string the rule pattern matches.
    """
    return {
        "MessageId": "m-1",
        "ReceiptHandle": receipt,
        "Body": json.dumps(
            {
                "version": "0",
                "source": SPOT_INTERRUPTION_EVENT_SOURCE,
                "detail-type": SPOT_INTERRUPTION_EVENT_DETAIL_TYPE,
                "detail": {"instance-id": instance_id, "instance-action": action},
            }
        ),
    }


class TestPollWarningQueue:
    """Draining warnings, and routing them to whichever handler owns them."""

    @pytest.fixture
    def monitor(self):
        monitor = SpotInterruptionMonitor(
            MagicMock(), check_interval=1, provider_id="abcdef123456"
        )
        monitor.warning_queue_url = QUEUE_URL
        return monitor

    @pytest.fixture
    def sqs(self):
        client = MagicMock()
        client.receive_message.return_value = {"Messages": [_warning_message()]}
        return client

    def test_a_warning_for_a_registered_instance_is_queued(self, monitor, sqs):
        monitor.register_instance("i-abc123", MagicMock())

        monitor._poll_warning_queue(sqs, MagicMock())

        event_type, instance_id, details = monitor.event_queue.get_nowait()
        assert event_type == "instance"
        assert instance_id == "i-abc123"
        assert details["InstanceId"] == "i-abc123"
        assert details["InstanceAction"] == "terminate"

    def test_the_source_marks_it_as_an_advance_warning(self, monitor, sqs):
        """This is how a handler knows it has two minutes rather than none.

        The EC2-state track stamps ``ec2-state`` and fires on an instance that
        is already dying; a handler that cannot tell them apart must either
        assume the worst and skip checkpointing entirely, or attempt a
        checkpoint that cannot complete.
        """
        monitor.register_instance("i-abc123", MagicMock())

        monitor._poll_warning_queue(sqs, MagicMock())

        _, _, details = monitor.event_queue.get_nowait()
        assert details["Source"] == "eventbridge"

    def test_a_stop_action_is_preserved(self, monitor, sqs):
        """``instance-action`` is one of terminate, stop, or hibernate.

        A stopped or hibernated instance can come back, which is a different
        recovery decision from a terminated one, so the value has to survive
        rather than be flattened to the default.
        """
        sqs.receive_message.return_value = {
            "Messages": [_warning_message(action="hibernate")]
        }
        monitor.register_instance("i-abc123", MagicMock())

        monitor._poll_warning_queue(sqs, MagicMock())

        _, _, details = monitor.event_queue.get_nowait()
        assert details["InstanceAction"] == "hibernate"

    def test_every_message_is_deleted_before_it_is_parsed(self, monitor, sqs):
        """An unparseable message must not redeliver for the retention period.

        Deleting after a successful parse looks equivalent and is not: the rule
        matches every spot interruption in the account, so this queue receives
        messages for instances the monitor does not own, and any message it
        cannot use would be received again on every single poll -- crowding out,
        via ``MaxNumberOfMessages``, the warnings it can.
        """
        sqs.receive_message.return_value = {
            "Messages": [
                {"MessageId": "m-1", "ReceiptHandle": "rh-junk", "Body": "not json"}
            ]
        }

        monitor._poll_warning_queue(sqs, MagicMock())

        sqs.delete_message.assert_called_once_with(
            QueueUrl=QUEUE_URL, ReceiptHandle="rh-junk"
        )

    def test_a_message_with_no_body_is_survived(self, monitor, sqs):
        """``Body`` is always present from SQS, but a mock or a proxy may drop it."""
        sqs.receive_message.return_value = {
            "Messages": [{"MessageId": "m-1", "ReceiptHandle": "rh-1"}]
        }

        monitor._poll_warning_queue(sqs, MagicMock())

        assert monitor.event_queue.empty()

    def test_a_failed_delete_does_not_lose_the_warning(self, monitor, sqs):
        """The warning is still actionable even if the message will redeliver.

        Dropping it here would trade a duplicate for a missed interruption, and
        only one of those two loses work.
        """
        sqs.delete_message.side_effect = ClientError(
            {"Error": {"Code": "ReceiptHandleIsInvalid", "Message": "expired"}},
            "DeleteMessage",
        )
        monitor.register_instance("i-abc123", MagicMock())

        monitor._poll_warning_queue(sqs, MagicMock())

        assert not monitor.event_queue.empty()

    def test_a_warning_for_an_unowned_instance_is_dropped_quietly(self, monitor, sqs):
        """The rule is account-wide, so this is the common case, not an error.

        The instances to watch are not known until the fleet launches, so the
        pattern cannot be scoped to them. Every other spot instance in the
        account and region delivers here too.
        """
        monitor.register_instance("i-somethingelse", MagicMock())

        monitor._poll_warning_queue(sqs, MagicMock())

        assert monitor.event_queue.empty()
        # Still deleted, or it would redeliver until the retention expires.
        sqs.delete_message.assert_called_once()

    def test_a_warning_for_a_fleet_member_routes_to_the_fleet_handler(
        self, monitor, sqs
    ):
        """An instance launched by a fleet is not registered under its own ID.

        Only the fleet is registered, so the instance has to be attributed by
        asking each registered fleet what it is running -- via the reserved
        ``aws:ec2:fleet-id`` tag, the only route that works for an instant
        fleet.
        """
        monitor.register_fleet("fleet-1", MagicMock())
        ec2 = MagicMock()

        with patch(
            "parsl_ephemeral_aws.compute.spot_interruption.get_ec2_fleet_instance_ids",
            return_value=["i-abc123", "i-other"],
        ) as lookup:
            monitor._poll_warning_queue(sqs, ec2)

        lookup.assert_called_once_with(ec2, "fleet-1")
        event_type, fleet_id, instance_ids, details = monitor.event_queue.get_nowait()
        assert event_type == "fleet"
        assert fleet_id == "fleet-1"
        # Only the interrupted instance, not the whole fleet: the others are
        # still running and their work must not be rescheduled.
        assert instance_ids == ["i-abc123"]
        assert details["FleetRequestId"] == "fleet-1"
        assert details["Source"] == "eventbridge"

    def test_a_directly_registered_instance_is_not_looked_up_by_fleet(
        self, monitor, sqs
    ):
        """The instance handler is more specific, and the lookup costs an API call.

        Falling through to the fleet scan would also mean a ``describe_instances``
        page-walk per registered fleet on every warning -- including the account's
        unrelated ones.
        """
        monitor.register_instance("i-abc123", MagicMock())
        monitor.register_fleet("fleet-1", MagicMock())

        with patch(
            "parsl_ephemeral_aws.compute.spot_interruption.get_ec2_fleet_instance_ids",
        ) as lookup:
            monitor._poll_warning_queue(sqs, MagicMock())

        lookup.assert_not_called()

    def test_all_ten_messages_in_a_batch_are_handled(self, monitor, sqs):
        """A batch arrives when several instances in a fleet go at once.

        Handling only the first would leave the rest to redeliver, spending a
        poll interval each -- and the whole lead time is two minutes.
        """
        sqs.receive_message.return_value = {
            "Messages": [
                _warning_message(instance_id=f"i-{n}", receipt=f"rh-{n}")
                for n in range(10)
            ]
        }
        for n in range(10):
            monitor.register_instance(f"i-{n}", MagicMock())

        monitor._poll_warning_queue(sqs, MagicMock())

        assert monitor.event_queue.qsize() == 10
        assert sqs.delete_message.call_count == 10

    def test_the_poll_long_polls_within_the_check_interval(self, monitor, sqs):
        """Long polling is what makes the warning arrive promptly.

        With short polling the warning waits out the check interval, spending up
        to a quarter of its two minutes idle. But the wait cannot exceed the
        interval either, or a monitor configured to check often would block past
        its own period and ignore ``stop_event`` for the difference.
        """
        monitor._poll_warning_queue(sqs, MagicMock())

        kwargs = sqs.receive_message.call_args.kwargs
        assert kwargs["QueueUrl"] == QUEUE_URL
        assert kwargs["MaxNumberOfMessages"] == 10
        assert kwargs["WaitTimeSeconds"] == 1  # check_interval, the smaller of the two

    def test_a_long_check_interval_is_capped_by_the_sqs_maximum(self):
        """SQS rejects ``WaitTimeSeconds`` above 20."""
        monitor = SpotInterruptionMonitor(MagicMock(), check_interval=300)
        monitor.warning_queue_url = QUEUE_URL
        sqs = MagicMock()
        sqs.receive_message.return_value = {}

        monitor._poll_warning_queue(sqs, MagicMock())

        assert (
            sqs.receive_message.call_args.kwargs["WaitTimeSeconds"]
            == SPOT_INTERRUPTION_QUEUE_WAIT_SECONDS
        )
        assert SPOT_INTERRUPTION_QUEUE_WAIT_SECONDS <= 20

    def test_a_receive_failure_does_not_break_the_loop(self, monitor, sqs):
        """The EC2-state poll runs in the same iteration and must still run.

        ``_monitoring_loop`` catches broadly, so a raise here would also skip
        ``_process_interruption_events`` and strand whatever is already queued.
        """
        sqs.receive_message.side_effect = ClientError(
            {
                "Error": {
                    "Code": "AWS.SimpleQueueService.NonExistentQueue",
                    "Message": "gone",
                }
            },
            "ReceiveMessage",
        )

        monitor._poll_warning_queue(sqs, MagicMock())

        assert monitor.event_queue.empty()

    def test_an_empty_poll_does_nothing(self, monitor, sqs):
        """SQS omits ``Messages`` entirely rather than returning an empty list."""
        sqs.receive_message.return_value = {"ResponseMetadata": {}}

        monitor._poll_warning_queue(sqs, MagicMock())

        assert monitor.event_queue.empty()
        sqs.delete_message.assert_not_called()

    def test_a_queued_warning_reaches_the_handler(self, monitor, sqs):
        """End to end within the monitor: message in, handler called.

        The two halves are tested separately above; this pins that they compose,
        since ``_poll_warning_queue`` enqueues a 3-tuple and
        ``_process_interruption_events`` unpacks by arity.
        """
        handler = MagicMock()
        monitor.register_instance("i-abc123", handler)

        monitor._poll_warning_queue(sqs, MagicMock())
        monitor._process_interruption_events()

        handler.assert_called_once()
        assert handler.call_args.args[0] == "i-abc123"
        assert handler.call_args.args[1]["Source"] == "eventbridge"


class TestMonitoringLoopWiring:
    """The loop only polls SQS when a notifier exists."""

    def _monitor(self, queue_url):
        monitor = SpotInterruptionMonitor(MagicMock(), check_interval=1)
        monitor.warning_queue_url = queue_url
        # One iteration: the loop checks stop_event before and after the body.
        monitor.stop_event.set()
        return monitor

    def test_the_queue_is_polled_when_a_notifier_exists(self):
        monitor = self._monitor(QUEUE_URL)
        monitor.stop_event.clear()

        def stop_after_one(*args, **kwargs):
            monitor.stop_event.set()

        monitor._poll_warning_queue = MagicMock(side_effect=stop_after_one)
        monitor._check_instance_interruptions = MagicMock()
        monitor._check_fleet_interruptions = MagicMock()

        monitor._monitoring_loop()

        monitor._poll_warning_queue.assert_called_once()

    def test_no_sqs_client_is_built_when_the_notifier_failed(self):
        """Otherwise every iteration polls ``None`` as a queue URL.

        SQS answers that with ``InvalidAddress``, once per check interval for
        the life of the workflow, burying the real fallback path in errors.
        """
        monitor = SpotInterruptionMonitor(MagicMock(), check_interval=1)
        assert monitor.warning_queue_url is None

        def stop_after_one(*args, **kwargs):
            monitor.stop_event.set()

        monitor._poll_warning_queue = MagicMock()
        monitor._check_instance_interruptions = MagicMock(side_effect=stop_after_one)
        monitor._check_fleet_interruptions = MagicMock()

        monitor._monitoring_loop()

        monitor._poll_warning_queue.assert_not_called()
        # The fallback track still ran.
        monitor._check_instance_interruptions.assert_called_once()
        assert "sqs" not in [
            call.args[0] for call in monitor.session.client.call_args_list
        ]

"""Real-AWS end-to-end tests for the spot interruption warning (issue #86).

Unit tests can prove the provider sends the right calls in the right order.
Only AWS can prove the three claims that actually matter here:

* **EventBridge delivers to SQS with no IAM role.** Every other common target
  type needs one, so ``put_targets`` succeeding without a ``RoleArn`` is a
  property of SQS's resource-policy authorisation, not something a mock can
  confirm. If it were wrong, the rule would exist, the queue would exist, and no
  warning would ever arrive -- indistinguishable from an account that simply had
  no interruptions.
* **The warning arrives while the instance is still running.** That is the entire
  point: the EC2-state poll it replaces can only see ``shutting-down``, after the
  reclaim, too late to checkpoint. A previous manual probe measured 15.2s from
  the Fault Injection Simulator experiment starting to the message landing in
  SQS, with the instance still ``running``.
* **The rule and queue are deleted on shutdown.** EventBridge caps rules per
  account per region, and SQS refuses to recreate a queue for 60 seconds after
  deletion -- so a leaked queue also blocks the next provider that picks the same
  provider-ID prefix.

The interruption itself is driven by AWS Fault Injection Simulator
(``aws:ec2:send-spot-instance-interruptions``), which is the only way to make a
real spot interruption happen on demand. ``ec2:send-spot-instance-interruptions``
is not an EC2 API -- boto3 has no such method -- and ``events:PutEvents`` cannot
be used to fake one either: EventBridge rejects a hand-written event whose
``Source`` starts with ``aws.`` outright, with
``NotAuthorizedForSourceException``. So a genuine interruption is the only
interruption available, and FIS is how to cause one.

Requires an IAM role FIS can assume, holding
``AWSFaultInjectionSimulatorEC2Access``. Set ``AWS_TEST_FIS_ROLE_ARN``; the FIS
tests skip without it, and the notifier-wiring tests above them do not need it.

Run with::

    AWS_TEST_REGION=us-east-1 AWS_TEST_VPC_ID=vpc-xxx \\
    AWS_TEST_SUBNET_ID=subnet-xxx AWS_TEST_SG_ID=sg-xxx \\
    AWS_TEST_FIS_ROLE_ARN=arn:aws:iam::NNN:role/FISRole \\
    AWS_PROFILE=aws uv run pytest tests/aws/test_spot_warning_e2e.py \\
        -m aws --no-cov -v

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import json
import logging
import os
import time
import uuid

import pytest

from parsl_ephemeral_aws.compute.spot_interruption import SpotInterruptionMonitor
from parsl_ephemeral_aws.constants import (
    SPOT_INTERRUPTION_EVENT_DETAIL_TYPE,
    SPOT_INTERRUPTION_EVENT_SOURCE,
    SPOT_INTERRUPTION_QUEUE_RETENTION_SECONDS,
    SPOT_INTERRUPTION_RULE_NAME_PREFIX,
    TAG_MANAGED,
)
from parsl_ephemeral_aws.provider import EphemeralAWSProvider
from parsl_ephemeral_aws.utils.aws import (
    create_spot_interruption_notifier,
    delete_spot_interruption_notifier,
)

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.aws, pytest.mark.slow]

AWS_TEST_FIS_ROLE_ARN = os.environ.get("AWS_TEST_FIS_ROLE_ARN")

# The warning arrived 15.2s in when this was measured by hand. Three minutes
# leaves room for FIS to resolve its targets and for one long-poll cycle, while
# still failing well inside the two-minute notice the experiment then honours.
WARNING_TIMEOUT_S = 180


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _rule_arn(events, name):
    return events.describe_rule(Name=name)["Arn"]


def _drain(sqs, queue_url, deadline):
    """Yield parsed message bodies until *deadline*, deleting as it goes.

    Long-polls, because a warning that is one second late is still a warning and
    a tight loop would just spend the budget on empty receives.
    """
    while time.time() < deadline:
        response = sqs.receive_message(
            QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=20
        )
        for message in response.get("Messages", []):
            sqs.delete_message(
                QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"]
            )
            try:
                yield json.loads(message["Body"])
            except ValueError:
                logger.warning("unparseable message body: %s", message.get("Body"))


@pytest.fixture
def notifier(aws_session, aws_region, test_run_id):
    """A real EventBridge rule wired to a real SQS queue, torn down after.

    Named with the run ID rather than a provider ID so a leaked one is traceable
    to the test that leaked it.
    """
    events = aws_session.client("events", region_name=aws_region)
    sqs = aws_session.client("sqs", region_name=aws_region)
    name = f"{SPOT_INTERRUPTION_RULE_NAME_PREFIX}-e2e-{test_run_id}"

    rule_name, queue_url, queue_arn = create_spot_interruption_notifier(
        events, sqs, name, tags=[{"Key": TAG_MANAGED, "Value": "true"}]
    )

    yield dict(
        events=events,
        sqs=sqs,
        rule_name=rule_name,
        queue_url=queue_url,
        queue_arn=queue_arn,
    )

    delete_spot_interruption_notifier(events, sqs, rule_name, queue_url)


# ---------------------------------------------------------------------------
# TestNotifierWiring
# ---------------------------------------------------------------------------


class TestNotifierWiring:
    """What AWS stored, as against what the provider sent."""

    def test_the_rule_targets_the_queue_without_a_role(self, notifier):
        """SQS delivery is authorised by the queue policy, not by a role.

        This is the claim a mock cannot check. ``put_targets`` answers
        ``FailedEntryCount=0`` here with no ``RoleArn`` sent, and the target is
        readable back -- which is what makes creating and leaking a role for
        this unnecessary.
        """
        targets = notifier["events"].list_targets_by_rule(Rule=notifier["rule_name"])[
            "Targets"
        ]

        assert len(targets) == 1
        assert targets[0]["Arn"] == notifier["queue_arn"]
        assert targets[0]["Id"] == "parsl-spot-warning-queue"
        assert "RoleArn" not in targets[0]

    def test_the_rule_is_enabled_and_scoped_to_the_warning(self, notifier):
        """A disabled rule matches nothing and reports no error for it."""
        rule = notifier["events"].describe_rule(Name=notifier["rule_name"])

        assert rule["State"] == "ENABLED"
        assert json.loads(rule["EventPattern"]) == {
            "source": [SPOT_INTERRUPTION_EVENT_SOURCE],
            "detail-type": [SPOT_INTERRUPTION_EVENT_DETAIL_TYPE],
        }

    def test_the_queue_policy_names_this_rule_only(self, notifier):
        """``aws:SourceArn`` has to resolve to the rule AWS actually created.

        The ARN is composed from the account and region at rule creation, so a
        wrong one is not a syntax error -- it is a policy that silently
        authorises nothing, and the queue receives no warnings at all.
        """
        attributes = notifier["sqs"].get_queue_attributes(
            QueueUrl=notifier["queue_url"], AttributeNames=["Policy"]
        )["Attributes"]
        statement = json.loads(attributes["Policy"])["Statement"][0]

        assert statement["Resource"] == notifier["queue_arn"]
        assert statement["Condition"]["ArnEquals"]["aws:SourceArn"] == _rule_arn(
            notifier["events"], notifier["rule_name"]
        )

    def test_the_queue_retention_was_accepted(self, notifier):
        """SQS clamps out-of-range values rather than rejecting them.

        The floor is 60 seconds; a shorter request comes back as 60, so reading
        it from SQS is the only way to know what retention is really in force.
        """
        attributes = notifier["sqs"].get_queue_attributes(
            QueueUrl=notifier["queue_url"], AttributeNames=["MessageRetentionPeriod"]
        )["Attributes"]

        assert attributes["MessageRetentionPeriod"] == str(
            SPOT_INTERRUPTION_QUEUE_RETENTION_SECONDS
        )

    def test_the_rule_is_tagged_for_cleanup(self, notifier):
        tags = notifier["events"].list_tags_for_resource(
            ResourceARN=_rule_arn(notifier["events"], notifier["rule_name"])
        )["Tags"]

        assert {"Key": TAG_MANAGED, "Value": "true"} in tags

    def test_deleting_the_notifier_removes_both(self, aws_session, aws_region):
        """The rule cannot be deleted while it has a target, so order matters.

        Deleted here rather than in the fixture teardown so the assertion
        happens inside a test: a teardown that silently failed would leave the
        rule counting against the account's per-region limit and nothing would
        report it.
        """
        events = aws_session.client("events", region_name=aws_region)
        sqs = aws_session.client("sqs", region_name=aws_region)
        name = f"{SPOT_INTERRUPTION_RULE_NAME_PREFIX}-del-{uuid.uuid4().hex[:8]}"

        rule_name, queue_url, _ = create_spot_interruption_notifier(events, sqs, name)
        delete_spot_interruption_notifier(events, sqs, rule_name, queue_url)

        with pytest.raises(events.exceptions.ResourceNotFoundException):
            events.describe_rule(Name=rule_name)
        with pytest.raises(sqs.exceptions.QueueDoesNotExist):
            sqs.get_queue_url(QueueName=name)

    def test_deleting_twice_is_not_an_error(self, aws_session, aws_region):
        """Cleanup runs from ``except`` handlers and from ``shutdown()``.

        Both can fire for the same provider, so the second pass has to be a
        no-op rather than a raise that masks the original failure.
        """
        events = aws_session.client("events", region_name=aws_region)
        sqs = aws_session.client("sqs", region_name=aws_region)
        name = f"{SPOT_INTERRUPTION_RULE_NAME_PREFIX}-twice-{uuid.uuid4().hex[:8]}"

        rule_name, queue_url, _ = create_spot_interruption_notifier(events, sqs, name)
        delete_spot_interruption_notifier(events, sqs, rule_name, queue_url)
        delete_spot_interruption_notifier(events, sqs, rule_name, queue_url)


# ---------------------------------------------------------------------------
# TestMonitorAgainstRealAWS
# ---------------------------------------------------------------------------


class TestMonitorAgainstRealAWS:
    """The monitor's own create/poll/delete cycle, against real services."""

    def test_start_and_stop_create_and_delete_the_notifier(
        self, aws_session, test_run_id
    ):
        """No orphan rule after a full cycle, and none before ``start``.

        The monitor is built in every mode's ``__init__``, so a construction
        that created the rule would leave one behind for every provider that
        then failed to initialise -- with no ``stop_monitoring`` ever reached.
        """
        monitor = SpotInterruptionMonitor(
            aws_session, check_interval=1, provider_id=f"e2e{test_run_id}"
        )
        events = aws_session.client("events")

        assert monitor.warning_rule_name is None

        monitor.start_monitoring()
        try:
            assert monitor.warning_queue_url, (
                "notifier was not created; without it the monitor degrades to "
                "the post-facto EC2 poll and there is no advance warning"
            )
            # Captured before the stop clears it, or there is nothing left to
            # look the deleted rule up by.
            created_rule = monitor.warning_rule_name
            rule = events.describe_rule(Name=created_rule)
            assert rule["State"] == "ENABLED"
        finally:
            monitor.stop_monitoring()

        assert monitor.warning_rule_name is None
        with pytest.raises(events.exceptions.ResourceNotFoundException):
            events.describe_rule(Name=created_rule)

    def test_the_monitor_receives_a_warning_it_is_watching_for(
        self, aws_session, test_run_id
    ):
        """A hand-placed message on the real queue reaches the real handler.

        Not a real interruption -- that needs FIS, and is covered below. What
        this isolates is the SQS half: a message whose envelope matches what
        EventBridge delivers is received, deleted, attributed to the registered
        instance, and passed to the handler. Splitting it out means a failure in
        the FIS test can be read as "the warning did not arrive" rather than
        "something in the receive path is broken".
        """
        monitor = SpotInterruptionMonitor(
            aws_session, check_interval=1, provider_id=f"rx{test_run_id}"
        )
        received = []
        monitor.register_instance(
            "i-0123456789abcdef0", lambda i, d: received.append(d)
        )

        monitor.start_monitoring()
        try:
            assert monitor.warning_queue_url, "notifier was not created"
            sqs = aws_session.client("sqs")
            sqs.send_message(
                QueueUrl=monitor.warning_queue_url,
                MessageBody=json.dumps(
                    {
                        "source": SPOT_INTERRUPTION_EVENT_SOURCE,
                        "detail-type": SPOT_INTERRUPTION_EVENT_DETAIL_TYPE,
                        "detail": {
                            "instance-id": "i-0123456789abcdef0",
                            "instance-action": "terminate",
                        },
                    }
                ),
            )

            deadline = time.time() + 60
            while time.time() < deadline and not received:
                time.sleep(2)

            assert received, (
                "the monitoring thread did not deliver a warning placed directly "
                "on its queue within 60s"
            )
            assert received[0]["InstanceId"] == "i-0123456789abcdef0"
            assert received[0]["Source"] == "eventbridge"
        finally:
            monitor.stop_monitoring()


# ---------------------------------------------------------------------------
# TestFISInterruption
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not AWS_TEST_FIS_ROLE_ARN,
    reason="Set AWS_TEST_FIS_ROLE_ARN to a role FIS can assume to run these",
)
class TestFISInterruption:
    """A genuine spot interruption, and where the warning is in its timeline."""

    def _experiment_template(self, fis, tag_value):
        """Create a template targeting spot instances by tag.

        ``durationBeforeInterruption`` is the *total* window, and its minimum is
        two minutes -- FIS sends the warning immediately and terminates at the
        end. So the instance really does stay alive for the notice period, which
        is what makes the "still running when the warning arrives" assertion
        meaningful rather than a race that happened to be won.
        """
        return fis.create_experiment_template(
            description="parsl-ephemeral-aws E2E spot interruption warning (#86)",
            targets={
                "workers": {
                    "resourceType": "aws:ec2:spot-instance",
                    "resourceTags": {"E2ETestRunId": tag_value},
                    "selectionMode": "ALL",
                }
            },
            actions={
                "interrupt": {
                    "actionId": "aws:ec2:send-spot-instance-interruptions",
                    "parameters": {"durationBeforeInterruption": "PT2M"},
                    "targets": {"SpotInstances": "workers"},
                }
            },
            stopConditions=[{"source": "none"}],
            roleArn=AWS_TEST_FIS_ROLE_ARN,
            tags={"ManagedBy": "parsl-ephemeral-aws-e2e"},
        )["experimentTemplate"]["id"]

    def test_the_warning_arrives_before_the_instance_dies(
        self, tmp_path, aws_session, aws_region, test_run_id, network_ids, notifier
    ):
        """The claim the whole of 6.3 rests on.

        The EC2-state poll this replaces reports an interruption only once the
        instance reaches ``shutting-down``. If the warning arrived no earlier
        than that, the EventBridge machinery would be pure cost -- so the
        assertion is not just that a message arrives, but that the instance is
        still ``running`` when it does, with time left to checkpoint.

        Uses its own provider rather than ``spot_provider`` so the E2E run tag
        FIS targets is the only thing selected: ``selectionMode: ALL`` would
        otherwise interrupt every tagged spot instance in the account.
        """
        fis = aws_session.client("fis", region_name=aws_region)
        ec2 = aws_session.client("ec2", region_name=aws_region)

        provider = EphemeralAWSProvider(
            region=aws_region,
            instance_type="t3.micro",
            mode="standard",
            use_spot=True,
            spot_interruption_handling=False,
            state_store_type="file",
            state_file_path=str(tmp_path / f"state-warn-{test_run_id}.json"),
            auto_shutdown=True,
            auto_create_instance_profile=True,
            profile_name=aws_session.profile_name or "aws",
            additional_tags={"E2ETestRunId": test_run_id, "AutoCleanup": "true"},
            waiter_delay=15,
            waiter_max_attempts=40,
            debug=True,
            **network_ids,
        )

        template_id = None
        job_id = None
        try:
            job_id = provider.submit("sleep 600", tasks_per_node=1)
            instance_id = provider.job_map[job_id]["resource_id"]
            logger.info("submitted spot instance %s for job %s", instance_id, job_id)

            # FIS resolves targets at experiment start, so the instance has to be
            # running first -- an experiment that resolves nothing fails outright
            # under emptyTargetResolutionMode: fail.
            ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])

            template_id = self._experiment_template(fis, test_run_id)
            experiment = fis.start_experiment(experimentTemplateId=template_id)[
                "experiment"
            ]
            started = time.time()
            logger.info("started FIS experiment %s", experiment["id"])

            deadline = started + WARNING_TIMEOUT_S
            warning = None
            for body in _drain(notifier["sqs"], notifier["queue_url"], deadline):
                if body.get("detail", {}).get("instance-id") == instance_id:
                    warning = body
                    break

            elapsed = time.time() - started
            assert warning is not None, (
                f"no interruption warning for {instance_id} within "
                f"{WARNING_TIMEOUT_S}s of the FIS experiment starting; the "
                "rule, the queue policy, or the target is not delivering"
            )

            # The assertion that distinguishes this from the poll it replaces.
            state = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][
                0
            ]["Instances"][0]["State"]["Name"]
            logger.info(
                "warning for %s arrived after %.1fs, instance state %s",
                instance_id,
                elapsed,
                state,
            )
            assert state == "running", (
                f"the warning arrived at {elapsed:.1f}s but {instance_id} was "
                f"already {state}; there is no lead time to checkpoint in and "
                "the EventBridge notifier buys nothing over the EC2-state poll"
            )

            assert warning["detail-type"] == SPOT_INTERRUPTION_EVENT_DETAIL_TYPE
            assert warning["source"] == SPOT_INTERRUPTION_EVENT_SOURCE
            assert warning["detail"]["instance-action"] in (
                "terminate",
                "stop",
                "hibernate",
            )
        finally:
            if job_id:
                try:
                    provider.cancel([job_id])
                except Exception as exc:
                    logger.warning("cancel raised (ignored): %s", exc)
            try:
                provider.shutdown()
            except Exception as exc:
                logger.warning("shutdown raised (ignored): %s", exc)
            if template_id:
                try:
                    fis.delete_experiment_template(id=template_id)
                except Exception as exc:
                    logger.warning("FIS template cleanup: %s (ignored)", exc)

    def test_a_registered_handler_fires_on_a_real_interruption(
        self, tmp_path, aws_session, aws_region, test_run_id, network_ids
    ):
        """The provider's own monitor, not a hand-driven queue drain.

        The test above proves the message arrives. This proves the wiring the
        provider builds for itself carries it the rest of the way: mode
        initialisation creates the notifier, ``_create_spot_instance`` registers
        the instance, the background thread polls, and the registered handler is
        called with ``Source: eventbridge`` -- the flag telling a checkpointing
        handler it still has time.

        The handler is swapped for a recorder rather than left as the real
        S3-checkpointing one, so the assertion is about delivery and not about
        whether a checkpoint happened to succeed. ``checkpoint_bucket`` is still
        required, because it is what makes the mode build a monitor at all.
        """
        fis = aws_session.client("fis", region_name=aws_region)
        ec2 = aws_session.client("ec2", region_name=aws_region)
        s3 = aws_session.client("s3", region_name=aws_region)
        bucket = f"parsl-e2e-warn-{test_run_id}"
        if aws_region == "us-east-1":
            s3.create_bucket(Bucket=bucket)
        else:
            s3.create_bucket(
                Bucket=bucket,
                CreateBucketConfiguration={"LocationConstraint": aws_region},
            )

        provider = EphemeralAWSProvider(
            region=aws_region,
            instance_type="t3.micro",
            mode="standard",
            use_spot=True,
            spot_interruption_handling=True,
            checkpoint_bucket=bucket,
            state_store_type="file",
            state_file_path=str(tmp_path / f"state-hdl-{test_run_id}.json"),
            auto_shutdown=True,
            auto_create_instance_profile=True,
            profile_name=aws_session.profile_name or "aws",
            additional_tags={"E2ETestRunId": test_run_id, "AutoCleanup": "true"},
            waiter_delay=15,
            waiter_max_attempts=40,
            debug=True,
            **network_ids,
        )

        template_id = None
        job_id = None
        try:
            mode = provider.operating_mode
            monitor = mode.spot_interruption_monitor
            assert monitor is not None, (
                "no interruption monitor was built despite "
                "spot_interruption_handling=True and a checkpoint bucket"
            )

            received = []
            job_id = provider.submit("sleep 600", tasks_per_node=1)
            instance_id = provider.job_map[job_id]["resource_id"]

            # Registered by _create_spot_instance during submit; replaced here so
            # the assertion is about delivery rather than about S3.
            assert instance_id in monitor.instance_handlers, (
                f"{instance_id} was never registered with the monitor, so no "
                "warning for it can ever be routed"
            )
            monitor.register_instance(instance_id, lambda i, d: received.append((i, d)))
            assert monitor.warning_queue_url, (
                "the monitor degraded to the EC2-state poll; check events/sqs "
                "permissions on the test profile"
            )

            ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])

            template_id = self._experiment_template(fis, test_run_id)
            fis.start_experiment(experimentTemplateId=template_id)
            started = time.time()

            while time.time() - started < WARNING_TIMEOUT_S and not received:
                time.sleep(5)

            assert received, (
                f"the registered handler was not called within "
                f"{WARNING_TIMEOUT_S}s of a real interruption of {instance_id}"
            )
            handled_id, details = received[0]
            assert handled_id == instance_id
            assert details["Source"] == "eventbridge", (
                "the warning was attributed to the post-facto EC2-state poll, "
                "so a handler would skip checkpointing"
            )
        finally:
            if job_id:
                try:
                    provider.cancel([job_id])
                except Exception as exc:
                    logger.warning("cancel raised (ignored): %s", exc)
            try:
                provider.shutdown()
            except Exception as exc:
                logger.warning("shutdown raised (ignored): %s", exc)
            if template_id:
                try:
                    fis.delete_experiment_template(id=template_id)
                except Exception as exc:
                    logger.warning("FIS template cleanup: %s (ignored)", exc)
            try:
                paginator = s3.get_paginator("list_objects_v2")
                for page in paginator.paginate(Bucket=bucket):
                    objects = page.get("Contents", [])
                    if objects:
                        s3.delete_objects(
                            Bucket=bucket,
                            Delete={"Objects": [{"Key": o["Key"]} for o in objects]},
                        )
                s3.delete_bucket(Bucket=bucket)
            except Exception as exc:
                logger.warning("checkpoint bucket cleanup: %s (ignored)", exc)

"""Detection of AWS spot instance interruptions.

This module detects that AWS is reclaiming a spot instance and calls the
handlers registered for it. It does not decide what to do about it: the
response lives on :class:`~parsl_ephemeral_provider.modes.base.OperatingMode`, which
marks the doomed block interrupted so Parsl re-runs its tasks.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import json
import logging
import time
import threading
import queue
import uuid
import boto3
from typing import Dict, List, Optional, Callable, Any
from botocore.exceptions import ClientError

from parsl_ephemeral_provider.constants import (
    DEFAULT_SPOT_INTERRUPTION_CHECK_INTERVAL,
    DEFAULT_SPOT_INTERRUPTION_LEAD_TIME,
    SPOT_INTERRUPTION_QUEUE_WAIT_SECONDS,
    SPOT_INTERRUPTION_RULE_NAME_PREFIX,
    TAG_MANAGED,
)
from parsl_ephemeral_provider.utils.aws import (
    create_spot_interruption_notifier,
    delete_spot_interruption_notifier,
    get_ec2_fleet_instance_ids,
)

logger = logging.getLogger(__name__)


class SpotInterruptionMonitor:
    """Monitor for AWS spot instance interruption notices.

    The SpotInterruptionMonitor checks for spot instance interruption notices and
    executes recovery actions when interruptions are detected. It can monitor both
    individual spot instances and spot fleet requests.

    Detection runs on two tracks. The EventBridge warning is the one that
    matters: it arrives roughly two minutes *before* the reclaim, with the
    instance still running, so the block can be marked ``STATUS_INTERRUPTED``
    while the executor still has time to stop dispatching into it. The EC2-state
    poll is kept as a fallback for when the notifier could not be created
    (missing ``events``/``sqs`` permissions, say), but it can only ever report an
    interruption post-facto, once the instance has already reached
    ``shutting-down`` -- by which time work has been dispatched to a worker that
    is already gone.

    Attributes
    ----------
    session : boto3.Session
        AWS session for making API calls
    check_interval : int
        Interval in seconds between checks for interruption notices
    lead_time : int
        Minimum time in seconds we want for recovery before instance termination
    instance_handlers : Dict[str, Callable]
        Mapping of instance IDs to handler functions
    fleet_handlers : Dict[str, Callable]
        Mapping of fleet request IDs to handler functions
    monitoring_thread : Optional[threading.Thread]
        Thread for background monitoring
    stop_event : threading.Event
        Event to signal thread termination
    warning_rule_name : Optional[str]
        EventBridge rule delivering interruption warnings, once created.
    warning_queue_url : Optional[str]
        SQS queue the rule delivers to, once created.
    """

    def __init__(
        self,
        session: boto3.Session,
        check_interval: int = DEFAULT_SPOT_INTERRUPTION_CHECK_INTERVAL,
        lead_time: int = DEFAULT_SPOT_INTERRUPTION_LEAD_TIME,
        provider_id: Optional[str] = None,
        use_event_bridge: bool = True,
    ) -> None:
        """Initialize the SpotInterruptionMonitor.

        Parameters
        ----------
        session : boto3.Session
            AWS session for making API calls
        check_interval : int, optional
            Interval in seconds between checks for interruption notices
        lead_time : int, optional
            Minimum time in seconds we want for recovery before instance termination
        provider_id : Optional[str], optional
            Names the EventBridge rule and SQS queue, keeping them unique per
            provider. Falls back to a random suffix when omitted.
        use_event_bridge : bool, optional
            Whether to create the EventBridge notifier that supplies the
            two-minute advance warning. Set False to rely solely on the
            post-facto EC2-state poll -- useful when the caller's IAM policy
            grants no ``events``/``sqs`` access.
        """
        self.session = session
        self.check_interval = check_interval
        self.lead_time = lead_time
        self.instance_handlers: Dict[str, Callable] = {}  # instance_id -> handler
        self.fleet_handlers: Dict[str, Callable] = {}  # fleet_request_id -> handler
        self._lock = threading.RLock()  # protects dict mutations from concurrent access

        # Background monitoring
        self.monitoring_thread = None
        self.stop_event = threading.Event()
        self.event_queue: queue.Queue = queue.Queue()

        # EventBridge notifier (#86). Created lazily by start_monitoring() rather
        # than here, so constructing a monitor makes no AWS calls and cannot
        # fail -- every mode builds one during __init__.
        self.use_event_bridge = use_event_bridge
        self._notifier_name = (
            f"{SPOT_INTERRUPTION_RULE_NAME_PREFIX}-"
            f"{(provider_id or uuid.uuid4().hex)[:8]}"
        )
        self.warning_rule_name: Optional[str] = None
        self.warning_queue_url: Optional[str] = None

    def register_instance(
        self, instance_id: str, handler: Callable[[str, Dict[str, Any]], None]
    ) -> None:
        """Register a spot instance to be monitored.

        Parameters
        ----------
        instance_id : str
            ID of the spot instance to monitor
        handler : Callable[[str, Dict[str, Any]], None]
            Function to call when interruption is detected, receives instance_id and event details
        """
        with self._lock:
            self.instance_handlers[instance_id] = handler
        logger.info(
            f"Registered spot instance {instance_id} for interruption monitoring"
        )

    def register_fleet(
        self,
        fleet_request_id: str,
        handler: Callable[[str, List[str], Dict[str, Any]], None],
    ) -> None:
        """Register a spot fleet to be monitored.

        Parameters
        ----------
        fleet_request_id : str
            ID of the spot fleet request to monitor
        handler : Callable[[str, List[str], Dict[str, Any]], None]
            Function to call when interruption is detected, receives fleet_request_id,
            list of affected instance_ids, and event details
        """
        with self._lock:
            self.fleet_handlers[fleet_request_id] = handler
        logger.info(
            f"Registered spot fleet {fleet_request_id} for interruption monitoring"
        )

    def deregister_instance(self, instance_id: str) -> None:
        """Stop monitoring a spot instance.

        Parameters
        ----------
        instance_id : str
            ID of the spot instance to stop monitoring
        """
        with self._lock:
            if instance_id in self.instance_handlers:
                del self.instance_handlers[instance_id]
        logger.info(
            f"Deregistered spot instance {instance_id} from interruption monitoring"
        )

    def deregister_fleet(self, fleet_request_id: str) -> None:
        """Stop monitoring a spot fleet.

        Parameters
        ----------
        fleet_request_id : str
            ID of the spot fleet request to stop monitoring
        """
        with self._lock:
            if fleet_request_id in self.fleet_handlers:
                del self.fleet_handlers[fleet_request_id]
        logger.info(
            f"Deregistered spot fleet {fleet_request_id} from interruption monitoring"
        )

    def start_monitoring(self) -> None:
        """Start background monitoring for spot interruption notices.

        Creates the EventBridge notifier first, when enabled. A failure there is
        logged and not raised: losing the advance warning degrades this to the
        post-facto EC2-state poll, which is worse but still functional, and is
        not a reason to fail the workflow that was about to run.
        """
        if self.monitoring_thread is not None and self.monitoring_thread.is_alive():
            logger.warning("Monitoring thread is already running")
            return

        self._ensure_warning_notifier()

        self.stop_event.clear()
        self.monitoring_thread = threading.Thread(
            target=self._monitoring_loop, daemon=True
        )
        self.monitoring_thread.start()
        logger.info("Started spot interruption monitoring")

    def stop_monitoring(self) -> None:
        """Stop background monitoring, and delete the EventBridge notifier.

        The notifier is torn down even when no thread was running, so a monitor
        that was stopped twice -- or whose thread died -- still cleans up the rule
        and queue it created rather than leaking them.
        """
        if self.monitoring_thread is None or not self.monitoring_thread.is_alive():
            logger.warning("No monitoring thread is running")
            self._delete_warning_notifier()
            return

        self.stop_event.set()
        self.monitoring_thread.join(timeout=5.0)
        if self.monitoring_thread.is_alive():
            logger.warning("Monitoring thread did not terminate gracefully")

        self.monitoring_thread = None
        self._delete_warning_notifier()
        logger.info("Stopped spot interruption monitoring")

    def _ensure_warning_notifier(self) -> None:
        """Create the EventBridge rule and SQS queue, if not already present."""
        if not self.use_event_bridge or self.warning_queue_url:
            return

        try:
            rule_name, queue_url, _ = create_spot_interruption_notifier(
                self.session.client("events"),
                self.session.client("sqs"),
                self._notifier_name,
                tags=[{"Key": TAG_MANAGED, "Value": "true"}],
            )
        except Exception as e:
            # Degrade to the EC2-state poll rather than failing the workflow.
            # The poll cannot see a warning in advance, so log loudly enough that
            # the loss of the two-minute lead time is visible.
            logger.warning(
                "Could not create the spot interruption notifier, falling back "
                "to post-facto EC2 state polling -- interruptions will be "
                f"detected only once an instance is already shutting down: {e}"
            )
            return

        self.warning_rule_name = rule_name
        self.warning_queue_url = queue_url

    def _delete_warning_notifier(self) -> None:
        """Delete the EventBridge rule and SQS queue, if this monitor made them."""
        if not (self.warning_rule_name or self.warning_queue_url):
            return

        delete_spot_interruption_notifier(
            self.session.client("events"),
            self.session.client("sqs"),
            self.warning_rule_name,
            self.warning_queue_url,
        )
        self.warning_rule_name = None
        self.warning_queue_url = None

    def _monitoring_loop(self) -> None:
        """Main loop for checking spot interruption notices."""
        ec2_client = self.session.client("ec2")
        cloudwatch_client = self.session.client("cloudwatch")
        sqs_client = self.session.client("sqs") if self.warning_queue_url else None

        while not self.stop_event.is_set():
            try:
                # The advance warning first: it is the only source that fires
                # while the instance is still running.
                if sqs_client is not None:
                    self._poll_warning_queue(sqs_client, ec2_client)

                # Check instance interruption notices
                self._check_instance_interruptions(ec2_client, cloudwatch_client)

                # Check fleet interruptions
                self._check_fleet_interruptions(ec2_client)

                # Process any interruption events in the queue
                self._process_interruption_events()

            except Exception as e:
                logger.error(f"Error in spot interruption monitoring: {e}")

            # Wait for next check interval or until stop is requested.
            # _poll_warning_queue already long-polls SQS, so when the notifier is
            # active most of the interval is spent blocked on a warning arriving
            # rather than idling.
            self.stop_event.wait(self.check_interval)

    def _poll_warning_queue(self, sqs_client: Any, ec2_client: Any) -> None:
        """Drain interruption warnings from the notifier queue (#86).

        Each message is an EventBridge envelope whose ``detail`` carries
        ``instance-id`` and ``instance-action``. Confirmed against a real
        FIS-driven interruption; the arriving instance was still ``running``,
        which is what makes this track actionable where the EC2-state poll is
        not.

        The rule cannot be scoped to specific instances -- their IDs are not
        known until the fleet launches -- so it matches every spot interruption
        in the account and region. Warnings for instances this monitor does not
        track are therefore expected, and are dropped rather than logged as
        errors.
        """
        try:
            response = sqs_client.receive_message(
                QueueUrl=self.warning_queue_url,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=min(
                    SPOT_INTERRUPTION_QUEUE_WAIT_SECONDS, self.check_interval
                ),
            )
        except ClientError as e:
            logger.error(f"Error polling spot interruption warning queue: {e}")
            return

        for message in response.get("Messages", []):
            # Delete first, unconditionally. A message that cannot be parsed, or
            # that names an instance this monitor does not own, would otherwise
            # be redelivered on every poll for the whole retention period.
            try:
                sqs_client.delete_message(
                    QueueUrl=self.warning_queue_url,
                    ReceiptHandle=message["ReceiptHandle"],
                )
            except ClientError as e:
                logger.warning(f"Could not delete warning message: {e}")

            try:
                body = json.loads(message["Body"])
            except (ValueError, KeyError) as e:
                logger.warning(f"Unparseable spot interruption warning: {e}")
                continue

            detail = body.get("detail", {})
            instance_id = detail.get("instance-id")
            if not instance_id:
                continue

            event_details = {
                "InstanceId": instance_id,
                "InstanceAction": detail.get("instance-action", "terminate"),
                "NoticeTime": time.time(),
                # Distinguishes an advance warning from the post-facto poll, so a
                # handler can tell whether the instance is still alive.
                "Source": "eventbridge",
            }
            self._queue_warning(instance_id, event_details, ec2_client)

    def _queue_warning(
        self, instance_id: str, event_details: Dict[str, Any], ec2_client: Any
    ) -> None:
        """Route a warning for *instance_id* to whichever handler owns it.

        An instance may be registered directly, or belong to a registered fleet,
        or be neither -- the rule matches the whole account, so unowned instances
        are the common case and are silently ignored.
        """
        with self._lock:
            if instance_id in self.instance_handlers:
                self.event_queue.put(("instance", instance_id, event_details))
                logger.info(
                    f"Spot interruption warning for instance {instance_id}: "
                    f"{event_details['InstanceAction']}"
                )
                return
            fleet_ids = list(self.fleet_handlers.keys())

        for fleet_id in fleet_ids:
            if instance_id in get_ec2_fleet_instance_ids(ec2_client, fleet_id):
                fleet_details = dict(event_details, FleetRequestId=fleet_id)
                self.event_queue.put(("fleet", fleet_id, [instance_id], fleet_details))
                logger.info(
                    f"Spot interruption warning for instance {instance_id} "
                    f"in fleet {fleet_id}"
                )
                return

        logger.debug(
            f"Ignoring spot interruption warning for unmonitored instance {instance_id}"
        )

    def _check_instance_interruptions(self, ec2_client, cloudwatch_client) -> None:
        """Detect already-interrupted instances from their EC2 state.

        The fallback track. It reports an interruption only once the instance has
        reached ``shutting-down`` or ``stopping`` -- after the reclaim -- so it
        exists to catch what the EventBridge notifier misses, or to cover the case
        where the notifier could not be created at all.
        :meth:`_poll_warning_queue` is the one that gives advance notice.
        """
        if not self.instance_handlers:
            return

        with self._lock:
            instance_ids = list(self.instance_handlers.keys())

        try:
            # Detect termination using real, observable EC2 states.
            instances = ec2_client.describe_instances(InstanceIds=instance_ids)
            for reservation in instances.get("Reservations", []):
                for instance in reservation.get("Instances", []):
                    instance_id = instance["InstanceId"]
                    state_name = instance.get("State", {}).get("Name", "")
                    is_spot = instance.get("InstanceLifecycle") == "spot"

                    if is_spot and state_name in ("shutting-down", "stopping"):
                        with self._lock:
                            handler = self.instance_handlers.get(instance_id)
                        if handler:
                            event_details = {
                                "InstanceId": instance_id,
                                "InstanceAction": "terminate",
                                "NoticeTime": time.time(),
                                # No lead time left on this track: the instance is
                                # already going, so a handler knows the marker is
                                # after the fact rather than ahead of it.
                                "Source": "ec2-state",
                            }
                            self.event_queue.put(
                                ("instance", instance_id, event_details)
                            )

        except ClientError as e:
            logger.error(f"Error checking spot instance interruptions: {e}")

    def _check_fleet_interruptions(self, ec2_client) -> None:
        """Detect already-interrupted fleet instances from their EC2 state.

        The fleet counterpart of :meth:`_check_instance_interruptions`, and
        equally post-facto. See that method for why this is the fallback track.
        """
        if not self.fleet_handlers:
            return

        with self._lock:
            fleet_request_ids = list(self.fleet_handlers.keys())

        try:
            # Get the instances in each fleet. Goes through the fleet-id tag
            # rather than describe_spot_fleet_instances, which rejects an EC2
            # Fleet of type instant outright (#86).
            for fleet_id in fleet_request_ids:
                instance_ids = get_ec2_fleet_instance_ids(ec2_client, fleet_id)

                if not instance_ids:
                    continue

                # Detect interruption via real observable EC2 states.
                # Spot instances entering "shutting-down" or "stopping" are
                # treated as interrupted (post-facto detection).
                instances = ec2_client.describe_instances(InstanceIds=instance_ids)

                interrupted_instances = []
                for reservation in instances.get("Reservations", []):
                    for instance in reservation.get("Instances", []):
                        instance_id = instance["InstanceId"]
                        state_name = instance.get("State", {}).get("Name", "")
                        is_spot = instance.get("InstanceLifecycle") == "spot"

                        if is_spot and state_name in ("shutting-down", "stopping"):
                            interrupted_instances.append(instance_id)

                if interrupted_instances:
                    with self._lock:
                        handler = self.fleet_handlers.get(fleet_id)
                    if handler:
                        event_details = {
                            "FleetRequestId": fleet_id,
                            "InstanceAction": "terminate",
                            "NoticeTime": time.time(),
                            "Source": "ec2-state",
                        }
                        self.event_queue.put(
                            ("fleet", fleet_id, interrupted_instances, event_details)
                        )

        except ClientError as e:
            logger.error(f"Error checking spot fleet interruptions: {e}")

    def _process_interruption_events(self) -> None:
        """Process any interruption events in the queue."""
        try:
            while True:
                event = self.event_queue.get_nowait()

                if event[0] == "instance":
                    _, instance_id, event_details = event
                    with self._lock:
                        handler = self.instance_handlers.get(instance_id)
                    if handler:
                        try:
                            handler(instance_id, event_details)
                        except Exception as e:
                            logger.error(
                                f"Error in instance interruption handler for {instance_id}: {e}"
                            )

                elif event[0] == "fleet":
                    _, fleet_id, instance_ids, event_details = event
                    with self._lock:
                        handler = self.fleet_handlers.get(fleet_id)
                    if handler:
                        try:
                            handler(fleet_id, instance_ids, event_details)
                        except Exception as e:
                            logger.error(
                                f"Error in fleet interruption handler for {fleet_id}: {e}"
                            )

                self.event_queue.task_done()

        except queue.Empty:
            pass


# The interruption *response* is not here: it lives on
# ``modes.base.OperatingMode.handle_instance_interruption``, which marks the
# doomed block ``STATUS_INTERRUPTED`` so the provider reports
# ``JobState.FAILED`` and Parsl re-runs the lost tasks under the executor's own
# ``retries``.
#
# This module used to also carry ``SpotInterruptionHandler`` and
# ``ParslSpotInterruptionHandler``, a checkpoint/recovery API that could not
# work at this layer and never ran (#137). Its entry point was
# ``register_task(task_id, instance_id)``, and a Parsl provider is never told a
# task ID -- ``submit(command, tasks_per_node, job_name)`` is the whole
# contract, because providers manage *blocks* while the executor manages tasks.
# Nothing in the package could call it, so ``task_mapping`` was always empty,
# every interruption logged "No registered tasks found", and
# ``save_checkpoint``/``recover_tasks``/``checkpointable`` were dead alongside
# it. Being documented as working made it worse than absent.

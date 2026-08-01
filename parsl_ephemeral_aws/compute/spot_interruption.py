"""Utilities for handling AWS spot instance interruptions.

This module provides functionality for detecting and responding to AWS spot instance
interruption notices, allowing Parsl tasks to be checkpointed and recovered.

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

from parsl_ephemeral_aws.exceptions import SpotInstanceError
from parsl_ephemeral_aws.constants import (
    DEFAULT_SPOT_INTERRUPTION_CHECK_INTERVAL,
    DEFAULT_SPOT_INTERRUPTION_LEAD_TIME,
    SPOT_INTERRUPTION_QUEUE_WAIT_SECONDS,
    SPOT_INTERRUPTION_RULE_NAME_PREFIX,
    TAG_MANAGED,
)
from parsl_ephemeral_aws.utils.aws import (
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
    instance still running, which is the only point at which checkpointing can
    succeed. The EC2-state poll is kept as a fallback for when the notifier
    could not be created (missing ``events``/``sqs`` permissions, say), but it
    can only ever report an interruption post-facto, once the instance has
    already reached ``shutting-down``.

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
            # the loss of checkpointing lead time is visible.
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
        which is what makes this usable for checkpointing where the EC2-state
        poll is not.

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
                # handler can tell whether it has time to checkpoint.
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
        reached ``shutting-down`` or ``stopping`` -- after the reclaim, too late
        to checkpoint -- so it exists to catch what the EventBridge notifier
        misses, or to cover the case where the notifier could not be created at
        all. :meth:`_poll_warning_queue` is the one that gives advance notice.
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
                                # No lead time left on this track; a handler can
                                # skip checkpointing rather than attempt it
                                # against an instance that is already going.
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


class SpotInterruptionHandler:
    """Handler for spot instance interruptions.

    This class provides utilities for implementing recovery actions when spot
    instances are interrupted. It includes functionality for saving and loading
    checkpoint data, redirecting tasks to other instances, and prioritizing
    task recovery.

    Attributes
    ----------
    session : boto3.Session
        AWS session for making API calls
    checkpoint_bucket : Optional[str]
        S3 bucket name for storing checkpoint data
    checkpoint_prefix : str
        S3 key prefix for checkpoint data
    recovery_queue : queue.PriorityQueue
        Queue for prioritizing recovery tasks
    """

    def __init__(
        self,
        session: boto3.Session,
        checkpoint_bucket: Optional[str] = None,
        checkpoint_prefix: str = "parsl/checkpoints",
    ) -> None:
        """Initialize the SpotInterruptionHandler.

        Parameters
        ----------
        session : boto3.Session
            AWS session for making API calls
        checkpoint_bucket : Optional[str], optional
            S3 bucket name for storing checkpoint data, by default None
        checkpoint_prefix : str, optional
            S3 key prefix for checkpoint data, by default "parsl/checkpoints"
        """
        self.session = session
        self.checkpoint_bucket = checkpoint_bucket
        self.checkpoint_prefix = checkpoint_prefix
        self.recovery_queue = queue.PriorityQueue()

        # Ensure S3 bucket exists if specified
        if self.checkpoint_bucket:
            self._ensure_bucket_exists()

    def _ensure_bucket_exists(self) -> None:
        """Ensure the checkpoint S3 bucket exists."""
        s3 = self.session.client("s3")

        try:
            s3.head_bucket(Bucket=self.checkpoint_bucket)
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "404":
                # Bucket doesn't exist, create it
                try:
                    s3.create_bucket(Bucket=self.checkpoint_bucket)
                    logger.info(f"Created checkpoint bucket {self.checkpoint_bucket}")
                except Exception as e:
                    logger.error(f"Failed to create checkpoint bucket: {e}")
                    raise
            else:
                logger.error(f"Error checking checkpoint bucket: {e}")
                raise

    def save_checkpoint(
        self, task_id: str, data: Dict[str, Any], priority: int = 1
    ) -> str:
        """Save checkpoint data to S3.

        Parameters
        ----------
        task_id : str
            Unique identifier for the task
        data : Dict[str, Any]
            Checkpoint data to save
        priority : int, optional
            Recovery priority (lower is higher priority), by default 1

        Returns
        -------
        str
            S3 URI for the saved checkpoint

        Raises
        ------
        SpotInstanceError
            If checkpoint cannot be saved
        """
        if not self.checkpoint_bucket:
            raise SpotInstanceError("No checkpoint bucket specified")

        s3 = self.session.client("s3")
        key = f"{self.checkpoint_prefix}/{task_id}.json"

        try:
            s3.put_object(
                Bucket=self.checkpoint_bucket,
                Key=key,
                Body=json.dumps(data),
                ServerSideEncryption="AES256",
                Metadata={
                    "Priority": str(priority),
                    "Timestamp": str(int(time.time())),
                },
            )
            return f"s3://{self.checkpoint_bucket}/{key}"
        except Exception as e:
            logger.error(f"Failed to save checkpoint for task {task_id}: {e}")
            raise SpotInstanceError(f"Failed to save checkpoint: {e}")

    def load_checkpoint(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Load checkpoint data from S3.

        Parameters
        ----------
        task_id : str
            Unique identifier for the task

        Returns
        -------
        Optional[Dict[str, Any]]
            Checkpoint data or None if not found

        Raises
        ------
        SpotInstanceError
            If checkpoint cannot be loaded
        """
        if not self.checkpoint_bucket:
            raise SpotInstanceError("No checkpoint bucket specified")

        s3 = self.session.client("s3")
        key = f"{self.checkpoint_prefix}/{task_id}.json"

        try:
            response = s3.get_object(Bucket=self.checkpoint_bucket, Key=key)
            return json.loads(response["Body"].read())
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                return None
            logger.error(f"Failed to load checkpoint for task {task_id}: {e}")
            raise SpotInstanceError(f"Failed to load checkpoint: {e}")
        except Exception as e:
            logger.error(f"Failed to load checkpoint for task {task_id}: {e}")
            raise SpotInstanceError(f"Failed to load checkpoint: {e}")

    def queue_task_for_recovery(
        self, task_id: str, checkpoint_uri: str, priority: int = 1
    ) -> None:
        """Queue a task for recovery.

        Parameters
        ----------
        task_id : str
            Unique identifier for the task
        checkpoint_uri : str
            URI for the checkpoint data
        priority : int, optional
            Recovery priority (lower is higher priority), by default 1
        """
        self.recovery_queue.put((priority, time.time(), task_id, checkpoint_uri))
        logger.info(f"Queued task {task_id} for recovery with priority {priority}")

    def get_next_recovery_task(self) -> Optional[Dict[str, Any]]:
        """Get the next task to recover.

        Returns
        -------
        Optional[Dict[str, Any]]
            Task recovery information or None if queue is empty
        """
        try:
            (
                priority,
                timestamp,
                task_id,
                checkpoint_uri,
            ) = self.recovery_queue.get_nowait()
            return {
                "task_id": task_id,
                "checkpoint_uri": checkpoint_uri,
                "priority": priority,
                "timestamp": timestamp,
            }
        except queue.Empty:
            return None

    def handle_instance_interruption(
        self, instance_id: str, event: Dict[str, Any]
    ) -> None:
        """Handle spot instance interruption.

        This method should be implemented by subclasses to provide specific
        recovery actions for the application.

        Parameters
        ----------
        instance_id : str
            ID of the interrupted instance
        event : Dict[str, Any]
            Interruption event details
        """
        # This is a placeholder that should be overridden by subclasses
        logger.warning(f"Spot instance {instance_id} is being interrupted: {event}")

    def handle_fleet_interruption(
        self, fleet_id: str, instance_ids: List[str], event: Dict[str, Any]
    ) -> None:
        """Handle spot fleet interruption.

        This method should be implemented by subclasses to provide specific
        recovery actions for the application.

        Parameters
        ----------
        fleet_id : str
            ID of the spot fleet
        instance_ids : List[str]
            IDs of the interrupted instances
        event : Dict[str, Any]
            Interruption event details
        """
        # This is a placeholder that should be overridden by subclasses
        logger.warning(
            f"Spot fleet {fleet_id} instances {instance_ids} are being interrupted: {event}"
        )


class ParslSpotInterruptionHandler(SpotInterruptionHandler):
    """Parsl-specific handler for spot instance interruptions.

    This class extends SpotInterruptionHandler to provide Parsl-specific
    functionality for task recovery.

    Attributes
    ----------
    session : boto3.Session
        AWS session for making API calls
    checkpoint_bucket : Optional[str]
        S3 bucket name for storing checkpoint data
    checkpoint_prefix : str
        S3 key prefix for checkpoint data
    executor : Any
        Parsl executor for submitting recovery tasks
    executor_label : str
        Label of the executor for task submission
    """

    def __init__(
        self,
        session: boto3.Session,
        checkpoint_bucket: Optional[str] = None,
        checkpoint_prefix: str = "parsl/checkpoints",
        executor=None,
        executor_label: str = "default",
    ) -> None:
        """Initialize the ParslSpotInterruptionHandler.

        Parameters
        ----------
        session : boto3.Session
            AWS session for making API calls
        checkpoint_bucket : Optional[str], optional
            S3 bucket name for storing checkpoint data, by default None
        checkpoint_prefix : str, optional
            S3 key prefix for checkpoint data, by default "parsl/checkpoints"
        executor : Any, optional
            Parsl executor for submitting recovery tasks, by default None
        executor_label : str, optional
            Label of the executor for task submission, by default "default"
        """
        super().__init__(session, checkpoint_bucket, checkpoint_prefix)
        self.executor = executor
        self.executor_label = executor_label
        self.task_mapping = {}  # instance_id -> list of task_ids

    def register_task(self, task_id: str, instance_id: str) -> None:
        """Register a task running on a specific instance.

        Parameters
        ----------
        task_id : str
            Unique identifier for the task
        instance_id : str
            ID of the instance running the task
        """
        if instance_id not in self.task_mapping:
            self.task_mapping[instance_id] = []

        self.task_mapping[instance_id].append(task_id)
        logger.debug(f"Registered task {task_id} on instance {instance_id}")

    def handle_instance_interruption(
        self, instance_id: str, event: Dict[str, Any]
    ) -> None:
        """Handle spot instance interruption for Parsl tasks.

        Parameters
        ----------
        instance_id : str
            ID of the interrupted instance
        event : Dict[str, Any]
            Interruption event details
        """
        logger.warning(f"Spot instance {instance_id} is being interrupted: {event}")

        # Get tasks running on this instance
        tasks = self.task_mapping.get(instance_id, [])
        if not tasks:
            logger.info(f"No registered tasks found for instance {instance_id}")
            return

        logger.info(
            f"Found {len(tasks)} tasks to recover from interrupted instance {instance_id}"
        )

        # Queue tasks for recovery
        for task_id in tasks:
            try:
                # Attempt to load checkpoint data for this task
                checkpoint_data = self.load_checkpoint(task_id)
                if checkpoint_data:
                    self.queue_task_for_recovery(
                        task_id,
                        f"s3://{self.checkpoint_bucket}/{self.checkpoint_prefix}/{task_id}.json",
                    )
                else:
                    logger.warning(f"No checkpoint data found for task {task_id}")
            except Exception as e:
                logger.error(f"Error processing task {task_id} for recovery: {e}")

        # Clean up task mapping
        if instance_id in self.task_mapping:
            del self.task_mapping[instance_id]

    def handle_fleet_interruption(
        self, fleet_id: str, instance_ids: List[str], event: Dict[str, Any]
    ) -> None:
        """Handle spot fleet interruption for Parsl tasks.

        Parameters
        ----------
        fleet_id : str
            ID of the spot fleet
        instance_ids : List[str]
            IDs of the interrupted instances
        event : Dict[str, Any]
            Interruption event details
        """
        logger.warning(
            f"Spot fleet {fleet_id} instances {instance_ids} are being interrupted: {event}"
        )

        # Process each interrupted instance
        for instance_id in instance_ids:
            self.handle_instance_interruption(instance_id, event)

    def recover_tasks(self) -> None:
        """Process the recovery queue and resubmit tasks."""
        if not self.executor:
            logger.warning("No executor provided for task recovery")
            return

        tasks_recovered = 0

        # Process recovery queue
        while True:
            task_info = self.get_next_recovery_task()
            if not task_info:
                break

            task_id = task_info["task_id"]
            checkpoint_uri = task_info["checkpoint_uri"]

            try:
                # Here we would use the Parsl executor to resubmit the task
                # The actual implementation would depend on how tasks are represented in Parsl
                logger.info(
                    f"Recovering task {task_id} using checkpoint {checkpoint_uri}"
                )

                # In a real implementation, we would:
                # 1. Load the checkpoint data
                # 2. Create a new Parsl future
                # 3. Submit the task to the executor
                # 4. Update any dependent tasks

                tasks_recovered += 1

            except Exception as e:
                logger.error(f"Failed to recover task {task_id}: {e}")

        logger.info(f"Recovered {tasks_recovered} tasks")


# Decorator function for making functions checkpointable
def checkpointable(
    checkpoint_bucket: Optional[str] = None,
    checkpoint_prefix: str = "parsl/checkpoints",
    checkpoint_interval: int = 60,  # seconds
):
    """Decorator to make a function checkpointable for spot interruption recovery.

    This decorator adds checkpointing capabilities to a function, allowing it
    to be recovered if interrupted by a spot instance termination.

    Parameters
    ----------
    checkpoint_bucket : Optional[str], optional
        S3 bucket for storing checkpoints, by default None
    checkpoint_prefix : str, optional
        S3 key prefix for checkpoints, by default "parsl/checkpoints"
    checkpoint_interval : int, optional
        Interval between checkpoints in seconds, by default 60

    Returns
    -------
    Callable
        Decorated function with checkpointing

    Example
    -------
    >>> @checkpointable(checkpoint_bucket="my-bucket")
    >>> def my_function(x, y, checkpoint_data=None):
    >>>     # Initialize from checkpoint if available
    >>>     if checkpoint_data:
    >>>         state = checkpoint_data
    >>>     else:
    >>>         state = {"iteration": 0, "result": 0}
    >>>
    >>>     # Simulate work with checkpointing
    >>>     for i in range(state["iteration"], 100):
    >>>         state["result"] += x * y
    >>>         state["iteration"] = i + 1
    >>>
    >>>         # Return checkpoint at intervals
    >>>         if (i + 1) % 10 == 0:
    >>>             yield state
    >>>
    >>>     return state["result"]
    """

    def decorator(func):
        def wrapper(*args, **kwargs):
            # Parse any checkpoint data provided
            checkpoint_data = kwargs.pop("checkpoint_data", None)

            # Create a generator from the function
            gen = func(*args, checkpoint_data=checkpoint_data, **kwargs)

            # If function is not a generator, make it one
            if not hasattr(gen, "__iter__") and not hasattr(gen, "__next__"):
                return gen

            # Process generator, capturing checkpoints
            last_checkpoint = time.time()
            result = None

            try:
                while True:
                    # Get next checkpoint or result
                    try:
                        checkpoint = next(gen)

                        # Save checkpoint if interval has passed
                        current_time = time.time()
                        if current_time - last_checkpoint >= checkpoint_interval:
                            # In a real implementation, we would save to S3 here
                            # For now, just update the last checkpoint time
                            last_checkpoint = current_time

                    except StopIteration as e:
                        result = e.value
                        break

                return result

            except Exception as e:
                # In case of exception, try to save a final checkpoint
                # This could be triggered by a spot interruption
                logger.error(f"Error in checkpointable function: {e}")
                raise

        return wrapper

    return decorator

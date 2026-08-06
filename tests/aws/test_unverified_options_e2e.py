"""Real-AWS end-to-end tests for three paths no other suite can settle (#166).

Each of these is a claim only a live run can check, because the failure mode is
"AWS accepted the request and did not honour it" -- which every mock and every
emulator reports as success:

1. ``ecs_container_image`` actually reaches the Fargate container. #136 made the
   option forwardable and moved the default off a Lambda base image, but nothing
   has ever watched a container start from a caller-chosen image. A mock cannot
   tell "the task definition accepted the image string" from "that image ran", so
   these tests read the container's own stdout out of CloudWatch Logs.

2. ``preserve_bastion`` and ``idle_timeout`` actually govern the bastion's
   lifetime. Both were unreachable through the provider before #136. The idle
   timer is shell that ``DetachedMode._prepare_bastion_init_script`` writes into
   UserData and registers as a cron job on the bastion; nothing in this repo
   executes it. It is also a cost path -- a bastion that never reclaims itself
   bills indefinitely, the same failure class as #132 and #163.

3. ``S3State(create_bucket_if_not_exists=True)`` against real S3. Covered under
   moto and under substrate, but never against the service: ``tests/aws``'s
   ``s3_state_bucket`` fixture pre-creates the bucket, so the provider always
   takes the already-exists branch there. #224 made the flag reachable through
   ``EphemeralProvider(s3_create_bucket=True)``; these tests still drive the store
   directly, for the reason the class docstring gives.

Run with::

    AWS_PROFILE=aws pytest tests/aws/test_unverified_options_e2e.py -m aws --no-cov -v

These cost real money and real time. The Fargate cases pull an image and run a
task; the idle-timeout case waits out a window that cannot be shortened below
one cron tick. See the per-class docstrings for the bounds.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import logging
import time
import uuid

import pytest
from botocore.exceptions import ClientError

from parsl_ephemeral_provider.exceptions import StateError
from parsl_ephemeral_provider.provider import EphemeralProvider
from parsl_ephemeral_provider.state.s3 import S3State

logger = logging.getLogger(__name__)

AWS_TEST_PROFILE = "aws"

POLL_INTERVAL_S = 15

# A Fargate task pulls its image before it runs, so this covers the pull as well
# as the task. Public Docker Hub images of this size settle well inside it.
FARGATE_MAX_WAIT_S = 900

# Fargate accepts only certain CPU/memory pairs, and an invalid combination fails
# the *task definition* -- a different error, at a different stage, that would
# read as an image problem. 512/1024 is the smallest valid pair, so also the
# cheapest.
FARGATE_CPU = 512
FARGATE_MEMORY = 1024

# Deliberately NOT DEFAULT_ECS_CONTAINER_IMAGE, which is python:3.12-slim. An
# image equal to the default cannot distinguish "the option was forwarded" from
# "the default was used" -- precisely the bug #136 fixed. 3.11 is one minor
# version off, so the assertion below fails if the default wins.
ECS_TEST_IMAGE = "python:3.11-slim"
ECS_EXPECTED_VERSION = "3.11"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wait_for_stack(cf_client, stack_name: str, timeout: int) -> str:
    """Poll until *stack_name* leaves IN_PROGRESS; return its final status.

    The status is returned rather than asserted on: ``ecs_worker.yml`` also
    creates two IAM roles, a cluster, and a log group, so a rollback here is
    often not about the image at all, and the caller can say so.
    """
    deadline = time.time() + timeout
    last_status = "UNKNOWN"
    while time.time() < deadline:
        try:
            stacks = cf_client.describe_stacks(StackName=stack_name)["Stacks"]
        except ClientError as exc:
            if "does not exist" in str(exc):
                return "DELETED"
            raise
        last_status = str(stacks[0]["StackStatus"])
        if not last_status.endswith("_IN_PROGRESS"):
            return last_status
        time.sleep(POLL_INTERVAL_S)
    return last_status


def _stack_output(cf_client, stack_name: str, key: str) -> str:
    """Return one stack output by key, or fail naming what was available.

    Read from the stack rather than reconstructed from the naming convention:
    ``LogGroupName`` is an actual Output of ``ecs_worker.yml``, and asserting
    against a locally rebuilt ``/aws/ecs/parsl-${WorkflowId}-${JobId}`` would
    pass even if the template stopped using that name.
    """
    outputs = cf_client.describe_stacks(StackName=stack_name)["Stacks"][0].get(
        "Outputs", []
    )
    for output in outputs:
        if output["OutputKey"] == key:
            return str(output["OutputValue"])
    pytest.fail(
        f"Stack {stack_name} has no output {key!r}; got "
        f"{[o['OutputKey'] for o in outputs]}"
    )


def _container_output(logs_client, log_group: str, timeout: int) -> str:
    """Return everything the container wrote, as one string.

    CloudWatch ingestion lags the container by a few seconds, so this polls
    rather than reading once. An empty result is returned rather than raised:
    "the container produced no output" is a meaningful assertion failure in the
    caller, and a more useful message than a timeout here.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            streams = logs_client.describe_log_streams(logGroupName=log_group).get(
                "logStreams", []
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                time.sleep(POLL_INTERVAL_S)
                continue
            raise

        lines = []
        for stream in streams:
            events = logs_client.get_log_events(
                logGroupName=log_group,
                logStreamName=stream["logStreamName"],
                startFromHead=True,
            ).get("events", [])
            lines.extend(e["message"] for e in events)
        if lines:
            return "\n".join(lines)
        time.sleep(POLL_INTERVAL_S)
    return ""


def _bastion_instance_id(cf_client, bastion_id: str) -> str:
    """Resolve ``bastion_id`` to an EC2 instance ID.

    ``bastion_host_type`` defaults to ``"cloudformation"``, so ``bastion_id`` is
    usually a *stack name*, not an instance ID -- ``describe_instances`` on it
    raises. ``bastion.yml`` publishes the instance as its ``BastionHostId``
    output.
    """
    if bastion_id.startswith("i-"):
        return bastion_id
    return _stack_output(cf_client, bastion_id, "BastionHostId")


def _instance_state(ec2_client, instance_id: str) -> str:
    """Return an instance's state name, or ``"gone"`` if the record is absent.

    A terminated instance ages out of ``describe_instances`` entirely, so
    ``NotFound`` and an empty reservation list both mean "terminated, and then
    some" -- not an error.
    """
    try:
        described = ec2_client.describe_instances(InstanceIds=[instance_id])
    except ClientError as exc:
        if "NotFound" in exc.response["Error"]["Code"]:
            return "gone"
        raise
    reservations = described.get("Reservations", [])
    if not reservations or not reservations[0].get("Instances"):
        return "gone"
    return str(reservations[0]["Instances"][0]["State"]["Name"])


# ---------------------------------------------------------------------------
# TestEcsContainerImageIsHonoured
# ---------------------------------------------------------------------------


@pytest.mark.aws
@pytest.mark.slow
class TestEcsContainerImageIsHonoured:
    """``ecs_container_image`` runs the caller's image, not the default (#166).

    This is the whole reason to choose Fargate over Lambda: an image that already
    carries the workload's dependencies. Every assertion here reads the
    container's *own output*, because that is the only signal separating "the
    task definition accepted the image string" from "that image ran".

    Each test deploys one CloudFormation stack and runs one Fargate task at the
    smallest legal CPU/memory pair.
    """

    @pytest.fixture
    def ecs_provider(self, tmp_path, aws_session, test_run_id, aws_region, network_ids):
        """A serverless provider pinned to ECS with a non-default image.

        ``compute_type="ecs"`` rather than the default: ``auto`` routes short
        single-task commands to Lambda (``_select_worker_type``), which would
        silently not exercise Fargate at all -- the failure this class exists to
        catch.
        """
        provider = EphemeralProvider(
            region=aws_region,
            mode="serverless",
            compute_type="ecs",
            ecs_container_image=ECS_TEST_IMAGE,
            ecs_task_cpu=FARGATE_CPU,
            ecs_task_memory=FARGATE_MEMORY,
            state_store_type="file",
            state_file_path=str(tmp_path / f"state-{test_run_id}.json"),
            auto_shutdown=True,
            auto_create_instance_profile=True,
            profile_name=AWS_TEST_PROFILE,
            additional_tags={"E2ETestRunId": test_run_id, "AutoCleanup": "true"},
            waiter_delay=15,
            waiter_max_attempts=40,
            debug=True,
            **network_ids,
        )

        yield provider

        try:
            provider.shutdown()
        except Exception as exc:
            logger.warning("ecs_provider shutdown raised (best-effort): %s", exc)

    def test_the_configured_image_is_what_runs(
        self, ecs_provider, aws_session, aws_region
    ):
        """The container reports the Python version of the image we asked for.

        The version is the assertion because it is what distinguishes the
        configured image from the default. Asserting merely that *something* ran
        would pass against ``python:3.12-slim`` too, and that is the exact
        regression -- the option silently not being forwarded.
        """
        # A plain shell string, with quoting in it. Since #226 the template
        # exec's Command under /bin/sh -c, so this is the same surface every other
        # mode takes; it used to have to be comma-joined into argv, and the
        # embedded quotes and spaces would not have survived that.
        job_id = ecs_provider.submit(
            "python -c \"import sys; print('PARSL_E2E_PYTHON=%d.%d' % "
            'sys.version_info[:2])"',
            tasks_per_node=1,
        )

        cf = aws_session.client("cloudformation", region_name=aws_region)
        logs = aws_session.client("logs", region_name=aws_region)
        stack_name = f"parsl-ecs-{job_id[:8]}"

        status = _wait_for_stack(cf, stack_name, FARGATE_MAX_WAIT_S)
        assert status in ("CREATE_COMPLETE", "UPDATE_COMPLETE"), (
            f"Stack {stack_name} ended at {status}. A rollback here is usually not "
            f"about the image: check whether Fargate rejected cpu={FARGATE_CPU}/"
            f"memory={FARGATE_MEMORY} at the task-definition stage, or an IAM "
            "role in the template failed, before reading this as a failure of "
            "ecs_container_image."
        )

        log_group = _stack_output(cf, stack_name, "LogGroupName")
        output = _container_output(logs, log_group, FARGATE_MAX_WAIT_S)

        assert output, (
            f"The container wrote nothing to {log_group}. This is how a wrong "
            "image fails quietly -- the old Lambda-base-image default started its "
            "runtime interface emulator, waited for an invocation event that "
            "never came, and exited with no error attributable to the image."
        )
        assert f"PARSL_E2E_PYTHON={ECS_EXPECTED_VERSION}" in output, (
            f"Expected the configured {ECS_TEST_IMAGE}; container said: {output[:500]}"
        )

    def test_the_task_did_not_stop_for_a_container_level_reason(
        self, ecs_provider, aws_session, aws_region
    ):
        """``stoppedReason`` must not name a pull or start failure.

        A task that cannot pull its image still reaches STOPPED, so "reached
        STOPPED" is evidence of nothing. #166 asks for ``stoppedReason``
        specifically because that is where ``CannotPullContainerError`` and
        entrypoint failures are reported.

        ``ecs_worker.yml`` runs the task under an ECS *Service* with a
        ``DesiredCount``, so a container that exits is replaced -- the assertion
        is about the first task observed to stop, not about the service settling.
        """
        job_id = ecs_provider.submit(
            "python -c \"print('PARSL_E2E_RAN')\"", tasks_per_node=1
        )

        cf = aws_session.client("cloudformation", region_name=aws_region)
        ecs = aws_session.client("ecs", region_name=aws_region)
        stack_name = f"parsl-ecs-{job_id[:8]}"

        assert _wait_for_stack(cf, stack_name, FARGATE_MAX_WAIT_S) in (
            "CREATE_COMPLETE",
            "UPDATE_COMPLETE",
        ), f"Stack {stack_name} did not deploy; nothing to inspect"

        cluster = _stack_output(cf, stack_name, "ClusterName")
        # Tasks are matched by task-definition family, not by tag: the service
        # sets no PropagateTags, so a task carries none of the stack's tags.
        family = f"parsl-ecs-task-{job_id[:8]}"

        deadline = time.time() + FARGATE_MAX_WAIT_S
        stopped = None
        while time.time() < deadline and stopped is None:
            for desired in ("STOPPED", "RUNNING"):
                arns = ecs.list_tasks(
                    cluster=cluster, family=family, desiredStatus=desired
                ).get("taskArns", [])
                if not arns:
                    continue
                for task in ecs.describe_tasks(cluster=cluster, tasks=arns).get(
                    "tasks", []
                ):
                    if task.get("lastStatus") == "STOPPED":
                        stopped = task
                        break
                if stopped is not None:
                    break
            if stopped is None:
                time.sleep(POLL_INTERVAL_S)

        assert stopped is not None, (
            f"No task in family {family} reached STOPPED within "
            f"{FARGATE_MAX_WAIT_S}s. A task stuck in PROVISIONING usually means "
            "the subnet cannot reach the registry -- Fargate pulls before it runs."
        )

        reason = stopped.get("stoppedReason", "")
        for marker in ("CannotPullContainerError", "ImagePull", "OutOfMemoryError"):
            assert marker not in reason, (
                f"Task stopped for a container-level reason: {reason}"
            )

        exit_codes = [c.get("exitCode") for c in stopped.get("containers", [])]
        assert exit_codes and all(code == 0 for code in exit_codes), (
            f"Container exit codes {exit_codes}, stoppedReason={reason!r}. A "
            "non-zero exit with no pull error is the signature of a command the "
            "image cannot execute -- check that the image has a /bin/sh, which "
            "ecs_worker.yml's Command relies on (#226)."
        )


# ---------------------------------------------------------------------------
# TestBastionLifetimeOptions
# ---------------------------------------------------------------------------


@pytest.mark.aws
@pytest.mark.slow
class TestBastionLifetimeOptions:
    """``preserve_bastion`` and ``idle_timeout`` govern the bastion's lifetime.

    Neither reached ``DetachedMode`` before #136, so the mode's defaults always
    won and no live run has ever depended on them.

    ``shutdown()`` is not the seam to test either through: it forces
    ``preserve_bastion = False`` before calling ``cleanup_infrastructure()``
    (``modes/detached.py:2044``), so it removes the bastion whatever the caller
    asked for -- which ``test_shutdown_removes_bastion`` in
    ``test_detached_mode_e2e.py`` already covers. These tests call
    ``cleanup_infrastructure()`` directly, which is where the flag is read.
    """

    # One cron tick (*/5) + the idle window + boot + shutdown, with room to
    # spare. The bound cannot go much below this: the script compares
    # IDLE_MINUTES -gt IDLE_TIMEOUT -- strictly greater -- so idle_timeout=1
    # needs more than a minute of idleness *and* the next five-minute tick.
    SELF_TERMINATE_MAX_WAIT_S = 1500

    @pytest.fixture
    def idle_bastion_provider(
        self, tmp_path, aws_session, test_run_id, aws_region, network_ids
    ):
        """A detached provider whose bastion should reclaim itself.

        ``idle_timeout`` is in *minutes*; 1 is the smallest value the script's
        strictly-greater comparison can ever act on. Constructing the provider is
        what launches the bastion -- ``__init__`` calls
        ``operating_mode.initialize()`` (``provider.py:791``), and there is no
        ``provider.initialize()`` to call.
        """
        provider = EphemeralProvider(
            region=aws_region,
            instance_type="t3.micro",
            mode="detached",
            bastion_instance_type="t3.micro",
            idle_timeout=1,
            preserve_bastion=False,
            state_store_type="file",
            state_file_path=str(tmp_path / f"state-{test_run_id}.json"),
            auto_shutdown=True,
            auto_create_instance_profile=True,
            profile_name=AWS_TEST_PROFILE,
            additional_tags={"E2ETestRunId": test_run_id, "AutoCleanup": "true"},
            waiter_delay=15,
            waiter_max_attempts=40,
            debug=True,
            **network_ids,
        )

        yield provider

        # Unconditional: if self-termination did not work, this is what stops the
        # bastion billing. A failing test must not also leak an instance.
        try:
            provider.shutdown()
        except Exception as exc:
            logger.warning(
                "idle_bastion_provider shutdown raised (best-effort): %s", exc
            )

    @pytest.fixture
    def preserved_bastion_provider(
        self, tmp_path, aws_session, test_run_id, aws_region, network_ids
    ):
        """A detached provider that keeps its bastion across cleanup.

        ``preserve_bastion=True`` is the shipped default, and the reason it
        exists: a preserved bastion is what a later session reconnects to. It is
        passed explicitly so the test states what it asserts rather than relying
        on a default it happens to share.
        """
        provider = EphemeralProvider(
            region=aws_region,
            instance_type="t3.micro",
            mode="detached",
            bastion_instance_type="t3.micro",
            preserve_bastion=True,
            state_store_type="file",
            state_file_path=str(tmp_path / f"state-{test_run_id}.json"),
            auto_shutdown=True,
            auto_create_instance_profile=True,
            profile_name=AWS_TEST_PROFILE,
            additional_tags={"E2ETestRunId": test_run_id, "AutoCleanup": "true"},
            waiter_delay=15,
            waiter_max_attempts=40,
            debug=True,
            **network_ids,
        )

        yield provider

        # shutdown() overrides preserve_bastion, which is what reclaims the
        # instance this test deliberately left running.
        try:
            provider.shutdown()
        except Exception as exc:
            logger.warning(
                "preserved_bastion_provider shutdown raised (best-effort): %s", exc
            )

    def test_the_bastion_terminates_itself_with_no_shutdown_call(
        self, idle_bastion_provider, aws_session, aws_region
    ):
        """The bastion reaches a terminal state without the client asking.

        Nothing in this body calls ``shutdown()`` or ``terminate_instances`` --
        that is the point. The fixture's teardown runs afterwards and is a safety
        net, not the mechanism under test.

        ``InstanceInitiatedShutdownBehavior: terminate`` (``bastion.yml:146``,
        and the same on the direct path) is what turns the script's
        ``shutdown -h now`` into a termination rather than a stop. A bastion found
        ``stopped`` means that setting regressed: it ends the compute charge but
        keeps the EBS volume billing, so it is a partial failure of the same cost
        path, not a pass.
        """
        mode = idle_bastion_provider.operating_mode
        assert mode.bastion_id, "No bastion was created; nothing to reclaim"

        cf = aws_session.client("cloudformation", region_name=aws_region)
        ec2 = aws_session.client("ec2", region_name=aws_region)
        instance_id = _bastion_instance_id(cf, mode.bastion_id)

        deadline = time.time() + self.SELF_TERMINATE_MAX_WAIT_S
        state = "unknown"
        while time.time() < deadline:
            state = _instance_state(ec2, instance_id)
            if state in ("terminated", "shutting-down", "gone"):
                return
            assert state != "stopped", (
                "The bastion stopped rather than terminated, so its EBS volume "
                "keeps billing. Check that "
                "InstanceInitiatedShutdownBehavior: terminate still reaches the "
                "launch template."
            )
            time.sleep(POLL_INTERVAL_S * 2)

        pytest.fail(
            f"Bastion {instance_id} was still {state} after "
            f"{self.SELF_TERMINATE_MAX_WAIT_S}s with idle_timeout=1 minute. The "
            "idle script is written into UserData by "
            "DetachedMode._prepare_bastion_init_script and registered as a */5 "
            "cron job. Note that script begins with 'set -e' and then runs "
            "'yum install -y python3 python3-pip jq awscli' -- awscli is not an "
            "installable package on Amazon Linux 2023, so if that line fails the "
            "whole UserData aborts before the cron job is ever registered. Check "
            "/var/log/cloud-init-output.log and whether "
            "/usr/local/bin/parsl-idle-shutdown.sh exists on the instance."
        )

    def test_cleanup_removes_the_bastion_when_not_preserving(
        self, idle_bastion_provider, aws_session, aws_region
    ):
        """``preserve_bastion=False`` makes ``cleanup_infrastructure()`` reclaim it.

        Called directly rather than through ``shutdown()``, which forces the flag
        to ``False`` and so cannot distinguish the two settings. This is the
        faster of this class's two lifetime tests: it does not wait out the idle
        window.
        """
        mode = idle_bastion_provider.operating_mode
        assert mode.bastion_id, "No bastion was created; nothing to reclaim"

        cf = aws_session.client("cloudformation", region_name=aws_region)
        ec2 = aws_session.client("ec2", region_name=aws_region)
        instance_id = _bastion_instance_id(cf, mode.bastion_id)

        mode.cleanup_infrastructure()

        deadline = time.time() + 600
        state = "unknown"
        while time.time() < deadline:
            state = _instance_state(ec2, instance_id)
            if state in ("terminated", "shutting-down", "gone"):
                return
            time.sleep(POLL_INTERVAL_S)

        pytest.fail(
            f"Bastion {instance_id} was still {state} 600s after "
            "cleanup_infrastructure() with preserve_bastion=False. It is billing."
        )

    def test_cleanup_keeps_the_bastion_when_preserving(
        self, preserved_bastion_provider, aws_session, aws_region
    ):
        """``preserve_bastion=True`` survives ``cleanup_infrastructure()``.

        The other half of the flag, and the half that is load-bearing for
        detached mode: a bastion torn down here is a workflow the client can
        never reconnect to. Asserting only the ``False`` case would pass against
        an implementation that always deleted.
        """
        mode = preserved_bastion_provider.operating_mode
        assert mode.bastion_id, "No bastion was created; nothing to preserve"

        cf = aws_session.client("cloudformation", region_name=aws_region)
        ec2 = aws_session.client("ec2", region_name=aws_region)
        instance_id = _bastion_instance_id(cf, mode.bastion_id)

        mode.cleanup_infrastructure()

        # Give a deletion time to become visible: asserting immediately would
        # pass even if cleanup had just issued a terminate.
        time.sleep(POLL_INTERVAL_S * 2)

        state = _instance_state(ec2, instance_id)
        assert state in ("pending", "running"), (
            f"Bastion {instance_id} is {state} after cleanup_infrastructure() "
            "with preserve_bastion=True. Detached mode's whole premise is that "
            "the client can disconnect and reconnect to this instance."
        )
        assert mode.bastion_id is not None, (
            "cleanup_infrastructure() cleared bastion_id while preserving the "
            "instance, so the next session cannot find it -- an orphan that bills"
        )


# ---------------------------------------------------------------------------
# TestS3BucketCreation
# ---------------------------------------------------------------------------


@pytest.mark.aws
@pytest.mark.slow
class TestS3BucketCreation:
    """``create_bucket_if_not_exists=True`` against real S3 (#166).

    Covered under moto and under substrate, but never against the service. The
    emulator gaps this path has already hit -- substrate#446 left
    ``PUT ?publicAccessBlock`` unrouted, so it answered ``BucketAlreadyExists``
    for a bucket just created -- are why a live check is worth having: the
    sequence ``CreateBucket`` → ``PutPublicAccessBlock`` → ``PutBucketTagging``
    is where real S3 differs most from an emulator.

    ``S3State`` is constructed directly rather than through a provider. That used
    to be forced: the flag was not reachable from ``EphemeralProvider`` at all,
    because ``_initialize_state_store`` built the store with only ``provider``,
    ``bucket_name``, and ``key_prefix``, so every provider-built store took the
    already-exists branch. #224 added ``s3_create_bucket`` and forwards it, so a
    provider can now reach this path -- but the direct construction stays,
    because building a real ``EphemeralProvider`` reaches AWS and creates a launch
    template and an IAM role, none of which this class is about. The forwarding
    itself is pinned under unit test
    (``test_provider_edge_cases.py::TestS3CreateBucketForwarding``); what only a
    live run can settle is what the store then does to real S3.
    """

    @pytest.fixture
    def bucket_name(self, aws_session, aws_region, test_run_id):
        """A bucket name that does not exist yet, removed afterwards.

        Deliberately not created here -- creating it is what is under test.
        Teardown covers both outcomes, since an assertion that fails partway
        through can still have left the bucket behind.
        """
        name = f"parsl-e2e-autocreate-{uuid.uuid4().hex[:8]}-{test_run_id}"
        s3 = aws_session.client("s3", region_name=aws_region)

        yield name

        try:
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=name):
                objects = page.get("Contents", [])
                if objects:
                    s3.delete_objects(
                        Bucket=name,
                        Delete={"Objects": [{"Key": o["Key"]} for o in objects]},
                    )
            s3.delete_bucket(Bucket=name)
            logger.info("bucket_name teardown: deleted %s", name)
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in ("NoSuchBucket", "404"):
                logger.warning("bucket_name teardown: %s", exc)

    @pytest.fixture
    def state_provider(self, aws_session, aws_region, test_run_id):
        """The minimum object ``S3State`` needs.

        ``resolve_session()`` returns ``provider.session`` when it is a
        ``boto3.Session``, so the attribute must be named ``session`` -- anything
        else falls through to assembling a fresh session from credential
        attributes this object does not have.

        A real ``EphemeralProvider`` is not used, and not merely because it is
        heavier: constructing one reaches AWS and creates a launch template and
        an IAM role, none of which this class is about.
        """

        class _StateProvider:
            def __init__(self):
                self.provider_id = f"e2e-{test_run_id}"
                self.workflow_id = f"wf-{test_run_id}"
                self.region = aws_region
                self.session = aws_session

        return _StateProvider()

    def test_a_missing_bucket_is_created_and_locked_down(
        self, state_provider, bucket_name, aws_session, aws_region
    ):
        """Creation, public-access block, and tagging.

        The public-access block is the part worth asserting, more than the
        creation: this bucket holds provider state -- instance IDs, network IDs,
        whatever the workflow put in its tags -- and the code replaced a
        deprecated ``ACL="private"`` with this. A bucket created without it is
        world-readable the moment any later policy allows it.
        """
        # A *throwaway* client for the pre-check, never reused below. A boto3 S3
        # client that has seen a 404 for a name goes on answering 404 for that
        # name for at least 15s after a *different* client creates it -- verified
        # live: same-client create-then-head succeeds, cross-client does not.
        # Asserting absence through the same client that later asserts presence
        # therefore fails on a bucket that demonstrably exists (list_buckets
        # shows it, and the state round-trip at the end of this test works).
        with pytest.raises(ClientError) as excinfo:
            aws_session.client("s3", region_name=aws_region).head_bucket(
                Bucket=bucket_name
            )
        assert excinfo.value.response["Error"]["Code"] in ("404", "NoSuchBucket")

        state = S3State(
            provider=state_provider,
            bucket_name=bucket_name,
            create_bucket_if_not_exists=True,
        )

        s3 = aws_session.client("s3", region_name=aws_region)
        s3.head_bucket(Bucket=bucket_name)

        blocked = s3.get_public_access_block(Bucket=bucket_name)[
            "PublicAccessBlockConfiguration"
        ]
        assert blocked["BlockPublicAcls"] is True
        assert blocked["IgnorePublicAcls"] is True
        assert blocked["BlockPublicPolicy"] is True
        assert blocked["RestrictPublicBuckets"] is True

        tags = {
            t["Key"]: t["Value"]
            for t in s3.get_bucket_tagging(Bucket=bucket_name)["TagSet"]
        }
        assert tags["ParslManagedBucket"] == "true"
        assert tags["ParslWorkflowId"] == state_provider.workflow_id

        # Usable, not merely present: a bucket in the wrong region answers
        # head_bucket through a redirect but fails PutObject.
        state.save_state("e2e-key", {"created": "auto", "live": True})
        loaded = state.load_state("e2e-key")
        assert loaded is not None and loaded["created"] == "auto"
        state.delete_state("e2e-key")

    def test_the_bucket_lands_in_the_configured_region(
        self, state_provider, bucket_name, aws_session, aws_region
    ):
        """A ``LocationConstraint`` mistake only shows up against real S3.

        ``us-east-1`` rejects a ``LocationConstraint`` while every other region
        requires one, and ``_ensure_bucket_exists`` branches on exactly that.
        Emulators accept either form, so the asymmetry is invisible everywhere
        except here -- and getting it wrong puts state in a region the workflow
        cannot reach.
        """
        S3State(
            provider=state_provider,
            bucket_name=bucket_name,
            create_bucket_if_not_exists=True,
        )

        s3 = aws_session.client("s3", region_name=aws_region)
        location = s3.get_bucket_location(Bucket=bucket_name)["LocationConstraint"]
        # us-east-1 is reported as None: the API's own quirk, not a defect.
        actual = location or "us-east-1"
        assert actual == aws_region, (
            f"Bucket was created in {actual}, not the configured {aws_region}"
        )

    def test_an_existing_bucket_is_adopted_not_recreated(
        self, state_provider, bucket_name
    ):
        """The flag must be idempotent against the service, not just the mock.

        A provider resumed from a state file constructs its store again, so every
        restart runs this against a bucket that already exists. Here
        ``head_bucket`` succeeds and ``CreateBucket`` is never reached -- which is
        the behaviour worth pinning, because the alternative (re-issuing
        ``CreateBucket``) answers differently by region and would make the resume
        path region-dependent.
        """
        first = S3State(
            provider=state_provider,
            bucket_name=bucket_name,
            create_bucket_if_not_exists=True,
        )
        first.save_state("adopted", {"ok": True})

        try:
            second = S3State(
                provider=state_provider,
                bucket_name=bucket_name,
                create_bucket_if_not_exists=True,
            )
        except StateError as exc:
            pytest.fail(
                f"Reconstructing the store against its own bucket raised: {exc}. "
                "This is the resume path -- every restart hits it."
            )

        # State the first store wrote is still readable through the second, so
        # the bucket was adopted rather than replaced.
        assert second.load_state("adopted") == {"ok": True}
        second.delete_state("adopted")

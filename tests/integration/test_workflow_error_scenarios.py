"""Integration tests for error handling in workflow scenarios.

Each test drives a *real* mode against the emulator, makes one thing fail, and
asserts on what survived: whether the mode is usable afterwards, whether a
partially created resource was released, and whether the failure reached the
caller as the right exception type. Nothing about the recovery path is mocked --
only the failure itself is injected, and always at the boundary the real error
would arrive at.

The previous version of this file could not test any of that. Every test patched
methods that do not exist on any mode -- ``_create_vpc``, ``_create_subnet``,
``_create_security_group``, ``_create_ec2_instance``, ``_delete_ec2_instance``,
``_delete_vpc``, ``_delete_subnet``, ``_delete_security_group``,
``_create_bastion_host``, ``_create_tags`` -- and ``patch.object`` raises
``AttributeError`` for a missing attribute, so each was a hard error rather than
a passing test. The shape they described was removed in #69: no mode creates
network resources, and all three IDs are now required in the constructor, which
is where the six failures actually stopped.

Two smaller faults went with it. ``ResourceDeletionError`` was referenced at line
452 but never imported, so that test could only ever raise ``NameError``. And a
class-scoped ``substrate_session`` fixture shadowed conftest's: it came from
``get_substrate_session()``, which binds no endpoint, so clients built from it
reach real AWS and fail on auth. conftest's wraps ``session.client`` to inject
``endpoint_url``, which is what makes code under test hit the emulator.

Two failure modes are deliberately *not* exercised here:

* **Mode-level retry does not exist.** Nothing in ``modes/`` uses
  ``error_handling.py`` -- ``grep`` for ``retry_with_backoff`` finds no hit
  outside ``compute/`` -- so there is no mode-level backoff to test. Adopting the
  framework in ``modes/`` is #91, deferred to v0.9.0. What does retry today is
  botocore, and ``TestThrottling`` tests that, at the layer it happens.
* **A serverless submit needs CloudFormation**, which substrate does not serve,
  so its failure path is asserted through the injected error rather than a real
  stack rollback.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import uuid
from unittest.mock import patch

import pytest
from botocore.awsrequest import AWSResponse
from botocore.config import Config
from botocore.exceptions import ClientError

from parsl_aws_provider.constants import RESOURCE_TYPE_EC2
from parsl_aws_provider.exceptions import (
    BastionHostError,
    EC2InstanceError,
    LambdaFunctionError,
    OperatingModeError,
    ResourceCreationError,
    ResourceNotFoundError,
)
from parsl_aws_provider.modes.detached import DetachedMode
from parsl_aws_provider.modes.serverless import ServerlessMode
from parsl_aws_provider.modes.standard import StandardMode
from parsl_aws_provider.state.file import FileStateStore
from tests.substrate_support import is_substrate_available

# A marker only *selects* tests; it never skips them. Every sibling
# emulator-backed file pairs its markers with this skipif, and this one did not --
# so a plain `pytest tests/integration` erred out on all 7 tests with "not
# running" from the class fixture, rather than skipping.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.substrate,
    pytest.mark.skipif(
        not is_substrate_available(),
        reason="substrate not available - start with 'make substrate-up'",
    ),
]

#: A well-formed ID that names nothing. Substrate distinguishes the two failures
#: real EC2 does -- a short ID answers `InvalidSubnetID.Malformed`, this one
#: answers `InvalidSubnetID.NotFound` -- and `_verify_resources` treats both as
#: unusable, so the ID has to be shaped correctly for the test to be about
#: absence rather than syntax.
GHOST_SUBNET_ID = "subnet-0123456789abcdef0"


@pytest.fixture
def state_store(tmp_path):
    """A real file-backed state store inside the test's sandbox.

    Real rather than mocked: every mode saves state on the success *and* failure
    paths this file exercises, and a MagicMock store hides a serialization error
    behind a recorded call. It also keeps ``state/file.py``'s ``fcntl.flock`` on
    a genuine file descriptor -- a mocked handle raises "fileno() returned a
    non-integer".
    """
    provider_id = f"test-provider-{uuid.uuid4().hex[:8]}"
    return FileStateStore(
        file_path=str(tmp_path / f"state-{provider_id}.json"), provider_id=provider_id
    )


def _standard(session, state_store, network, **overrides):
    """A StandardMode bound to the emulator, with the caller's network."""
    kwargs = dict(
        provider_id=f"test-provider-{uuid.uuid4().hex[:8]}",
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


def _detached(session, state_store, network, **overrides):
    """A DetachedMode with a direct bastion.

    ``bastion_host_type="direct"``, not the "cloudformation" default: the stack
    path needs an endpoint substrate does not serve. The direct path launches
    with ``run_instances`` and is a real bastion in the emulator, which is what
    these tests need in order to check it was cleaned up.
    """
    kwargs = dict(
        workflow_id=f"test-workflow-{uuid.uuid4().hex[:8]}",
        bastion_host_type="direct",
        bastion_instance_type="t3.micro",
        preserve_bastion=False,
    )
    kwargs.update(overrides)
    return _detached_mode(session, state_store, network, **kwargs)


def _detached_mode(session, state_store, network, **kwargs):
    return DetachedMode(
        provider_id=f"test-provider-{uuid.uuid4().hex[:8]}",
        session=session,
        state_store=state_store,
        region=session.region_name,
        instance_type="t3.micro",
        image_id="ami-12345678",
        vpc_id=network["vpc_id"],
        subnet_id=network["subnet_id"],
        security_group_id=network["security_group_id"],
        **kwargs,
    )


def instance_state(session, instance_id):
    """Return an instance's state name, or None if EC2 has forgotten it."""
    try:
        described = session.client("ec2").describe_instances(InstanceIds=[instance_id])
    except ClientError:
        return None
    for reservation in described["Reservations"]:
        for instance in reservation["Instances"]:
            return str(instance["State"]["Name"])
    return None


class TestNetworkVerificationFailure:
    """A network ID that names nothing must fail before anything is launched."""

    def test_a_missing_subnet_stops_initialize_naming_it(
        self, substrate_session, substrate_network, state_store
    ):
        """The error names the unusable resource, and nothing is created.

        Before #69 each mode nulled the attribute out here so that
        ``initialize()`` would create a replacement. Nothing creates one now, so
        the ``None`` propagated into ``run_instances`` and surfaced as an opaque
        ``InvalidParameterValue`` far from the missing subnet.
        """
        mode = _standard(
            substrate_session,
            state_store,
            {**substrate_network, "subnet_id": GHOST_SUBNET_ID},
        )

        with pytest.raises(ResourceNotFoundError, match=GHOST_SUBNET_ID):
            mode.initialize()

        assert not mode.initialized
        # Verification runs ahead of everything else, so no template was built.
        assert mode._launch_template_id is None

    def test_a_usable_network_initializes_and_builds_a_template(
        self, substrate_session, substrate_network, state_store
    ):
        """The counterpart: the same construction succeeds once the ID is real.

        Without this, the test above would also pass if ``initialize()`` were
        broken for every input.
        """
        mode = _standard(substrate_session, state_store, substrate_network)

        try:
            mode.initialize()

            assert mode.initialized
            assert mode._launch_template_id is not None
        finally:
            mode.cleanup_infrastructure()


class TestSubmitFailure:
    """What a failed submit leaves behind."""

    def test_a_launch_failure_is_wrapped_and_tracks_nothing(
        self, substrate_session, substrate_network, state_store
    ):
        """No half-tracked resource, and the caller gets OperatingModeError.

        ``InsufficientInstanceCapacity`` is the realistic failure for a launch
        AWS accepted but could not satisfy. The mode wraps every submit failure
        in ``OperatingModeError`` -- Parsl's provider contract -- but must not
        leave a tracking entry for an instance that never existed, or cleanup
        would later try to terminate it.
        """
        mode = _standard(substrate_session, state_store, substrate_network)
        try:
            mode.initialize()

            no_capacity = ClientError(
                {
                    "Error": {
                        "Code": "InsufficientInstanceCapacity",
                        "Message": "There is no Spot capacity available.",
                    }
                },
                "RunInstances",
            )
            with patch.object(mode, "_create_instance", side_effect=no_capacity):
                with pytest.raises(
                    OperatingModeError, match="InsufficientInstanceCapacity"
                ):
                    mode.submit_job("test-job", "echo hello", 1)

            assert mode.resources == {}
        finally:
            mode.cleanup_infrastructure()

    def test_the_mode_still_works_after_a_failed_submit(
        self, substrate_session, substrate_network, state_store
    ):
        """Recovery, which is what this file is about.

        A failed submit must not poison the mode: the next one launches a real
        instance. Asserted against the emulator rather than the tracking dict,
        so a submit that recorded a resource without launching anything fails
        here.
        """
        mode = _standard(substrate_session, state_store, substrate_network)
        try:
            mode.initialize()

            with patch.object(
                mode, "_create_instance", side_effect=EC2InstanceError("transient")
            ):
                with pytest.raises(OperatingModeError):
                    mode.submit_job("doomed-job", "echo hello", 1)

            instance_id = mode.submit_job("good-job", "echo hello", 1)

            assert instance_id in mode.resources
            assert mode.resources[instance_id]["job_id"] == "good-job"
            assert instance_state(substrate_session, instance_id) in (
                "pending",
                "running",
            )
        finally:
            mode.cleanup_infrastructure()

    def test_submitting_before_initialize_is_refused(
        self, substrate_session, substrate_network, state_store
    ):
        """Refused outright rather than launching into an unbuilt mode."""
        mode = _standard(substrate_session, state_store, substrate_network)

        with pytest.raises(OperatingModeError, match="must be initialized"):
            mode.submit_job("test-job", "echo hello", 1)

        assert mode.resources == {}


class TestBastionFailure:
    """A bastion that cannot be created, and one that can be on the retry."""

    def test_a_bastion_failure_fails_initialize_and_leaves_no_bastion(
        self, substrate_session, substrate_network, state_store
    ):
        """The failure is wrapped, and the caller's network is untouched.

        ``initialize()`` calls ``cleanup_infrastructure()`` on the way out. That
        must release what the mode created and nothing else -- the VPC, subnet
        and security group belong to the caller (#69), and deleting them would
        be a far worse bug than the failure being recovered from.
        """
        mode = _detached(substrate_session, state_store, substrate_network)

        with patch.object(
            mode,
            "_create_bastion_direct",
            side_effect=BastionHostError("Bastion host creation failed"),
        ):
            with pytest.raises(ResourceCreationError, match="Bastion host creation"):
                mode.initialize()

        assert not mode.initialized
        assert mode.bastion_id is None
        # The caller's IDs are still set, and still real.
        assert mode.vpc_id == substrate_network["vpc_id"]
        assert mode.subnet_id == substrate_network["subnet_id"]
        assert mode.security_group_id == substrate_network["security_group_id"]
        substrate_session.client("ec2").describe_vpcs(
            VpcIds=[substrate_network["vpc_id"]]
        )

    def test_initialize_succeeds_on_a_retry_after_a_bastion_failure(
        self, substrate_session, substrate_network, state_store
    ):
        """The same mode object recovers, and the bastion really runs.

        This is the recovery the old test claimed to check but could not: it
        patched a ``_create_bastion_host`` that does not exist, so the mocked
        return value -- a dict, where the real method returns an instance ID
        string -- was never compared against anything.
        """
        mode = _detached(substrate_session, state_store, substrate_network)

        with patch.object(
            mode, "_create_bastion_direct", side_effect=BastionHostError("transient")
        ):
            with pytest.raises(ResourceCreationError):
                mode.initialize()

        try:
            mode.initialize()

            assert mode.initialized
            assert mode.bastion_id is not None
            assert instance_state(substrate_session, mode.bastion_id) in (
                "pending",
                "running",
            )
        finally:
            bastion_id = mode.bastion_id
            mode.cleanup_infrastructure()

        # preserve_bastion=False, so it goes with the mode.
        assert mode.bastion_id is None
        assert instance_state(substrate_session, bastion_id) in (
            "shutting-down",
            "terminated",
            None,
        )


class TestServerlessFailure:
    """A Lambda deployment that fails, and what it leaves tracked."""

    def _mode(self, session, state_store, **overrides):
        """A Lambda-only ServerlessMode.

        No network IDs: Lambda runs in the Lambda-managed VPC and needs none of
        the three, and ``ServerlessMode`` only enforces the base class's
        requirement when ECS is reachable.
        """
        kwargs = dict(
            provider_id=f"test-provider-{uuid.uuid4().hex[:8]}",
            session=session,
            state_store=state_store,
            region=session.region_name,
            worker_type="lambda",
            lambda_memory=128,
            lambda_timeout=30,
        )
        kwargs.update(overrides)
        return ServerlessMode(**kwargs)

    def test_a_lambda_deployment_failure_tracks_nothing(
        self, substrate_session, state_store
    ):
        """A failed submit leaves no tracking entry behind.

        The record is created *before* dispatch on purpose -- both submit
        helpers end by updating it, and creating it afterwards made every submit
        raise ``KeyError`` from inside a blanket ``except``, leaving a live stack
        nothing could clean up (#115). So the failure path has to remove it
        again, which is what this asserts: an entry for a function that was never
        deployed would make ``cancel``/``status`` report on a resource that does
        not exist.
        """
        mode = self._mode(substrate_session, state_store)
        mode.initialize()

        with patch.object(
            mode.lambda_manager,
            "_generate_lambda_code",
            side_effect=LambdaFunctionError("Lambda function creation failed"),
        ):
            with pytest.raises(OperatingModeError, match="Lambda function creation"):
                mode.submit_job("test-job", "echo hello", 1)

        assert mode.resources == {}

        mode.cleanup_infrastructure()
        assert not mode.initialized

    def test_a_lambda_only_mode_needs_no_network(self, substrate_session, state_store):
        """Initialization succeeds with no VPC, and builds only the Lambda manager.

        Pins the asymmetry the test above depends on: ECS/Fargate requires a
        subnet and security group for its mandatory ``awsvpcConfiguration``, and
        Lambda requires none, so the base class's network check is conditional on
        the worker type.
        """
        mode = self._mode(substrate_session, state_store)
        try:
            mode.initialize()

            assert mode.initialized
            assert mode.lambda_manager is not None
            assert mode.ecs_manager is None
        finally:
            mode.cleanup_infrastructure()


class TestThrottling:
    """Retry after an AWS API throttle.

    Tested at the botocore layer because that is the only layer that retries.
    Nothing in ``modes/`` uses ``error_handling.py`` -- adopting it there is #91,
    deferred to v0.9.0 -- so the old test's ``patch.object(mode.ec2_manager,
    "max_retries", 3)`` patched an attribute of an object the mode does not have.

    Substrate cannot be asked to throttle, so the 503 is injected at
    ``before-send``, which is inside the retry loop: botocore sees a genuine
    throttled HTTP response and decides for itself whether to re-send.
    """

    #: What EC2 returns when it sheds load. `RequestLimitExceeded` at HTTP 503 is
    #: the pair botocore's own retry policy for EC2 matches on -- see the
    #: `request_limit_exceeded` entry in botocore's `_retry.json`.
    THROTTLE_BODY = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b"<Response><Errors><Error><Code>RequestLimitExceeded</Code>"
        b"<Message>Request limit exceeded.</Message></Error></Errors></Response>"
    )

    class _RawBytes:
        """The minimal stream ``AWSResponse.content`` will read from."""

        def __init__(self, data):
            self._data = data

        def stream(self, **kwargs):
            yield self._data

    def _throttle_first(self, count):
        """Return a before-send hook that throttles the first *count* sends.

        The hook also records how many times it ran, which is the attempt count:
        returning a response short-circuits the send, returning None lets the
        real request through.
        """
        state = {"sends": 0}

        def hook(request, **kwargs):
            state["sends"] += 1
            if state["sends"] <= count:
                return AWSResponse(
                    request.url, 503, {}, self._RawBytes(self.THROTTLE_BODY)
                )
            return None

        return hook, state

    def test_a_throttled_launch_is_retried_and_succeeds(
        self, substrate_session, substrate_network
    ):
        """One throttle, one retry, and a real instance at the end."""
        ec2 = substrate_session.client("ec2")
        hook, state = self._throttle_first(1)
        ec2.meta.events.register_first("before-send.ec2.RunInstances", hook)

        response = ec2.run_instances(
            ImageId="ami-12345678",
            InstanceType="t3.micro",
            MinCount=1,
            MaxCount=1,
            NetworkInterfaces=[
                {
                    "DeviceIndex": 0,
                    "SubnetId": substrate_network["subnet_id"],
                    "Groups": [substrate_network["security_group_id"]],
                }
            ],
        )

        instance_id = response["Instances"][0]["InstanceId"]
        try:
            assert state["sends"] == 2, "the throttled send was not retried"
            assert response["ResponseMetadata"]["RetryAttempts"] == 1
            assert instance_state(substrate_session, instance_id) in (
                "pending",
                "running",
            )
        finally:
            ec2.terminate_instances(InstanceIds=[instance_id])

    def test_sustained_throttling_eventually_surfaces_to_the_caller(
        self, substrate_session, substrate_network
    ):
        """Retry is bounded: it does not mask a throttle that never lets up.

        ``max_attempts=2`` keeps the test quick; the default is 5. What matters
        is that the attempts are finite and the final failure reaches the caller
        as ``RequestLimitExceeded`` rather than as a hang or a silent success.
        """
        ec2 = substrate_session.client(
            "ec2", config=Config(retries={"max_attempts": 2, "mode": "legacy"})
        )
        hook, state = self._throttle_first(count=99)
        ec2.meta.events.register_first("before-send.ec2.RunInstances", hook)

        with pytest.raises(ClientError) as excinfo:
            ec2.run_instances(
                ImageId="ami-12345678", InstanceType="t3.micro", MinCount=1, MaxCount=1
            )

        assert excinfo.value.response["Error"]["Code"] == "RequestLimitExceeded"
        # One initial send plus two retries, then it gives up.
        assert state["sends"] == 3


class TestPartialCleanup:
    """A cleanup where one deletion fails must still do the rest."""

    def test_a_failed_template_deletion_does_not_stop_cleanup(
        self, substrate_session, substrate_network, state_store
    ):
        """The error is logged, the rest proceeds, and the ID is cleared.

        Clearing it either way is deliberate: a template that could not be
        deleted must not be referenced by a later launch, and cleanup is not
        retried.
        """
        mode = _standard(substrate_session, state_store, substrate_network)
        mode.initialize()
        instance_id = mode.submit_job("test-job", "echo hello", 1)
        assert mode._launch_template_id is not None

        denied = ClientError(
            {"Error": {"Code": "UnauthorizedOperation", "Message": "denied"}},
            "DeleteLaunchTemplate",
        )
        with patch(
            "parsl_aws_provider.modes.standard.delete_launch_template",
            side_effect=denied,
        ):
            with patch("logging.Logger.error") as mock_error:
                mode.cleanup_infrastructure()

        assert mock_error.called
        assert mode._launch_template_id is None
        assert not mode.initialized
        # The instance was released despite the template failure.
        assert mode.resources == {}
        assert instance_state(substrate_session, instance_id) in (
            "shutting-down",
            "terminated",
        )

    def test_an_already_deleted_template_is_not_an_error(
        self, substrate_session, substrate_network, state_store
    ):
        """Cleanup is idempotent, so a resumed provider can run it twice.

        ``InvalidLaunchTemplateId.NotFound`` is swallowed by
        ``utils/aws.delete_launch_template`` for exactly this case.
        """
        mode = _standard(substrate_session, state_store, substrate_network)
        mode.initialize()
        template_id = mode._launch_template_id

        # Delete it out from under the mode, as a second cleanup pass or a
        # concurrent operator would.
        substrate_session.client("ec2").delete_launch_template(
            LaunchTemplateId=template_id
        )

        with patch("logging.Logger.error") as mock_error:
            mode.cleanup_infrastructure()

        assert not mock_error.called
        assert not mode.initialized

    def test_a_failed_termination_keeps_the_resource_for_the_next_pass(
        self, substrate_session, substrate_network, state_store
    ):
        """A resource that could not be released must stay tracked.

        Dropping it would orphan a running, billed instance with nothing left
        pointing at it. ``cleanup_resources`` is called directly rather than
        through ``cleanup_infrastructure``, which would first spend its three
        minutes in the ``instance_terminated`` waiter on an instance that, by
        construction, never terminates.
        """
        mode = _standard(substrate_session, state_store, substrate_network)
        mode.initialize()
        instance_id = mode.submit_job("test-job", "echo hello", 1)

        real_client = substrate_session.client

        class TerminateDenied:
            """An EC2 client that refuses only TerminateInstances."""

            def __init__(self, inner):
                self._inner = inner

            def terminate_instances(self, **kwargs):
                raise ClientError(
                    {"Error": {"Code": "UnauthorizedOperation", "Message": "denied"}},
                    "TerminateInstances",
                )

            def __getattr__(self, name):
                return getattr(self._inner, name)

        def denying_client(service_name, **kwargs):
            client = real_client(service_name, **kwargs)
            return TerminateDenied(client) if service_name == "ec2" else client

        substrate_session.client = denying_client
        try:
            with patch("logging.Logger.error") as mock_error:
                mode.cleanup_resources([instance_id])

            assert mock_error.called
            assert instance_id in mode.resources, "a live instance was forgotten"
        finally:
            substrate_session.client = real_client
            mode.cleanup_infrastructure()

    def test_an_instance_already_gone_is_dropped_from_tracking(
        self, substrate_session, substrate_network, state_store
    ):
        """The other half of the removal policy.

        ``InvalidInstanceID.NotFound`` means the instance is gone, so keeping the
        entry would make every later cleanup pass retry a termination that can
        never succeed. Only that code is treated this way -- see the test above.
        """
        mode = _standard(substrate_session, state_store, substrate_network)
        mode.initialize()
        try:
            ghost = "i-0123456789abcdef0"
            mode.resources[ghost] = {
                "type": RESOURCE_TYPE_EC2,
                "job_id": "vanished-job",
                "status": "RUNNING",
            }

            mode.cleanup_resources([ghost])

            assert ghost not in mode.resources
        finally:
            mode.cleanup_infrastructure()


class TestSpotInterruption:
    """A reclaim must surface as a failure, not a success."""

    def test_a_spot_interruption_is_reported_as_a_failure(
        self, substrate_session, substrate_network, state_store
    ):
        """The error scenario this file is about: AWS takes the capacity away.

        What made it dangerous was not that it went unhandled but that it was
        reported *wrongly* -- a reclaimed instance reaches ``shutting-down``,
        which ``EC2_STATUS_MAPPING`` renders COMPLETED, so the block claimed
        success and its tasks were silently dropped rather than re-run under
        Parsl's ``retries`` (#137).
        """
        instance_id = "i-spot-12345"
        mode = _standard(
            substrate_session,
            state_store,
            substrate_network,
            use_spot=True,
            spot_interruption_handling=True,
        )

        try:
            assert mode.spot_interruption_monitor is not None

            mode.resources[instance_id] = {
                "type": "ec2",
                "job_id": f"test-job-{uuid.uuid4().hex[:8]}",
                "status": "RUNNING",
                "is_spot": True,
            }
            # The registration every spot launch performs.
            mode._register_spot_instance(instance_id)
            assert instance_id in mode.spot_interruption_monitor.instance_handlers

            mode.spot_interruption_monitor.event_queue.put(
                (
                    "instance",
                    instance_id,
                    {"InstanceId": instance_id, "InstanceAction": "terminate"},
                )
            )
            mode.spot_interruption_monitor._process_interruption_events()

            assert mode.resources[instance_id]["status"] == "INTERRUPTED"
            assert mode.get_job_status([instance_id])[instance_id] == "INTERRUPTED"
        finally:
            if mode.spot_interruption_monitor:
                mode.spot_interruption_monitor.stop_monitoring()

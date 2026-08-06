"""Real-AWS end-to-end tests for EICE reverse tunnels (issue #134).

The unit suite covers command construction, the supervisor's reconnect and
recycle logic, and the mode wiring, with a stub standing in for ``ssh``. What it
cannot cover is the only claim that matters:

    a worker on an instance with no public IP, in a subnet with no NAT, can reach
    a socket bound to the driver's **loopback**

-- because that is a statement about EC2 Instance Connect, sshd, and a websocket
tunnel, and a stub ``ssh`` agrees with any claim you make about the network. So
the central test binds a real ``zmq.ROUTER`` on driver loopback, exactly as
HTEX's interchange does, and has the *worker* drive a ``zmq.DEALER`` through the
tunnel and back. ZMTP greets in both directions, so a forward that only
half-works fails it -- unlike a raw TCP connect.

The four constraints that shaped the design are each asserted here rather than
taken on trust, because each was discovered live and each would fail silently:

* ``--max-tunnel-duration`` is rejected at 3600, so the module clamps to 3599.
* ``send-ssh-public-key`` authorises for roughly a minute, so a key pushed once
  is not enough. ``test_a_stale_key_is_refused`` shows the failure the re-push
  exists to avoid.
* ``ec2-instance-connect:OpenTunnel`` is conditionable on ``remotePort``, so the
  documented policy scopes to port 22 alone -- checked against IAM's own
  evaluator, not by reading the JSON.
* endpoint creation takes minutes, so it is caller-supplied and never created
  here. It is verified in ``initialize()`` alongside the VPC/subnet/SG.

Requires an EC2 Instance Connect Endpoint in ``AWS_TEST_SUBNET_ID`` (see
``eice_endpoint_id``), plus ``ssh`` and AWS CLI v2 on the machine running the
tests. Each is skipped for, not failed on.

Run with::

    AWS_TEST_REGION=us-east-1 AWS_TEST_VPC_ID=vpc-xxx \\
    AWS_TEST_SUBNET_ID=subnet-xxx AWS_TEST_SG_ID=sg-xxx \\
    AWS_TEST_EICE_ID=eice-xxx \\
    AWS_PROFILE=aws pytest tests/aws/test_eice_tunnel_e2e.py -m aws --no-cov -v

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import json
import logging
import os
import shutil
import subprocess
import time

import pytest
from parsl.jobs.states import JobState

from parsl_ephemeral_provider.constants import (
    TUNNEL_OPEN_RETRY_DELAY,
    TUNNEL_OPEN_TIMEOUT,
)
from parsl_ephemeral_provider.network.eice import (
    EICE_TUNNEL_MAX_DURATION,
    EICETunnel,
    eice_iam_statements,
)
from parsl_ephemeral_provider.provider import EphemeralProvider

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.aws, pytest.mark.slow]

AWS_TEST_PROFILE = "aws"

POLL_INTERVAL_S = 15
#: SSM registration plus worker_init on real iron, matching the one-shot suite.
MAX_WAIT_S = 900

#: The driver-side port the fake interchange binds. Inside HTEX's default
#: ``worker_port_range`` (54000-55000) so this exercises the range the provider
#: will really see, but fixed rather than random so a leaked forward is
#: identifiable.
INTERCHANGE_PORT = 54321

#: pyzmq on the worker, which Amazon Linux 2023 does not ship. Installed rather
#: than the whole of Parsl because the round trip is the point and Parsl would
#: add minutes to every boot.
#:
#: ``python3-pip`` first: AL2023 ships python3 without pip, so
#: ``python3 -m pip`` on a bare instance fails with "No module named pip" --
#: and it fails in ``worker_init``, whose output goes to cloud-init's log and
#: never reaches the test. The probe reinstalls below for the same reason: a
#: failure there lands in the SSM invocation output, where it is visible.
E2E_WORKER_INIT = (
    "dnf install -y python3-pip >/dev/null 2>&1 || true\n"
    "python3 -m pip install --quiet pyzmq\n"
)


# ---------------------------------------------------------------------------
# The worker-side probe
# ---------------------------------------------------------------------------

#: Runs on the instance. A DEALER through the tunnel to the driver's ROUTER,
#: which is HTEX's own socket pair. Kept as a module constant so the shell
#: quoting has one home.
#:
#: ``__PORT__`` rather than ``%d``/``{}``: the body has its own ``%r`` and its own
#: braces, so both ``%`` formatting and ``str.format`` misfire on it.
_PROBE_BODY = """
import sys
import zmq

context = zmq.Context()
sock = context.socket(zmq.DEALER)
sock.setsockopt(zmq.LINGER, 0)
sock.setsockopt_string(zmq.IDENTITY, "parsl-e2e-worker")
# 127.0.0.1 on the *worker*: the reverse forward is what puts the driver's
# interchange on the worker's own loopback. Nothing routable is involved, which
# is the whole point of #134.
sock.connect("tcp://127.0.0.1:__PORT__")

sock.send(b"PARSL_TUNNEL_PROBE")

poller = zmq.Poller()
poller.register(sock, zmq.POLLIN)
if not poller.poll(60000):
    print("no reply from the interchange through the tunnel")
    sys.exit(1)

reply = sock.recv()
if reply != b"PARSL_TUNNEL_ACK":
    print("wrong reply through the tunnel: %r" % reply)
    sys.exit(1)

print("round trip through the reverse tunnel completed")
"""


def _probe_command(port: int) -> str:
    """A command that fails unless the tunnel carries a full ZMQ round trip.

    Shaped so the provider sees a ``--port`` exactly where HTEX would put one:
    the provider is never told where the interchange is, it reads the port out of
    the command string. The ``:`` no-op carries the flag without needing a
    ``process_worker_pool.py`` on the instance, and ``-a 127.0.0.1`` is the
    address a tunnelled worker should be given.
    """
    return "\n".join(
        [
            f": -a 127.0.0.1 --port={port}",
            # Belt and braces on the dependency. worker_init installs it, but if
            # that failed the reason went to cloud-init's log on the instance,
            # and the test would report only "nothing arrived at the socket" --
            # blaming the tunnel for a missing module. Here the output is the
            # SSM invocation's, which the test can read.
            "python3 -c 'import zmq' 2>/dev/null || {",
            "  dnf install -y python3-pip >/dev/null 2>&1 || true",
            "  python3 -m pip install --quiet pyzmq || {",
            "    echo 'could not install pyzmq; this is not a tunnel failure' >&2",
            "    exit 2",
            "  }",
            "}",
            "python3 - <<'PARSL_E2E_PROBE'",
            _PROBE_BODY.replace("__PORT__", str(port)),
            "PARSL_E2E_PROBE",
        ]
    )


def _poll_until_terminal(provider, job_id: str, timeout: int = MAX_WAIT_S):
    """Poll ``status()`` until *job_id* leaves PENDING/RUNNING."""
    non_terminal = (JobState.PENDING, JobState.RUNNING)
    deadline = time.time() + timeout
    while time.time() < deadline:
        statuses = provider.status([job_id])
        if statuses and statuses[0].state not in non_terminal:
            return statuses[0].state
        time.sleep(POLL_INTERVAL_S)
    return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def local_tunnel_binaries():
    """Skip unless ``ssh`` and the AWS CLI are present.

    This is the only part of the package that shells out, so their absence is a
    genuine "cannot test here" rather than a failure. Checked once per session.
    """
    missing = [name for name in ("ssh", "aws") if shutil.which(name) is None]
    if missing:
        pytest.skip(f"EICE tunnels need {', '.join(missing)} on PATH")


@pytest.fixture(scope="session")
def eice_endpoint_id(network_ids):
    """Return a usable EC2 Instance Connect Endpoint in the test subnet.

    Taken from ``AWS_TEST_EICE_ID`` when set, otherwise discovered in the test
    VPC. Never created: creation took about four and a half minutes in testing,
    which is why the design treats the endpoint as pre-provisioned network per
    #69, and a fixture that created one would both be slow and hide that.

    The state is checked because a ``create-in-progress`` endpoint accepts
    ``open-tunnel`` calls and fails them.
    """
    import boto3

    region = os.environ.get("AWS_TEST_REGION", "us-west-2")
    ec2 = boto3.Session(profile_name=AWS_TEST_PROFILE, region_name=region).client("ec2")

    explicit = os.environ.get("AWS_TEST_EICE_ID")
    if explicit:
        try:
            found = ec2.describe_instance_connect_endpoints(
                InstanceConnectEndpointIds=[explicit]
            )["InstanceConnectEndpoints"]
        except Exception as exc:
            pytest.fail(f"AWS_TEST_EICE_ID={explicit} is not usable in {region}: {exc}")
        if found[0]["State"] != "create-complete":
            pytest.skip(f"{explicit} is {found[0]['State']}, not create-complete")
        return explicit

    candidates = ec2.describe_instance_connect_endpoints(
        Filters=[{"Name": "subnet-id", "Values": [network_ids["subnet_id"]]}]
    )["InstanceConnectEndpoints"]
    usable = [c for c in candidates if c["State"] == "create-complete"]
    if not usable:
        pytest.skip(
            f"No create-complete EC2 Instance Connect Endpoint in "
            f"{network_ids['subnet_id']}. Create one (it takes ~5 minutes) or set "
            "AWS_TEST_EICE_ID."
        )
    return usable[0]["InstanceConnectEndpointId"]


@pytest.fixture
def interchange_socket():
    """A real ``zmq.ROUTER`` on driver loopback, echoing one probe.

    Bound to 127.0.0.1 deliberately: if this listened on 0.0.0.0 the worker
    might reach it by some other route and the test would pass without the
    tunnel doing anything. Loopback is unreachable from EC2 by construction, so
    a round trip proves the forward carried it.
    """
    zmq = pytest.importorskip("zmq")

    context = zmq.Context()
    router = context.socket(zmq.ROUTER)
    router.setsockopt(zmq.LINGER, 0)
    router.bind(f"tcp://127.0.0.1:{INTERCHANGE_PORT}")

    class Interchange:
        """The socket plus a one-shot echo. A wrapper because ``zmq.Socket``
        turns attribute assignment into a socket-option set and rejects
        anything it does not recognise."""

        socket = router

        @staticmethod
        def serve(timeout_s: int) -> bool:
            """Reply to one probe, returning whether one arrived."""
            poller = zmq.Poller()
            poller.register(router, zmq.POLLIN)
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                if poller.poll(1000):
                    identity, payload = router.recv_multipart()
                    logger.info("interchange received %r from %r", payload, identity)
                    router.send_multipart([identity, b"PARSL_TUNNEL_ACK"])
                    return True
            return False

    yield Interchange

    router.close()
    context.term()


@pytest.fixture
def tunnel_provider(
    tmp_path,
    test_run_id,
    aws_region,
    network_ids,
    eice_endpoint_id,
    local_tunnel_binaries,
):
    """A one-shot provider that tunnels.

    One-shot rather than the default UserData path for two reasons: it reports
    the command's exit code, which is what lets the worker's own verdict on the
    tunnel reach the test, and it dispatches over SSM *after* the tunnel is up,
    which is the ordering the mode guarantees on that path.

    What makes the proof airtight is the interchange binding 127.0.0.1, not the
    subnet: the shared test subnet has an internet gateway and assigns public IPs,
    so the worker *can* route out. It still cannot route to the driver's loopback
    by any path other than the reverse forward, which is the claim under test.
    """
    provider = EphemeralProvider(
        region=aws_region,
        instance_type="t3.micro",
        mode="standard",
        one_shot=True,
        auto_create_instance_profile=True,
        instance_connect_endpoint_id=eice_endpoint_id,
        worker_init=E2E_WORKER_INIT,
        state_store_type="file",
        state_file_path=str(tmp_path / f"eice-{test_run_id}.json"),
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
        logger.warning("Provider shutdown raised (best-effort): %s", exc)


@pytest.fixture
def live_instance(aws_provider, test_run_id):
    """A *booted* instance to tunnel to, without a provider that tunnels.

    Used by the tests that drive :class:`EICETunnel` directly. ``aws_provider``
    is the shared standard-mode fixture, so its own teardown terminates this.

    ``instance_status_ok``, not ``instance_running``: "running" is not "sshd is
    accepting connections", and the difference is not academic. A tunnel started
    against a merely-running instance fails with ``Websocket Closure Reason:
    Unable to connect to target``, which is exactly why
    ``_open_reverse_tunnel`` retries for ``TUNNEL_OPEN_TIMEOUT``. These tests
    bypass that retry to exercise ``EICETunnel`` directly, so they have to wait
    here instead.
    """
    job_id = aws_provider.submit("sleep 900", tasks_per_node=1)
    instance_id = aws_provider.job_map[job_id]["resource_id"]

    ec2 = aws_provider.session.client("ec2")
    ec2.get_waiter("instance_status_ok").wait(
        InstanceIds=[instance_id], WaiterConfig={"Delay": 15, "MaxAttempts": 40}
    )
    return instance_id


def _start_with_retry(tunnel, timeout: int = TUNNEL_OPEN_TIMEOUT):
    """Start *tunnel*, retrying the way ``_open_reverse_tunnel`` does.

    Even past ``instance_status_ok`` the first connect can lose a race with
    sshd, and each attempt re-pushes the key -- which is required anyway, since
    the previous attempt's authorisation has a ~60s life. Sharing production's
    budget rather than inventing one keeps this test from being more patient
    than the provider is.
    """
    deadline = time.time() + timeout
    while True:
        try:
            tunnel.start()
            return
        except Exception:
            if time.time() >= deadline:
                raise
            time.sleep(TUNNEL_OPEN_RETRY_DELAY)


# ---------------------------------------------------------------------------
# The central test: a worker reaches driver loopback
# ---------------------------------------------------------------------------


class TestAWorkerReachesTheInterchange:
    """The claim no mock can check: loopback on the driver, from a private subnet."""

    def test_the_worker_completes_a_zmq_round_trip_through_the_tunnel(
        self, tunnel_provider, interchange_socket
    ):
        """COMPLETED here means a DEALER on EC2 spoke to a ROUTER on this machine.

        This is the whole of #134 in one assertion. The ROUTER is bound to
        127.0.0.1, so no route exists from EC2 to it; the worker connects to
        *its own* 127.0.0.1 and the reverse forward carries it. The probe exits
        non-zero on a missing or wrong reply, and one-shot mode surfaces that as
        FAILED -- so this is the worker's verdict, not the driver's hope.
        """
        job_id = tunnel_provider.submit(
            _probe_command(INTERCHANGE_PORT), tasks_per_node=1
        )

        # Serve the probe while the job runs. The instance has to boot, install
        # pyzmq, and be dispatched to, so the wait here covers the whole submit.
        served = interchange_socket.serve(MAX_WAIT_S)

        state = _poll_until_terminal(tunnel_provider, job_id)

        assert served, (
            "nothing arrived at the interchange socket, so the reverse forward "
            "never carried a connection"
        )
        assert state == JobState.COMPLETED, (
            f"the worker could not complete the round trip (state={state}); the "
            "SSM invocation's StandardOutputContent says which step failed"
        )

    def test_the_tunnel_is_open_before_the_command_is_dispatched(
        self, tunnel_provider, interchange_socket
    ):
        """Ordering is load-bearing: a worker connects as soon as it starts.

        A tunnel opened after dispatch races the connection it exists to carry.
        Checked against the live supervisor rather than by patching, because the
        unit suite already asserts the call order and what is in question here is
        whether a real tunnel is up by then.
        """
        job_id = tunnel_provider.submit(
            _probe_command(INTERCHANGE_PORT), tasks_per_node=1
        )
        mode = tunnel_provider.operating_mode
        instance_id = tunnel_provider.job_map[job_id]["resource_id"]

        try:
            assert mode._tunnel_supervisor is not None, (
                "submit returned with no supervisor, so no tunnel was opened"
            )
            assert instance_id in mode._tunnel_supervisor.instance_ids
            tunnel = mode._tunnel_supervisor._tunnels[instance_id]
            assert tunnel.is_alive(), (
                "the ssh process holding the forward is not running, yet the "
                "command has already been dispatched to the worker"
            )
            assert tunnel.ports == [INTERCHANGE_PORT], (
                f"the tunnel forwards {tunnel.ports}, not the port named in the "
                "command; the worker would connect to nothing"
            )
        finally:
            interchange_socket.serve(120)
            try:
                tunnel_provider.cancel([job_id])
            except Exception as exc:
                logger.warning("teardown cancel raised (ignored): %s", exc)

    def test_shutdown_closes_the_tunnel_and_removes_the_key(self, tunnel_provider):
        """The ssh process and the generated private key must not outlive the run.

        A leaked ssh child holds a forward to a terminated instance and
        reconnects forever; a leaked private key sits in ``/tmp`` authorised for
        nothing but still readable.

        The command has to name a ``--port`` even though nothing here uses the
        forward: the provider reads the port out of the command and opens no
        tunnel at all when there is none, so a bare ``sleep`` would leave this
        asserting against a supervisor that was never built.
        """
        job_id = tunnel_provider.submit(
            f": -a 127.0.0.1 --port={INTERCHANGE_PORT}\nsleep 60", tasks_per_node=1
        )
        mode = tunnel_provider.operating_mode
        instance_id = tunnel_provider.job_map[job_id]["resource_id"]
        tunnel = mode._tunnel_supervisor._tunnels[instance_id]
        key_dir = mode._tunnel_key_dir
        assert tunnel.is_alive()
        assert key_dir and os.path.isdir(key_dir)

        tunnel_provider.shutdown()

        assert not tunnel.is_alive(), "the ssh process survived shutdown"
        assert mode._tunnel_supervisor is None
        assert not os.path.exists(key_dir), (
            f"the generated tunnel keypair survived shutdown in {key_dir}"
        )


# ---------------------------------------------------------------------------
# The API constraints, asserted against the live API
# ---------------------------------------------------------------------------


class TestTheAPIConstraints:
    """Each was discovered live and each would otherwise fail silently."""

    def _tunnel(self, session, instance_id, endpoint_id, keys, **kwargs):
        private, public = keys
        return EICETunnel(
            session=session,
            instance_id=instance_id,
            endpoint_id=endpoint_id,
            ports=[INTERCHANGE_PORT],
            public_key_path=public,
            private_key_path=private,
            profile_name=AWS_TEST_PROFILE,
            **kwargs,
        )

    @pytest.fixture
    def keys(self, tmp_path):
        """A real ed25519 pair, generated the way the mode generates one."""
        private = str(tmp_path / "tunnel_key")
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", private],
            check=True,
        )
        os.chmod(private, 0o600)
        return private, f"{private}.pub"

    def test_the_duration_ceiling_is_what_the_api_enforces(
        self, eice_endpoint_id, local_tunnel_binaries
    ):
        """Above the ceiling is rejected, so the module must ask for no more.

        Rejection is ``ParamValidation``, raised before any call is made, which
        is why no instance is needed here -- a malformed instance ID is enough to
        show *which* error came first. The message says "less than 3600" and is
        off by one: 3600 itself is accepted, 3601 is not. Both are asserted,
        because a clamp justified by a claim nobody checks is how the claim goes
        stale, and this notices if AWS ever moves the ceiling.
        """
        assert EICE_TUNNEL_MAX_DURATION == 3600

        def attempt(duration):
            return subprocess.run(  # noqa: S603 - argv is built here
                [
                    shutil.which("aws"),
                    "ec2-instance-connect",
                    "open-tunnel",
                    "--instance-id",
                    "i-0000000000000000",  # malformed on purpose
                    "--instance-connect-endpoint-id",
                    eice_endpoint_id,
                    "--region",
                    os.environ.get("AWS_TEST_REGION", "us-west-2"),
                    "--profile",
                    AWS_TEST_PROFILE,
                    "--max-tunnel-duration",
                    str(duration),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )

        over = attempt(EICE_TUNNEL_MAX_DURATION + 1)
        assert "ParamValidation" in over.stderr, (
            f"the API accepted max-tunnel-duration={EICE_TUNNEL_MAX_DURATION + 1}, "
            "so the clamp in EICETunnel is no longer needed: " + over.stderr
        )

        # At the ceiling the duration is fine and validation moves on to the
        # deliberately malformed instance ID, which is how "accepted" is visible
        # without opening a real tunnel.
        at = attempt(EICE_TUNNEL_MAX_DURATION)
        assert "ParamValidation" not in at.stderr, (
            f"max-tunnel-duration={EICE_TUNNEL_MAX_DURATION} is now rejected too; "
            "the ceiling moved: " + at.stderr
        )
        assert "InvalidInstanceID.Malformed" in at.stderr

    def test_a_tunnel_established_this_way_carries_the_forward(
        self, aws_session, live_instance, eice_endpoint_id, keys, local_tunnel_binaries
    ):
        """``EICETunnel.start()`` against a real instance, with no provider involved.

        ``ExitOnForwardFailure=yes`` makes a failed bind an immediate exit, so a
        process that is still alive after the settle window has really bound the
        forward. That is the property ``start()`` relies on to distinguish
        established from failed, and it is checked here against real sshd.
        """
        tunnel = self._tunnel(aws_session, live_instance, eice_endpoint_id, keys)

        try:
            _start_with_retry(tunnel)

            assert tunnel.is_alive(), (
                "ssh exited despite start() returning, so the settle window is "
                "too short to distinguish established from failed"
            )
            assert tunnel.age() > 0
        finally:
            tunnel.stop()

        assert not tunnel.is_alive()

    def test_a_stale_key_is_refused(
        self, aws_session, live_instance, eice_endpoint_id, keys, local_tunnel_binaries
    ):
        """The ~60s authorisation window is why the key is re-pushed every connect.

        Without the re-push a reconnect twenty minutes later fails with
        ``Permission denied (publickey)`` -- and because the supervisor reconnects
        on a dead tunnel, that would turn one dropped tunnel into a worker that
        never comes back. Rather than sleep out the window, this drives the
        underlying ssh directly with a key that was never authorised at all,
        which is the same server-side state.
        """
        tunnel = self._tunnel(aws_session, live_instance, eice_endpoint_id, keys)

        # ssh_command() with no preceding push_key(): the key is valid, well
        # formed, and unknown to the instance.
        result = subprocess.run(  # noqa: S603 - argv is built by EICETunnel
            tunnel.ssh_command(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert result.returncode != 0, (
            "sshd accepted an unauthorised key, so send-ssh-public-key is not "
            "what is granting access and the re-push logic is untested"
        )
        assert (
            "denied" in result.stderr.lower() or "authenticat" in result.stderr.lower()
        )

        # And the same key works once pushed, so the failure above was the
        # authorisation and not something about the key or the endpoint.
        try:
            _start_with_retry(tunnel)
            assert tunnel.is_alive()
        finally:
            tunnel.stop()

    def test_the_supervisor_reconnects_a_killed_tunnel(
        self, aws_session, live_instance, eice_endpoint_id, keys, local_tunnel_binaries
    ):
        """A dropped tunnel must come back inside HTEX's 120s heartbeat threshold.

        The kill stands in for AWS closing the websocket at the ceiling, which is
        the failure the supervisor exists for. Driving ``_supervise_once``
        directly rather than waiting on the poll interval keeps this about the
        reconnect and not about the timer.
        """
        from parsl_ephemeral_provider.network.eice import EICETunnelSupervisor

        private, public = keys
        supervisor = EICETunnelSupervisor(
            session=aws_session,
            endpoint_id=eice_endpoint_id,
            ports=[INTERCHANGE_PORT],
            public_key_path=public,
            private_key_path=private,
            profile_name=AWS_TEST_PROFILE,
        )
        try:
            # add() starts the tunnel itself, so the first-connect race with
            # sshd lands here; retry on the same budget the provider uses.
            deadline = time.time() + TUNNEL_OPEN_TIMEOUT
            while True:
                try:
                    tunnel = supervisor.add(live_instance)
                    break
                except Exception:
                    if time.time() >= deadline:
                        raise
                    time.sleep(TUNNEL_OPEN_RETRY_DELAY)

            first_pid = tunnel._process.pid
            tunnel._process.kill()
            tunnel._process.wait(timeout=30)
            assert not tunnel.is_alive()

            started = time.time()
            supervisor._supervise_once()
            elapsed = time.time() - started

            assert tunnel.is_alive(), "the supervisor did not re-establish the tunnel"
            assert tunnel._process.pid != first_pid, "the same process was reported"
            assert elapsed < 120, (
                f"the reconnect took {elapsed:.0f}s, past HTEX's 120s "
                "heartbeat_threshold; the interchange would have given up on "
                "the manager and rescheduled its tasks"
            )
        finally:
            supervisor.shutdown()

    def test_a_bad_endpoint_fails_at_initialize(
        self, tmp_path, test_run_id, aws_region, network_ids
    ):
        """A typo must fail in ``initialize()``, not inside a ProxyCommand.

        The endpoint joins the VPC/subnet/SG in ``_verify_resources``, so this is
        the same class of check as the other three. Without it the failure
        arrives at the first submit as an ssh process exiting for no visible
        reason, minutes in, after an instance has been billed.
        """
        from parsl_ephemeral_provider.exceptions import ResourceNotFoundError

        with pytest.raises((ResourceNotFoundError, Exception), match="eice-"):
            EphemeralProvider(
                region=aws_region,
                instance_type="t3.micro",
                mode="standard",
                one_shot=True,
                auto_create_instance_profile=True,
                instance_connect_endpoint_id="eice-00000000000000000",
                state_store_type="file",
                state_file_path=str(tmp_path / f"bad-eice-{test_run_id}.json"),
                profile_name=AWS_TEST_PROFILE,
                additional_tags={"E2ETestRunId": test_run_id, "AutoCleanup": "true"},
                **network_ids,
            )


# ---------------------------------------------------------------------------
# The documented least-privilege policy, checked against IAM itself
# ---------------------------------------------------------------------------


class TestLeastPrivilegePolicy:
    """``eice_iam_statements`` is advice, so IAM should be asked to confirm it.

    The interesting claim is the scoping: ``OpenTunnel`` conditioned on
    ``remotePort == 22``. This design only ever tunnels to sshd and carries the
    ZMQ ports *inside* that session, so a grant open to any remote port would let
    the holder reach any service on any instance they can name. A policy document
    nobody evaluates is a plausible-looking suggestion; ``SimulateCustomPolicy``
    is IAM's own verdict.
    """

    def _simulate(self, aws_session, statements, action, resource, context=None):
        iam = aws_session.client("iam")
        kwargs = {
            "PolicyInputList": [
                json.dumps({"Version": "2012-10-17", "Statement": statements})
            ],
            "ActionNames": [action],
            "ResourceArns": [resource],
        }
        if context:
            kwargs["ContextEntries"] = context
        try:
            response = iam.simulate_custom_policy(**kwargs)
        except iam.exceptions.ClientError as exc:
            pytest.skip(f"iam:SimulateCustomPolicy not permitted: {exc}")
        return response["EvaluationResults"][0]["EvalDecision"]

    def _port_context(self, port):
        return [
            {
                "ContextKeyName": "ec2-instance-connect:remotePort",
                "ContextKeyValues": [str(port)],
                "ContextKeyType": "numeric",
            }
        ]

    def test_it_allows_the_tunnel_to_port_22(self, aws_session, eice_endpoint_id):
        statements = eice_iam_statements(endpoint_id=eice_endpoint_id)
        arn = f"arn:aws:ec2:*:*:instance-connect-endpoint/{eice_endpoint_id}"

        decision = self._simulate(
            aws_session,
            statements,
            "ec2-instance-connect:OpenTunnel",
            arn,
            self._port_context(22),
        )

        assert decision == "allowed", (
            f"the documented policy does not permit the only tunnel this design "
            f"opens: {decision}"
        )

    def test_it_does_not_allow_a_tunnel_to_any_other_port(
        self, aws_session, eice_endpoint_id
    ):
        """The condition is the only thing making this narrower than ``*``.

        An unconditioned ``OpenTunnel`` grant reaches every port on every
        instance the holder can name, which for a driver credential is a much
        larger blast radius than "can ssh to workers".
        """
        statements = eice_iam_statements(endpoint_id=eice_endpoint_id)
        arn = f"arn:aws:ec2:*:*:instance-connect-endpoint/{eice_endpoint_id}"

        decision = self._simulate(
            aws_session,
            statements,
            "ec2-instance-connect:OpenTunnel",
            arn,
            self._port_context(INTERCHANGE_PORT),
        )

        assert decision != "allowed", (
            "the policy permits tunnelling to an arbitrary port; the remotePort "
            "condition is not doing what it claims"
        )

    def test_it_allows_pushing_a_key_for_the_documented_user_only(self, aws_session):
        """``ec2:osuser`` scopes the key push, so root is not reachable.

        ``SendSSHPublicKey`` with no condition authorises a key for *any* OS user
        on the instance, including root.
        """
        statements = eice_iam_statements()
        arn = "arn:aws:ec2:us-east-1:942542972736:instance/i-0123456789abcdef0"

        def decide(user):
            return self._simulate(
                aws_session,
                statements,
                "ec2-instance-connect:SendSSHPublicKey",
                arn,
                [
                    {
                        "ContextKeyName": "ec2:osuser",
                        "ContextKeyValues": [user],
                        "ContextKeyType": "string",
                    }
                ],
            )

        assert decide("ec2-user") == "allowed"
        assert decide("root") != "allowed", (
            "the policy permits authorising a key for root on any instance"
        )

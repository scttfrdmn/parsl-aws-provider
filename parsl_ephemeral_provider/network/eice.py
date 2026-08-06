"""Reverse tunnels to HTEX workers over EC2 Instance Connect Endpoints (#134).

Why a *reverse* tunnel
----------------------
HTEX's workers connect **outbound** to the interchange: ``DEFAULT_LAUNCH_CMD``
hands the worker ``-a {addresses} --port={worker_port}``, and the interchange
binds that port on the driver (``interchange.py``, ``bind_to_random_port`` over
``worker_port_range``, default ``54000-55000``). The driver must therefore accept
*inbound* TCP from EC2. From a laptop behind home or office NAT that cannot work,
and the project's answer until now was "use detached mode" -- pay for a bastion to
work around a network topology problem.

#134 proposed ``aws ec2-instance-connect open-tunnel --remote-port`` to carry the
interchange ports. That does not work, and the reason is worth stating because it
shapes everything here: ``--remote-port`` is "the remote port to connect to *on
the instance*" and ``--local-port`` is "the local port to listen on". It is a
**forward** tunnel, driver to worker. Pointing it at 54000-55000 forwards
driver-side connections to ports on the worker where nothing is listening.

What works, and was verified live against a ``t3.micro`` in a subnet with no
public IP and no NAT, is to use EICE for the one thing it does -- reach port 22 --
and let SSH carry the reverse forward::

    ssh -o ProxyCommand="aws ec2-instance-connect open-tunnel --instance-id <id>" \\
        -R <port>:127.0.0.1:<port> ec2-user@<id>

A ``zmq.DEALER`` on the worker then completes a full round trip to a
``zmq.ROUTER`` bound to driver loopback -- exactly HTEX's socket pair, on exactly
its port range. ZMTP does a bidirectional greeting, so this is a stronger check
than raw TCP: a forward that only half-works fails it.

The consequence for configuration is pleasant: because the reverse forward makes
the interchange appear on the worker's *own* loopback, the worker's ``-a`` should
be ``127.0.0.1`` and **no driver address needs to be routable at all**. It also
composes with #62 -- a worker talking to loopback is a better story for
``encrypted=True`` than one talking to a public IP.

The constraints, all confirmed against the live API
--------------------------------------------------
* **A tunnel is capped at 3600 seconds.** Anything above it is rejected with
  ``ParamValidation: Value must be greater than 1 and less than 3600``. That
  message is off by one -- 3600 itself is accepted and 3601 is not -- so the cap
  is inclusive. Either way a tunnel cannot outlive an hour, which is why this is
  a *supervised*, reconnecting design rather than a fire-and-forget one.
* **The reconnect budget is HTEX's ``heartbeat_threshold``, default 120s**
  (``heartbeat_period`` 30s). Reconnect must complete well inside that or the
  interchange declares the manager lost and reschedules its tasks.
* **``send-ssh-public-key`` grants roughly a 60-second window.** A key pushed at
  submit time is useless on a reconnect twenty minutes later, which fails with
  ``Permission denied (publickey)``. So the key is re-pushed immediately before
  *every* connect attempt, not once per instance.
* **Endpoint creation takes several minutes** (~4.5 in testing). The endpoint is
  therefore caller-supplied, per the pre-provisioned-network model of #69, and is
  never created here.

Dependencies
------------
This is the only part of the package that shells out: it needs an ``ssh`` binary
and the AWS CLI v2 (for the ``open-tunnel`` ProxyCommand, which has no boto3
equivalent -- ``ec2-instance-connect`` exposes only ``SendSSHPublicKey`` and
``SendSerialConsoleSSHPublicKey`` in the API). Both are checked up front, because
a missing binary should be a clear configuration error rather than a worker that
mysteriously never registers.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import logging
import os
import re
import shutil

# The ssh and aws argv are built in this module, never from caller strings;
# see ssh_command() and proxy_command().
import subprocess  # nosec B404
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from botocore.exceptions import ClientError

from parsl_ephemeral_provider.exceptions import (
    ProviderConfigurationError,
    ResourceCreationError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    import boto3

logger = logging.getLogger(__name__)

#: The hard ceiling the API enforces on a single tunnel, in seconds. Verified
#: live: 3600 is accepted and 3601 is rejected by ParamValidation before the call
#: is made, despite the error saying "less than 3600".
EICE_TUNNEL_MAX_DURATION = 3600

#: How long before the cap to voluntarily recycle a tunnel. A tunnel torn down by
#: AWS at the ceiling drops mid-message; one recycled early is replaced while the
#: old one still works, so the gap is a reconnect rather than a failure.
DEFAULT_RECYCLE_MARGIN = 300

#: Default OS user for Amazon Linux, matching the AMIs in ``constants.py``.
DEFAULT_INSTANCE_OS_USER = "ec2-user"

#: Seconds to wait for SSH to establish before giving up on an attempt. Generous
#: because it covers the websocket handshake through the endpoint as well.
DEFAULT_CONNECT_TIMEOUT = 60

#: ``send-ssh-public-key`` authorises the key for about a minute, so pushing it
#: earlier than this before connecting is pointless.
KEY_VALIDITY_S = 60

#: How long to watch a freshly started ``ssh`` before calling it established.
#: With ``ExitOnForwardFailure=yes`` a bind failure is an immediate exit, so this
#: only has to outlast process startup plus the websocket handshake.
DEFAULT_SETTLE_TIMEOUT = 5

#: Grace given to ``ssh`` after SIGTERM before escalating to SIGKILL.
DEFAULT_TERMINATE_TIMEOUT = 10


#: HTEX writes ``--port={worker_port}`` into the launch command (see
#: ``DEFAULT_LAUNCH_CMD``). Both ``=`` and whitespace are accepted for the same
#: reason ``extract_cert_dir`` accepts both: the format string is upstream's and
#: a caller may supply their own ``launch_cmd``.
_WORKER_PORT_RE = re.compile(r"--port[=\s]+(?P<port>\d+)")

#: The interchange also hands workers ``-a <addresses>``. Matched so the mode can
#: warn when the command names an address the tunnel makes irrelevant.
_ADDRESSES_RE = re.compile(r"(?:^|\s)-a[=\s]+(?P<addresses>[^\s]+)")


def extract_worker_ports(command: str) -> List[int]:
    """Return the interchange ports named by *command*, for reverse forwarding.

    The provider is never told where the interchange is listening; it reads the
    port out of the command HTEX interpolated, exactly as
    :func:`~parsl_ephemeral_provider.security.curvezmq.extract_cert_dir` reads
    ``--cert_dir``. There is normally exactly one: the interchange binds a single
    ``ROUTER`` for workers via ``bind_to_random_port`` over ``worker_port_range``.

    Parameters
    ----------
    command : str
        The command the executor asked the provider to run.

    Returns
    -------
    List[int]
        Ports to forward, in the order found, deduplicated. Empty when the
        command names none -- which means the caller passed a command that is not
        an HTEX worker pool, and there is nothing to tunnel.
    """
    ports: List[int] = []
    for match in _WORKER_PORT_RE.finditer(command):
        port = int(match.group("port"))
        if port not in ports:
            ports.append(port)
    return ports


def extract_addresses(command: str) -> Optional[str]:
    """Return the ``-a`` addresses in *command*, if any.

    Used only to tell the caller when their configuration is self-defeating: with
    a reverse tunnel the interchange arrives on the worker's own loopback, so an
    ``address`` naming a public IP means HTEX will try that first and the tunnel
    buys nothing.
    """
    match = _ADDRESSES_RE.search(command)
    return match.group("addresses") if match else None


def resolve_ssh_binary(ssh_binary: Optional[str] = None) -> str:
    """Locate the ``ssh`` executable, or explain what is missing.

    Parameters
    ----------
    ssh_binary : Optional[str], optional
        Explicit path, by default None (search ``PATH``).

    Returns
    -------
    str
        Absolute path to an ``ssh`` executable.

    Raises
    ------
    ProviderConfigurationError
        If no usable ``ssh`` is found.
    """
    candidate = ssh_binary or shutil.which("ssh")
    if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    raise ProviderConfigurationError(
        f"No usable ssh executable ({ssh_binary or 'ssh not on PATH'}). The EICE "
        "reverse tunnel shells out to ssh, because EC2 Instance Connect only "
        "forwards *into* an instance and the reverse forward has to come from "
        "somewhere. Install OpenSSH or pass ssh_binary."
    )


def resolve_aws_cli(aws_cli: Optional[str] = None) -> str:
    """Locate the AWS CLI, which supplies the tunnel ProxyCommand.

    ``open-tunnel`` is a CLI-only feature: the ``ec2-instance-connect`` API model
    exposes only ``SendSSHPublicKey`` and ``SendSerialConsoleSSHPublicKey``, so
    there is no boto3 call to use instead.

    Parameters
    ----------
    aws_cli : Optional[str], optional
        Explicit path, by default None (search ``PATH``).

    Returns
    -------
    str
        Absolute path to the ``aws`` executable.

    Raises
    ------
    ProviderConfigurationError
        If no usable ``aws`` is found.
    """
    candidate = aws_cli or shutil.which("aws")
    if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    raise ProviderConfigurationError(
        f"No usable aws executable ({aws_cli or 'aws not on PATH'}). The tunnel "
        "uses `aws ec2-instance-connect open-tunnel` as an SSH ProxyCommand; it "
        "has no boto3 equivalent. Install AWS CLI v2 or pass aws_cli."
    )


class EICETunnel:
    """One reverse tunnel to one instance, held open by a child ``ssh`` process.

    A tunnel is a *process*, not a request. Nothing here is durable: if the
    driver dies the tunnel dies with it, which is correct -- the interchange it
    forwards to died too.

    Parameters
    ----------
    session : boto3.Session
        Session used to push the SSH key.
    instance_id : str
        Instance to tunnel to.
    endpoint_id : str
        Caller-supplied EC2 Instance Connect Endpoint ID.
    ports : List[int]
        Driver-side ports to expose on the worker's loopback. Each becomes an
        ``ssh -R port:127.0.0.1:port``.
    public_key_path : str
        Public key to authorise via ``send-ssh-public-key``.
    private_key_path : str
        Matching private key for ``ssh -i``.
    os_user : str, optional
        Remote user, by default ``ec2-user``.
    region : Optional[str], optional
        Region for the CLI call, by default the session's.
    profile_name : Optional[str], optional
        Profile for the CLI call, by default None. The ProxyCommand runs in a
        separate process and does not inherit a boto3 session, so a profile the
        session was built from has to be passed through explicitly.
    ssh_binary : Optional[str], optional
        Path to ``ssh``, by default resolved from ``PATH``.
    aws_cli : Optional[str], optional
        Path to ``aws``, by default resolved from ``PATH``.
    connect_timeout : int, optional
        Seconds to allow for the connection, by default 60.
    max_duration : int, optional
        Tunnel lifetime requested of AWS, by default 3600 (the ceiling).
    settle_timeout : int, optional
        Seconds to watch a new ``ssh`` before calling it established, by default
        5. Exposed so tests need not spend it; there is no reason to change it
        in production.
    terminate_timeout : int, optional
        Seconds after SIGTERM before escalating to SIGKILL, by default 10.
    """

    def __init__(
        self,
        session: "boto3.Session",
        instance_id: str,
        endpoint_id: str,
        ports: List[int],
        public_key_path: str,
        private_key_path: str,
        os_user: str = DEFAULT_INSTANCE_OS_USER,
        region: Optional[str] = None,
        profile_name: Optional[str] = None,
        ssh_binary: Optional[str] = None,
        aws_cli: Optional[str] = None,
        connect_timeout: int = DEFAULT_CONNECT_TIMEOUT,
        max_duration: int = EICE_TUNNEL_MAX_DURATION,
        settle_timeout: int = DEFAULT_SETTLE_TIMEOUT,
        terminate_timeout: int = DEFAULT_TERMINATE_TIMEOUT,
    ) -> None:
        if not ports:
            raise ProviderConfigurationError(
                "An EICE reverse tunnel needs at least one port to forward. "
                "Pass the interchange's worker port."
            )
        if max_duration >= EICE_TUNNEL_MAX_DURATION:
            # Clamp rather than raise: asking for "as long as possible" is
            # reasonable. Landing a second under the ceiling rather than on it
            # because the API's own error message claims the boundary is
            # exclusive when it is not -- one of the two is wrong, and this way
            # it does not matter which.
            max_duration = EICE_TUNNEL_MAX_DURATION - 1

        self.session = session
        self.instance_id = instance_id
        self.endpoint_id = endpoint_id
        self.ports = list(ports)
        self.public_key_path = public_key_path
        self.private_key_path = private_key_path
        self.os_user = os_user
        self.region = region or session.region_name
        self.profile_name = profile_name
        self.ssh_binary = resolve_ssh_binary(ssh_binary)
        self.aws_cli = resolve_aws_cli(aws_cli)
        self.connect_timeout = connect_timeout
        self.max_duration = max_duration
        self.settle_timeout = settle_timeout
        self.terminate_timeout = terminate_timeout

        self._process: Optional[subprocess.Popen] = None
        self._started_at: Optional[float] = None
        # start() and stop() are reached from two threads: the submit path calls
        # them through the supervisor's add()/remove(), and the supervision thread
        # calls them from _supervise_once(). Without this, two concurrent start()s
        # both pass the is_alive() check, both spawn ssh, and the second
        # assignment to _process orphans the first process for good -- an ssh that
        # nothing holds a handle to and no shutdown can reap.
        self._lifecycle_lock = threading.Lock()
        # Set once the tunnel is deliberately retired, so a supervision pass that
        # picked it up before the removal cannot start it again. Without it,
        # closing a tunnel because the instance is being terminated is undone by
        # whichever pass was already in flight.
        self._retired = False

    # ------------------------------------------------------------------
    # Command construction
    # ------------------------------------------------------------------

    def proxy_command(self) -> str:
        """Return the ``ProxyCommand`` string that opens the websocket tunnel.

        ``%h`` is not used: the instance is named by ID rather than by hostname,
        because that is what ``open-tunnel`` takes and it avoids depending on DNS
        inside a private subnet.
        """
        parts = [
            self.aws_cli,
            "ec2-instance-connect",
            "open-tunnel",
            "--instance-id",
            self.instance_id,
            "--instance-connect-endpoint-id",
            self.endpoint_id,
            "--region",
            str(self.region),
            "--max-tunnel-duration",
            str(self.max_duration),
        ]
        if self.profile_name:
            parts += ["--profile", self.profile_name]
        return " ".join(parts)

    def ssh_command(self) -> List[str]:
        """Return the full ``ssh`` argv for the reverse tunnel.

        ``-N`` (no remote command) and ``-T`` (no TTY) because this process
        exists only to hold forwards open. ``ExitOnForwardFailure=yes`` is the
        load-bearing option: without it ssh reports success while the forward
        silently did not bind, and the failure would surface much later as a
        worker that never registers.
        """
        argv = [
            self.ssh_binary,
            "-N",
            "-T",
            "-o",
            f"ProxyCommand={self.proxy_command()}",
            "-o",
            "StrictHostKeyChecking=no",
            # A fresh instance's host key is genuinely unknown, and every
            # instance is ephemeral, so a known_hosts file would only accumulate
            # entries and eventually produce spurious mismatches.
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
            # Notice a dead tunnel in ~60s rather than waiting on TCP timeouts,
            # so the supervisor can reconnect inside the 120s heartbeat budget.
            "-o",
            "ServerAliveInterval=20",
            "-o",
            "ServerAliveCountMax=3",
            "-i",
            self.private_key_path,
        ]
        for port in self.ports:
            # Bind the worker end to loopback explicitly: the default would make
            # the forwarded port reachable from elsewhere in the VPC if the
            # instance had GatewayPorts on, and only the worker needs it.
            argv += ["-R", f"127.0.0.1:{port}:127.0.0.1:{port}"]
        argv.append(f"{self.os_user}@{self.instance_id}")
        return argv

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def push_key(self) -> None:
        """Authorise the public key on the instance for the next ~60 seconds.

        Raises
        ------
        ResourceCreationError
            If the key could not be pushed.
        """
        with open(self.public_key_path, "r") as handle:
            public_key = handle.read().strip()

        client = self.session.client("ec2-instance-connect")
        try:
            client.send_ssh_public_key(
                InstanceId=self.instance_id,
                InstanceOSUser=self.os_user,
                SSHPublicKey=public_key,
            )
        except ClientError as exc:
            raise ResourceCreationError(
                f"Could not authorise an SSH key on {self.instance_id}: {exc}. "
                "The tunnel needs ec2-instance-connect:SendSSHPublicKey."
            ) from exc
        logger.debug(
            f"Authorised SSH key on {self.instance_id} for user {self.os_user} "
            f"(valid ~{KEY_VALIDITY_S}s)"
        )

    def start(self) -> None:
        """Push the key and start the ``ssh`` process holding the forwards.

        The key push is deliberately immediately before the connect: it is only
        valid for about a minute, so any gap here is a gap that fails.

        Serialized against ``stop()`` and against another ``start()``: the submit
        path and the supervision thread both reach this, and a lost race leaks an
        ssh process nothing can reap.

        Raises
        ------
        ResourceCreationError
            If ssh exits immediately, which means the forward never bound.
        """
        with self._lifecycle_lock:
            self._start_locked()

    def _start_locked(self) -> None:
        """``start()`` proper, with ``_lifecycle_lock`` already held."""
        if self.is_alive():
            return
        if self._retired:
            # Retired means the instance is going away, so a start here would be
            # a tunnel to nothing -- and it would be invisible, because the
            # supervisor has already dropped its reference.
            logger.debug(
                f"Not starting a retired tunnel to {self.instance_id}; the "
                "instance is no longer supervised."
            )
            return

        self.push_key()
        argv = self.ssh_command()
        logger.debug(f"Opening EICE reverse tunnel: {' '.join(argv)}")
        # stdin closed: BatchMode already forbids prompting, and an inherited
        # stdin would let ssh consume the parent's input.
        self._process = subprocess.Popen(  # noqa: S603 # nosec B603
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._started_at = time.time()

        # ExitOnForwardFailure makes a bind failure an immediate exit, so a short
        # wait distinguishes "established" from "failed" without polling ZMQ.
        try:
            self._process.wait(timeout=self.settle_timeout)
        except subprocess.TimeoutExpired:
            logger.info(
                f"EICE reverse tunnel to {self.instance_id} established, "
                f"forwarding {self.ports} to driver loopback"
            )
            return

        stderr = b""
        if self._process.stderr is not None:
            stderr = self._process.stderr.read() or b""
        rc = self._process.returncode
        self._process = None
        raise ResourceCreationError(
            f"The EICE reverse tunnel to {self.instance_id} exited immediately "
            f"(rc={rc}): {stderr.decode(errors='replace').strip()}. With "
            "ExitOnForwardFailure=yes this means the forward never bound, so no "
            "worker would have reached the interchange."
        )

    def is_alive(self) -> bool:
        """Whether the tunnel process is still running."""
        return self._process is not None and self._process.poll() is None

    def age(self) -> float:
        """Seconds since the tunnel started, or 0 if it never did."""
        return 0.0 if self._started_at is None else time.time() - self._started_at

    def needs_recycle(self, margin: int = DEFAULT_RECYCLE_MARGIN) -> bool:
        """Whether the tunnel is close enough to the 3600s cap to replace.

        Replacing early is the whole reason this is supervised: a tunnel AWS
        tears down at the ceiling drops whatever was in flight.
        """
        return self.age() >= max(0, self.max_duration - margin)

    def stop(self) -> None:
        """Terminate the tunnel process, escalating to kill if needed."""
        with self._lifecycle_lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        """``stop()`` proper, with ``_lifecycle_lock`` already held."""
        if self._process is None:
            return
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=self.terminate_timeout)
            except subprocess.TimeoutExpired:
                logger.warning(
                    f"ssh for {self.instance_id} ignored SIGTERM; killing it"
                )
                self._process.kill()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                    logger.error(f"Could not reap ssh for {self.instance_id}")
        # Popen holds two pipes; closing them keeps a long-running driver from
        # leaking file descriptors one tunnel at a time.
        for stream in (self._process.stdout, self._process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except Exception:  # nosec B110 # pragma: no cover - defensive
                    pass
        logger.debug(f"Closed EICE reverse tunnel to {self.instance_id}")
        self._process = None
        self._started_at = None

    def restart(self) -> None:
        """Replace the tunnel process, holding the lock across both halves.

        The supervisor's stop-then-start has to be atomic: with two separate
        acquisitions a concurrent ``remove()`` can land in the gap, and the start
        then revives a tunnel to an instance that is already being terminated.
        """
        with self._lifecycle_lock:
            self._stop_locked()
            self._start_locked()

    def retire(self) -> None:
        """Close the tunnel for good, so no later ``start()`` can revive it.

        This is what ``remove()`` and ``shutdown()`` want rather than ``stop()``.
        ``_supervise_once()`` iterates a snapshot, so a pass already in flight
        holds a reference to a tunnel the caller has just dropped; a plain
        ``stop()`` is undone by that pass, leaving an ssh process pointed at an
        instance the provider is terminating and no longer tracked by anything.
        """
        with self._lifecycle_lock:
            self._retired = True
            self._stop_locked()


class EICETunnelSupervisor:
    """Keeps one tunnel per instance alive for as long as the job needs it.

    The supervisor exists because of the 3600-second cap. A single background
    thread re-establishes tunnels that died and pre-emptively recycles ones
    approaching the ceiling, which is what turns a one-hour ceiling into a
    reconnect concern rather than a workflow-length limit.

    Parameters
    ----------
    session : boto3.Session
        Session for key pushes.
    endpoint_id : str
        Caller-supplied endpoint ID, shared by every tunnel.
    ports : List[int]
        Driver-side ports to forward.
    public_key_path, private_key_path : str
        Key pair to authorise and use.
    poll_interval : int, optional
        Seconds between health checks, by default 20. Must stay well under
        HTEX's 120s ``heartbeat_threshold`` so a dead tunnel is noticed and
        replaced before the interchange gives up on the manager.
    **tunnel_kwargs
        Passed to each :class:`EICETunnel`.
    """

    def __init__(
        self,
        session: "boto3.Session",
        endpoint_id: str,
        ports: List[int],
        public_key_path: str,
        private_key_path: str,
        poll_interval: int = 20,
        **tunnel_kwargs: Any,
    ) -> None:
        self.session = session
        self.endpoint_id = endpoint_id
        self.ports = list(ports)
        self.public_key_path = public_key_path
        self.private_key_path = private_key_path
        self.poll_interval = poll_interval
        self.tunnel_kwargs = tunnel_kwargs

        self._tunnels: Dict[str, EICETunnel] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def instance_ids(self) -> List[str]:
        """Instances currently supervised."""
        with self._lock:
            return list(self._tunnels)

    def add(self, instance_id: str) -> EICETunnel:
        """Open a tunnel to *instance_id* and supervise it.

        Returns
        -------
        EICETunnel
            The tunnel, already started.
        """
        with self._lock:
            existing = self._tunnels.get(instance_id)
            if existing is not None and existing.is_alive():
                return existing
            tunnel = EICETunnel(
                session=self.session,
                instance_id=instance_id,
                endpoint_id=self.endpoint_id,
                ports=self.ports,
                public_key_path=self.public_key_path,
                private_key_path=self.private_key_path,
                **self.tunnel_kwargs,
            )

        # Started outside the lock: it blocks for up to five seconds, and the
        # supervisor thread must not be stalled behind an unrelated connect.
        # Registered only once it is up, so the supervision thread -- already
        # running if this is not the first instance -- cannot find a tunnel that
        # has never been started, read that as "died", and reconnect it
        # underneath this call. EICETunnel serialises its own lifecycle, so that
        # race no longer orphans an ssh process, but it would still mean two
        # key pushes and a connect nobody asked for.
        tunnel.start()
        with self._lock:
            self._tunnels[instance_id] = tunnel
        self.start()
        return tunnel

    def remove(self, instance_id: str) -> None:
        """Stop supervising *instance_id* and close its tunnel."""
        with self._lock:
            tunnel = self._tunnels.pop(instance_id, None)
        if tunnel is not None:
            # retire(), not stop(): a supervision pass that snapshotted this
            # tunnel before the pop would otherwise reconnect it.
            tunnel.retire()

    def start(self) -> None:
        """Start the supervision thread if it is not already running."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._supervise,
            name="parsl-eice-tunnel-supervisor",
            daemon=True,
        )
        self._thread.start()
        logger.debug("Started EICE tunnel supervisor")

    def _supervise_once(self) -> None:
        """Run one health-check pass over every supervised tunnel.

        Split out of the loop so it can be driven directly: a test that had to
        wait ``poll_interval`` to observe a reconnect would either be slow or be
        tuned so short that it raced the ssh process it was checking.
        """
        with self._lock:
            items = list(self._tunnels.items())

        for instance_id, tunnel in items:
            if self._stop_event.is_set():
                return
            try:
                if tunnel.needs_recycle():
                    logger.info(
                        f"Recycling the tunnel to {instance_id} after "
                        f"{int(tunnel.age())}s, before AWS closes it at "
                        f"{EICE_TUNNEL_MAX_DURATION}s"
                    )
                    tunnel.restart()
                elif not tunnel.is_alive():
                    logger.warning(
                        f"The tunnel to {instance_id} died after "
                        f"{int(tunnel.age())}s; reconnecting. The worker "
                        "cannot reach the interchange until it is back."
                    )
                    tunnel.restart()
            except Exception as exc:
                # A reconnect failure must not kill the supervisor: the other
                # tunnels are still worth keeping, and this one may recover on
                # the next pass (the instance may simply be terminating).
                logger.warning(
                    f"Could not re-establish the tunnel to {instance_id}: "
                    f"{exc}. Retrying in {self.poll_interval}s."
                )

    def _supervise(self) -> None:
        """Re-establish dead tunnels and recycle ones nearing the cap."""
        while not self._stop_event.is_set():
            self._supervise_once()
            self._stop_event.wait(self.poll_interval)

    def shutdown(self) -> None:
        """Stop supervising and close every tunnel."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=self.poll_interval + 10)
        self._thread = None
        with self._lock:
            tunnels = list(self._tunnels.values())
            self._tunnels.clear()
        for tunnel in tunnels:
            try:
                tunnel.retire()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(f"Error closing a tunnel during shutdown: {exc}")
        logger.debug("EICE tunnel supervisor shut down")


def eice_iam_statements(
    port_range: Optional[tuple] = None,
    endpoint_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return least-privilege IAM statements for the driver side of the tunnel.

    These are for the *driver's* principal, not the worker's -- the worker needs
    nothing, since the tunnel terminates at its sshd.

    ``ec2-instance-connect:OpenTunnel`` is conditionable on
    ``ec2-instance-connect:remotePort``, so the grant can be scoped to port 22
    alone: this design only ever tunnels to sshd, and the reverse forward rides
    inside that SSH session. A grant open to any remote port would let the holder
    reach any service on any instance they can name.

    Parameters
    ----------
    port_range : Optional[tuple], optional
        Unused, and accepted only to make the shape of the mistake obvious: the
        forwarded ZMQ ports never appear in this policy, because they are never
        an EICE ``remotePort``. Kept as an explicit parameter so a caller who
        expects to pass them finds this note rather than silently wrong policy.
    endpoint_id : Optional[str], optional
        Restrict to one endpoint, by default None (any).

    Returns
    -------
    List[Dict[str, Any]]
        Statements for an IAM policy document.
    """
    if port_range is not None:
        logger.debug(
            "eice_iam_statements ignores port_range: the ZMQ ports are forwarded "
            "inside the SSH session, so only port 22 is an EICE remotePort."
        )

    tunnel_resource = (
        f"arn:aws:ec2:*:*:instance-connect-endpoint/{endpoint_id}"
        if endpoint_id
        else "*"
    )
    return [
        {
            "Sid": "ParslEphemeralOpenTunnelToSshOnly",
            "Effect": "Allow",
            "Action": ["ec2-instance-connect:OpenTunnel"],
            "Resource": [tunnel_resource],
            "Condition": {"NumericEquals": {"ec2-instance-connect:remotePort": "22"}},
        },
        {
            "Sid": "ParslEphemeralAuthoriseEphemeralSshKey",
            "Effect": "Allow",
            "Action": ["ec2-instance-connect:SendSSHPublicKey"],
            "Resource": ["arn:aws:ec2:*:*:instance/*"],
            "Condition": {
                "StringEquals": {
                    "ec2:osuser": DEFAULT_INSTANCE_OS_USER,
                }
            },
        },
        {
            "Sid": "ParslEphemeralDescribeForTunnelling",
            "Effect": "Allow",
            "Action": [
                "ec2:DescribeInstances",
                "ec2:DescribeInstanceConnectEndpoints",
            ],
            "Resource": ["*"],
        },
    ]

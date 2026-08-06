"""EICE reverse tunnels for workers behind NAT (#134).

The subject here is a *subprocess*, so these tests are written against the argv
and the lifecycle rather than against AWS. That is deliberate: every AWS-side
constraint that shapes this design was already confirmed live during the spike
(the 3600s tunnel cap, the ~60s key validity, the several-minute endpoint
creation), and none of them can be re-checked from a unit test. What a unit test
*can* check is the part that silently breaks -- that ``ExitOnForwardFailure``
stays in the argv, that the key is pushed before every connect and not once, and
that the reverse forward is ``-R`` in the direction that carries worker to
driver rather than the ``--remote-port`` forward the issue originally proposed.

``ssh`` is replaced with a stub script throughout. A real ``ssh`` would need a
real endpoint and a real instance, which is ``tests/aws/test_eice_tunnel_e2e.py``.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import os
import stat
import subprocess
import threading
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from parsl_ephemeral_provider.exceptions import (
    ProviderConfigurationError,
    ResourceCreationError,
)
from parsl_ephemeral_provider.network.eice import (
    EICE_TUNNEL_MAX_DURATION,
    EICETunnel,
    EICETunnelSupervisor,
    eice_iam_statements,
    extract_addresses,
    extract_worker_ports,
    resolve_aws_cli,
    resolve_ssh_binary,
)

pytestmark = pytest.mark.unit


#: A real ``DEFAULT_LAUNCH_CMD`` after HTEX interpolates it. Kept verbatim rather
#: than trimmed to the interesting flags, because the parser has to survive the
#: whole thing -- ``--hb_period=30`` and ``-p 0`` are the sort of neighbours that
#: break a loose regex.
LAUNCH_CMD = (
    "process_worker_pool.py  --max_workers_per_node=4 -a 127.0.0.1 -p 0 "
    "-c 1 -m None --poll 10 --port=54321 "
    "--cert_dir /home/u/runinfo/000/htex/certificates "
    "--logdir=/x --block_id=0 --hb_period=30 --hb_threshold=120 "
    "--drain_period=None --cpu-affinity none --mpi-launcher=mpiexec "
    "--available-accelerators"
)


@pytest.fixture
def keypair(tmp_path):
    """A private/public path pair. The contents only have to be readable."""
    private = tmp_path / "tunnel_key"
    private.write_text("PRIVATE")
    public = tmp_path / "tunnel_key.pub"
    public.write_text("ssh-ed25519 AAAAC3Nz test\n")
    return str(private), str(public)


@pytest.fixture
def fake_ssh(tmp_path):
    """An ``ssh`` that blocks until killed, standing in for a live tunnel."""
    script = tmp_path / "ssh-holds-open"
    script.write_text("#!/bin/sh\nwhile true; do sleep 30; done\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


@pytest.fixture
def failing_ssh(tmp_path):
    """An ``ssh`` that exits at once, as ExitOnForwardFailure makes it do."""
    script = tmp_path / "ssh-fails"
    script.write_text(
        "#!/bin/sh\n"
        "echo 'Warning: remote port forwarding failed for listen port 54321' >&2\n"
        "exit 255\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


@pytest.fixture
def fake_aws(tmp_path):
    """An ``aws`` binary. Never executed -- it only has to resolve."""
    script = tmp_path / "aws"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


@pytest.fixture
def session():
    """A session whose ``ec2-instance-connect`` client is a mock."""
    sess = MagicMock()
    sess.region_name = "us-east-1"
    client = MagicMock()
    sess.client.return_value = client
    return sess


#: The two waits a stub ``ssh`` should not make a test pay. Both are production
#: tunables (5s to watch a new process settle, 10s of SIGTERM grace) whose values
#: only matter against a real ssh reaching a real endpoint; a stub either holds
#: open or exits immediately, and both are decided in milliseconds.
FAST_TIMEOUTS = {"settle_timeout": 1, "terminate_timeout": 1}


def _tunnel(session, keypair, fake_ssh, fake_aws, **overrides):
    private, public = keypair
    kwargs = {
        "session": session,
        "instance_id": "i-0123456789abcdef0",
        "endpoint_id": "eice-0123456789abcdef0",
        "ports": [54321],
        "private_key_path": private,
        "public_key_path": public,
        "ssh_binary": fake_ssh,
        "aws_cli": fake_aws,
        **FAST_TIMEOUTS,
    }
    kwargs.update(overrides)
    return EICETunnel(**kwargs)


class TestExtractWorkerPorts:
    """The provider is never told the port; it reads it out of the command.

    Same seam ``extract_cert_dir`` uses for #62, for the same reason: HTEX
    interpolates ``--port=`` into ``launch_cmd`` before the provider sees it, and
    that is the only record of where the interchange bound.
    """

    def test_it_finds_the_port_in_a_real_launch_command(self):
        assert extract_worker_ports(LAUNCH_CMD) == [54321]

    def test_it_accepts_a_space_instead_of_equals(self):
        # A caller may supply their own launch_cmd; the '=' is upstream's choice.
        assert extract_worker_ports("worker.py --port 54999 -a 1.2.3.4") == [54999]

    def test_it_is_not_fooled_by_neighbouring_flags(self):
        # -p 0 (prefetch) and --hb_period=30 are the near misses that matter.
        assert extract_worker_ports(LAUNCH_CMD) == [54321]

    def test_it_deduplicates_while_keeping_order(self):
        command = "worker.py --port=54001 --port=54002 --port=54001"
        assert extract_worker_ports(command) == [54001, 54002]

    def test_no_port_is_not_an_error(self):
        # A caller-supplied command that is not an HTEX worker pool has nothing
        # to forward, and the mode treats the empty list as "skip tunnelling".
        assert extract_worker_ports("echo hello") == []


class TestExtractAddresses:
    """Only used to warn, so the bar is that it reads the real shape."""

    def test_it_finds_the_addresses_flag(self):
        assert extract_addresses(LAUNCH_CMD) == "127.0.0.1"

    def test_it_finds_a_comma_separated_list(self):
        command = "worker.py -a 10.0.1.5,127.0.0.1 --port=54321"
        assert extract_addresses(command) == "10.0.1.5,127.0.0.1"

    def test_absent_is_none(self):
        assert extract_addresses("worker.py --port=54321") is None


class TestBinaryResolution:
    """A missing binary must be a configuration error, not a dead tunnel.

    This is the only part of the package that shells out, so "aws is not
    installed" is a new failure mode for this project and deserves to be said
    plainly rather than surfacing as a worker that never registers.
    """

    def test_an_explicit_ssh_path_is_used(self, fake_ssh):
        assert resolve_ssh_binary(fake_ssh) == fake_ssh

    def test_a_missing_ssh_is_a_configuration_error(self, tmp_path):
        with pytest.raises(ProviderConfigurationError, match="No usable ssh"):
            resolve_ssh_binary(str(tmp_path / "nope"))

    def test_a_non_executable_ssh_is_a_configuration_error(self, tmp_path):
        candidate = tmp_path / "ssh"
        candidate.write_text("#!/bin/sh\n")
        candidate.chmod(0o644)
        with pytest.raises(ProviderConfigurationError, match="No usable ssh"):
            resolve_ssh_binary(str(candidate))

    def test_a_missing_aws_cli_says_why_boto3_is_not_enough(self, tmp_path):
        with pytest.raises(ProviderConfigurationError, match="no boto3 equivalent"):
            resolve_aws_cli(str(tmp_path / "nope"))


class TestTunnelCommandConstruction:
    """The argv is the design. Each assertion here is a defect that shipped once.

    Nothing in this class starts a process; ``ssh_command`` and
    ``proxy_command`` are pure.
    """

    def test_the_forward_is_reverse_and_binds_worker_loopback(
        self, session, keypair, fake_ssh, fake_aws
    ):
        # The whole point. #134 proposed `open-tunnel --remote-port`, which is a
        # *forward* tunnel and would send driver-side connections to a port on
        # the worker where nothing listens. -R is the direction that works.
        argv = _tunnel(session, keypair, fake_ssh, fake_aws).ssh_command()
        assert "-R" in argv
        assert argv[argv.index("-R") + 1] == "127.0.0.1:54321:127.0.0.1:54321"

    def test_every_port_gets_its_own_forward(
        self, session, keypair, fake_ssh, fake_aws
    ):
        argv = _tunnel(
            session, keypair, fake_ssh, fake_aws, ports=[54001, 54002]
        ).ssh_command()
        forwards = [argv[i + 1] for i, a in enumerate(argv) if a == "-R"]
        assert forwards == [
            "127.0.0.1:54001:127.0.0.1:54001",
            "127.0.0.1:54002:127.0.0.1:54002",
        ]

    def test_exit_on_forward_failure_is_set(self, session, keypair, fake_ssh, fake_aws):
        # Load-bearing: without it ssh reports success while the forward silently
        # did not bind, and the failure surfaces minutes later as a worker that
        # never registers. `start()` relies on it to tell established from failed.
        argv = _tunnel(session, keypair, fake_ssh, fake_aws).ssh_command()
        assert "ExitOnForwardFailure=yes" in argv

    def test_it_cannot_prompt(self, session, keypair, fake_ssh, fake_aws):
        # A provider running unattended must never block on a password or a host
        # key prompt; that would look like a hung submit.
        argv = _tunnel(session, keypair, fake_ssh, fake_aws).ssh_command()
        assert "BatchMode=yes" in argv
        assert "StrictHostKeyChecking=no" in argv
        assert "UserKnownHostsFile=/dev/null" in argv

    def test_keepalives_fit_inside_the_heartbeat_budget(
        self, session, keypair, fake_ssh, fake_aws
    ):
        # HTEX's heartbeat_threshold is 120s. 20s x 3 notices a dead tunnel in
        # ~60s, leaving the supervisor time to reconnect before the interchange
        # declares the manager lost.
        argv = _tunnel(session, keypair, fake_ssh, fake_aws).ssh_command()
        assert "ServerAliveInterval=20" in argv
        assert "ServerAliveCountMax=3" in argv

    def test_it_runs_no_remote_command(self, session, keypair, fake_ssh, fake_aws):
        argv = _tunnel(session, keypair, fake_ssh, fake_aws).ssh_command()
        assert "-N" in argv and "-T" in argv

    def test_it_connects_as_the_os_user_at_the_instance_id(
        self, session, keypair, fake_ssh, fake_aws
    ):
        # By ID, not hostname: that is what open-tunnel takes, and it avoids
        # depending on DNS inside a private subnet.
        argv = _tunnel(session, keypair, fake_ssh, fake_aws).ssh_command()
        assert argv[-1] == "ec2-user@i-0123456789abcdef0"

    def test_a_custom_os_user_is_honoured(self, session, keypair, fake_ssh, fake_aws):
        argv = _tunnel(
            session, keypair, fake_ssh, fake_aws, os_user="ubuntu"
        ).ssh_command()
        assert argv[-1] == "ubuntu@i-0123456789abcdef0"

    def test_the_proxy_command_opens_the_tunnel_through_the_endpoint(
        self, session, keypair, fake_ssh, fake_aws
    ):
        proxy = _tunnel(session, keypair, fake_ssh, fake_aws).proxy_command()
        assert "ec2-instance-connect open-tunnel" in proxy
        assert "--instance-id i-0123456789abcdef0" in proxy
        assert "--instance-connect-endpoint-id eice-0123456789abcdef0" in proxy
        assert "--region us-east-1" in proxy

    def test_the_proxy_command_carries_the_profile(
        self, session, keypair, fake_ssh, fake_aws
    ):
        # The ProxyCommand is a separate process and inherits no boto3 session,
        # so a profile the session was built from has to be passed explicitly or
        # the CLI falls back to default credentials.
        proxy = _tunnel(
            session, keypair, fake_ssh, fake_aws, profile_name="aws"
        ).proxy_command()
        assert "--profile aws" in proxy

    def test_the_proxy_command_omits_profile_when_there_is_none(
        self, session, keypair, fake_ssh, fake_aws
    ):
        assert (
            "--profile"
            not in _tunnel(session, keypair, fake_ssh, fake_aws).proxy_command()
        )


class TestDurationCeiling:
    """3600s is an API limit, confirmed live: 3601 and up are rejected.

    The CLI's own message ("must be greater than 1 and less than 3600") is off by
    one -- 3600 itself is accepted -- so the clamp lands a second under the
    ceiling and it does not matter which of the two is wrong.
    ``tests/aws/test_eice_tunnel_e2e.py`` asserts both boundaries against the
    real API.
    """

    def test_the_default_is_clamped_below_the_ceiling(
        self, session, keypair, fake_ssh, fake_aws
    ):
        # Asking for "as long as possible" is reasonable, so this clamps rather
        # than raising -- but it must land under the boundary the API rejects.
        tunnel = _tunnel(session, keypair, fake_ssh, fake_aws)
        assert tunnel.max_duration == EICE_TUNNEL_MAX_DURATION - 1
        assert f"--max-tunnel-duration {EICE_TUNNEL_MAX_DURATION - 1}" in (
            tunnel.proxy_command()
        )

    def test_an_over_ceiling_request_is_clamped_not_rejected(
        self, session, keypair, fake_ssh, fake_aws
    ):
        tunnel = _tunnel(session, keypair, fake_ssh, fake_aws, max_duration=99999)
        assert tunnel.max_duration < EICE_TUNNEL_MAX_DURATION

    def test_a_shorter_request_is_left_alone(
        self, session, keypair, fake_ssh, fake_aws
    ):
        tunnel = _tunnel(session, keypair, fake_ssh, fake_aws, max_duration=600)
        assert tunnel.max_duration == 600

    def test_recycling_is_due_before_aws_closes_the_tunnel(
        self, session, keypair, fake_ssh, fake_aws
    ):
        tunnel = _tunnel(session, keypair, fake_ssh, fake_aws, max_duration=600)
        tunnel._started_at = 0.0  # ancient, so age() is far past the margin
        assert tunnel.needs_recycle(margin=300)

    def test_a_young_tunnel_is_not_recycled(
        self, session, keypair, fake_ssh, fake_aws, monkeypatch
    ):
        tunnel = _tunnel(session, keypair, fake_ssh, fake_aws, max_duration=3599)
        monkeypatch.setattr(tunnel, "age", lambda: 10.0)
        assert not tunnel.needs_recycle()

    def test_a_tunnel_needs_at_least_one_port(self, session, keypair, fake_ssh):
        with pytest.raises(ProviderConfigurationError, match="at least one port"):
            EICETunnel(
                session=session,
                instance_id="i-1",
                endpoint_id="eice-1",
                ports=[],
                private_key_path=keypair[0],
                public_key_path=keypair[1],
                ssh_binary=fake_ssh,
            )


class TestKeyPush:
    """``SendSSHPublicKey`` grants roughly 60 seconds, so timing is the design."""

    def test_the_key_is_pushed_for_the_configured_user(
        self, session, keypair, fake_ssh, fake_aws
    ):
        tunnel = _tunnel(session, keypair, fake_ssh, fake_aws, os_user="ubuntu")
        tunnel.push_key()
        session.client.assert_called_with("ec2-instance-connect")
        kwargs = session.client.return_value.send_ssh_public_key.call_args.kwargs
        assert kwargs["InstanceId"] == "i-0123456789abcdef0"
        assert kwargs["InstanceOSUser"] == "ubuntu"
        assert kwargs["SSHPublicKey"] == "ssh-ed25519 AAAAC3Nz test"

    def test_a_denied_push_names_the_permission_needed(
        self, session, keypair, fake_ssh, fake_aws
    ):
        session.client.return_value.send_ssh_public_key.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
            "SendSSHPublicKey",
        )
        tunnel = _tunnel(session, keypair, fake_ssh, fake_aws)
        with pytest.raises(ResourceCreationError, match="SendSSHPublicKey"):
            tunnel.push_key()

    def test_every_connect_re_pushes_the_key(
        self, session, keypair, fake_ssh, fake_aws
    ):
        # The reason this is per-connect rather than per-instance: a key pushed at
        # submit is dead by the time a tunnel recycles at ~55 minutes, and the
        # reconnect fails with 'Permission denied (publickey)'. Observed live.
        tunnel = _tunnel(session, keypair, fake_ssh, fake_aws)
        try:
            tunnel.start()
            tunnel.stop()
            tunnel.start()
        finally:
            tunnel.stop()
        assert session.client.return_value.send_ssh_public_key.call_count == 2


class TestTunnelLifecycle:
    """Started, alive, stopped -- against stub ``ssh`` binaries."""

    def test_a_tunnel_that_holds_open_is_alive(
        self, session, keypair, fake_ssh, fake_aws
    ):
        tunnel = _tunnel(session, keypair, fake_ssh, fake_aws)
        try:
            tunnel.start()
            assert tunnel.is_alive()
        finally:
            tunnel.stop()
        assert not tunnel.is_alive()

    def test_an_immediate_exit_is_reported_as_a_failed_forward(
        self, session, keypair, failing_ssh, fake_aws
    ):
        # ExitOnForwardFailure turns a bind failure into an immediate exit, so
        # this is the signal that no worker would have reached the interchange.
        tunnel = _tunnel(session, keypair, failing_ssh, fake_aws)
        with pytest.raises(ResourceCreationError, match="exited immediately"):
            tunnel.start()
        assert not tunnel.is_alive()

    def test_the_failure_message_carries_ssh_stderr(
        self, session, keypair, failing_ssh, fake_aws
    ):
        # Without this the operator sees only a return code, and 255 from ssh
        # means everything from DNS to permissions.
        tunnel = _tunnel(session, keypair, failing_ssh, fake_aws)
        with pytest.raises(ResourceCreationError, match="remote port forwarding"):
            tunnel.start()

    def test_starting_an_already_live_tunnel_is_a_no_op(
        self, session, keypair, fake_ssh, fake_aws
    ):
        tunnel = _tunnel(session, keypair, fake_ssh, fake_aws)
        try:
            tunnel.start()
            first = tunnel._process
            tunnel.start()
            assert tunnel._process is first
        finally:
            tunnel.stop()

    def test_stopping_a_tunnel_that_never_started_is_not_an_error(
        self, session, keypair, fake_ssh, fake_aws
    ):
        _tunnel(session, keypair, fake_ssh, fake_aws).stop()

    def test_stop_closes_the_pipes(self, session, keypair, fake_ssh, fake_aws):
        # Popen holds two; a long-running driver that recycled tunnels hourly
        # would otherwise leak file descriptors one tunnel at a time.
        tunnel = _tunnel(session, keypair, fake_ssh, fake_aws)
        tunnel.start()
        process = tunnel._process
        tunnel.stop()
        assert process.stdout.closed and process.stderr.closed

    def test_an_unresponsive_ssh_is_killed(self, session, keypair, tmp_path, fake_aws):
        # SIGTERM-ignoring ssh must not hang shutdown forever.
        script = tmp_path / "ssh-ignores-term"
        script.write_text("#!/bin/sh\ntrap '' TERM\nwhile true; do sleep 30; done\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        tunnel = _tunnel(session, keypair, str(script), fake_aws)
        tunnel.start()
        tunnel.stop()
        assert not tunnel.is_alive()

    def test_age_is_zero_before_it_starts(self, session, keypair, fake_ssh, fake_aws):
        assert _tunnel(session, keypair, fake_ssh, fake_aws).age() == 0.0


class TestSupervisor:
    """One tunnel per instance, kept alive across the 3600s ceiling."""

    def _supervisor(self, session, keypair, ssh, aws, **kwargs):
        private, public = keypair
        return EICETunnelSupervisor(
            session=session,
            endpoint_id="eice-0123456789abcdef0",
            ports=[54321],
            private_key_path=private,
            public_key_path=public,
            ssh_binary=ssh,
            aws_cli=aws,
            # A short poll interval so shutdown()'s thread join is quick. The
            # health-check pass itself is always driven directly via
            # _supervise_once(); nothing here waits for the loop to come round.
            poll_interval=1,
            **{**FAST_TIMEOUTS, **kwargs},
        )

    def test_add_starts_a_tunnel_and_tracks_it(
        self, session, keypair, fake_ssh, fake_aws
    ):
        sup = self._supervisor(session, keypair, fake_ssh, fake_aws)
        try:
            tunnel = sup.add("i-1")
            assert tunnel.is_alive()
            assert sup.instance_ids == ["i-1"]
        finally:
            sup.shutdown()

    def test_adding_the_same_instance_twice_reuses_the_live_tunnel(
        self, session, keypair, fake_ssh, fake_aws
    ):
        # A warm instance re-submitted must not accumulate ssh processes, and its
        # existing forward is already correct.
        sup = self._supervisor(session, keypair, fake_ssh, fake_aws)
        try:
            first = sup.add("i-1")
            assert sup.add("i-1") is first
            assert sup.instance_ids == ["i-1"]
        finally:
            sup.shutdown()

    def test_remove_closes_the_tunnel(self, session, keypair, fake_ssh, fake_aws):
        sup = self._supervisor(session, keypair, fake_ssh, fake_aws)
        try:
            tunnel = sup.add("i-1")
            sup.remove("i-1")
            assert not tunnel.is_alive()
            assert sup.instance_ids == []
        finally:
            sup.shutdown()

    def test_removing_an_unknown_instance_is_not_an_error(
        self, session, keypair, fake_ssh, fake_aws
    ):
        # cleanup_resources() calls this for every resource ID it is given,
        # including ones that never had a tunnel.
        sup = self._supervisor(session, keypair, fake_ssh, fake_aws)
        sup.remove("i-never-existed")

    def test_shutdown_closes_every_tunnel(self, session, keypair, fake_ssh, fake_aws):
        sup = self._supervisor(session, keypair, fake_ssh, fake_aws)
        tunnels = [sup.add(f"i-{n}") for n in range(3)]
        sup.shutdown()
        assert not any(t.is_alive() for t in tunnels)
        assert sup.instance_ids == []

    def test_shutdown_stops_the_thread(self, session, keypair, fake_ssh, fake_aws):
        # A daemon thread would not block exit, but it would keep reconnecting
        # to instances the provider has terminated.
        sup = self._supervisor(session, keypair, fake_ssh, fake_aws)
        sup.add("i-1")
        thread = sup._thread
        sup.shutdown()
        assert thread is not None and not thread.is_alive()

    def test_a_dead_tunnel_is_reconnected(self, session, keypair, fake_ssh, fake_aws):
        # The reconnect loop is the reason this class exists. Driven directly
        # rather than by waiting on the poll interval, which no test should do.
        sup = self._supervisor(session, keypair, fake_ssh, fake_aws)
        try:
            tunnel = sup.add("i-1")
            tunnel._process.kill()
            tunnel._process.wait()
            assert not tunnel.is_alive()

            sup._stop_event.clear()
            sup._supervise_once()
            assert tunnel.is_alive()
        finally:
            sup.shutdown()

    def test_a_tunnel_near_the_ceiling_is_recycled(
        self, session, keypair, fake_ssh, fake_aws
    ):
        sup = self._supervisor(session, keypair, fake_ssh, fake_aws)
        try:
            tunnel = sup.add("i-1")
            original = tunnel._process
            tunnel._started_at = 0.0  # ancient: past the recycle margin
            sup._supervise_once()
            assert tunnel.is_alive()
            assert tunnel._process is not original
        finally:
            sup.shutdown()

    def test_a_reconnect_failure_does_not_kill_the_supervisor(
        self, session, keypair, fake_ssh, failing_ssh, fake_aws
    ):
        # The other tunnels are still worth keeping, and a terminating instance
        # legitimately refuses connections.
        sup = self._supervisor(session, keypair, fake_ssh, fake_aws)
        try:
            good = sup.add("i-good")
            bad = sup.add("i-bad")
            bad.ssh_binary = failing_ssh
            bad._process.kill()
            bad._process.wait()

            sup._supervise_once()  # must not raise
            assert good.is_alive()
            assert not bad.is_alive()
        finally:
            sup.shutdown()

    def test_concurrent_starts_do_not_orphan_an_ssh_process(
        self, session, keypair, fake_ssh, fake_aws
    ):
        """Two threads racing to start one tunnel must leave exactly one process.

        Found by noticing two ``ssh`` processes with PPID 1 outliving a unit run.
        ``start()`` is reached from the submit path and from the supervision
        thread, and a check-then-act on ``_process`` let both spawn: the second
        assignment overwrote the first handle, orphaning a process that no
        ``stop()`` or ``shutdown()`` could ever reap. On a long-lived driver that
        is an unbounded leak of ssh processes and file descriptors.
        """
        sup = self._supervisor(session, keypair, fake_ssh, fake_aws)
        try:
            tunnel = sup.add("i-1")
            first = tunnel._process
            tunnel._process.kill()
            tunnel._process.wait()

            seen = []
            # Four racers plus this thread release it, so five parties.
            barrier = threading.Barrier(5)

            def racer():
                barrier.wait(timeout=30)
                try:
                    tunnel.start()
                except Exception:  # pragma: no cover - not the property tested
                    pass
                seen.append(tunnel._process)

            threads = [threading.Thread(target=racer) for _ in range(4)]
            for thread in threads:
                thread.start()
            barrier.wait(timeout=30)
            for thread in threads:
                thread.join(timeout=30)

            # Every racer must have observed the same process, and it must be a
            # new one: any other handle spawned would be unreachable.
            assert tunnel.is_alive()
            assert tunnel._process is not first
            assert {id(p) for p in seen} == {id(tunnel._process)}

            # And it is genuinely reapable, which is what an orphan is not.
            pid = tunnel._process.pid
            tunnel.stop()
            with pytest.raises(OSError):
                os.kill(pid, 0)
        finally:
            sup.shutdown()

    def test_a_removed_tunnel_is_not_revived_by_a_pass_in_flight(
        self, session, keypair, fake_ssh, fake_aws
    ):
        """``remove()`` must win against a supervision pass that already began.

        ``_supervise_once()`` iterates a snapshot taken under the lock, so a pass
        in flight still holds a reference to a tunnel that ``remove()`` has since
        dropped. If removal only stopped the process, that pass would see it dead,
        reconnect it, and leave an ssh pointed at an instance being terminated --
        untracked, so nothing would ever close it. The snapshot is taken here
        explicitly rather than raced, because the interleaving that matters is
        deterministic and a timing-based version of this test would pass by luck.
        """
        sup = self._supervisor(session, keypair, fake_ssh, fake_aws)
        try:
            tunnel = sup.add("i-1")

            # A pass that has snapshotted the tunnel, before the removal.
            snapshot = list(sup._tunnels.items())
            sup.remove("i-1")
            assert not tunnel.is_alive()
            assert sup.instance_ids == []

            # Now let that stale pass run: it finds a dead tunnel and would
            # reconnect it.
            for _, stale in snapshot:
                if not stale.is_alive():
                    stale.restart()

            assert not tunnel.is_alive(), (
                "a supervision pass in flight revived a removed tunnel; the ssh "
                "process would outlive the instance untracked"
            )
        finally:
            sup.shutdown()


class TestIAMStatements:
    """The grant is scoped to port 22; the ZMQ ports ride inside the session."""

    def test_open_tunnel_is_restricted_to_ssh(self):
        # Validated live with iam:SimulateCustomPolicy: port 22 allowed, port
        # 54321 implicitDeny. An unconditioned grant would let the holder reach
        # any service on any instance they can name.
        statements = eice_iam_statements()
        tunnel = next(
            s for s in statements if s["Sid"] == "ParslEphemeralOpenTunnelToSshOnly"
        )
        condition = tunnel["Condition"]["NumericEquals"]
        assert condition["ec2-instance-connect:remotePort"] == "22"

    def test_the_zmq_ports_never_appear_in_the_policy(self):
        # The shape of the mistake the port_range parameter exists to catch: the
        # forwarded ports are not EICE remotePorts and must not be granted.
        rendered = repr(eice_iam_statements(port_range=(54000, 55000)))
        assert "54000" not in rendered and "55000" not in rendered

    def test_the_key_push_is_scoped_to_one_os_user(self):
        # ec2:osuser is what stops the same permission authorising a key for
        # root.  Confirmed live: root is an implicitDeny under this policy.
        statements = eice_iam_statements()
        push = next(
            s
            for s in statements
            if s["Sid"] == "ParslEphemeralAuthoriseEphemeralSshKey"
        )
        assert push["Condition"]["StringEquals"]["ec2:osuser"] == "ec2-user"

    def test_an_endpoint_id_narrows_the_resource(self):
        statements = eice_iam_statements(endpoint_id="eice-0123456789abcdef0")
        tunnel = next(
            s for s in statements if s["Sid"] == "ParslEphemeralOpenTunnelToSshOnly"
        )
        assert tunnel["Resource"] == [
            "arn:aws:ec2:*:*:instance-connect-endpoint/eice-0123456789abcdef0"
        ]

    def test_without_an_endpoint_id_it_is_any_endpoint(self):
        statements = eice_iam_statements()
        tunnel = next(
            s for s in statements if s["Sid"] == "ParslEphemeralOpenTunnelToSshOnly"
        )
        assert tunnel["Resource"] == ["*"]

    def test_every_statement_is_an_allow_with_a_sid(self):
        for statement in eice_iam_statements():
            assert statement["Effect"] == "Allow"
            assert statement["Sid"].startswith("ParslEphemeral")


class TestNoUnexpectedSubprocesses:
    """Nothing in construction may shell out.

    ``EICETunnel.__init__`` resolving binaries is a PATH lookup, not an exec; a
    constructor that ran ``aws`` would put a network call on the submit path.
    """

    def test_construction_runs_no_process(
        self, session, keypair, fake_ssh, fake_aws, monkeypatch
    ):
        def refuse(*args, **kwargs):
            raise AssertionError(f"unexpected subprocess: {args!r}")

        monkeypatch.setattr(subprocess, "Popen", refuse)
        monkeypatch.setattr(subprocess, "run", refuse)
        tunnel = _tunnel(session, keypair, fake_ssh, fake_aws)
        tunnel.ssh_command()
        tunnel.proxy_command()

    def test_the_private_key_is_passed_by_path_not_content(
        self, session, keypair, fake_ssh, fake_aws
    ):
        # Key material in an argv is visible in `ps` to every user on the box.
        argv = _tunnel(session, keypair, fake_ssh, fake_aws).ssh_command()
        assert "PRIVATE" not in " ".join(argv)
        assert argv[argv.index("-i") + 1] == keypair[0]


class TestGeneratedKeys:
    """StandardMode generates a pair when the caller supplies none."""

    def test_the_generated_key_directory_is_private(self, tmp_path, monkeypatch):
        # 0700, because the private key in it authorises SSH as the OS user.
        from parsl_ephemeral_provider.modes import standard as standard_module

        mode = object.__new__(standard_module.StandardMode)
        mode.provider_id = "prov1234"
        mode.tunnel_private_key_path = None
        mode.tunnel_public_key_path = None
        mode._tunnel_key_dir = None
        monkeypatch.setattr(
            standard_module.tempfile,
            "mkdtemp",
            lambda **kwargs: str(tmp_path / "keys"),
        )
        (tmp_path / "keys").mkdir()

        private, public = mode._ensure_tunnel_keys()
        assert os.path.exists(private) and os.path.exists(public)
        assert stat.S_IMODE(os.stat(mode._tunnel_key_dir).st_mode) == 0o700

    def test_a_missing_ssh_keygen_is_a_configuration_error(self, tmp_path, monkeypatch):
        """No ssh-keygen must name itself, not fail inside subprocess.

        The alternative is a ``FileNotFoundError`` from ``subprocess.run`` on the
        submit path, which says nothing about what to install or that supplying a
        keypair avoids the problem entirely.
        """
        from parsl_ephemeral_provider.exceptions import OperatingModeError
        from parsl_ephemeral_provider.modes import standard as standard_module

        mode = object.__new__(standard_module.StandardMode)
        mode.provider_id = "prov1234"
        mode.tunnel_private_key_path = None
        mode.tunnel_public_key_path = None
        mode._tunnel_key_dir = None
        monkeypatch.setattr(
            standard_module.tempfile, "mkdtemp", lambda **kwargs: str(tmp_path)
        )
        monkeypatch.setattr(standard_module.shutil, "which", lambda name: None)

        with pytest.raises(OperatingModeError, match="ssh-keygen is not on PATH"):
            mode._ensure_tunnel_keys()

    def test_a_supplied_pair_is_used_unchanged(self, keypair):
        from parsl_ephemeral_provider.modes import standard as standard_module

        mode = object.__new__(standard_module.StandardMode)
        mode.provider_id = "prov1234"
        mode.tunnel_private_key_path, mode.tunnel_public_key_path = keypair
        mode._tunnel_key_dir = None
        assert mode._ensure_tunnel_keys() == keypair
        assert mode._tunnel_key_dir is None  # nothing generated, nothing to clean

"""Real-AWS end-to-end tests for CurveZMQ certificate distribution (issue #62).

The unit suite proves the hard part locally: that a worker given these two files
completes a CurveZMQ handshake, and that one given unrelated keys is rejected.
What it cannot prove is the claim the whole design rests on --

    a worker carrying nothing but this provider's auto-created instance profile
    can read a ``SecureString`` and decrypt it, with no extra IAM and no KMS key
    policy of its own

-- because that is a statement about ``AmazonSSMManagedInstanceCore`` and the
``alias/aws/ssm`` managed key, and a mocked SSM client agrees with any claim you
make about IAM. So the central test here hands the *worker* the assertions: the
dispatched command re-derives the SHA-256 of each file it found on disk, checks
the directory and file modes ``curvezmq._load_certificate`` demands, and exits
non-zero on any mismatch. One-shot mode surfaces that exit code as ``FAILED``,
so a ``COMPLETED`` job is worker-side evidence rather than driver-side hope.

The rest is lifecycle, and is deliberately kept off EC2: publish, decrypt,
tag, and delete run against real Parameter Store through the distributor
directly, which costs an API call rather than a five-minute boot.

Two properties are worth naming because they are the security argument:

* the key material must never reach UserData, which is readable for the life of
  the instance through IMDS and returned in plaintext by
  ``DescribeInstanceAttribute``. ``test_the_key_material_is_not_in_user_data``
  checks the live attribute, not the string the provider built.
* the parameters hold the interchange's **server secret key**, so they must not
  outlive the run. Two tests cover that: shutdown deletes them, and a provider
  reconstructed from a state file deletes what its predecessor published.

Run with::

    AWS_TEST_REGION=us-east-1 AWS_TEST_VPC_ID=vpc-xxx \\
    AWS_TEST_SUBNET_ID=subnet-xxx AWS_TEST_SG_ID=sg-xxx \\
    AWS_PROFILE=aws pytest tests/aws/test_curvezmq_e2e.py -m aws --no-cov -v

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import base64
import hashlib
import json
import logging
import os
import time

import pytest
from parsl import curvezmq
from parsl.jobs.states import JobState

from parsl_ephemeral_provider.provider import EphemeralProvider
from parsl_ephemeral_provider.security.curvezmq import (
    WORKER_CERT_FILES,
    CurveZMQCertificateDistributor,
    certificate_iam_statements,
)

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.aws, pytest.mark.slow]

AWS_TEST_PROFILE = "aws"

POLL_INTERVAL_S = 15
#: SSM registration plus worker_init on real iron, matching the one-shot suite.
MAX_WAIT_S = 900

#: Appending to a file instead of pip-installing Parsl. Nothing here needs Parsl
#: on the worker -- the certificates are validated with hashlib and os.stat, and
#: whether they *work* is settled by the handshake test in the unit suite.
E2E_WORKER_INIT = "mkdir -p /var/log/parsl-e2e\ndate +%s >> /var/log/parsl-e2e/boots\n"


# ---------------------------------------------------------------------------
# The worker-side probe
# ---------------------------------------------------------------------------

#: Runs on the instance. Kept as a module constant rather than built inline so
#: the shell quoting has one home, and assembled by concatenation rather than an
#: f-string because the body is Python and full of braces.
_PROBE_BODY = """
failures = []

try:
    dir_mode = stat.S_IMODE(os.stat(cert_dir).st_mode)
except OSError as exc:
    print("cert dir %s is missing: %s" % (cert_dir, exc))
    sys.exit(1)

# curvezmq._load_certificate raises OSError on anything but 0700, so a worker
# with correct key material and a group-readable directory still dies.
if dir_mode != 0o700:
    failures.append("cert dir mode is 0o%o, curvezmq requires 0o700" % dir_mode)

for name in sorted(expected):
    path = os.path.join(cert_dir, name)
    if not os.path.exists(path):
        failures.append("%s was not fetched" % name)
        continue
    file_mode = stat.S_IMODE(os.stat(path).st_mode)
    if file_mode != 0o600:
        failures.append("%s mode is 0o%o, expected 0o600" % (name, file_mode))
    with open(path, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    if digest != expected[name]:
        failures.append(
            "%s digest %s does not match the driver's %s"
            % (name, digest, expected[name])
        )

# The public-only files are not shipped. Asserted here as well as in the unit
# suite because it is the difference between "the worker got what it needs" and
# "the worker got the whole directory".
for name in ("client.key", "server.key"):
    if os.path.exists(os.path.join(cert_dir, name)):
        failures.append("%s was shipped and should not have been" % name)

if failures:
    for line in failures:
        print("FAIL: %s" % line)
    sys.exit(1)

print("certificates verified: %s" % ", ".join(sorted(expected)))
"""


def _cert_digests(cert_dir: str) -> dict:
    """SHA-256 of each file a worker needs, as the driver sees it."""
    digests = {}
    for name in WORKER_CERT_FILES:
        with open(os.path.join(cert_dir, name), "rb") as handle:
            digests[name] = hashlib.sha256(handle.read()).hexdigest()
    return digests


def _validation_command(driver_cert_dir: str) -> str:
    """A command that validates the fetched certificates and fails if they are wrong.

    Shaped so the provider sees a ``--cert_dir`` exactly where HTEX would put
    one: the provider never asks where the certificates are, it reads the path
    out of the command string. The ``:`` no-op carries the flag without needing a
    ``process_worker_pool.py`` on the instance.

    The probe looks for the certificates at the *driver's* path, because that is
    what the design does: the fetch script recreates whatever ``--cert_dir``
    names, so the command HTEX already interpolated needs no rewriting. Pointing
    the flag at some other worker-side directory would not be a stricter check,
    it would be an impossible one -- the driver reads that path to publish from.
    """
    probe = (
        "import hashlib, os, stat, sys\n"
        "cert_dir = " + repr(driver_cert_dir) + "\n"
        "expected = " + repr(_cert_digests(driver_cert_dir)) + "\n" + _PROBE_BODY
    )
    return "\n".join(
        [
            f": --cert_dir {driver_cert_dir}",
            "python3 - <<'PARSL_E2E_PROBE'",
            probe,
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


def _parameter_exists(ssm, name: str) -> bool:
    try:
        ssm.get_parameter(Name=name)
        return True
    except ssm.exceptions.ParameterNotFound:
        return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def driver_cert_dir(tmp_path):
    """Real interchange certificates, generated the way HTEX generates them.

    ``curvezmq.create_certificates`` is what ``HighThroughputExecutor.start()``
    calls, so these are the actual files a live run would distribute -- not
    stand-ins whose shape happens to look right.
    """
    return str(curvezmq.create_certificates(tmp_path / "driver"))


@pytest.fixture
def cert_provider(tmp_path, test_run_id, aws_region, network_ids):
    """A one-shot provider that distributes certificates.

    One-shot rather than the default UserData path for one reason: it reports the
    command's exit code, which is what lets the worker's own verdict on its
    certificates reach the test. ``auto_create_instance_profile=True`` is the
    configuration under test -- the claim is that this profile, unmodified, can
    read a SecureString.
    """
    provider = EphemeralProvider(
        region=aws_region,
        instance_type="t3.micro",
        mode="standard",
        one_shot=True,
        distribute_certificates=True,
        auto_create_instance_profile=True,
        worker_init=E2E_WORKER_INIT,
        state_store_type="file",
        state_file_path=str(tmp_path / f"certs-{test_run_id}.json"),
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
def distributor(aws_session, test_run_id):
    """A distributor over real SSM, cleaned up whatever the test does.

    The teardown deletes by name rather than through ``revoke_all()`` so a test
    that breaks the bookkeeping still cannot leave the interchange's server
    secret key sitting in Parameter Store.
    """
    dist = CurveZMQCertificateDistributor(
        session=aws_session, provider_id=f"e2e-{test_run_id}"
    )
    published = []
    original_publish = dist.publish

    def tracking_publish(job_id, cert_dir):
        published.append(dist.parameter_name(job_id))
        return original_publish(job_id, cert_dir)

    dist.publish = tracking_publish
    yield dist

    ssm = aws_session.client("ssm")
    for name in published:
        try:
            ssm.delete_parameter(Name=name)
        except Exception as exc:
            logger.debug("teardown delete of %s raised (ignored): %s", name, exc)


# ---------------------------------------------------------------------------
# The central test: a worker fetches and validates its own certificates
# ---------------------------------------------------------------------------


class TestAWorkerFetchesItsCertificates:
    """The claim mocks cannot check: the default instance profile suffices."""

    def test_the_worker_finds_exactly_the_certificates_the_driver_published(
        self, cert_provider, driver_cert_dir
    ):
        """COMPLETED here means the worker verified its own certificates.

        The dispatched command compares SHA-256 digests against the driver's,
        checks the 0700 directory mode ``curvezmq`` insists on and 0600 on each
        file, and confirms the public-only files were not shipped. Any of those
        failing exits 1, which one-shot mode reports as FAILED. So this is not
        "the provider says it published" -- it is the worker saying it can read
        what it needs, with no IAM beyond ``AmazonSSMManagedInstanceCore``.
        """
        job_id = cert_provider.submit(
            _validation_command(driver_cert_dir), tasks_per_node=1
        )

        state = _poll_until_terminal(cert_provider, job_id)

        assert state == JobState.COMPLETED, (
            f"the worker rejected its certificates (state={state}); the SSM "
            "invocation's StandardOutputContent names which check failed"
        )

    def test_the_key_material_is_not_in_user_data(self, cert_provider, driver_cert_dir):
        """UserData must carry the fetch, never the keys.

        UserData is readable by any process that can reach IMDS for the life of
        the instance, and ``DescribeInstanceAttribute`` returns it in plaintext
        to anyone with that permission -- which is why the certificates go
        through a SecureString at all. Read back from EC2 rather than from the
        string the provider built, so this fails if the transport ever changes.
        """
        ec2 = cert_provider.session.client("ec2")
        with open(os.path.join(driver_cert_dir, "server.key_secret"), "rb") as handle:
            secret = handle.read()

        job_id = cert_provider.submit(
            _validation_command(driver_cert_dir), tasks_per_node=1
        )
        resource_id = cert_provider.job_map[job_id]["resource_id"]

        try:
            attribute = ec2.describe_instance_attribute(
                InstanceId=resource_id, Attribute="userData"
            )
            user_data = base64.b64decode(
                attribute.get("UserData", {}).get("Value", "")
            ).decode()

            assert "aws ssm get-parameter" in user_data, (
                "UserData does not fetch the certificates at all"
            )
            assert base64.b64encode(secret).decode("ascii") not in user_data, (
                "the server secret key was embedded in UserData"
            )
            # The CURVE key line itself, in case an encoding other than base64
            # is ever used for the payload.
            for line in secret.decode("ascii", "replace").splitlines():
                if "=" in line and len(line.strip()) > 40:
                    assert line.strip() not in user_data, (
                        f"key material leaked into UserData: {line.strip()[:16]}..."
                    )
        finally:
            try:
                cert_provider.cancel([job_id])
            except Exception as exc:
                logger.warning("teardown cancel raised (ignored): %s", exc)


# ---------------------------------------------------------------------------
# Parameter Store lifecycle, without paying for an instance
# ---------------------------------------------------------------------------


class TestParameterStoreRoundTrip:
    """Publish and read back through real SSM and the SSM-managed KMS key."""

    def test_the_certificates_round_trip_byte_for_byte(
        self, distributor, driver_cert_dir, aws_session
    ):
        """A truncated or re-encoded key is indistinguishable from a wrong one.

        CURVE keys are 40 Z85 characters; anything that mangles a byte produces
        a file that still looks like a certificate and fails at handshake time,
        minutes later and somewhere else.
        """
        name = distributor.publish("job-round-trip", driver_cert_dir)
        ssm = aws_session.client("ssm")

        value = ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
        fetched = json.loads(value)

        for filename in WORKER_CERT_FILES:
            with open(os.path.join(driver_cert_dir, filename), "rb") as handle:
                assert base64.b64decode(fetched[filename]) == handle.read(), (
                    f"{filename} did not survive the round trip intact"
                )

    def test_it_is_stored_encrypted(self, distributor, driver_cert_dir, aws_session):
        """A String parameter would be readable by anything with GetParameter.

        Checked two ways because the type alone is cheap to get right and easy to
        get wrong: SSM reports ``SecureString``, and the undecrypted value is not
        the plaintext.
        """
        name = distributor.publish("job-encrypted", driver_cert_dir)
        ssm = aws_session.client("ssm")

        parameter = ssm.get_parameter(Name=name)["Parameter"]

        assert parameter["Type"] == "SecureString"
        assert "client.key_secret" not in parameter["Value"], (
            "the undecrypted value contains the payload, so it was not encrypted"
        )

    def test_it_is_tagged_for_cleanup(self, distributor, driver_cert_dir, aws_session):
        """The tags are how a leaked parameter is traced to the run that made it.

        They arrive in a second call: ``put_parameter`` rejects ``Tags`` together
        with ``Overwrite=True``, and overwrite is required because the warm pool
        reuses an instance across jobs.
        """
        name = distributor.publish("job-tagged", driver_cert_dir)
        ssm = aws_session.client("ssm")

        tags = {
            tag["Key"]: tag["Value"]
            for tag in ssm.list_tags_for_resource(
                ResourceType="Parameter", ResourceId=name
            )["TagList"]
        }

        assert tags.get("ProviderId") == distributor.provider_id
        assert tags.get("CreatedBy") == "parsl-ephemeral-provider"

    def test_republishing_the_same_job_succeeds(
        self, distributor, driver_cert_dir, aws_session
    ):
        """A retried submit must not fail on the parameter its own retry wrote.

        Without ``Overwrite=True`` this raises ``ParameterAlreadyExists``, which
        would make every warm-pool reuse and every retry a hard failure.
        """
        name = distributor.publish("job-twice", driver_cert_dir)

        assert distributor.publish("job-twice", driver_cert_dir) == name
        assert distributor.published_parameters.count(name) == 1, (
            "the parameter was recorded twice, so cleanup would try to delete a "
            "name it had already deleted"
        )

    def test_revoke_deletes_it(self, distributor, driver_cert_dir, aws_session):
        """The material must not outlive the job it was published for."""
        name = distributor.publish("job-revoked", driver_cert_dir)
        ssm = aws_session.client("ssm")
        assert _parameter_exists(ssm, name)

        distributor.revoke("job-revoked")

        assert not _parameter_exists(ssm, name)
        assert name not in distributor.published_parameters

    def test_revoking_something_already_gone_is_not_an_error(
        self, distributor, driver_cert_dir, aws_session
    ):
        """Cleanup runs after failures too, and must not mask the real one.

        ``ParameterNotFound`` is the expected outcome when a previous cleanup
        got there first; raising here would replace the caller's error with a
        teardown error.
        """
        name = distributor.publish("job-double-revoke", driver_cert_dir)
        aws_session.client("ssm").delete_parameter(Name=name)

        distributor.revoke_all()  # must not raise

        assert distributor.published_parameters == []


# ---------------------------------------------------------------------------
# Cleanup across a provider's life, and across two of them
# ---------------------------------------------------------------------------


class TestCleanupAcrossProviderLifetime:
    """Nothing published may survive shutdown, including across a restart."""

    def test_shutdown_deletes_the_published_parameters(
        self, cert_provider, driver_cert_dir, aws_session
    ):
        """Publishing is driven from UserData assembly, so no instance is needed.

        ``_prepare_init_script`` is the seam where the certificates are published
        in production too; calling it directly buys the same coverage without a
        boot.
        """
        mode = cert_provider.operating_mode
        mode._prepare_init_script(_validation_command(driver_cert_dir), "job-shutdown")
        published = mode._cert_distributor.published_parameters
        assert published, "nothing was published, so this proves nothing"

        ssm = aws_session.client("ssm")
        assert all(_parameter_exists(ssm, name) for name in published)

        cert_provider.shutdown()

        remaining = [name for name in published if _parameter_exists(ssm, name)]
        assert not remaining, (
            f"parameters holding the server secret key survived shutdown: {remaining}"
        )

    def test_a_restarted_provider_deletes_what_its_predecessor_published(
        self,
        tmp_path,
        test_run_id,
        aws_region,
        network_ids,
        driver_cert_dir,
        aws_session,
    ):
        """The state file is the only record of the parameter names.

        A driver process that dies between submit and shutdown leaves them
        behind; the successor over the same state file is the only thing that can
        still delete them. It adopts them whether or not *it* has the flag on,
        which is the case that matters -- turning the flag off after deciding you
        do not want key material in Parameter Store must not be what strands it
        there.
        """
        state_file = str(tmp_path / f"certs-restart-{test_run_id}.json")
        ssm = aws_session.client("ssm")

        def _provider(**overrides):
            kwargs = dict(
                region=aws_region,
                instance_type="t3.micro",
                mode="standard",
                one_shot=True,
                distribute_certificates=True,
                auto_create_instance_profile=True,
                worker_init=E2E_WORKER_INIT,
                state_store_type="file",
                state_file_path=state_file,
                profile_name=AWS_TEST_PROFILE,
                additional_tags={"E2ETestRunId": test_run_id, "AutoCleanup": "true"},
                waiter_delay=15,
                waiter_max_attempts=40,
                debug=True,
                **network_ids,
            )
            kwargs.update(overrides)
            return EphemeralProvider(**kwargs)

        # Both providers auto-create an instance profile, so every exit path has
        # to shut them down or this test leaks the IAM pair #132 exists to stop.
        # `first` is never shut down inside the body: the scenario is a driver
        # that vanished mid-run, and if its own cleanup ran there would be
        # nothing left for `second` to prove. "Vanished" must not outlast the
        # test, though, so the teardown below covers it.
        published: list = []
        pending = []
        try:
            first = _provider()
            pending.append(first)
            first.operating_mode._prepare_init_script(
                _validation_command(driver_cert_dir), "job-restart"
            )
            published = first.operating_mode._cert_distributor.published_parameters
            assert published
            first._save_state()
            first.operating_mode.save_state()

            second = _provider(distribute_certificates=False)
            pending.append(second)
            assert second.provider_id == first.provider_id, (
                "the successor did not adopt the persisted provider_id, so it "
                "read no state and could not know what to delete"
            )
            assert (
                second.operating_mode._cert_distributor.published_parameters
                == published
            ), "the successor did not adopt the parameter names from the state file"

            # The deletion under test. Ordered before first's teardown so it is
            # unambiguously the successor that removed them.
            pending.remove(second)
            second.shutdown()

            remaining = [name for name in published if _parameter_exists(ssm, name)]
        finally:
            for provider in pending:
                try:
                    provider.shutdown()
                except Exception as exc:
                    logger.warning("teardown shutdown raised (ignored): %s", exc)
            for name in published:
                try:
                    if _parameter_exists(ssm, name):
                        ssm.delete_parameter(Name=name)
                except Exception as exc:
                    logger.warning("belt-and-braces delete raised (ignored): %s", exc)

        assert not remaining, (
            f"the restarted provider did not delete {remaining}, so the server "
            "secret key would stay in Parameter Store indefinitely"
        )


# ---------------------------------------------------------------------------
# The documented least-privilege policy, checked against IAM itself
# ---------------------------------------------------------------------------


class TestLeastPrivilegePolicy:
    """``certificate_iam_statements`` is advice, so IAM should be asked to confirm it.

    It exists for callers who supply their own ``iam_instance_profile_arn`` and
    want the narrowest grant that works. A policy document nobody evaluates is a
    plausible-looking suggestion; ``SimulateCustomPolicy`` is IAM's own verdict.
    """

    def _simulate(self, aws_session, statements, action, resource):
        iam = aws_session.client("iam")
        try:
            response = iam.simulate_custom_policy(
                PolicyInputList=[
                    json.dumps({"Version": "2012-10-17", "Statement": statements})
                ],
                ActionNames=[action],
                ResourceArns=[resource],
            )
        except iam.exceptions.ClientError as exc:
            pytest.skip(f"iam:SimulateCustomPolicy not permitted: {exc}")
        return response["EvaluationResults"][0]["EvalDecision"]

    def test_it_allows_reading_this_providers_certificates(
        self, aws_session, aws_region, test_run_id
    ):
        provider_id = f"e2e-{test_run_id}"
        statements = certificate_iam_statements(provider_id)
        account = aws_session.client("sts").get_caller_identity()["Account"]
        arn = (
            f"arn:aws:ssm:{aws_region}:{account}:parameter"
            f"/parsl-ephemeral/certs/{provider_id}/job-1"
        )

        decision = self._simulate(aws_session, statements, "ssm:GetParameter", arn)

        assert decision == "allowed", (
            f"the documented least-privilege policy does not permit the fetch "
            f"it exists for: {decision}"
        )

    def test_it_does_not_allow_reading_another_providers_certificates(
        self, aws_session, aws_region, test_run_id
    ):
        """Scoping to the provider is the only thing making this narrower than ``*``.

        ``AmazonSSMManagedInstanceCore`` already grants ``ssm:GetParameter`` on
        every parameter in the account, so a policy that did not scope by
        provider would be advice with no value.
        """
        statements = certificate_iam_statements(f"e2e-{test_run_id}")
        account = aws_session.client("sts").get_caller_identity()["Account"]
        arn = (
            f"arn:aws:ssm:{aws_region}:{account}:parameter"
            f"/parsl-ephemeral/certs/some-other-provider/job-1"
        )

        decision = self._simulate(aws_session, statements, "ssm:GetParameter", arn)

        assert decision != "allowed", (
            "the policy permits reading another provider's certificates, which "
            "include that interchange's server secret key"
        )

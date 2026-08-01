"""Real-AWS end-to-end tests for the warm pool (issue #65).

The warm pool is the one feature in this package whose entire value proposition
is invisible to a mock. It keeps a finished instance *running* so the next job
skips both the EC2 boot and ``worker_init``, and it dispatches over SSM
``SendCommand`` rather than UserData so the command's exit code is observable.
Every one of those properties is a live-AWS property:

* SSM ``SendCommand`` only reaches an instance whose IAM profile carries
  ``AmazonSSMManagedInstanceCore`` and whose agent has registered. Nothing local
  can tell you whether that happened.
* "the instance was reused" means *no second* ``RunInstances`` — a claim about
  what did not happen, which mocks assert by construction and prove nothing.
* "``worker_init`` did not run again" is the actual saving, and it is measurable
  only by looking at the filesystem of the instance that served both jobs.
* a WARM instance is a **billed** instance. TTL expiry and pool-full eviction are
  cost controls, and a cost control that only works against a mock is not one.

``worker_init`` here is deliberately not the package default (which pip-installs
Parsl and adds minutes to every boot). It appends a line to a file instead, which
keeps these tests to a workable runtime *and* gives
``test_a_reused_instance_does_not_run_worker_init_again`` something real to count.
Nothing under test depends on Parsl being present: the warm pool's contract is
about dispatch and reuse, not about what the command happens to be.

Run with::

    AWS_TEST_REGION=us-east-1 AWS_TEST_VPC_ID=vpc-xxx \\
    AWS_TEST_SUBNET_ID=subnet-xxx AWS_TEST_SG_ID=sg-xxx \\
    AWS_PROFILE=aws pytest tests/aws/test_warm_pool_e2e.py -m aws --no-cov -v

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import logging
import time

import pytest
from parsl.jobs.states import JobState

from parsl_aws_provider.constants import STATUS_WARM
from parsl_aws_provider.provider import EphemeralAWSProvider

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.aws, pytest.mark.slow]

AWS_TEST_PROFILE = "aws"

POLL_INTERVAL_S = 15
#: SSM registration plus worker_init on real iron. The mode's own budget is
#: 300s for SSM online and 600s for the ready marker, so submit() can legally
#: take most of this before a job even starts.
MAX_WAIT_S = 900

#: Both files live under a directory this script creates, so a failure to create
#: it shows up as a missing count rather than a silent no-op append.
E2E_LOG_DIR = "/var/log/parsl-e2e"
WORKER_INIT_LOG = f"{E2E_LOG_DIR}/worker_init_runs"
JOB_LOG = f"{E2E_LOG_DIR}/job_runs"

#: Records one line per boot. The warm pool's claim is that a reused instance
#: does not re-run this, so it has to be countable after the fact.
WARM_POOL_WORKER_INIT = f"mkdir -p {E2E_LOG_DIR}\ndate +%s >> {WORKER_INIT_LOG}\n"

#: Records one line per job. Two lines on one instance is reuse.
JOB_COMMAND = f"date +%s >> {JOB_LOG}; echo hello-warm-pool"


def _poll_until_terminal(provider, job_id: str, timeout: int = MAX_WAIT_S):
    """Poll ``status()`` until *job_id* leaves PENDING/RUNNING.

    Returns
    -------
    Optional[JobState]
        The terminal state reached, or None on timeout.
    """
    non_terminal = (JobState.PENDING, JobState.RUNNING)
    deadline = time.time() + timeout
    while time.time() < deadline:
        statuses = provider.status([job_id])
        if statuses and statuses[0].state not in non_terminal:
            return statuses[0].state
        time.sleep(POLL_INTERVAL_S)
    return None


def _instance_ids_for_run(ec2, test_run_id: str, states=None):
    """Every instance tagged with this test run, in *states* (default: any).

    The count is how "no new instance was launched" is verified. It is a proxy
    for "no second ``RunInstances``" and an honest one: the tag is applied in the
    ``TagSpecifications`` of the launch itself (``_create_instance``), so an
    instance cannot exist for this run without carrying it.
    """
    filters = [{"Name": "tag:E2ETestRunId", "Values": [test_run_id]}]
    if states:
        filters.append({"Name": "instance-state-name", "Values": list(states)})
    response = ec2.describe_instances(Filters=filters)
    return sorted(
        inst["InstanceId"]
        for reservation in response.get("Reservations", [])
        for inst in reservation.get("Instances", [])
    )


def _instance_state(ec2, instance_id: str) -> str:
    """Current EC2 state name, or ``"terminated"`` if the record has aged out."""
    try:
        response = ec2.describe_instances(InstanceIds=[instance_id])
    except Exception as exc:
        logger.warning("describe_instances raised (treated as gone): %s", exc)
        return "terminated"
    for reservation in response.get("Reservations", []):
        for instance in reservation.get("Instances", []):
            return instance["State"]["Name"]
    return "terminated"


def _wait_for_instance_state(ec2, instance_id: str, wanted, timeout: int = 600):
    """Poll ``describe_instances`` until *instance_id* reaches one of *wanted*."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = _instance_state(ec2, instance_id)
        if last in wanted:
            return last
        time.sleep(POLL_INTERVAL_S)
    return last


def _ssm_shell(ssm, instance_id: str, command: str, timeout: int = 180) -> str:
    """Run *command* on *instance_id* over SSM and return its stdout.

    Used to read the instance's own filesystem, which is the only place the
    "``worker_init`` ran once, the job ran twice" evidence exists.
    """
    response = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [command]},
        Comment="warm pool E2E probe",
    )
    command_id = response["Command"]["CommandId"]

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            invocation = ssm.get_command_invocation(
                CommandId=command_id, InstanceId=instance_id
            )
        except Exception as exc:  # InvocationDoesNotExist while it propagates
            logger.debug("get_command_invocation not ready yet: %s", exc)
            time.sleep(5)
            continue
        if invocation["Status"] not in ("Pending", "InProgress", "Delayed"):
            assert invocation["Status"] == "Success", (
                f"probe {command!r} on {instance_id} reported "
                f"{invocation['Status']}: {invocation.get('StandardErrorContent', '')}"
            )
            return invocation.get("StandardOutputContent", "")
        time.sleep(5)
    raise AssertionError(f"probe {command!r} on {instance_id} did not finish")


@pytest.fixture
def warm_pool_providers(tmp_path, test_run_id, aws_region, network_ids):
    """Factory for warm-pool providers, torn down in reverse order.

    A factory rather than a plain fixture because the pool's cost controls are
    only observable at specific settings — ``warm_pool_ttl=30`` for expiry,
    ``warm_pool_size=1`` for eviction — and because two tests need a *second*
    provider over the same state file.

    ``auto_create_instance_profile=True`` is mandatory, not incidental: SSM
    ``SendCommand`` needs ``AmazonSSMManagedInstanceCore`` on the instance, and
    the provider refuses to construct a warm pool without it. Each provider owns
    the pair it creates and deletes it on shutdown (#132), so the teardown below
    is what keeps a full run from leaving one IAM principal per test behind.
    """
    created = []

    def _make(
        warm_pool_size: int = 1,
        warm_pool_ttl: int = 600,
        state_file: str = None,
        **overrides,
    ) -> EphemeralAWSProvider:
        kwargs = dict(
            region=aws_region,
            instance_type="t3.micro",
            mode="standard",
            warm_pool_size=warm_pool_size,
            warm_pool_ttl=warm_pool_ttl,
            worker_init=WARM_POOL_WORKER_INIT,
            auto_create_instance_profile=True,
            state_store_type="file",
            state_file_path=state_file
            or str(tmp_path / f"warm-pool-{test_run_id}.json"),
            profile_name=AWS_TEST_PROFILE,
            additional_tags={"E2ETestRunId": test_run_id, "AutoCleanup": "true"},
            waiter_delay=15,
            waiter_max_attempts=40,
            debug=True,
            **network_ids,
        )
        kwargs.update(overrides)
        provider = EphemeralAWSProvider(**kwargs)
        created.append(provider)
        return provider

    yield _make

    for provider in reversed(created):
        try:
            provider.shutdown()
        except Exception as exc:
            logger.warning("Provider shutdown raised (best-effort): %s", exc)


class TestWarmPoolColdStart:
    """The first job has no instance to reuse and must build one."""

    def test_a_cold_start_dispatches_over_ssm_and_completes(
        self, warm_pool_providers, aws_session, aws_region
    ):
        """PENDING → RUNNING → COMPLETED with the command delivered by SSM.

        The status has to come from the SSM invocation. UserData carries no
        command on this path (``_prepare_init_script`` returns after the ready
        marker), so an instance that never received a dispatch would sit at
        RUNNING until ``max_idle_time`` and report success on the way down.
        """
        provider = warm_pool_providers()
        ec2 = aws_session.client("ec2", region_name=aws_region)

        job_id = provider.submit(JOB_COMMAND, tasks_per_node=1)
        resource_id = provider.job_map[job_id]["resource_id"]

        assert resource_id.startswith("i-"), (
            f"expected an EC2 instance ID, got {resource_id!r}"
        )
        record = provider.operating_mode.resources[resource_id]
        assert record.get("warm_pool") is True, (
            f"resource is not marked as warm-pool managed: {record}"
        )
        assert record.get("ssm_command_id"), (
            "submit() returned without an SSM command ID, so the command was "
            f"never dispatched: {record}"
        )

        state = _poll_until_terminal(provider, job_id)
        assert state == JobState.COMPLETED, f"expected COMPLETED, got {state}"

        # The point of the pool: the instance outlives its job.
        assert _instance_state(ec2, resource_id) == "running", (
            f"instance {resource_id} did not stay running after its job finished"
        )


class TestWarmPoolReuse:
    """A second job must land on the first job's instance."""

    def test_a_second_job_reuses_the_instance_without_launching_another(
        self, warm_pool_providers, test_run_id, aws_session, aws_region
    ):
        """One instance serves both jobs — the whole reason the pool exists."""
        provider = warm_pool_providers()
        ec2 = aws_session.client("ec2", region_name=aws_region)

        first_job = provider.submit(JOB_COMMAND, tasks_per_node=1)
        first_instance = provider.job_map[first_job]["resource_id"]
        assert _poll_until_terminal(provider, first_job) == JobState.COMPLETED

        after_first = _instance_ids_for_run(ec2, test_run_id)
        assert after_first == [first_instance], (
            f"expected exactly one instance for this run, got {after_first}"
        )

        second_job = provider.submit(JOB_COMMAND, tasks_per_node=1)
        second_instance = provider.job_map[second_job]["resource_id"]

        assert second_instance == first_instance, (
            f"second job went to {second_instance} instead of reusing {first_instance}"
        )
        after_second = _instance_ids_for_run(ec2, test_run_id)
        assert after_second == [first_instance], (
            "a second instance was launched despite a warm one being available: "
            f"{after_second}"
        )

        assert _poll_until_terminal(provider, second_job) == JobState.COMPLETED

    def test_a_reused_instance_does_not_run_worker_init_again(
        self, warm_pool_providers, aws_session, aws_region
    ):
        """``worker_init`` once, the job twice — read off the instance itself.

        Skipping ``worker_init`` is the saving the pool is sold on, and it is not
        implied by instance reuse: the SSM path could have re-run it per dispatch.
        Counting lines on the instance is the only way to tell.
        """
        provider = warm_pool_providers()
        ssm = aws_session.client("ssm", region_name=aws_region)

        first_job = provider.submit(JOB_COMMAND, tasks_per_node=1)
        instance_id = provider.job_map[first_job]["resource_id"]
        assert _poll_until_terminal(provider, first_job) == JobState.COMPLETED

        second_job = provider.submit(JOB_COMMAND, tasks_per_node=1)
        assert provider.job_map[second_job]["resource_id"] == instance_id
        assert _poll_until_terminal(provider, second_job) == JobState.COMPLETED

        worker_init_runs = _ssm_shell(ssm, instance_id, f"wc -l < {WORKER_INIT_LOG}")
        job_runs = _ssm_shell(ssm, instance_id, f"wc -l < {JOB_LOG}")

        assert int(worker_init_runs.strip()) == 1, (
            f"worker_init ran {worker_init_runs.strip()} times on {instance_id}; "
            "a warm instance must not re-run it"
        )
        assert int(job_runs.strip()) == 2, (
            f"the job ran {job_runs.strip()} times on {instance_id}; both "
            "dispatches should have executed there"
        )


class TestWarmPoolStatus:
    """Status must be read from the SSM command, not the instance state."""

    def test_status_follows_the_ssm_command_not_the_instance_state(
        self, warm_pool_providers, aws_session, aws_region
    ):
        """A failing command reports FAILED while its instance is still running.

        This is the discriminator no EC2-derived status can produce. On the warm
        path the instance stays up regardless of outcome, so ``describe_instances``
        says ``running`` for a success and a failure alike — every job would
        report RUNNING and then COMPLETED. FAILED can only have come from the SSM
        invocation's response code.
        """
        provider = warm_pool_providers()
        ec2 = aws_session.client("ec2", region_name=aws_region)
        ssm = aws_session.client("ssm", region_name=aws_region)

        job_id = provider.submit("sleep 45; exit 3", tasks_per_node=1)
        resource_id = provider.job_map[job_id]["resource_id"]
        command_id = provider.operating_mode.resources[resource_id]["ssm_command_id"]

        # Mid-flight: the command is still going and the provider says RUNNING.
        statuses = provider.status([job_id])
        assert statuses[0].state == JobState.RUNNING, (
            f"expected RUNNING while the command runs, got {statuses[0].state}"
        )
        invocation = ssm.get_command_invocation(
            CommandId=command_id, InstanceId=resource_id
        )
        assert invocation["Status"] in ("Pending", "InProgress", "Delayed"), (
            f"the SSM command was already {invocation['Status']}; this assertion "
            "needs a command still in flight"
        )
        assert _instance_state(ec2, resource_id) == "running"

        state = _poll_until_terminal(provider, job_id)
        assert state == JobState.FAILED, (
            f"expected FAILED from the command's exit code, got {state}"
        )

        invocation = ssm.get_command_invocation(
            CommandId=command_id, InstanceId=resource_id
        )
        assert invocation["ResponseCode"] == 3, (
            f"expected exit code 3, got {invocation['ResponseCode']}"
        )


class TestWarmPoolState:
    """WARM bookkeeping, in memory and across a restart."""

    def test_a_finished_job_leaves_its_instance_warm(
        self, warm_pool_providers, aws_session, aws_region
    ):
        """COMPLETED → ``STATUS_WARM`` in both the record and the reuse list.

        Two places, and both matter: the record is what TTL expiry and eviction
        act on, and ``_warm_instances`` is what the next ``submit_job`` pops. A
        record without a list entry is a billed instance no job will ever reach.
        """
        provider = warm_pool_providers()
        ec2 = aws_session.client("ec2", region_name=aws_region)

        job_id = provider.submit(JOB_COMMAND, tasks_per_node=1)
        resource_id = provider.job_map[job_id]["resource_id"]
        assert _poll_until_terminal(provider, job_id) == JobState.COMPLETED

        record = provider.resources[resource_id]
        assert record["status"] == STATUS_WARM, (
            f"expected {STATUS_WARM}, got {record['status']!r}"
        )
        assert record.get("warm_since"), (
            f"warm_since was not stamped, so the TTL can never expire: {record}"
        )
        assert resource_id in provider.operating_mode._warm_instances, (
            f"{resource_id} is WARM but not in the reuse list "
            f"{provider.operating_mode._warm_instances}"
        )
        assert _instance_state(ec2, resource_id) == "running"

    def test_a_restarted_provider_restores_the_pool_and_reuses_it(
        self, warm_pool_providers, tmp_path, test_run_id, aws_session, aws_region
    ):
        """A second provider over the same state file inherits the warm instance.

        The pool is only free if it survives the driver process. Without this,
        a restart leaves a running instance that nothing tracks — billed, and
        invisible to the successor that would otherwise have reused it.
        """
        state_file = str(tmp_path / f"warm-restart-{test_run_id}.json")
        ec2 = aws_session.client("ec2", region_name=aws_region)

        first = warm_pool_providers(state_file=state_file)
        job_id = first.submit(JOB_COMMAND, tasks_per_node=1)
        instance_id = first.job_map[job_id]["resource_id"]
        assert _poll_until_terminal(first, job_id) == JobState.COMPLETED
        assert instance_id in first.operating_mode._warm_instances

        # No provider_id: the successor adopts the persisted one, which is how a
        # restart is meant to work (nothing but a shared state location).
        second = warm_pool_providers(state_file=state_file)

        assert second.provider_id == first.provider_id, (
            "the successor did not adopt the persisted provider_id, so it read "
            "none of the state"
        )
        assert second.operating_mode._warm_instances == [instance_id], (
            f"restored pool is {second.operating_mode._warm_instances}, expected "
            f"[{instance_id!r}]"
        )

        reused_job = second.submit(JOB_COMMAND, tasks_per_node=1)
        assert second.job_map[reused_job]["resource_id"] == instance_id, (
            "the restarted provider launched a new instance instead of reusing "
            "the warm one it restored"
        )
        assert _instance_ids_for_run(ec2, test_run_id) == [instance_id]
        assert _poll_until_terminal(second, reused_job) == JobState.COMPLETED


class TestWarmPoolEviction:
    """The two cost controls. A warm instance bills at the full rate."""

    def test_ttl_expiry_terminates_the_idle_instance(
        self, warm_pool_providers, aws_session, aws_region
    ):
        """Past ``warm_pool_ttl``, the next poll must terminate it."""
        ttl = 30
        provider = warm_pool_providers(warm_pool_ttl=ttl)
        ec2 = aws_session.client("ec2", region_name=aws_region)

        job_id = provider.submit(JOB_COMMAND, tasks_per_node=1)
        resource_id = provider.job_map[job_id]["resource_id"]
        assert _poll_until_terminal(provider, job_id) == JobState.COMPLETED
        assert provider.resources[resource_id]["status"] == STATUS_WARM

        time.sleep(ttl + 10)

        # status() drives _cleanup_resources() whenever a pool is configured;
        # the job itself is already terminal, so this poll exists only for that.
        provider.status([job_id])

        assert resource_id not in provider.resources, (
            "the expired instance is still tracked as a resource"
        )
        assert resource_id not in provider.operating_mode._warm_instances, (
            "the expired instance is still offered for reuse"
        )
        final = _wait_for_instance_state(
            ec2, resource_id, ("shutting-down", "terminated")
        )
        assert final in ("shutting-down", "terminated"), (
            f"instance {resource_id} is '{final}' well past its {ttl}s TTL and "
            "is still billing"
        )

    def test_a_full_pool_evicts_the_oldest_instance(
        self, warm_pool_providers, aws_session, aws_region
    ):
        """``warm_pool_size`` is a cap, so the second arrival displaces the first.

        Both jobs are submitted before either finishes, which is what forces two
        cold starts: ``_get_warm_instance`` has nothing to hand out until a job
        completes, so the second submit cannot reuse the first instance.
        """
        provider = warm_pool_providers(warm_pool_size=1)
        ec2 = aws_session.client("ec2", region_name=aws_region)

        first_job = provider.submit(JOB_COMMAND, tasks_per_node=1)
        first_instance = provider.job_map[first_job]["resource_id"]
        second_job = provider.submit(JOB_COMMAND, tasks_per_node=1)
        second_instance = provider.job_map[second_job]["resource_id"]

        assert second_instance != first_instance, (
            "both jobs landed on one instance, so nothing can be evicted; the "
            "second submit should have cold-started while the first was busy"
        )

        assert _poll_until_terminal(provider, first_job) == JobState.COMPLETED
        assert _poll_until_terminal(provider, second_job) == JobState.COMPLETED

        warm = provider.operating_mode._warm_instances
        assert warm == [second_instance], (
            f"expected only the newer instance to be warm, got {warm}"
        )
        assert first_instance not in provider.resources, (
            "the evicted instance is still tracked"
        )
        assert provider.resources[second_instance]["status"] == STATUS_WARM

        final = _wait_for_instance_state(
            ec2, first_instance, ("shutting-down", "terminated")
        )
        assert final in ("shutting-down", "terminated"), (
            f"evicted instance {first_instance} is '{final}' and still billing"
        )


class TestWarmPoolTeardown:
    """The acceptance criterion: nothing warm survives the session."""

    def test_shutdown_terminates_the_warm_instance(
        self, warm_pool_providers, test_run_id, aws_session, aws_region
    ):
        """A WARM instance's job is already COMPLETED — it must still be killed.

        This is the leak the pool makes easy: the job finished successfully, so
        nothing about the job's own state says an instance is still running. If
        ``shutdown()`` reasons only over unfinished work, every run leaves one
        billed instance per pool slot behind.
        """
        provider = warm_pool_providers()
        ec2 = aws_session.client("ec2", region_name=aws_region)

        job_id = provider.submit(JOB_COMMAND, tasks_per_node=1)
        resource_id = provider.job_map[job_id]["resource_id"]
        assert _poll_until_terminal(provider, job_id) == JobState.COMPLETED
        assert provider.resources[resource_id]["status"] == STATUS_WARM

        provider.shutdown()

        final = _wait_for_instance_state(
            ec2, resource_id, ("shutting-down", "terminated")
        )
        assert final in ("shutting-down", "terminated"), (
            f"instance {resource_id} survived shutdown() as '{final}'"
        )
        alive = _instance_ids_for_run(
            ec2, test_run_id, states=("pending", "running", "stopping", "stopped")
        )
        assert not alive, f"instances still billable after shutdown: {alive}"


class TestWarmPoolConfiguration:
    """Guards that keep an undispatchable pool from being configured."""

    def test_a_warm_pool_requires_an_instance_profile(
        self, tmp_path, test_run_id, aws_region, network_ids
    ):
        """No profile means SSM cannot reach the instance, so refuse up front.

        Accepting this would produce a pool whose every dispatch times out: the
        agent never registers, ``_wait_for_ssm_online`` exhausts its 300s, and
        submit() terminates the instance it just paid to boot.
        """
        with pytest.raises(ValueError, match="warm_pool_size > 0 requires"):
            EphemeralAWSProvider(
                region=aws_region,
                mode="standard",
                warm_pool_size=1,
                auto_create_instance_profile=False,
                state_store_type="file",
                state_file_path=str(tmp_path / f"warm-guard-{test_run_id}.json"),
                profile_name=AWS_TEST_PROFILE,
                **network_ids,
            )

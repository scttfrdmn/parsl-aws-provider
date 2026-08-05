"""Integration tests for operating modes using substrate.

These drive each mode's real lifecycle -- initialize, submit, status, cancel,
cleanup -- against the emulator, with no mocking of the mode itself.

Two things had kept the whole file from running. ``FileStateStore(file_path=...)``
omitted the required ``provider_id``, so every test errored during fixture setup;
and the assertions described the pre-#69 provider, which created its own VPC,
subnet and security group. It no longer does: the caller supplies them, so
``initialize()`` verifies rather than creates, and ``cleanup_infrastructure()``
leaves them alone -- deleting a caller's network would be the same class of bug as
the serverless security-group deletion fixed in #100. The old
``assert mode.vpc_id is None`` after cleanup asserted the opposite.

Job IDs here lead with their random part rather than a ``test-job-`` prefix. The
stack names derive from ``job_id[:8]`` (``modes/serverless.py:564,717``), so
``f"test-job-{uuid…}"`` truncated to the same ``test-job`` for all three tests and
the suite passed only against a freshly reset emulator -- a second run met
``AlreadyExists``. #183 found this while moving the spot-fleet files off moto, where
a fresh mock per test hid it.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import base64
import os
import re
import urllib.request
import uuid

import pytest
from botocore.exceptions import ClientError

from parsl_ephemeral_provider.constants import (
    BASTION_SCRIPT_URL_TTL,
    MAX_CFN_PARAMETER_BYTES,
    MAX_EC2_USER_DATA_BYTES,
)
from parsl_ephemeral_provider.modes.detached import DetachedMode
from parsl_ephemeral_provider.modes.serverless import ServerlessMode
from parsl_ephemeral_provider.modes.standard import StandardMode
from parsl_ephemeral_provider.state.file import FileStateStore
from tests.substrate_support import is_substrate_available

pytestmark = pytest.mark.skipif(
    not is_substrate_available(),
    reason="substrate not available - start with 'make substrate-up'",
)


@pytest.fixture
def provider_id():
    """Generate a unique provider ID for tests."""
    return f"test-provider-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def temp_state_store(tmp_path, provider_id):
    """Create a temporary state store for testing.

    ``provider_id`` is required (``state/file.py``): it is the key the document is
    written under, so a store without one cannot address its own state.
    """
    return FileStateStore(
        file_path=str(tmp_path / "state.json"), provider_id=provider_id
    )


@pytest.mark.integration
@pytest.mark.substrate
class TestStandardModeSubstrate:
    """Integration tests for StandardMode using substrate."""

    def test_initialize_and_cleanup(
        self, substrate_session, substrate_network, temp_state_store, provider_id
    ):
        """initialize() verifies the caller's network and builds a launch template."""
        mode = StandardMode(
            provider_id=provider_id,
            session=substrate_session,
            state_store=temp_state_store,
            region="us-east-1",
            instance_type="t3.micro",
            image_id="ami-12345678",
            vpc_id=substrate_network["vpc_id"],
            subnet_id=substrate_network["subnet_id"],
            security_group_id=substrate_network["security_group_id"],
        )

        try:
            mode.initialize()

            assert mode.initialized is True
            # The launch template is the mode's own resource, unlike the network.
            assert mode._launch_template_id is not None

            ec2 = substrate_session.client("ec2")
            templates = ec2.describe_launch_templates(
                LaunchTemplateIds=[mode._launch_template_id]
            )
            assert len(templates["LaunchTemplates"]) == 1

            assert os.path.exists(temp_state_store.file_path)

        finally:
            mode.cleanup_infrastructure()

        assert mode.initialized is False
        # The network belongs to the caller and must survive cleanup (#69).
        assert mode.vpc_id == substrate_network["vpc_id"]
        ec2 = substrate_session.client("ec2")
        assert len(ec2.describe_vpcs(VpcIds=[mode.vpc_id])["Vpcs"]) == 1

    def test_submit_job_and_status(
        self, substrate_session, substrate_network, temp_state_store, provider_id
    ):
        """A submitted job launches a tracked instance and can be cancelled."""
        mode = StandardMode(
            provider_id=provider_id,
            session=substrate_session,
            state_store=temp_state_store,
            region="us-east-1",
            instance_type="t3.micro",
            image_id="ami-12345678",
            vpc_id=substrate_network["vpc_id"],
            subnet_id=substrate_network["subnet_id"],
            security_group_id=substrate_network["security_group_id"],
        )

        try:
            mode.initialize()

            job_id = f"{uuid.uuid4().hex[:8]}-test-job"
            resource_id = mode.submit_job(job_id, "echo hello", 1)

            assert resource_id in mode.resources
            assert mode.resources[resource_id]["job_id"] == job_id

            assert mode.get_job_status([resource_id])[resource_id] == "RUNNING"
            assert mode.cancel_jobs([resource_id])[resource_id] == "CANCELED"

            mode.cleanup_resources([resource_id])
            assert resource_id not in mode.resources

        finally:
            mode.cleanup_infrastructure()


@pytest.mark.integration
@pytest.mark.substrate
class TestDetachedModeSubstrate:
    """Integration tests for DetachedMode using substrate.

    ``bastion_host_type="direct"`` throughout, which is now a choice rather than a
    constraint: substrate has served CloudFormation since ``0.87.0`` and deploys
    ``bastion.yml`` end to end since ``0.87.1``. The default stack path is covered by
    ``test_detached_mode_spot_fleet_integration.py``, so keeping ``direct`` here means
    both bastion paths get exercised instead of only one.
    """

    def _mode(self, session, network, store, provider_id, **overrides):
        return DetachedMode(
            provider_id=provider_id,
            session=session,
            state_store=store,
            region="us-east-1",
            instance_type="t3.micro",
            image_id="ami-12345678",
            workflow_id=f"test-workflow-{uuid.uuid4().hex[:8]}",
            bastion_instance_type="t3.micro",
            bastion_host_type="direct",
            vpc_id=network["vpc_id"],
            subnet_id=network["subnet_id"],
            security_group_id=network["security_group_id"],
            **overrides,
        )

    def test_initialize_and_cleanup(
        self, substrate_session, substrate_network, temp_state_store, provider_id
    ):
        """A direct-mode bastion launches with no key pair, and is torn down.

        No ``key_name`` is passed, which is the ordinary configuration -- SSM is
        how you reach the bastion, and it needs no key. That combination used to
        fail botocore's parameter validation before any API call, so this mode
        could not start at all (#158).
        """
        mode = self._mode(
            substrate_session, substrate_network, temp_state_store, provider_id
        )

        try:
            mode.initialize()

            assert mode.initialized is True
            assert mode.bastion_id is not None

            ec2 = substrate_session.client("ec2")
            reservations = ec2.describe_instances(InstanceIds=[mode.bastion_id])
            assert len(reservations["Reservations"]) == 1

        finally:
            mode.preserve_bastion = False
            mode.cleanup_infrastructure()

        assert mode.initialized is False
        assert mode.bastion_id is None
        # Again, the caller's network outlives the mode.
        assert mode.vpc_id == substrate_network["vpc_id"]

    def test_the_bastion_script_is_staged_and_fetchable(
        self, substrate_session, substrate_network, temp_state_store, provider_id
    ):
        """The staged script really is retrievable by the URL in UserData (#227).

        This is the assertion that needed an emulator rather than a mock. The
        init script is ~32 KB against a 4,096 B CloudFormation parameter limit
        and a 16,384 B EC2 UserData limit, so it is uploaded to S3 and UserData
        carries only a presigned fetch. A unit test can show that ``put_object``
        was *called*; only a real S3 and a real HTTP GET show that the URL the
        bastion is handed actually returns the script.

        It caught a defect a mock could not: botocore's presigner emits SigV2 by
        default even when the client reports ``s3v4``, and a SigV2 URL is
        rejected by every region created after 2014 (substrate answers 501).
        """
        mode = self._mode(
            substrate_session, substrate_network, temp_state_store, provider_id
        )

        # This test needs the bucket *absent*, so that the mode creates it and
        # therefore owns it. The name derives from ``provider_id[:8]``, which
        # truncates to a shared ``test-pro`` across the file, so a bucket left by
        # a sibling test would make the mode a non-owner and the cleanup
        # assertions below would fail for the wrong reason.
        s3 = substrate_session.client("s3")
        leftover = f"parsl-bastion-script-{mode.provider_id[:8]}"
        try:
            for obj in s3.list_objects_v2(Bucket=leftover).get("Contents", []):
                s3.delete_object(Bucket=leftover, Key=obj["Key"])
            s3.delete_bucket(Bucket=leftover)
        except s3.exceptions.NoSuchBucket:
            pass

        try:
            user_data = mode._prepare_bastion_user_data()
            assert mode._owns_script_bucket is True

            # UserData is a fetch, not the program.
            assert len(user_data.encode()) < MAX_EC2_USER_DATA_BYTES
            assert len(base64.b64encode(user_data.encode())) < MAX_CFN_PARAMETER_BYTES
            assert "parsl-bastion-manager.py" not in user_data

            # The object exists where the mode says it staged it.
            s3 = substrate_session.client("s3")
            staged = s3.get_object(Bucket=mode._script_bucket, Key=mode._script_key)[
                "Body"
            ].read()
            assert b"parsl-bastion-manager.py" in staged
            assert len(staged) > MAX_EC2_USER_DATA_BYTES

            # And the URL embedded in UserData serves it, unauthenticated --
            # which is the whole reason for a presigned URL: the direct path
            # attaches no instance profile, so the bastion has no credentials
            # of its own to fetch with.
            match = re.search(r"'(https?://[^']+)'", user_data)
            assert match is not None, "no fetch URL found in UserData"
            with urllib.request.urlopen(match.group(1), timeout=30) as response:
                assert response.read() == staged

            # A SigV4 URL, not the SigV2 one botocore's presigner emits by
            # default. That default is what substrate answers 501 to, and what
            # post-2014 regions reject outright.
            assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in match.group(1)
            assert f"X-Amz-Expires={BASTION_SCRIPT_URL_TTL}" in match.group(1)
        finally:
            mode.preserve_bastion = False
            mode.cleanup_infrastructure()

        # Cleanup removes the object and the bucket the mode created, so a
        # bastion's script does not outlive it.
        assert mode._script_key is None
        assert mode._script_bucket is None

    def test_a_caller_supplied_script_bucket_survives_cleanup(
        self, substrate_session, substrate_network, temp_state_store, provider_id
    ):
        """Cleanup deletes only a bucket this mode created.

        The ownership gate, same hazard class as the serverless security-group
        deletion fixed in #100: an existing bucket is reused, and
        ``_owns_script_bucket`` stays False, which is what keeps it.
        """
        mode = self._mode(
            substrate_session, substrate_network, temp_state_store, provider_id
        )

        s3 = substrate_session.client("s3")
        bucket = f"parsl-bastion-script-{mode.provider_id[:8]}"
        create_args = {"Bucket": bucket}
        if substrate_session.region_name != "us-east-1":
            create_args["CreateBucketConfiguration"] = {
                "LocationConstraint": substrate_session.region_name
            }
        # Tolerated because the bucket name is what is under test: the mode
        # derives it from ``provider_id[:8]``, and the ``provider_id`` fixture's
        # value truncates to a shared ``test-pro`` for every test in the file --
        # the same "put the random part first" trap the module docstring records
        # for job IDs. Pre-existing is precisely the state this test wants.
        try:
            s3.create_bucket(**create_args)
        except s3.exceptions.BucketAlreadyExists:
            pass
        except s3.exceptions.BucketAlreadyOwnedByYou:
            pass

        try:
            mode._prepare_bastion_user_data()
            assert mode._script_bucket == bucket
            assert mode._owns_script_bucket is False

            mode.preserve_bastion = False
            mode.cleanup_infrastructure()

            # The bucket is still there; only the object went.
            s3.head_bucket(Bucket=bucket)
        finally:
            for obj in s3.list_objects_v2(Bucket=bucket).get("Contents", []):
                s3.delete_object(Bucket=bucket, Key=obj["Key"])
            s3.delete_bucket(Bucket=bucket)

    def test_the_bucket_is_resolved_once_per_mode(
        self, substrate_session, substrate_network, temp_state_store, provider_id
    ):
        """A second staging call reuses the resolved bucket rather than re-creating.

        The memoized return matters for ownership, not for the API call count:
        ``create_bucket`` on a bucket you already own answers
        ``BucketAlreadyOwnedByYou``, which the except branch treats as "reusing
        an existing bucket" -- so a second pass through it would clear the
        ownership flag this mode needs to delete what it created.
        """
        mode = self._mode(
            substrate_session, substrate_network, temp_state_store, provider_id
        )

        s3 = substrate_session.client("s3")
        bucket = f"parsl-bastion-script-{mode.provider_id[:8]}"
        try:
            for obj in s3.list_objects_v2(Bucket=bucket).get("Contents", []):
                s3.delete_object(Bucket=bucket, Key=obj["Key"])
            s3.delete_bucket(Bucket=bucket)
        except s3.exceptions.NoSuchBucket:
            pass

        try:
            first = mode._ensure_script_bucket()
            assert mode._owns_script_bucket is True

            assert mode._ensure_script_bucket() == first
            assert mode._owns_script_bucket is True
        finally:
            mode.preserve_bastion = False
            mode.cleanup_infrastructure()

    def test_an_unexpected_bucket_error_is_not_swallowed(
        self, substrate_session, substrate_network, temp_state_store, provider_id
    ):
        """Only "already exists" is tolerated; anything else propagates.

        The except branch exists to make a restart or a rebuilt bastion
        idempotent, and it would be easy to write as a bare ``except
        ClientError: pass`` -- which would report a bucket that was never
        created and leave the bastion fetching from nowhere. ``InvalidBucketName``
        is a real S3 error code, raised here by an uppercase provider ID: bucket
        names must be lowercase.
        """
        mode = self._mode(
            substrate_session,
            substrate_network,
            temp_state_store,
            f"UPPER-{uuid.uuid4().hex[:8]}",
        )

        with pytest.raises(ClientError) as excinfo:
            mode._ensure_script_bucket()
        assert excinfo.value.response["Error"]["Code"] == "InvalidBucketName"

        # And nothing was recorded, so cleanup has nothing to chase.
        assert mode._script_bucket is None
        assert mode._owns_script_bucket is False

    def test_cleanup_empties_a_bucket_holding_another_bastions_script(
        self, substrate_session, substrate_network, temp_state_store, provider_id
    ):
        """Leftover objects are swept, since S3 refuses to delete a full bucket.

        The mode deletes its own ``_script_key`` first, so on the ordinary path
        the bucket is already empty by the time it is removed. This exercises the
        case that is not ordinary: a script staged by an earlier bastion whose
        cleanup never ran -- exactly the leak this bucket's teardown exists to
        catch. Without the sweep, ``delete_bucket`` answers ``BucketNotEmpty``
        and the bucket outlives every provider that used it.
        """
        mode = self._mode(
            substrate_session, substrate_network, temp_state_store, provider_id
        )

        s3 = substrate_session.client("s3")
        bucket = f"parsl-bastion-script-{mode.provider_id[:8]}"
        try:
            for obj in s3.list_objects_v2(Bucket=bucket).get("Contents", []):
                s3.delete_object(Bucket=bucket, Key=obj["Key"])
            s3.delete_bucket(Bucket=bucket)
        except s3.exceptions.NoSuchBucket:
            pass

        mode._prepare_bastion_user_data()
        assert mode._owns_script_bucket is True

        # A script from a bastion that never got collected.
        s3.put_object(
            Bucket=bucket, Key="bastion-init-orphaned.sh", Body=b"#!/bin/bash\n"
        )

        mode.preserve_bastion = False
        mode.cleanup_infrastructure()

        assert mode._script_bucket is None
        with pytest.raises(ClientError) as excinfo:
            s3.head_bucket(Bucket=bucket)
        assert excinfo.value.response["Error"]["Code"] in ("404", "NoSuchBucket")

    def test_cleanup_survives_a_bucket_deleted_underneath_it(
        self, substrate_session, substrate_network, temp_state_store, provider_id
    ):
        """A staged script already gone must not break teardown.

        Cleanup runs on the failure path too, so it has to tolerate the state it
        finds. Here the bucket is removed behind the mode's back -- an operator
        sweeping leftovers, or a lifecycle rule -- and both the object delete and
        the bucket delete raise ``NoSuchBucket``. The mode must log and finish:
        raising would mask whatever error triggered the cleanup, and would leave
        the bastion and the caller's network uncollected.
        """
        mode = self._mode(
            substrate_session, substrate_network, temp_state_store, provider_id
        )

        s3 = substrate_session.client("s3")
        bucket = f"parsl-bastion-script-{mode.provider_id[:8]}"
        try:
            for obj in s3.list_objects_v2(Bucket=bucket).get("Contents", []):
                s3.delete_object(Bucket=bucket, Key=obj["Key"])
            s3.delete_bucket(Bucket=bucket)
        except s3.exceptions.NoSuchBucket:
            pass

        mode._prepare_bastion_user_data()
        assert mode._owns_script_bucket is True
        staged_key = mode._script_key

        # Pull the bucket out from under it.
        s3.delete_object(Bucket=bucket, Key=staged_key)
        s3.delete_bucket(Bucket=bucket)

        mode.preserve_bastion = False
        mode.cleanup_infrastructure()

        # Cleared regardless, so a retry does not chase a bucket that is gone.
        assert mode._script_key is None
        assert mode._script_bucket is None
        assert mode._owns_script_bucket is False

    def test_submit_job_and_status(
        self, substrate_session, substrate_network, temp_state_store, provider_id
    ):
        """The bastion records the job in SSM Parameter Store when submitted."""
        mode = self._mode(
            substrate_session, substrate_network, temp_state_store, provider_id
        )

        try:
            mode.initialize()

            job_id = f"{uuid.uuid4().hex[:8]}-test-job"
            resource_id = mode.submit_job(job_id, "echo hello", 1)

            assert resource_id in mode.resources
            assert mode.resources[resource_id]["job_id"] == job_id

            # Jobs are handed to the bastion through Parameter Store, so the
            # parameter existing is what proves the dispatch happened.
            ssm = substrate_session.client("ssm")
            prefix = f"/parsl/workflows/{mode.workflow_id}"
            described = ssm.describe_parameters(
                ParameterFilters=[
                    {"Key": "Name", "Option": "BeginsWith", "Values": [prefix]}
                ]
            )
            assert described["Parameters"], f"no SSM parameters under {prefix}"

            assert resource_id in mode.get_job_status([resource_id])
            assert resource_id in mode.cancel_jobs([resource_id])

        finally:
            mode.preserve_bastion = False
            mode.cleanup_infrastructure()


@pytest.mark.integration
@pytest.mark.substrate
class TestServerlessModeSubstrate:
    """Integration tests for ServerlessMode using substrate."""

    def test_lambda_mode_needs_no_network(
        self, substrate_session, temp_state_store, provider_id
    ):
        """Lambda-only serverless initializes without any caller network.

        Functions run in the Lambda-managed VPC, which is why the provider's
        network guard exempts this combination.
        """
        mode = ServerlessMode(
            provider_id=provider_id,
            session=substrate_session,
            state_store=temp_state_store,
            region="us-east-1",
            worker_type="lambda",
            lambda_memory=128,
            lambda_timeout=30,
        )

        try:
            mode.initialize()

            assert mode.initialized is True
            assert mode.vpc_id is None
            assert mode.subnet_id is None
            assert mode.security_group_id is None

        finally:
            mode.cleanup_infrastructure()

        assert mode.initialized is False

    def test_ecs_mode_uses_the_supplied_network(
        self, substrate_session, substrate_network, temp_state_store, provider_id
    ):
        """ECS/Fargate tasks run in the caller's subnet, so the IDs are kept."""
        mode = ServerlessMode(
            provider_id=provider_id,
            session=substrate_session,
            state_store=temp_state_store,
            region="us-east-1",
            worker_type="ecs",
            ecs_task_cpu=256,
            ecs_task_memory=512,
            ecs_container_image="python:3.12-slim",
            vpc_id=substrate_network["vpc_id"],
            subnet_id=substrate_network["subnet_id"],
            security_group_id=substrate_network["security_group_id"],
        )

        try:
            mode.initialize()

            assert mode.initialized is True
            assert mode.vpc_id == substrate_network["vpc_id"]
            assert mode.subnet_id == substrate_network["subnet_id"]
            assert mode.security_group_id == substrate_network["security_group_id"]

        finally:
            mode.cleanup_infrastructure()

    def test_submit_lambda_job(
        self,
        substrate_session,
        temp_state_store,
        provider_id,
        requires_cloudformation,
    ):
        """Submitting a Lambda job provisions its function through a stack.

        Skipped rather than failed while the emulator serves no CloudFormation:
        ``_submit_lambda_job`` deploys the worker as a stack, so this exercises the
        emulator's gap, not the provider. Covered for real in ``tests/aws/``.
        """
        mode = ServerlessMode(
            provider_id=provider_id,
            session=substrate_session,
            state_store=temp_state_store,
            region="us-east-1",
            worker_type="lambda",
            lambda_memory=128,
            lambda_timeout=30,
        )

        try:
            mode.initialize()

            job_id = f"{uuid.uuid4().hex[:8]}-test-job"
            resource_id = mode.submit_job(job_id, "echo hello", 1)

            assert resource_id in mode.resources
            assert mode.resources[resource_id]["job_id"] == job_id
            assert mode.resources[resource_id]["worker_type"] == "lambda"

            assert resource_id in mode.get_job_status([resource_id])
            assert resource_id in mode.cancel_jobs([resource_id])

            mode.cleanup_resources([resource_id])
            assert resource_id not in mode.resources

        finally:
            mode.cleanup_infrastructure()

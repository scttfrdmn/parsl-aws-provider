"""Integration tests for state persistence mechanisms.

These tests verify that each state persistence implementation works correctly
with real storage backends (file system, substrate for AWS services).

Each AWS-backed store is built from conftest's ``substrate_session``, whose
``client`` is wrapped to bind ``endpoint_url``. The three classes here each used
to declare a class-scoped ``substrate_session`` of their own from
``get_substrate_session()``, which binds *no* endpoint -- so ``resolve_session``
handed the store a session whose clients reach real AWS, and every S3 test
skipped on ``InvalidAccessKeyId`` from the fixture's ``create_bucket``. That skip
read as "substrate can't do this" while the cause was the shadowing fixture.

Those same three classes also patched ``boto3.Session`` to inject the session,
which has never been necessary: ``state/base.resolve_session`` prefers a
``session`` attribute on the provider object when one is present (#77), so the
provider stub simply carries it. The patch was also the reason two tests *errored*
rather than failed -- ``patch`` was used without being imported.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import os
import uuid
import pytest
import tempfile
from types import SimpleNamespace
from botocore.exceptions import ClientError

from parsl_ephemeral_aws.exceptions import StateError
from parsl_ephemeral_aws.state.base import STATE_KEY_PROVIDER
from parsl_ephemeral_aws.state.file import FileStateStore
from parsl_ephemeral_aws.state.parameter_store import ParameterStoreState
from parsl_ephemeral_aws.state.s3 import S3State
from tests.substrate_support import is_substrate_available


# Skip all tests if the substrate emulator is not available
pytestmark = pytest.mark.skipif(
    not is_substrate_available(),
    reason="substrate not available - start with 'make substrate-up'",
)


def _provider_stub(session, **overrides):
    """The minimum a state store reads off a provider.

    ``resolve_session`` takes the ``session`` attribute when it is a real
    ``boto3.Session`` and only falls back to assembling one from the credential
    attributes otherwise -- so passing the endpoint-bound session here is what
    keeps the store on the emulator, with no patching.

    A ``SimpleNamespace`` rather than a ``MagicMock``: ``resolve_session`` reads
    five optional attributes with ``getattr(..., None)`` and treats any truthy
    value as credentials to build a *new* session from, which a MagicMock would
    supply -- silently replacing the bound session with an unbound one.
    """
    attrs = {
        "session": session,
        "provider_id": f"test-provider-{uuid.uuid4().hex[:8]}",
        "workflow_id": f"test-workflow-{uuid.uuid4().hex[:8]}",
        "region": session.region_name,
    }
    attrs.update(overrides)
    return SimpleNamespace(**attrs)


class TestFileStateStoreIntegration:
    """Integration tests for FileStateStore."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for state files."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            yield tmp_dir

    @pytest.fixture
    def file_state_store(self, temp_dir):
        """Create a FileStateStore instance with a real file."""
        provider_id = f"test-provider-{uuid.uuid4().hex[:8]}"
        file_path = os.path.join(temp_dir, f"{provider_id}-state.json")
        return FileStateStore(file_path=file_path, provider_id=provider_id)

    @pytest.fixture
    def complex_state(self):
        """Create a complex state dictionary with nested structures."""
        # Bound to names so the two resource keys are visibly distinct. Written
        # inline they were textually identical expressions -- distinct at runtime
        # because uuid4() differs, but indistinguishable to a reader or a linter.
        first_res = f"res-{uuid.uuid4().hex[:8]}"
        second_res = f"res-{uuid.uuid4().hex[:8]}"
        return {
            "provider_info": {
                "id": f"provider-{uuid.uuid4().hex[:8]}",
                "region": "us-east-1",
                "created_at": "2023-01-01T00:00:00Z",
            },
            "resources": {
                first_res: {
                    "instance_id": f"i-{uuid.uuid4().hex[:12]}",
                    "status": "running",
                    "ip_address": "10.0.0.1",
                    "tags": ["compute", "worker"],
                },
                second_res: {
                    "instance_id": f"i-{uuid.uuid4().hex[:12]}",
                    "status": "pending",
                    "ip_address": "10.0.0.2",
                    "tags": ["storage", "worker"],
                },
            },
            "jobs": {
                f"job-{uuid.uuid4().hex[:8]}": {
                    "status": "running",
                    "submitted_at": "2023-01-02T00:00:00Z",
                    "resource_id": "res-1",
                    "command": "echo 'Hello World'",
                    "environment": {"PATH": "/usr/bin:/bin", "HOME": "/home/user"},
                }
            },
            "statistics": {
                "job_count": 10,
                "success_count": 8,
                "failure_count": 1,
                "pending_count": 1,
                "average_runtime": 42.5,
            },
        }

    def test_full_lifecycle(self, file_state_store, complex_state):
        """Test the full lifecycle of state persistence."""
        # 1. Save state
        file_state_store.save_state(STATE_KEY_PROVIDER, complex_state)

        # Verify file exists
        assert os.path.exists(file_state_store.file_path)

        # 2. Load state
        loaded_state = file_state_store.load_state(STATE_KEY_PROVIDER)

        # Verify loaded state matches original
        assert loaded_state is not None
        assert (
            loaded_state["provider_info"]["id"] == complex_state["provider_info"]["id"]
        )
        assert len(loaded_state["resources"]) == len(complex_state["resources"])
        assert (
            loaded_state["statistics"]["job_count"]
            == complex_state["statistics"]["job_count"]
        )

        # 3. Update state
        loaded_state["statistics"]["job_count"] += 1
        loaded_state["statistics"]["success_count"] += 1
        file_state_store.save_state(STATE_KEY_PROVIDER, loaded_state)

        # 4. Reload state and verify updates
        reloaded_state = file_state_store.load_state(STATE_KEY_PROVIDER)
        assert reloaded_state["statistics"]["job_count"] == 11
        assert reloaded_state["statistics"]["success_count"] == 9

        # 5. Delete state
        file_state_store.delete_state(STATE_KEY_PROVIDER)

        # Verify file no longer exists — this was the only key in it
        assert not os.path.exists(file_state_store.file_path)

        # 6. Load after delete should return None
        final_state = file_state_store.load_state(STATE_KEY_PROVIDER)
        assert final_state is None

    def test_concurrent_access(self, temp_dir, complex_state):
        """Test concurrent access to the same state file."""
        provider_id = f"shared-provider-{uuid.uuid4().hex[:8]}"
        file_path = os.path.join(temp_dir, f"{provider_id}-state.json")

        # Create two separate state stores for the same file
        store1 = FileStateStore(file_path=file_path, provider_id=provider_id)
        store2 = FileStateStore(file_path=file_path, provider_id=provider_id)

        # Store 1 saves initial state
        store1.save_state(STATE_KEY_PROVIDER, complex_state)

        # Store 2 loads state, modifies it, and saves back
        state2 = store2.load_state(STATE_KEY_PROVIDER)
        state2["statistics"]["job_count"] = 20
        state2["provider_info"]["updated_by"] = "store2"
        store2.save_state(STATE_KEY_PROVIDER, state2)

        # Store 1 reloads state - should see Store 2's changes
        updated_state = store1.load_state(STATE_KEY_PROVIDER)
        assert updated_state["statistics"]["job_count"] == 20
        assert updated_state["provider_info"]["updated_by"] == "store2"


@pytest.mark.integration
class TestParameterStoreStateIntegration:
    """Integration tests for ParameterStoreState using substrate."""

    @pytest.fixture
    def mock_provider(self, substrate_session):
        """A provider stub carrying the endpoint-bound session."""
        return _provider_stub(substrate_session)

    @pytest.fixture
    def parameter_store_state(self, mock_provider, substrate_session):
        """Create a ParameterStoreState instance with substrate."""
        state_prefix = f"/parsl/test/{uuid.uuid4().hex[:8]}"

        state_store = ParameterStoreState(provider=mock_provider, prefix=state_prefix)
        yield state_store

        # Cleanup
        try:
            # Find all parameters under our prefix
            paginator = substrate_session.client("ssm").get_paginator(
                "get_parameters_by_path"
            )
            page_iterator = paginator.paginate(
                Path=state_prefix, Recursive=True, WithDecryption=True
            )

            parameters_to_delete = []
            for page in page_iterator:
                for param in page.get("Parameters", []):
                    parameters_to_delete.append(param["Name"])

            # Delete in batches
            ssm_client = substrate_session.client("ssm")
            for i in range(0, len(parameters_to_delete), 10):
                batch = parameters_to_delete[i : i + 10]
                if batch:
                    try:
                        ssm_client.delete_parameters(Names=batch)
                    except Exception as e:
                        print(f"Error cleaning up parameters: {e}")
        except Exception as e:
            print(f"Error during cleanup: {e}")

    @pytest.fixture
    def complex_state(self):
        """Create a complex state dictionary with nested structures."""
        return {
            "provider_info": {
                "id": f"provider-{uuid.uuid4().hex[:8]}",
                "region": "us-east-1",
                "created_at": "2023-01-01T00:00:00Z",
            },
            "resources": {
                f"res-{uuid.uuid4().hex[:8]}": {
                    "instance_id": f"i-{uuid.uuid4().hex[:12]}",
                    "status": "running",
                    "tags": ["compute", "worker"],
                }
            },
            "statistics": {
                "job_count": 5,
                "success_count": 3,
                "failure_count": 1,
                "pending_count": 1,
            },
        }

    @pytest.mark.substrate
    def test_parameter_store_lifecycle(self, parameter_store_state, complex_state):
        """Test the full lifecycle of a Parameter Store state."""
        state_key = f"test-state-{uuid.uuid4().hex[:8]}"

        # 1. Save state
        parameter_store_state.save_state(state_key, complex_state)

        # 2. Load state
        loaded_state = parameter_store_state.load_state(state_key)

        # Verify loaded state matches original
        assert loaded_state is not None
        assert (
            loaded_state["provider_info"]["id"] == complex_state["provider_info"]["id"]
        )
        assert len(loaded_state["resources"]) == len(complex_state["resources"])
        assert (
            loaded_state["statistics"]["job_count"]
            == complex_state["statistics"]["job_count"]
        )

        # 3. Update state
        loaded_state["statistics"]["job_count"] += 1
        loaded_state["statistics"]["success_count"] += 1
        parameter_store_state.save_state(state_key, loaded_state)

        # 4. Reload state and verify updates
        reloaded_state = parameter_store_state.load_state(state_key)
        assert reloaded_state["statistics"]["job_count"] == 6
        assert reloaded_state["statistics"]["success_count"] == 4

        # 5. Delete state
        parameter_store_state.delete_state(state_key)

        # 6. Load after delete should return None
        final_state = parameter_store_state.load_state(state_key)
        assert final_state is None

    @pytest.mark.substrate
    def test_list_parameters(self, parameter_store_state):
        """Test listing parameters with a prefix."""
        # Create multiple parameters with a common prefix
        prefix = f"list-test-{uuid.uuid4().hex[:8]}"

        # Save multiple states with the same prefix
        parameter_store_state.save_state(
            f"{prefix}/state1", {"id": "state1", "value": 1}
        )
        parameter_store_state.save_state(
            f"{prefix}/state2", {"id": "state2", "value": 2}
        )
        parameter_store_state.save_state(
            f"{prefix}/state3", {"id": "state3", "value": 3}
        )

        # Also save a state with a different prefix
        parameter_store_state.save_state(
            f"other-prefix-{uuid.uuid4().hex[:8]}", {"id": "other"}
        )

        # List states with our prefix
        states = parameter_store_state.list_states(prefix)

        # Verify we got the right states
        assert len(states) == 3
        assert any(state["id"] == "state1" for state in states.values())
        assert any(state["id"] == "state2" for state in states.values())
        assert any(state["id"] == "state3" for state in states.values())

        # Cleanup created parameters
        for key in [f"{prefix}/state1", f"{prefix}/state2", f"{prefix}/state3"]:
            parameter_store_state.delete_state(key)


@pytest.mark.integration
class TestS3StateIntegration:
    """Integration tests for S3State using substrate."""

    @pytest.fixture
    def s3_bucket_name(self, substrate_session):
        """Create a unique S3 bucket name and ensure it exists.

        ``CreateBucketConfiguration`` is required outside ``us-east-1`` and
        rejected inside it, and this session follows ``AWS_TEST_REGION``
        (default ``us-west-2``) -- so the constraint is passed conditionally
        rather than omitted, which is what made this fixture fail before.
        """
        bucket_name = f"test-bucket-{uuid.uuid4().hex[:16]}"
        s3_client = substrate_session.client("s3")
        region = substrate_session.region_name

        kwargs = {"Bucket": bucket_name}
        if region and region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
        s3_client.create_bucket(**kwargs)

        yield bucket_name

        # Cleanup
        try:
            # Delete all objects first
            objects = s3_client.list_objects_v2(Bucket=bucket_name)
            if "Contents" in objects:
                delete_keys = {
                    "Objects": [{"Key": obj["Key"]} for obj in objects["Contents"]]
                }
                s3_client.delete_objects(Bucket=bucket_name, Delete=delete_keys)

            # Then delete bucket
            s3_client.delete_bucket(Bucket=bucket_name)
        except Exception as e:
            print(f"Error cleaning up test bucket: {e}")

    @pytest.fixture
    def mock_provider(self, substrate_session):
        """A provider stub carrying the endpoint-bound session."""
        return _provider_stub(substrate_session)

    @pytest.fixture
    def s3_state(self, mock_provider, s3_bucket_name):
        """Create an S3State instance with substrate."""
        return S3State(
            provider=mock_provider,
            bucket_name=s3_bucket_name,
            key_prefix=f"parsl/test/{uuid.uuid4().hex[:8]}",
        )

    @pytest.fixture
    def complex_state(self):
        """Create a complex state dictionary with nested structures."""
        return {
            "provider_info": {
                "id": f"provider-{uuid.uuid4().hex[:8]}",
                "region": "us-east-1",
                "created_at": "2023-01-01T00:00:00Z",
            },
            "resources": {
                f"res-{uuid.uuid4().hex[:8]}": {
                    "instance_id": f"i-{uuid.uuid4().hex[:12]}",
                    "status": "running",
                    "tags": ["compute", "worker"],
                }
            },
            "statistics": {
                "job_count": 5,
                "success_count": 3,
                "failure_count": 1,
                "pending_count": 1,
            },
            "spot_fleets": {
                f"sfr-{uuid.uuid4().hex[:8]}": {
                    "instances": [f"i-{uuid.uuid4().hex[:12]}" for _ in range(3)],
                    "status": "active",
                }
            },
        }

    @pytest.mark.substrate
    def test_s3_state_lifecycle(self, s3_state, complex_state):
        """Test the full lifecycle of S3 state."""
        state_key = f"test-state-{uuid.uuid4().hex[:8]}"

        # 1. Save state
        s3_state.save_state(state_key, complex_state)

        # 2. Load state
        loaded_state = s3_state.load_state(state_key)

        # Verify loaded state matches original
        assert loaded_state is not None
        assert (
            loaded_state["provider_info"]["id"] == complex_state["provider_info"]["id"]
        )
        assert len(loaded_state["resources"]) == len(complex_state["resources"])
        assert (
            loaded_state["statistics"]["job_count"]
            == complex_state["statistics"]["job_count"]
        )
        assert len(loaded_state["spot_fleets"]) == len(complex_state["spot_fleets"])

        # 3. Update state
        loaded_state["statistics"]["job_count"] += 1
        loaded_state["statistics"]["success_count"] += 1
        s3_state.save_state(state_key, loaded_state)

        # 4. Reload state and verify updates
        reloaded_state = s3_state.load_state(state_key)
        assert reloaded_state["statistics"]["job_count"] == 6
        assert reloaded_state["statistics"]["success_count"] == 4

        # 5. Delete state
        s3_state.delete_state(state_key)

        # 6. Load after delete should return None
        final_state = s3_state.load_state(state_key)
        assert final_state is None

    @pytest.mark.substrate
    def test_list_s3_objects(self, s3_state):
        """Test listing S3 objects with a prefix."""
        # Create multiple states with a common prefix
        prefix = f"list-test-{uuid.uuid4().hex[:8]}"

        # Save multiple states with the same prefix
        s3_state.save_state(f"{prefix}/state1", {"id": "state1", "value": 1})
        s3_state.save_state(f"{prefix}/state2", {"id": "state2", "value": 2})
        s3_state.save_state(f"{prefix}/state3", {"id": "state3", "value": 3})

        # Also save a state with a different prefix
        s3_state.save_state(f"other-prefix-{uuid.uuid4().hex[:8]}", {"id": "other"})

        # List states with our prefix
        states = s3_state.list_states(prefix)

        # Verify we got the right states
        assert len(states) == 3
        assert any(state["id"] == "state1" for state in states.values())
        assert any(state["id"] == "state2" for state in states.values())
        assert any(state["id"] == "state3" for state in states.values())

        # Cleanup created objects
        for key in [f"{prefix}/state1", f"{prefix}/state2", f"{prefix}/state3"]:
            s3_state.delete_state(key)

    @pytest.mark.substrate
    def test_cleanup_workflow_states(self, s3_state, mock_provider, substrate_session):
        """Test cleaning up all workflow states."""
        # Create several objects for this workflow
        workflow_prefix = mock_provider.workflow_id

        # Save multiple states for this workflow
        for i in range(5):
            s3_state.save_state(
                f"{workflow_prefix}/state{i}",
                {"workflow": workflow_prefix, "id": f"state{i}"},
            )

        # Save a state for a different workflow
        other_key = f"other-workflow-{uuid.uuid4().hex[:8]}/state"
        s3_state.save_state(other_key, {"workflow": "other", "id": "other"})

        # Cleanup workflow states
        s3_state.cleanup_workflow_states()

        # Verify our workflow states are gone
        for i in range(5):
            assert s3_state.load_state(f"{workflow_prefix}/state{i}") is None

        # Verify other workflow state still exists
        assert s3_state.load_state(other_key) is not None

        # Cleanup the other state
        s3_state.delete_state(other_key)

    @pytest.mark.substrate
    def test_create_bucket_if_not_exists(self, mock_provider, substrate_session):
        """A missing bucket is created, blocked from public access, and usable.

        Blocking public access is the part worth asserting rather than the
        creation: the provider writes provider state -- instance IDs, network
        IDs, and whatever the workflow put in its tags -- into this bucket, and it
        replaced a deprecated ``ACL="private"`` with the modern equivalent. A
        bucket created without it would be world-readable if any later policy
        allowed it.

        ``xfail`` on substrate#446 rather than skip: ``?publicAccessBlock`` is
        unrouted there, so ``PUT`` falls through to the ``CreateBucket`` handler
        and answers ``BucketAlreadyExists`` for the bucket that was just made.
        The provider wraps that as ``StateError: Failed to create S3 bucket``, so
        the whole constructor fails. xfail keeps the test running -- it will
        report ``XPASS`` the moment substrate routes the subresource, where a skip
        would sit silently forever.

        Creation itself is covered under moto in
        ``tests/unit/test_aws_mocking.py::test_bucket_is_created_when_requested``.
        Nothing covers it against real S3: ``tests/aws`` pre-creates the bucket in
        its ``s3_state_bucket`` fixture, so the provider takes the
        already-exists branch there, and no E2E test passes
        ``create_bucket_if_not_exists``. The public-access assertions below are
        therefore unverified against AWS until substrate#446 lands.
        """
        bucket_name = f"auto-create-bucket-{uuid.uuid4().hex[:16]}"
        s3_client = substrate_session.client("s3")

        with pytest.raises(ClientError) as excinfo:
            s3_client.head_bucket(Bucket=bucket_name)
        assert excinfo.value.response["Error"]["Code"] == "404"

        try:
            s3_state = S3State(
                provider=mock_provider,
                bucket_name=bucket_name,
                create_bucket_if_not_exists=True,
            )
        except StateError as exc:
            if "BucketAlreadyExists" in str(exc):
                pytest.xfail(
                    "substrate#446: PUT ?publicAccessBlock is routed to "
                    "CreateBucket, so the provider cannot lock down a bucket it "
                    "just created."
                )
            raise

        try:
            # Created, and no longer publicly reachable.
            s3_client.head_bucket(Bucket=bucket_name)
            blocked = s3_client.get_public_access_block(Bucket=bucket_name)[
                "PublicAccessBlockConfiguration"
            ]
            assert blocked["BlockPublicAcls"] is True
            assert blocked["IgnorePublicAcls"] is True
            assert blocked["BlockPublicPolicy"] is True
            assert blocked["RestrictPublicBuckets"] is True

            # Tagged so an operator can tell which workflow owns it.
            tags = {
                t["Key"]: t["Value"]
                for t in s3_client.get_bucket_tagging(Bucket=bucket_name)["TagSet"]
            }
            assert tags["ParslManagedBucket"] == "true"
            assert tags["ParslWorkflowId"] == mock_provider.workflow_id

            s3_state.save_state("test_key", {"created": "auto", "test": True})
            assert s3_state.load_state("test_key")["created"] == "auto"
        finally:
            s3_state.delete_state("test_key")
            try:
                s3_client.delete_bucket(Bucket=bucket_name)
            except Exception as e:
                print(f"Error cleaning up bucket: {e}")

    @pytest.mark.substrate
    def test_an_existing_bucket_is_adopted_rather_than_recreated(
        self, mock_provider, s3_bucket_name
    ):
        """``create_bucket_if_not_exists`` must be idempotent.

        A provider resumed from a state file constructs its store again, so this
        runs against a bucket that already exists on every restart. ``CreateBucket``
        is not reached at all in that case -- which is also why this test passes
        where the one above cannot.
        """
        s3_state = S3State(
            provider=mock_provider,
            bucket_name=s3_bucket_name,
            create_bucket_if_not_exists=True,
        )

        s3_state.save_state("adopted", {"ok": True})
        assert s3_state.load_state("adopted") == {"ok": True}
        s3_state.delete_state("adopted")

    @pytest.mark.substrate
    def test_an_empty_bucket_is_reclaimed_and_a_used_one_is_not(
        self, mock_provider, s3_bucket_name
    ):
        """``delete_bucket_if_empty`` must not destroy state it still holds.

        Called on shutdown, where the bucket may be shared with another provider
        or hold state a resumed run still needs. Emptiness is the only signal
        available, so the check has to be right in both directions.
        """
        s3_state = S3State(
            provider=mock_provider,
            bucket_name=s3_bucket_name,
            key_prefix=f"parsl/test/{uuid.uuid4().hex[:8]}",
        )
        s3_state.save_state("still-needed", {"ok": True})

        assert s3_state.delete_bucket_if_empty() is False
        assert s3_state.load_state("still-needed") == {"ok": True}

        s3_state.delete_state("still-needed")

        assert s3_state.delete_bucket_if_empty() is True
        # The bucket is gone, so the fixture's teardown has nothing to do -- and
        # the store now fails loudly rather than silently returning None.
        with pytest.raises(StateError, match="NoSuchBucket"):
            s3_state.load_state("still-needed")

"""Unit tests for SSM instance profile resolution.

Warm-pool and one-shot dispatch both go through SSM ``SendCommand``, which needs
the instance to carry a profile holding ``AmazonSSMManagedInstanceCore``. The
provider accepted ``auto_create_instance_profile`` but never forwarded it, so no
profile was ever attached and every dispatch silently fell back to UserData.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from parsl_aws_provider.exceptions import ResourceNotFoundError
from parsl_aws_provider.modes.standard import StandardMode
from parsl_aws_provider.utils.aws import (
    _wait_for_instance_profile,
    delete_ssm_instance_profile,
    get_or_create_ssm_instance_profile,
    ssm_instance_profile_names,
)


pytestmark = pytest.mark.unit

NETWORK_IDS = {
    "vpc_id": "vpc-12345",
    "subnet_id": "subnet-12345",
    "security_group_id": "sg-12345",
}

SSM_POLICY_ARN = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"


@pytest.fixture
def iam_session(monkeypatch):
    """A moto-backed session that can attach AWS managed policies.

    moto only serves the AWS managed policy catalogue when
    ``MOTO_IAM_LOAD_MANAGED_POLICIES`` is set, and it reads the variable when the
    IAM backend is built — so it has to be in place before ``mock_aws`` starts.
    The synthetic credentials keep moto from picking up an ambient real profile.
    """
    monkeypatch.setenv("MOTO_IAM_LOAD_MANAGED_POLICIES", "true")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("AWS_PROFILE", raising=False)

    with mock_aws():
        yield boto3.Session(region_name="us-east-1")


class TestGetOrCreateSSMInstanceProfile:
    """Tests for ``utils.aws.get_or_create_ssm_instance_profile``."""

    def test_explicit_arn_is_returned_verbatim(self):
        """A supplied ARN wins; nothing is created."""
        session = MagicMock()

        result = get_or_create_ssm_instance_profile(
            session=session,
            name_suffix="wf",
            iam_instance_profile_arn="arn:aws:iam::1:instance-profile/mine",
            auto_create=True,
        )

        assert result == "arn:aws:iam::1:instance-profile/mine"
        session.client.assert_not_called()

    def test_returns_none_without_auto_create(self):
        """With neither an ARN nor auto-creation, instances get no profile."""
        session = MagicMock()

        result = get_or_create_ssm_instance_profile(
            session=session, name_suffix="wf", auto_create=False
        )

        assert result is None
        session.client.assert_not_called()

    def test_creates_role_and_profile_with_the_ssm_policy(self, iam_session):
        """The created role must carry AmazonSSMManagedInstanceCore."""
        session = iam_session

        arn = get_or_create_ssm_instance_profile(
            session=session, name_suffix="wf-1", auto_create=True
        )

        assert arn is not None
        iam = session.client("iam")
        profile = iam.get_instance_profile(
            InstanceProfileName="parsl-ephemeral-ssm-profile-wf-1"
        )["InstanceProfile"]

        assert profile["Arn"] == arn
        role_names = [role["RoleName"] for role in profile["Roles"]]
        assert role_names == ["parsl-ephemeral-ssm-role-wf-1"]

        attached = iam.list_attached_role_policies(
            RoleName="parsl-ephemeral-ssm-role-wf-1"
        )["AttachedPolicies"]
        assert SSM_POLICY_ARN in [p["PolicyArn"] for p in attached]

    def test_is_idempotent(self, iam_session):
        """A second call reuses the profile rather than failing on the conflict."""
        session = iam_session

        first = get_or_create_ssm_instance_profile(
            session=session, name_suffix="wf-2", auto_create=True
        )
        second = get_or_create_ssm_instance_profile(
            session=session, name_suffix="wf-2", auto_create=True
        )

        assert first == second

        # And the role is still attached exactly once.
        iam = session.client("iam")
        profile = iam.get_instance_profile(
            InstanceProfileName="parsl-ephemeral-ssm-profile-wf-2"
        )["InstanceProfile"]
        assert len(profile["Roles"]) == 1

    def test_attaches_the_role_to_a_pre_existing_empty_profile(self, iam_session):
        """A profile left behind without its role must still be usable.

        The original implementation returned early on ``get_instance_profile``
        success, so a profile whose role attachment had failed stayed empty
        forever and SSM never came online.
        """
        session = iam_session
        iam = session.client("iam")
        iam.create_instance_profile(
            InstanceProfileName="parsl-ephemeral-ssm-profile-wf-3"
        )

        get_or_create_ssm_instance_profile(
            session=session, name_suffix="wf-3", auto_create=True
        )

        profile = iam.get_instance_profile(
            InstanceProfileName="parsl-ephemeral-ssm-profile-wf-3"
        )["InstanceProfile"]
        assert [r["RoleName"] for r in profile["Roles"]] == [
            "parsl-ephemeral-ssm-role-wf-3"
        ]


def _client_error(code):
    """Return a ClientError carrying *code*, as boto3 would raise it."""
    return ClientError({"Error": {"Code": code, "Message": code}}, "RunInstances")


class TestWaitForInstanceProfile:
    """IAM is eventually consistent with respect to EC2.

    ``create_instance_profile`` returns an ARN that ``RunInstances`` rejects with
    ``InvalidParameterValue: Invalid IAM Instance Profile ARN`` for roughly the
    first ten seconds (measured against real AWS: created at t+4.1s, accepted at
    t+14.5s). Making ``auto_create_instance_profile`` actually take effect
    exposed this -- every warm-pool launch hit it. ``get_instance_profile``
    succeeds immediately and so proves nothing; only a dry-run ``RunInstances``
    exercises the path that matters.
    """

    @pytest.fixture
    def session(self):
        session = MagicMock(spec=boto3.Session)
        session.region_name = "us-east-1"
        return session

    def test_returns_as_soon_as_ec2_accepts_the_arn(self, session):
        """DryRunOperation means EC2 validated everything, profile included."""
        ec2 = session.client.return_value
        ec2.run_instances.side_effect = _client_error("DryRunOperation")

        with patch("parsl_aws_provider.utils.aws.time.sleep") as sleep:
            _wait_for_instance_profile(session, "arn:aws:iam::1:instance-profile/p")

        assert ec2.run_instances.call_count == 1
        sleep.assert_not_called()

    def test_retries_while_the_profile_is_still_propagating(self, session):
        """InvalidParameterValue is the propagation window; keep waiting."""
        ec2 = session.client.return_value
        ec2.run_instances.side_effect = [
            _client_error("InvalidParameterValue"),
            _client_error("InvalidParameterValue"),
            _client_error("DryRunOperation"),
        ]

        with patch("parsl_aws_provider.utils.aws.time.sleep") as sleep:
            _wait_for_instance_profile(session, "arn:aws:iam::1:instance-profile/p")

        assert ec2.run_instances.call_count == 3
        assert sleep.call_count == 2

    def test_dry_run_passes_the_profile_arn_to_ec2(self, session):
        """The dry run must actually name the profile, or it checks nothing."""
        ec2 = session.client.return_value
        ec2.run_instances.side_effect = _client_error("DryRunOperation")

        _wait_for_instance_profile(session, "arn:aws:iam::1:instance-profile/p")

        kwargs = ec2.run_instances.call_args.kwargs
        assert kwargs["IamInstanceProfile"] == {
            "Arn": "arn:aws:iam::1:instance-profile/p"
        }
        assert kwargs["DryRun"] is True

    def test_unrelated_errors_do_not_spin(self, session):
        """An unusable AMI here is not what this check is for.

        Retrying would burn the whole timeout on an error that will never clear;
        the real launch is the right place to report it.
        """
        ec2 = session.client.return_value
        ec2.run_instances.side_effect = _client_error("InvalidAMIID.NotFound")

        with patch("parsl_aws_provider.utils.aws.time.sleep") as sleep:
            _wait_for_instance_profile(session, "arn:aws:iam::1:instance-profile/p")

        assert ec2.run_instances.call_count == 1
        sleep.assert_not_called()

    def test_timeout_warns_rather_than_raising(self, session):
        """A slow propagation must not become a hard failure.

        The launch that follows reports the real error if there is one; raising
        here would fail a profile that is about to start working. Sleep drives a
        fake clock so the loop really does iterate and then give up -- a zero
        timeout would skip the body and prove nothing about the retry path.
        """
        ec2 = session.client.return_value
        ec2.run_instances.side_effect = _client_error("InvalidParameterValue")

        clock = [1000.0]

        def advance(seconds):
            clock[0] += seconds

        with (
            patch(
                "parsl_aws_provider.utils.aws.time.time", side_effect=lambda: clock[0]
            ),
            patch("parsl_aws_provider.utils.aws.time.sleep", side_effect=advance),
        ):
            _wait_for_instance_profile(
                session, "arn:aws:iam::1:instance-profile/p", timeout=10
            )

        # It retried for the full window, then returned so the launch reports
        # the real error itself.
        assert ec2.run_instances.call_count == 5
        assert clock[0] == 1010.0


class TestStandardModeProfileResolution:
    """``StandardMode`` must resolve an ARN before instances are launched."""

    @pytest.fixture
    def mock_session(self):
        session = MagicMock(spec=boto3.Session)
        session.region_name = "us-east-1"
        return session

    @pytest.fixture
    def mock_state_store(self):
        store = MagicMock()
        store.load_state.return_value = None
        return store

    def _mode(self, mock_session, mock_state_store, **overrides):
        params = {
            "provider_id": "test-provider",
            "session": mock_session,
            "state_store": mock_state_store,
            "image_id": "ami-12345",
            "region": "us-east-1",
            **NETWORK_IDS,
        }
        params.update(overrides)
        return StandardMode(**params)

    def test_auto_create_flag_is_accepted(self, mock_session, mock_state_store):
        """Regression: the mode had no such parameter, so it fell into kwargs."""
        mode = self._mode(
            mock_session,
            mock_state_store,
            warm_pool_size=2,
            auto_create_instance_profile=True,
        )
        assert mode.auto_create_instance_profile is True

    def test_initialize_resolves_the_arn(self, mock_session, mock_state_store):
        """After initialize(), `_create_instance` must have an ARN to attach."""
        mode = self._mode(
            mock_session,
            mock_state_store,
            warm_pool_size=2,
            auto_create_instance_profile=True,
        )
        assert mode.iam_instance_profile_arn is None

        with patch(
            "parsl_aws_provider.modes.standard.get_or_create_ssm_instance_profile",
            return_value="arn:aws:iam::1:instance-profile/created",
        ) as resolve:
            mode.initialize()

        assert (
            mode.iam_instance_profile_arn == "arn:aws:iam::1:instance-profile/created"
        )
        assert resolve.call_args.kwargs["name_suffix"] == "test-provider"
        assert resolve.call_args.kwargs["auto_create"] is True

    def test_explicit_arn_is_not_overwritten(self, mock_session, mock_state_store):
        """An explicitly supplied profile must be left alone."""
        mode = self._mode(
            mock_session,
            mock_state_store,
            warm_pool_size=2,
            iam_instance_profile_arn="arn:aws:iam::1:instance-profile/mine",
            auto_create_instance_profile=True,
        )

        with patch(
            "parsl_aws_provider.modes.standard.get_or_create_ssm_instance_profile"
        ) as resolve:
            mode.initialize()

        resolve.assert_not_called()
        assert mode.iam_instance_profile_arn == "arn:aws:iam::1:instance-profile/mine"

    def test_no_profile_is_created_when_not_requested(
        self, mock_session, mock_state_store
    ):
        """Without the flag, no IAM resources are touched at all."""
        mode = self._mode(mock_session, mock_state_store)

        with patch(
            "parsl_aws_provider.modes.standard.get_or_create_ssm_instance_profile"
        ) as resolve:
            mode.initialize()

        resolve.assert_not_called()
        assert mode.iam_instance_profile_arn is None

    def test_profile_creation_failure_is_not_fatal(
        self, mock_session, mock_state_store
    ):
        """Dispatch degrades to UserData rather than the provider failing."""
        mode = self._mode(
            mock_session,
            mock_state_store,
            warm_pool_size=2,
            auto_create_instance_profile=True,
        )

        with patch(
            "parsl_aws_provider.modes.standard.get_or_create_ssm_instance_profile",
            side_effect=RuntimeError("IAM denied"),
        ):
            mode.initialize()

        assert mode.initialized is True
        assert mode.iam_instance_profile_arn is None


class TestDeleteSSMInstanceProfile:
    """``delete_ssm_instance_profile`` is the inverse of the creator (#132).

    Nothing deleted the role or profile before this, so every run left a standing
    principal holding ``AmazonSSMManagedInstanceCore`` behind. The account under
    test had accumulated 94 of them against IAM's 1,000-role quota.
    """

    def test_round_trip_leaves_nothing_behind(self, iam_session):
        """Create then delete must return IAM to its starting state."""
        session = iam_session
        iam = session.client("iam")

        get_or_create_ssm_instance_profile(
            session=session, name_suffix="rt", auto_create=True
        )
        role_name, profile_name = ssm_instance_profile_names("rt")
        # Precondition: both really exist, so the assertions below mean something.
        iam.get_role(RoleName=role_name)
        iam.get_instance_profile(InstanceProfileName=profile_name)

        assert delete_ssm_instance_profile(session, "rt") is True

        with pytest.raises(ClientError) as role_gone:
            iam.get_role(RoleName=role_name)
        assert role_gone.value.response["Error"]["Code"] == "NoSuchEntity"

        with pytest.raises(ClientError) as profile_gone:
            iam.get_instance_profile(InstanceProfileName=profile_name)
        assert profile_gone.value.response["Error"]["Code"] == "NoSuchEntity"

    def test_deleting_what_was_never_created_succeeds(self, iam_session):
        """Cleanup runs on partially-completed paths, so absent means done.

        Reporting failure here would turn an already-clean account into a logged
        error on every teardown.
        """
        assert delete_ssm_instance_profile(iam_session, "never-existed") is True

    def test_is_idempotent(self, iam_session):
        """A second delete must not report failure."""
        session = iam_session
        get_or_create_ssm_instance_profile(
            session=session, name_suffix="twice", auto_create=True
        )

        assert delete_ssm_instance_profile(session, "twice") is True
        assert delete_ssm_instance_profile(session, "twice") is True

    def test_detaches_operator_added_policies_too(self, iam_session):
        """``delete_role`` refuses while *any* policy is attached.

        Detaching only ``AmazonSSMManagedInstanceCore`` by name would strand the
        role forever the moment an operator attached anything else, so the
        implementation lists what is actually there.
        """
        session = iam_session
        iam = session.client("iam")
        get_or_create_ssm_instance_profile(
            session=session, name_suffix="extra", auto_create=True
        )
        role_name, _ = ssm_instance_profile_names("extra")
        iam.attach_role_policy(
            RoleName=role_name,
            PolicyArn="arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess",
        )

        assert delete_ssm_instance_profile(session, "extra") is True

        with pytest.raises(ClientError):
            iam.get_role(RoleName=role_name)

    def test_the_names_match_the_creator(self):
        """The creator and deleter derive names from one helper, not two literals."""
        assert ssm_instance_profile_names("abc") == (
            "parsl-ephemeral-ssm-role-abc",
            "parsl-ephemeral-ssm-profile-abc",
        )


class TestStandardModeProfileOwnership:
    """Who deletes the IAM pair, and who must never delete it (#132)."""

    @pytest.fixture
    def mock_session(self):
        session = MagicMock(spec=boto3.Session)
        session.region_name = "us-east-1"
        return session

    @pytest.fixture
    def mock_state_store(self):
        store = MagicMock()
        store.load_state.return_value = None
        return store

    def _mode(self, mock_session, mock_state_store, **overrides):
        params = {
            "provider_id": "test-provider",
            "session": mock_session,
            "state_store": mock_state_store,
            "image_id": "ami-12345",
            "region": "us-east-1",
            **NETWORK_IDS,
        }
        params.update(overrides)
        return StandardMode(**params)

    def test_auto_created_profile_is_owned_and_deleted(
        self, mock_session, mock_state_store
    ):
        """The leak itself: an auto-created pair must be torn down."""
        mode = self._mode(
            mock_session,
            mock_state_store,
            one_shot=True,
            auto_create_instance_profile=True,
        )

        with patch(
            "parsl_aws_provider.modes.standard.get_or_create_ssm_instance_profile",
            return_value="arn:aws:iam::1:instance-profile/auto",
        ):
            mode.initialize()

        assert mode._owns_instance_profile is True

        with patch(
            "parsl_aws_provider.modes.standard.delete_ssm_instance_profile"
        ) as delete:
            mode.cleanup_infrastructure()

        delete.assert_called_once_with(mock_session, "test-provider")

    def test_a_supplied_profile_is_never_deleted(self, mock_session, mock_state_store):
        """The dangerous case. A caller's profile is shared infrastructure.

        Deleting it would break every other workload using it -- a worse bug than
        the leak, and the same hazard as the serverless SG deletion in #100.
        """
        mode = self._mode(
            mock_session,
            mock_state_store,
            one_shot=True,
            iam_instance_profile_arn="arn:aws:iam::1:instance-profile/theirs",
            auto_create_instance_profile=True,
        )
        mode.initialize()

        assert mode._owns_instance_profile is False

        with patch(
            "parsl_aws_provider.modes.standard.delete_ssm_instance_profile"
        ) as delete:
            mode.cleanup_infrastructure()

        delete.assert_not_called()

    def test_nothing_is_deleted_when_nothing_was_created(
        self, mock_session, mock_state_store
    ):
        """No flag, no ARN, no IAM call on either end."""
        mode = self._mode(mock_session, mock_state_store)
        mode.initialize()

        assert mode._owns_instance_profile is False

        with patch(
            "parsl_aws_provider.modes.standard.delete_ssm_instance_profile"
        ) as delete:
            mode.cleanup_infrastructure()

        delete.assert_not_called()

    def test_ownership_survives_a_restart(self, mock_session, mock_state_store):
        """A resumed provider must still delete the pair it created earlier.

        The names derive from ``provider_id``, so on restart the resolver *fetches*
        rather than creates. Gating ownership on create-vs-fetch would therefore
        disown the pair on every restart and leak it permanently -- which is why
        the flag is persisted rather than recomputed.
        """
        mock_state_store.load_state.return_value = {
            "provider_id": "test-provider",
            "resources": {},
            "initialized": True,
            "owns_instance_profile": True,
        }
        mode = self._mode(
            mock_session,
            mock_state_store,
            one_shot=True,
            auto_create_instance_profile=True,
        )

        assert mode.load_state() is True
        assert mode._owns_instance_profile is True

        with patch(
            "parsl_aws_provider.modes.standard.delete_ssm_instance_profile"
        ) as delete:
            mode.cleanup_infrastructure()

        delete.assert_called_once_with(mock_session, "test-provider")

    def test_ownership_is_persisted(self, mock_session, mock_state_store):
        """``save_state`` must carry the flag, or the restart case cannot work."""
        mode = self._mode(
            mock_session,
            mock_state_store,
            one_shot=True,
            auto_create_instance_profile=True,
        )

        with patch(
            "parsl_aws_provider.modes.standard.get_or_create_ssm_instance_profile",
            return_value="arn:aws:iam::1:instance-profile/auto",
        ):
            mode.initialize()

        mode.save_state()

        saved = mock_state_store.save_state.call_args[0][1]
        assert saved["owns_instance_profile"] is True

    def test_a_failed_deletion_does_not_raise(self, mock_session, mock_state_store):
        """Cleanup must not mask whatever the caller was originally doing."""
        mode = self._mode(
            mock_session,
            mock_state_store,
            one_shot=True,
            auto_create_instance_profile=True,
        )

        with patch(
            "parsl_aws_provider.modes.standard.get_or_create_ssm_instance_profile",
            return_value="arn:aws:iam::1:instance-profile/auto",
        ):
            mode.initialize()

        with patch(
            "parsl_aws_provider.modes.standard.delete_ssm_instance_profile",
            side_effect=RuntimeError("AccessDenied"),
        ):
            mode.cleanup_infrastructure()  # must not raise

        # And the ARN is dropped either way: it no longer names anything usable.
        assert mode.iam_instance_profile_arn is None

    def test_a_failed_verification_still_deletes_the_pair(
        self, mock_session, mock_state_store
    ):
        """The remaining leak after #132: initialize() failing before its own teardown.

        ``_resolve_instance_profile`` creates the pair; ``_verify_resources`` runs
        next and raises on a network ID that does not resolve. Both used to sit
        *above* the try/except that calls ``cleanup_infrastructure``, so a mistyped
        subnet ID -- an ordinary typo, and one you make repeatedly while debugging
        -- leaked a role and a profile on every attempt. #196 hit this from the
        Globus side, where each rejected config load was another pair.
        """
        ec2 = MagicMock()
        mock_session.client.return_value = ec2
        ec2.describe_subnets.side_effect = ClientError(
            {"Error": {"Code": "InvalidSubnetID.NotFound", "Message": "nope"}},
            "DescribeSubnets",
        )

        mode = self._mode(
            mock_session,
            mock_state_store,
            one_shot=True,
            auto_create_instance_profile=True,
        )

        with (
            patch(
                "parsl_aws_provider.modes.standard.get_or_create_ssm_instance_profile",
                return_value="arn:aws:iam::1:instance-profile/auto",
            ),
            patch(
                "parsl_aws_provider.modes.standard.delete_ssm_instance_profile"
            ) as delete,
            pytest.raises(ResourceNotFoundError, match="subnet-12345"),
        ):
            mode.initialize()

        delete.assert_called_once_with(mock_session, "test-provider")

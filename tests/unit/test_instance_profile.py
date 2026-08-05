"""Unit tests for SSM instance profile resolution.

Warm-pool and one-shot dispatch both go through SSM ``SendCommand``, which needs
the instance to carry a profile holding ``AmazonSSMManagedInstanceCore``. The
provider accepted ``auto_create_instance_profile`` but never forwarded it, so no
profile was ever attached and every dispatch silently fell back to UserData.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import re
import uuid
from unittest.mock import MagicMock, patch

import boto3
import pytest
import yaml
from botocore.exceptions import ClientError
from moto import mock_aws

from parsl_ephemeral_provider.exceptions import ResourceNotFoundError
from parsl_ephemeral_provider.modes.detached import DetachedMode
from parsl_ephemeral_provider.modes.standard import StandardMode
from parsl_ephemeral_provider.utils.aws import (
    BASTION_INLINE_POLICY_NAME,
    _wait_for_instance_profile,
    bastion_instance_profile_names,
    bastion_role_policy,
    create_bastion_instance_profile,
    delete_bastion_instance_profile,
    delete_ssm_instance_profile,
    get_cf_template,
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

        with patch("parsl_ephemeral_provider.utils.aws.time.sleep") as sleep:
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

        with patch("parsl_ephemeral_provider.utils.aws.time.sleep") as sleep:
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

        with patch("parsl_ephemeral_provider.utils.aws.time.sleep") as sleep:
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
                "parsl_ephemeral_provider.utils.aws.time.time",
                side_effect=lambda: clock[0],
            ),
            patch("parsl_ephemeral_provider.utils.aws.time.sleep", side_effect=advance),
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
            "parsl_ephemeral_provider.modes.standard.get_or_create_ssm_instance_profile",
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
            "parsl_ephemeral_provider.modes.standard.get_or_create_ssm_instance_profile"
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
            "parsl_ephemeral_provider.modes.standard.get_or_create_ssm_instance_profile"
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
            "parsl_ephemeral_provider.modes.standard.get_or_create_ssm_instance_profile",
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
            "parsl_ephemeral_provider.modes.standard.get_or_create_ssm_instance_profile",
            return_value="arn:aws:iam::1:instance-profile/auto",
        ):
            mode.initialize()

        assert mode._owns_instance_profile is True

        with patch(
            "parsl_ephemeral_provider.modes.standard.delete_ssm_instance_profile"
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
            "parsl_ephemeral_provider.modes.standard.delete_ssm_instance_profile"
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
            "parsl_ephemeral_provider.modes.standard.delete_ssm_instance_profile"
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
            "parsl_ephemeral_provider.modes.standard.delete_ssm_instance_profile"
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
            "parsl_ephemeral_provider.modes.standard.get_or_create_ssm_instance_profile",
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
            "parsl_ephemeral_provider.modes.standard.get_or_create_ssm_instance_profile",
            return_value="arn:aws:iam::1:instance-profile/auto",
        ):
            mode.initialize()

        with patch(
            "parsl_ephemeral_provider.modes.standard.delete_ssm_instance_profile",
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
                "parsl_ephemeral_provider.modes.standard.get_or_create_ssm_instance_profile",
                return_value="arn:aws:iam::1:instance-profile/auto",
            ),
            patch(
                "parsl_ephemeral_provider.modes.standard.delete_ssm_instance_profile"
            ) as delete,
            pytest.raises(ResourceNotFoundError, match="subnet-12345"),
        ):
            mode.initialize()

        delete.assert_called_once_with(mock_session, "test-provider")


class TestBastionRolePolicy:
    """The bastion policy has to match what the manager script actually calls.

    A bastion whose policy is missing an action fails the same way a bastion with
    no policy at all does -- at runtime, inside a loop that ``Restart=always``
    restarts every ten seconds, while ``systemctl is-active`` keeps reporting
    ``active`` (#229). So the policy is asserted against the script rather than
    against a plausible-looking list.
    """

    def _script_calls(self):
        """Every AWS API the generated bastion manager script invokes.

        Parsed out of the script rather than listed here: a hand-maintained copy
        of this list would drift from the script, which is the exact failure the
        test exists to catch.
        """
        mode = DetachedMode(
            provider_id="policy-test",
            session=MagicMock(spec=boto3.Session),
            state_store=MagicMock(),
            workflow_id="wf-policy",
            **NETWORK_IDS,
        )
        script = mode._get_bastion_manager_script()

        calls = set()
        # `ssm.get_parameter(...)`, `ec2.run_instances(...)`, and the paginators,
        # which authorize as the operation they paginate rather than as
        # `GetPaginator`.
        for service, method in re.findall(r"\b(ec2|ssm)\.([a-z_]+)\(", script):
            if method == "get_paginator":
                continue
            calls.add((service, method))
        for service, method in re.findall(
            r"\b(ec2|ssm)\.get_paginator\(['\"]([a-z_]+)", script
        ):
            calls.add((service, method))
        return calls

    def _policy_actions(self, policy):
        return {
            action
            for statement in policy["Statement"]
            for action in statement["Action"]
        }

    @staticmethod
    def _to_api_name(snake):
        """``get_parameters_by_path`` -> ``GetParametersByPath``."""
        return "".join(part.title() for part in snake.split("_"))

    def test_every_call_the_script_makes_is_granted(self):
        """No action the manager invokes may be missing from the policy.

        This is what found the CloudFormation policy short: it granted neither
        ``ec2:RunInstances`` nor any of the launch-template or fleet calls, so a
        bastion deployed by ``bastion.yml`` could not launch a worker either. The
        direct path's total absence of credentials was the louder half of #229,
        not the whole of it.
        """
        granted = self._policy_actions(
            bastion_role_policy("us-east-1", "123456789012", "wf-1")
        )
        # AmazonSSMManagedInstanceCore covers the agent's own ssm:* traffic, which
        # is not what the script calls; only the script's calls are checked here.
        required = {
            f"{service}:{self._to_api_name(method)}"
            for service, method in self._script_calls()
        }

        assert required, "the script parse found nothing, so this proves nothing"
        assert not required - granted, (
            f"the manager script calls {sorted(required - granted)}, which the "
            "bastion policy does not grant"
        )

    def test_nothing_is_granted_that_the_script_never_calls(self):
        """The reverse direction: no action beyond what the script needs.

        ``bastion.yml`` granted ``ec2:StartInstances`` and ``ec2:StopInstances``
        for a script that only ever terminates workers -- and this assertion also
        caught ``ec2:DescribeInstanceStatus`` and ``ec2:DescribeTags``, which the
        script has never called either. Least privilege is the point: this role
        can already terminate instances, so surplus grants on it are not free.

        ``ec2:CreateTags`` is the one grant that is required without appearing as
        a call. The script tags through ``TagSpecifications`` on
        ``RunInstances``/``CreateFleet``/``CreateLaunchTemplate``, which AWS
        authorizes as ``CreateTags`` against the resource being created.
        """
        implied_by_tag_specifications = {"ec2:CreateTags"}
        granted = (
            self._policy_actions(
                bastion_role_policy("us-east-1", "123456789012", "wf-1")
            )
            - implied_by_tag_specifications
        )
        called = {
            f"{service}:{self._to_api_name(method)}"
            for service, method in self._script_calls()
        }

        assert not granted - called, (
            f"the bastion policy grants {sorted(granted - called)}, which the "
            "manager script never calls"
        )

    def test_no_pass_role(self):
        """``iam:PassRole`` is deliberately absent.

        The manager launches workers with no instance profile, so it passes no
        role. Granting it would let a compromised bastion attach any passable
        role to an instance it launches -- a privilege-escalation primitive, not a
        convenience.
        """
        granted = self._policy_actions(
            bastion_role_policy("us-east-1", "123456789012", "wf-1")
        )

        assert not any(action.startswith("iam:") for action in granted)

    def test_ssm_is_scoped_to_the_workflow(self):
        """The parameter path is the control channel, so it must not be broad.

        Job commands live under it. A bastion that could read another workflow's
        path could read that workflow's commands.
        """
        policy = bastion_role_policy("eu-west-1", "999988887777", "wf-scoped")
        ssm_statements = [
            statement
            for statement in policy["Statement"]
            if any(action.startswith("ssm:") for action in statement["Action"])
        ]

        assert len(ssm_statements) == 1
        assert ssm_statements[0]["Resource"] == (
            "arn:aws:ssm:eu-west-1:999988887777:parameter/parsl/workflows/wf-scoped/*"
        )

    def test_it_matches_the_cloudformation_template(self):
        """Both bastion paths run the same script, so both policies must agree.

        Two deployment paths granting different permissions means a bug reachable
        from one of them only, which is the hardest kind to find -- #229 is an
        instance of exactly that.
        """
        template = yaml.safe_load(
            # `!Sub` and friends would otherwise stop safe_load; only the
            # BastionHostPolicy actions are needed, so the tags are neutralised.
            re.sub(r"!\w+", "", get_cf_template("bastion.yml"))
        )
        policies = template["Resources"]["BastionHostRole"]["Properties"]["Policies"]
        cfn_actions = {
            action
            for policy in policies
            for statement in policy["PolicyDocument"]["Statement"]
            for action in statement["Action"]
        }
        python_actions = self._policy_actions(
            bastion_role_policy("us-east-1", "123456789012", "wf-1")
        )

        assert cfn_actions == python_actions


class TestBastionInstanceProfileLifecycle:
    """Create-and-delete for the bastion pair, with the #132 ownership gate."""

    def test_round_trip_leaves_nothing_behind(self, iam_session):
        """The inline policy must not strand the role.

        This is where the bastion pair differs from the SSM pair: its permissions
        are inline (scoped to one workflow, so there is nothing reusable to make a
        managed policy from), and ``delete_role`` refuses while an inline policy
        remains just as it does for a managed one. The two are listed and removed
        by different API calls, so a teardown handling only managed policies would
        delete every SSM pair and no bastion pair.
        """
        session = iam_session
        iam = session.client("iam")

        create_bastion_instance_profile(session, "rt-bastion", "wf-rt")
        role_name, profile_name = bastion_instance_profile_names("rt-bastion")
        iam.get_role(RoleName=role_name)
        iam.get_instance_profile(InstanceProfileName=profile_name)
        # Precondition: the inline policy really is there, so its removal below
        # is what makes the delete succeed.
        assert iam.list_role_policies(RoleName=role_name)["PolicyNames"] == [
            BASTION_INLINE_POLICY_NAME
        ]

        assert delete_bastion_instance_profile(session, "rt-bastion") is True

        with pytest.raises(ClientError) as role_gone:
            iam.get_role(RoleName=role_name)
        assert role_gone.value.response["Error"]["Code"] == "NoSuchEntity"
        with pytest.raises(ClientError):
            iam.get_instance_profile(InstanceProfileName=profile_name)

    def test_the_two_pairs_do_not_collide(self, iam_session):
        """A bastion pair and an SSM pair for the same provider are distinct.

        A detached-mode provider can hold both, so shared names would mean each
        creator repairing the other's role and either teardown deleting the
        other's credentials.
        """
        bastion_names = set(bastion_instance_profile_names("same-id"))
        ssm_names = set(ssm_instance_profile_names("same-id"))

        assert not bastion_names & ssm_names

    def test_the_names_fit_iams_limit(self):
        """IAM caps role and profile names at 64 characters.

        ``provider_id`` is a UUID by default -- 36 characters -- and a name over
        the limit fails at ``create_role``, i.e. only on the live path.
        """
        for name in bastion_instance_profile_names(str(uuid.uuid4())):
            assert len(name) <= 64

    def test_is_idempotent(self, iam_session):
        """Re-creating repairs rather than fails, and re-deleting is not an error.

        ``put_role_policy`` is a replace keyed on the policy name, which is what
        makes the second create update a role left by an older version whose
        policy was narrower.
        """
        session = iam_session

        first = create_bastion_instance_profile(session, "twice-bastion", "wf-1")
        second = create_bastion_instance_profile(session, "twice-bastion", "wf-1")
        assert first == second

        assert delete_bastion_instance_profile(session, "twice-bastion") is True
        assert delete_bastion_instance_profile(session, "twice-bastion") is True

    def test_recreating_rewrites_the_policy(self, iam_session):
        """A second create updates the workflow scope rather than leaving it stale.

        A resumed provider whose workflow ID changed would otherwise carry a
        policy scoped to the previous workflow's parameter path and be unable to
        read its own jobs.
        """
        session = iam_session
        iam = session.client("iam")

        create_bastion_instance_profile(session, "rescope", "wf-old")
        create_bastion_instance_profile(session, "rescope", "wf-new")

        role_name, _ = bastion_instance_profile_names("rescope")
        document = iam.get_role_policy(
            RoleName=role_name, PolicyName=BASTION_INLINE_POLICY_NAME
        )["PolicyDocument"]
        resources = str(document)

        assert "wf-new" in resources
        assert "wf-old" not in resources

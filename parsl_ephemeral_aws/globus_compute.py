"""Globus Compute integration for EphemeralAWSProvider.

Exposes ``GlobusComputeProvider``, a thin subclass of ``EphemeralAWSProvider``
that:

* Carries ``endpoint_id`` and ``container_image`` metadata for Globus Compute.
* Provides ``generate_endpoint_config(path)`` which writes a Globus Compute
  endpoint configuration the ``globus-compute-endpoint`` daemon can load.

Usage::

    from parsl_ephemeral_aws import GlobusComputeProvider

    provider = GlobusComputeProvider(
        endpoint_id="<your-globus-endpoint-uuid>",
        region="us-east-1",
        instance_type="t3.medium",
        mode="standard",
        vpc_id="vpc-...",
        subnet_id="subnet-...",
        security_group_id="sg-...",
        use_spot=True,
        auto_create_instance_profile=True,
        display_name="My Ephemeral AWS Endpoint",
    )
    provider.generate_endpoint_config("~/.globus_compute/my_aws_endpoint")

How Globus Compute finds this provider
--------------------------------------
Globus Compute resolves the ``provider: type:`` key in ``config.yaml`` by plain
attribute lookup on the ``parsl.providers`` module -- ``getattr(parsl.providers,
type_name, None)``, raising if the result is ``None``
(``globus_compute_endpoint/endpoint/config/dispatch.py``). Two consequences,
both verified against ``globus-compute-endpoint`` 4.15.0:

1. A dotted path can never resolve, because ``getattr`` does not walk dots. The
   type key must therefore be the bare class name.
2. The bare name only resolves if something has already assigned the class onto
   ``parsl.providers``. Importing this module does that (see
   :func:`_register_with_parsl_providers`), but the endpoint daemon has no
   reason to import it -- it reads ``config.yaml`` and nothing else.

So ``generate_endpoint_config()`` writes *two* files: the ``config.yaml`` that
holds the configuration, and a small ``config.py`` shim that imports this
package -- registering the class -- and then hands the YAML to Globus Compute's
own loader. ``get_config()`` prefers ``config.py`` when both are present, which
is what makes the pair work. ``config.yaml`` stays the single place to edit.

This covers single-user endpoints, which is what ``generate_endpoint_config()``
produces. **Multi-user (manager) endpoints are not supported** (#133): those
render ``user_config_template.yaml.j2`` to a string and the forked user-endpoint
process calls ``load_config_yaml()`` on it directly, never going through
``get_config()`` -- so there is no ``config.py`` hook and the same "not a valid
provider" failure returns. Resolving that needs dotted-path support upstream in
``TypeDispatcher.build_instance``; see #133.

Minimum IAM permissions
-----------------------
:meth:`GlobusComputeProvider.minimum_iam_policy` returns these as a policy
document. The lists are derived from the AWS API calls the package actually
makes on the ``mode="standard"`` path, which is what a generated endpoint
config uses; AWS may require further implicit permissions.

EC2 (always required)
    ec2:RunInstances, ec2:TerminateInstances, ec2:DescribeInstances,
    ec2:DescribeInstanceTypes, ec2:CreateTags, ec2:DescribeTags,
    ec2:DescribeImages, ec2:CreateImage, ec2:DeregisterImage,
    ec2:DeleteSnapshot, ec2:DescribeVpcs, ec2:DescribeSubnets,
    ec2:DescribeSecurityGroups, ec2:CreateLaunchTemplate,
    ec2:CreateLaunchTemplateVersion, ec2:DeleteLaunchTemplate,
    ec2:CreateFleet, ec2:DescribeFleets, ec2:DeleteFleets,
    ec2:RequestSpotInstances, ec2:DescribeSpotInstanceRequests,
    ec2:DescribeSpotPriceHistory

    The network resources are read-only: ``vpc_id``, ``subnet_id`` and
    ``security_group_id`` have been caller-supplied since v0.7.0, so no
    create/delete grant is needed for them.

SSM (required)
    ssm:GetParameter (resolves the current Amazon Linux 2023 AMI),
    ssm:SendCommand, ssm:GetCommandInvocation,
    ssm:DescribeInstanceInformation (warm-pool and one-shot dispatch),
    ssm:StartSession, ssm:TerminateSession, ssm:ResumeSession,
    ssm:DescribeSessions, ssm:GetConnectionStatus (Session Manager tunnels)

EventBridge + SQS (required when use_spot and spot_interruption_handling)
    events:PutRule, events:PutTargets, events:RemoveTargets,
    events:DeleteRule, events:TagResource, sqs:CreateQueue,
    sqs:GetQueueAttributes, sqs:SetQueueAttributes, sqs:ReceiveMessage,
    sqs:DeleteMessage, sqs:DeleteQueue

IAM (required when auto_create_instance_profile=True)
    iam:CreateRole, iam:GetRole, iam:AttachRolePolicy,
    iam:CreateInstanceProfile, iam:GetInstanceProfile,
    iam:AddRoleToInstanceProfile, iam:PassRole

ECR (only when container_image references an ECR repository)
    ecr:GetAuthorizationToken, ecr:BatchGetImage,
    ecr:GetDownloadUrlForLayer, ecr:BatchCheckLayerAvailability

Not covered by ``minimum_iam_policy()``: the non-default state backends
(``state_store_type="s3"`` needs S3 object access, ``"parameter_store"`` needs
``ssm:PutParameter``/``GetParameter``/``DeleteParameter``/
``GetParametersByPath``), ``mode="detached"`` (CloudFormation stack and SSM
Parameter Store access), and ``mode="serverless"`` (Lambda, ECS, CloudWatch
Logs, and IAM role lifecycle).

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import inspect
import logging
import os
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

import parsl.providers

from parsl_ephemeral_aws.provider import EphemeralAWSProvider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# YAML template helpers
# ---------------------------------------------------------------------------

# Bare class name, not a dotted path: Globus Compute's ProviderDispatcher does
# `getattr(parsl.providers, type_name, None)`, and getattr does not walk dots
# (#87). The name resolves because _register_with_parsl_providers() below puts
# the class there.
_PROVIDER_TYPE = "GlobusComputeProvider"

# Indentation helpers for hand-rolled YAML (avoids a PyYAML import at module
# level and keeps the output human-readable with predictable ordering).
_INDENT2 = "  "
_INDENT4 = "    "
_INDENT6 = "      "

# Constructor parameters deliberately left out of the generated config.
#
# ``provider_id`` identifies one provider's state document. Emitting it would
# pin every endpoint restart to the *generating* process's ID; leaving it out
# lets the constructor adopt whatever is already persisted at the state location,
# which is what _adopt_persisted_provider_id() exists to do.
#
# ``nodes_per_block``, ``cores_per_node`` and ``mem_per_node`` belong to Parsl's
# own ExecutionProvider surface. GlobusComputeEngine sets them from its own
# config keys, so emitting them here would put two writers on one value.
#
# ``debug`` is a local troubleshooting flag, not endpoint configuration.
_SKIP_PARAMS = frozenset(
    {
        "provider_id",
        "nodes_per_block",
        "cores_per_node",
        "mem_per_node",
        "debug",
    }
)

# Parameters whose name differs from the attribute holding the resolved value.
# ``mode`` is the only one: the constructor normalises it into ``mode_type`` (an
# OperatingModeType) and keeps no ``self.mode``.
_ATTRIBUTE_OVERRIDES = {"mode": "mode_type"}

# Emitted even when the caller did not pass them. The first four are what someone
# reads the file to find out, and a config that states them is easier to trust
# than one where their absence has to be interpreted.
#
# ``worker_init`` is here for a stronger reason: it is the only thing that puts
# ``globus-compute-endpoint`` on a worker's PATH (see
# DEFAULT_GLOBUS_WORKER_INIT). Leaving it out when unset would mean the
# reconstructed provider falls back to EphemeralAWSProvider's parsl-only
# DEFAULT_WORKER_INIT -- the #138 failure, reintroduced one level down. It is
# always emitted, so the subclass default travels with the config.
_ALWAYS_EMIT = frozenset(
    {"region", "instance_type", "mode", "max_blocks", "worker_init"}
)

# A Globus Compute worker is not launched with Parsl's process_worker_pool.py.
# GlobusComputeEngine._get_compute_launch_cmd() rewrites the template to
#
#     globus-compute-endpoint python-exec \
#         parsl.executors.high_throughput.process_worker_pool ...
#
# so ``globus-compute-endpoint`` must be on the worker's PATH. Nothing but
# worker_init puts it there, and EphemeralAWSProvider's DEFAULT_WORKER_INIT
# installs ``parsl`` alone -- every worker launched with it fails "command not
# found" (#138). This default installs both.
#
# The pip install carries no --upgrade: globus-compute-endpoint pins parsl
# exactly, so letting pip take a newer parsl would break the pin it just
# resolved.
DEFAULT_GLOBUS_WORKER_INIT = (
    "dnf install -y python3.11 python3.11-pip\n"
    "ln -sf /usr/bin/python3.11 /usr/bin/python3\n"
    "pip3.11 install --quiet globus-compute-endpoint\n"
)


def _yaml_str(value: str) -> str:
    """Quote a string value if it contains YAML-special characters.

    A bare colon is only a YAML mapping indicator when followed by a space or
    at end of line (e.g. ``"key: value"`` or trailing ``":"``).  Simple values
    such as Docker image tags (``python:3.11-slim``) do not need quoting.

    Strings holding a newline are emitted as a double-quoted scalar with the
    escape left intact -- ``worker_init`` is multi-line shell script, and a raw
    newline inside a plain scalar would end the line and corrupt the document.
    """
    if "\n" in value or "\t" in value:
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\t", "\\t")
        )
        return f'"{escaped}"'
    needs_quoting = (
        ": " in value
        or value.endswith(":")
        or value.startswith(("#", "&", "*", "!", "|", ">", "'", '"'))
        or any(c in value for c in ("{", "}", "[", "]", ","))
    )
    if needs_quoting:
        return f'"{value}"'
    return value


def _yaml_line(key: str, value: Any, indent: str = "") -> str:
    if value is None:
        return f"{indent}{key}: null"
    if isinstance(value, bool):
        return f"{indent}{key}: {str(value).lower()}"
    # Before the str branch, not after: OperatingModeType and friends are
    # ``(str, Enum)``, so an isinstance(value, str) test matches them and
    # ``str(value)`` renders "OperatingModeType.STANDARD" -- which the provider
    # constructor then rejects as an invalid mode. ``.value`` is what it accepts.
    if isinstance(value, Enum):
        return f"{indent}{key}: {_yaml_str(str(value.value))}"
    if isinstance(value, str):
        return f"{indent}{key}: {_yaml_str(value)}"
    if isinstance(value, dict):
        inner = ", ".join(
            f"{_yaml_str(str(k))}: {_yaml_str(str(v))}" for k, v in value.items()
        )
        return f"{indent}{key}: {{{inner}}}"
    if isinstance(value, (list, tuple)):
        return f"{indent}{key}: [{', '.join(_yaml_str(str(v)) for v in value)}]"
    return f"{indent}{key}: {value}"


# The ``config.py`` shim written alongside ``config.yaml``. Globus Compute
# imports this file and reads its module-level ``config``; the import of
# ``parsl_ephemeral_aws`` is what puts ``GlobusComputeProvider`` on
# ``parsl.providers`` so the YAML's ``type:`` key resolves.
_CONFIG_PY_SHIM = '''\
"""Loader shim generated by parsl-ephemeral-aws. Edit config.yaml, not this file.

Globus Compute resolves a provider's ``type:`` by attribute lookup on the
``parsl.providers`` module, which only knows about providers that ship with
Parsl. Importing ``parsl_ephemeral_aws`` registers ``GlobusComputeProvider``
there, so this file exists purely to do that import before the YAML is parsed.
``globus-compute-endpoint`` prefers ``config.py`` over ``config.yaml`` when both
are present, which is what gets this executed.
"""

import pathlib

import parsl_ephemeral_aws  # noqa: F401  registers GlobusComputeProvider
from globus_compute_endpoint.endpoint.config.utils import load_config_yaml

config = load_config_yaml((pathlib.Path(__file__).parent / "config.yaml").read_text())
'''


def _register_with_parsl_providers() -> None:
    """Make ``GlobusComputeProvider`` resolvable from a Globus Compute config.

    Globus Compute's ``ProviderDispatcher`` looks a provider up with
    ``getattr(parsl.providers, type_name, None)`` and raises when that is
    ``None``, so a class Parsl does not ship is unreachable until it is present
    as an attribute on that module. ``parsl.providers`` defines no
    ``__getattr__`` hook to intercept the lookup, so the attribute has to be
    assigned outright.

    Assigning rather than only extending ``__all__`` is deliberate: ``getattr``
    consults the module namespace, not ``__all__``. ``__all__`` is extended too
    so ``from parsl.providers import *`` and the dispatcher's "valid options"
    error message both list it.
    """
    setattr(parsl.providers, "GlobusComputeProvider", GlobusComputeProvider)
    exported = getattr(parsl.providers, "__all__", None)
    if isinstance(exported, list) and "GlobusComputeProvider" not in exported:
        exported.append("GlobusComputeProvider")


# ---------------------------------------------------------------------------
# GlobusComputeProvider
# ---------------------------------------------------------------------------


class GlobusComputeProvider(EphemeralAWSProvider):
    """Globus Compute-aware wrapper around ``EphemeralAWSProvider``.

    Extends ``EphemeralAWSProvider`` with Globus Compute endpoint metadata
    and a helper that generates a ready-to-use ``config.yaml`` for the
    ``globus-compute-endpoint`` daemon.

    All ``EphemeralAWSProvider`` constructor parameters are accepted as-is
    (forwarded via ``**kwargs``).

    Parameters
    ----------
    endpoint_id : str, optional
        Globus Compute endpoint UUID.  May be ``None`` during development;
        the generated ``config.yaml`` will include a ``# TODO`` placeholder.
    container_image : str, optional
        Container image URI to run Parsl workers inside a container.
        Examples: ``"python:3.11-slim"``, ``"123456789.dkr.ecr.us-east-1.amazonaws.com/my-image:latest"``.
        When set, the generated ``config.yaml`` includes ``container_type: docker``
        and ``container_uri: <image>`` under the ``engine`` block.
    display_name : str, optional
        Human-readable name for the Globus Compute endpoint.
        Default is ``"Ephemeral AWS Endpoint"``.
    encrypted : bool, optional
        Whether the engine encrypts worker traffic with CurveZMQ. Default is
        ``False``, and that default is deliberate: ``HighThroughputExecutor``
        generates the certificates under its own ``run_dir`` on the *endpoint
        host* and passes that path to workers as ``--cert_dir``, so an EC2
        worker is handed a directory that does not exist on it and dies with
        ``FileNotFoundError``. Until certificate distribution is implemented
        (#62), ``True`` cannot work for remote workers and rely on VPC isolation
        instead. This was hardcoded ``true`` before #138, which made every
        generated config unusable.

        High-Assurance endpoints are the exception -- ``assert_ha_compliant()``
        rejects ``encrypted=False``, so those need #62 resolved rather than this
        default.
    \\*\\*kwargs
        All keyword arguments accepted by ``EphemeralAWSProvider``.
    """

    label = "globus_compute_aws"

    def __init__(
        self,
        endpoint_id: Optional[str] = None,
        container_image: Optional[str] = None,
        display_name: str = "Ephemeral AWS Endpoint",
        encrypted: bool = False,
        **kwargs: Any,
    ) -> None:
        # A Globus worker needs globus-compute-endpoint on its PATH, which the
        # inherited DEFAULT_WORKER_INIT does not install (#138). Only defaulted,
        # never overridden: an explicit worker_init is the caller's business.
        kwargs.setdefault("worker_init", DEFAULT_GLOBUS_WORKER_INIT)

        # Recorded before super().__init__ because the parent normalises and
        # resolves several of these in place. _provider_params_yaml() emits this
        # set, so it is the caller's stated intent that reaches the config rather
        # than whatever the attributes hold afterwards -- see that method for why
        # the difference matters for image_id.
        self._explicit_params: frozenset = frozenset(kwargs)

        super().__init__(**kwargs)
        self.endpoint_id: Optional[str] = endpoint_id
        self.container_image: Optional[str] = container_image
        self.display_name: str = display_name
        self.encrypted: bool = encrypted

    # ------------------------------------------------------------------
    # Config generation
    # ------------------------------------------------------------------

    def generate_endpoint_config(self, path: str) -> str:
        """Write a loadable Globus Compute endpoint configuration to *path*.

        Creates the directory at *path* if it does not exist, then writes two
        files into it:

        ``config.yaml``
            The configuration itself -- the file to edit.
        ``config.py``
            A three-line shim that imports ``parsl_ephemeral_aws`` (registering
            ``GlobusComputeProvider`` on ``parsl.providers``) and then hands
            ``config.yaml`` to Globus Compute's own loader.

        Both are needed. Globus Compute looks a provider up by attribute on
        ``parsl.providers``, so a ``config.yaml`` alone is unloadable: the
        daemon never imports this package, and the lookup fails with *"'...' is
        not a valid provider"* (#87). ``get_config()`` prefers ``config.py``
        when both exist, so the shim runs first and the YAML then resolves.

        Returns the absolute path to ``config.yaml`` -- the file a caller would
        want to read or edit.

        The result is ready for the ``globus-compute-endpoint`` daemon::

            globus-compute-endpoint start my_aws_endpoint

        Parameters
        ----------
        path : str
            Path to the Globus Compute endpoint directory
            (e.g. ``"~/.globus_compute/my_aws_endpoint"``).

        Returns
        -------
        str
            Absolute path to the written ``config.yaml``.
        """
        endpoint_dir = Path(os.path.expanduser(path)).resolve()
        endpoint_dir.mkdir(parents=True, exist_ok=True)
        config_path = endpoint_dir / "config.yaml"

        yaml_content = self._build_config_yaml()
        config_path.write_text(yaml_content, encoding="utf-8")

        shim_path = endpoint_dir / "config.py"
        shim_path.write_text(_CONFIG_PY_SHIM, encoding="utf-8")

        logger.info(
            "Globus Compute endpoint config written to %s (with loader shim %s)",
            config_path,
            shim_path,
        )
        return str(config_path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_config_yaml(self) -> str:
        """Render the endpoint ``config.yaml`` content as a string."""
        lines: list[str] = []

        # ---- Header comment ----
        lines.append(
            "# Globus Compute endpoint configuration generated by GlobusComputeProvider"
        )
        lines.append("# Edit this file to customise the endpoint, then run:")
        lines.append("#   globus-compute-endpoint start <endpoint-name>")
        lines.append("")

        # ---- display_name ----
        lines.append(_yaml_line("display_name", self.display_name))

        # ---- engine block ----
        lines.append("")
        lines.append("engine:")
        lines.append(f"{_INDENT2}type: GlobusComputeEngine")
        if not self.encrypted:
            lines.append(
                f"{_INDENT2}# CurveZMQ certificates live in the endpoint host's run_dir,"
                " which an EC2"
            )
            lines.append(
                f"{_INDENT2}# worker cannot read -- see #62. Set true once that is"
                " distributed."
            )
        lines.append(_yaml_line("encrypted", self.encrypted, indent=_INDENT2))
        lines.append(f"{_INDENT2}max_retries_on_system_failure: 3")

        # ---- container (optional) ----
        if self.container_image:
            lines.append("")
            lines.append(f"{_INDENT2}# Container configuration")
            lines.append(f"{_INDENT2}container_type: docker")
            lines.append(
                _yaml_line("container_uri", self.container_image, indent=_INDENT2)
            )

        # ---- provider sub-block ----
        lines.append("")
        lines.append(f"{_INDENT2}provider:")
        lines.append(f"{_INDENT4}type: {_PROVIDER_TYPE}")
        lines.extend(self._provider_params_yaml())

        # ---- endpoint_id (trailing comment/reminder) ----
        lines.append("")
        if self.endpoint_id:
            lines.append(f"# endpoint_id: {self.endpoint_id}")
        else:
            lines.append(
                "# TODO: set endpoint_id after running:"
                " globus-compute-endpoint register <endpoint-name>"
            )

        lines.append("")  # trailing newline
        return "\n".join(lines)

    def _provider_params_yaml(self) -> list[str]:
        """Return YAML lines (with 4-space indent) for provider parameters.

        The emitted set is every parameter the caller passed explicitly, plus
        those in :data:`_ALWAYS_EMIT`, minus :data:`_SKIP_PARAMS`. Parameter
        names come from ``inspect.signature`` rather than a hand-written list,
        because the hand-written version covered 15 of 52 and silently dropped
        the rest (#138) -- ``worker_init`` among them, without which the
        reconstructed provider installs ``parsl`` but not
        ``globus-compute-endpoint`` and every worker command is "command not
        found". A signature-derived set cannot fall behind a new option.

        Keyed on what the caller *passed*, not on what differs from the default,
        because two attributes are resolved at runtime and would otherwise be
        frozen into the config:

        * ``image_id`` defaults to ``None`` and is then filled in from SSM with
          the current Amazon Linux 2023 AMI (#84). Emitting the resolved value
          would pin the endpoint to whatever AMI was current on the day the
          config was generated, which is the staleness #84 removed.
        * ``provider_id`` defaults to a fresh UUID; see :data:`_SKIP_PARAMS`.

        A caller who passes a value equal to the default still gets it emitted.
        That is intended -- it was a deliberate choice, and a default can change
        between releases.
        """
        lines: list[str] = []
        signature = inspect.signature(EphemeralAWSProvider.__init__)

        for name, parameter in signature.parameters.items():
            if name in _SKIP_PARAMS or parameter.kind is parameter.VAR_KEYWORD:
                continue
            if name not in _ALWAYS_EMIT and name not in self._explicit_params:
                continue

            attribute = _ATTRIBUTE_OVERRIDES.get(name, name)
            if not hasattr(self, attribute):  # pragma: no cover - defensive
                continue

            lines.append(_yaml_line(name, getattr(self, attribute), indent=_INDENT4))

        # These two are GlobusComputeProvider's own, so the signature loop above
        # cannot see them -- it reads EphemeralAWSProvider.__init__. Both are real
        # kwargs of this subclass, so they bind on the way back in.
        #
        # container_image is emitted here as well as as engine.container_uri: the
        # engine key is what actually containerises the worker, while this one
        # keeps the value on the reconstructed provider, so regenerating a config
        # from a loaded one does not silently drop it. The other two subclass
        # params live elsewhere in the document -- display_name is a top-level
        # Globus key and encrypted belongs to the engine.
        if self.endpoint_id:
            lines.append(_yaml_line("endpoint_id", self.endpoint_id, indent=_INDENT4))
        if self.container_image:
            lines.append(
                _yaml_line("container_image", self.container_image, indent=_INDENT4)
            )

        return lines

    # ------------------------------------------------------------------
    # Minimum IAM policy document
    # ------------------------------------------------------------------

    @staticmethod
    def minimum_iam_policy(include_ecr: bool = False) -> Dict[str, Any]:
        """Return the minimum IAM policy document as a Python dict.

        The returned dict can be serialised to JSON and attached to the IAM
        principal that *runs* the provider -- the user, role, or endpoint host
        that calls ``submit()``. It is not the instance role: workers need only
        ``AmazonSSMManagedInstanceCore``, which
        ``auto_create_instance_profile=True`` attaches for them.

        Scope: the ``mode="standard"`` path with file-backed state, which is
        what a generated endpoint config uses. Actions were derived from the
        package's actual API calls, so the set is narrower than it was before
        v0.7.0 -- network resources are caller-supplied since #69, so no
        VPC/subnet/security-group/NAT/gateway create or delete grant appears,
        and Spot Fleet was replaced by EC2 Fleet in #86. See the module
        docstring for what is deliberately *not* covered (the S3 and Parameter
        Store state backends, and detached and serverless modes).

        Parameters
        ----------
        include_ecr : bool
            When ``True``, include ECR permissions required to pull images
            from a private ECR repository (needed when ``container_image``
            references an ECR URI).

        Returns
        -------
        dict
            IAM policy document compatible with ``json.dumps()``.
        """
        ec2_actions = [
            # Instance lifecycle
            "ec2:RunInstances",
            "ec2:TerminateInstances",
            "ec2:DescribeInstances",
            "ec2:DescribeInstanceTypes",
            "ec2:CreateTags",
            "ec2:DescribeTags",
            # Read-only on the caller-supplied network, for the existence check
            # in _verify_resources(). No create or delete: those IDs have been
            # required rather than provisioned since #69.
            "ec2:DescribeVpcs",
            "ec2:DescribeSubnets",
            "ec2:DescribeSecurityGroups",
            # AMI resolution, plus bake_ami=True
            "ec2:DescribeImages",
            "ec2:CreateImage",
            "ec2:DeregisterImage",
            "ec2:DeleteSnapshot",
            # Launch templates: every launch path goes through one since #85
            "ec2:CreateLaunchTemplate",
            "ec2:CreateLaunchTemplateVersion",
            "ec2:DeleteLaunchTemplate",
            # EC2 Fleet, which replaced Spot Fleet in #86
            "ec2:CreateFleet",
            "ec2:DescribeFleets",
            "ec2:DeleteFleets",
            # Single spot instances (use_spot without use_spot_fleet)
            "ec2:RequestSpotInstances",
            "ec2:DescribeSpotInstanceRequests",
            "ec2:DescribeSpotPriceHistory",
        ]

        ssm_actions = [
            # Resolves the current AL2023 AMI from AWS's public parameter
            "ssm:GetParameter",
            # Command dispatch: warm-pool reuse and one-shot mode
            "ssm:SendCommand",
            "ssm:GetCommandInvocation",
            "ssm:DescribeInstanceInformation",
            # Session Manager tunnels to reach workers in a private subnet
            "ssm:StartSession",
            "ssm:TerminateSession",
            "ssm:ResumeSession",
            "ssm:DescribeSessions",
            "ssm:GetConnectionStatus",
        ]

        # The advance spot-interruption warning (#86) is delivered by an
        # EventBridge rule into an SQS queue the provider creates and polls.
        # Only reached when use_spot and spot_interruption_handling are both on,
        # but included unconditionally: the alternative is a policy that works
        # until someone enables spot, then fails at initialize().
        events_actions = [
            "events:PutRule",
            "events:PutTargets",
            "events:RemoveTargets",
            "events:DeleteRule",
            "events:TagResource",
        ]

        sqs_actions = [
            "sqs:CreateQueue",
            "sqs:GetQueueAttributes",
            "sqs:SetQueueAttributes",
            "sqs:ReceiveMessage",
            "sqs:DeleteMessage",
            "sqs:DeleteQueue",
        ]

        # Only needed for auto_create_instance_profile=True. No delete actions:
        # the provider does not tear the profile down (#132), so granting them
        # would permit more than it performs.
        iam_actions = [
            "iam:CreateRole",
            "iam:GetRole",
            "iam:AttachRolePolicy",
            "iam:CreateInstanceProfile",
            "iam:GetInstanceProfile",
            "iam:AddRoleToInstanceProfile",
            "iam:PassRole",
        ]

        statements = [
            {
                "Sid": "EC2Management",
                "Effect": "Allow",
                "Action": ec2_actions,
                "Resource": "*",
            },
            {
                "Sid": "SSMTunneling",
                "Effect": "Allow",
                "Action": ssm_actions,
                "Resource": "*",
            },
            {
                "Sid": "SpotInterruptionWarning",
                "Effect": "Allow",
                "Action": events_actions + sqs_actions,
                "Resource": "*",
            },
            {
                "Sid": "IAMInstanceProfile",
                "Effect": "Allow",
                "Action": iam_actions,
                "Resource": "*",
            },
        ]

        if include_ecr:
            ecr_actions = [
                "ecr:GetAuthorizationToken",
                "ecr:BatchGetImage",
                "ecr:GetDownloadUrlForLayer",
                "ecr:BatchCheckLayerAvailability",
            ]
            statements.append(
                {
                    "Sid": "ECRContainerImages",
                    "Effect": "Allow",
                    "Action": ecr_actions,
                    "Resource": "*",
                }
            )

        return {
            "Version": "2012-10-17",
            "Statement": statements,
        }


# Runs on import, which is the only moment that reliably precedes a Globus
# Compute config load: the generated ``config.py`` shim imports this package
# before parsing the YAML. Defined above the class, called here, because it
# references the class by name.
_register_with_parsl_providers()

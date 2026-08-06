"""Parsl Ephemeral Compute Provider for Globus Compute.

An independent Parsl provider for running ephemeral compute on AWS through Globus
Compute. AWS stays named here because it is the platform the compute actually runs
on -- this is a subclass of the AWS provider, not an alternative to it -- and
because a description is exactly where the AWS Trademark Guidelines permit the mark
(s13, plain-text factual reference); s7 keeps it out of the identifiers.

Exposes ``EphemeralComputeProvider``, a thin subclass of ``EphemeralProvider``
that:

* Carries ``endpoint_id`` and ``container_image`` metadata for Globus Compute.
* Provides ``generate_endpoint_config(path)`` which writes a Globus Compute
  endpoint configuration the ``globus-compute-endpoint`` daemon can load.

Usage::

    from parsl_ephemeral_provider import EphemeralComputeProvider

    provider = EphemeralComputeProvider(
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

The shape of a startable endpoint
---------------------------------
Every endpoint ``globus-compute-endpoint`` 4.15.0 can start is a *manager*
endpoint (MEP). The classification is made by one key: ``load_config_yaml()``
pops ``engine`` and picks ``ManagerEndpointConfig`` when it is absent,
``UserEndpointConfig`` when it is present. ``start`` then refuses anything that
is not a ``ManagerEndpointConfig`` (``cli.py:899``), and the only other entry
point -- ``_start-user-endpoint`` -- reads its config from stdin and is invoked
solely by a running manager. So a ``config.yaml`` carrying a top-level
``engine:`` block cannot be started at all: that was #196.

``generate_endpoint_config()`` therefore writes the manager/template pair
upstream's own ``configure`` produces:

``config.yaml``
    Manager configuration. ``display_name`` and nothing else that matters --
    upstream's packaged ``default_config.yaml`` is the single line
    ``display_name: null``.
``user_config_template.yaml.j2``
    The ``engine:`` block, including the ``provider:`` sub-block that names this
    class. The manager renders this per user endpoint.

How Globus Compute finds this provider
--------------------------------------
Globus Compute resolves the ``provider: type:`` key by plain attribute lookup on
the ``parsl.providers`` module -- ``getattr(parsl.providers, type_name, None)``,
raising if the result is ``None``
(``globus_compute_endpoint/endpoint/config/dispatch.py``). Two consequences,
both verified against 4.15.0:

1. A dotted path can never resolve, because ``getattr`` does not walk dots. The
   type key must therefore be the bare class name.
2. The bare name only resolves if something has already assigned the class onto
   ``parsl.providers``. Importing this package does that (see
   :func:`_register_with_parsl_providers`), but nothing in the endpoint's own
   startup has a reason to import it.

The template is rendered and loaded in a *different interpreter* from the
manager: ``EndpointManager`` forks and ``os.execvpe``s ``globus-compute-endpoint
_start-user-endpoint <name>``, and that child calls ``load_config_yaml()`` on
the rendered string handed to it on stdin. So the ``config.py`` shim this
package used to write (#87) cannot help -- ``get_config()`` is never reached in
the child, and the import has to happen before its first line of user code.

The seam that does reach it is ``user_environment.yaml``, which the manager
reads and merges into the child's environment immediately before ``execvpe``
(``endpoint_manager.py:1069``). ``generate_endpoint_config()`` writes a
``PYTHONPATH`` there pointing at a ``_bootstrap/`` directory holding a
``sitecustomize.py`` whose body is ``import parsl_ephemeral_provider`` -- so the
interpreter registers the class during ``site`` initialisation, before the
config is parsed. Verified in a genuinely fresh interpreter: with the
``PYTHONPATH`` the bare name resolves, without it ``getattr`` returns ``None``.

Dotted-path support upstream (#133) would make the bootstrap unnecessary. Until
then this is what makes a generated endpoint start.

One platform caveat: ``start`` requires ``pyprctl``, which is Linux-only, so on
macOS it exits "multi-user endpoints are not supported on this system" before
reading any configuration. Generation works anywhere; running an endpoint needs
Linux, and that is true of every 4.15.0 endpoint, not just these.

Minimum IAM permissions
-----------------------
:meth:`EphemeralComputeProvider.minimum_iam_policy` returns these as a policy
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
    ssm:PutParameter, ssm:DeleteParameter, ssm:DeleteParameters
    (``state_store_type="parameter_store"``)

    No Session Manager grants. ssm:StartSession, TerminateSession,
    ResumeSession, DescribeSessions and GetConnectionStatus were listed here for
    "Session Manager tunnels", but no such transport exists in this package and
    nothing calls them (#195).

STS (always required)
    sts:GetCallerIdentity -- ``create_session()`` verifies every session with it,
    so this is the first AWS call the provider makes.

EventBridge + SQS (required when use_spot and spot_interruption_handling)
    events:PutRule, events:PutTargets, events:RemoveTargets,
    events:DeleteRule, events:TagResource, sqs:CreateQueue,
    sqs:GetQueueAttributes, sqs:SetQueueAttributes, sqs:ReceiveMessage,
    sqs:DeleteMessage, sqs:DeleteQueue

IAM (required when auto_create_instance_profile=True)
    iam:CreateRole, iam:GetRole, iam:AttachRolePolicy,
    iam:CreateInstanceProfile, iam:GetInstanceProfile,
    iam:AddRoleToInstanceProfile, iam:PassRole,
    iam:RemoveRoleFromInstanceProfile, iam:DeleteInstanceProfile,
    iam:ListAttachedRolePolicies, iam:DetachRolePolicy,
    iam:ListRolePolicies, iam:DeleteRolePolicy, iam:DeleteRole

    The teardown half is required, not optional: ``cleanup_infrastructure()``
    deletes the pair it created (#132), and cleanup logs rather than raises, so
    a missing delete grant leaks a standing privileged principal silently
    (#195).

ECR (only when container_image references an ECR repository)
    ecr:GetAuthorizationToken, ecr:BatchGetImage,
    ecr:GetDownloadUrlForLayer, ecr:BatchCheckLayerAvailability

Not covered by ``minimum_iam_policy()``: ``state_store_type="s3"`` (needs S3
object access), ``mode="detached"`` (CloudFormation stack management), and
``mode="serverless"`` (Lambda, ECS, CloudWatch Logs, and IAM role lifecycle).
The Parameter Store backend *is* covered now -- it was listed here as uncovered
while the policy also omitted the actions, so the omission read as deliberate
(#195).

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

from parsl_ephemeral_provider.provider import EphemeralProvider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# YAML template helpers
# ---------------------------------------------------------------------------

# Bare class name, not a dotted path: Globus Compute's ProviderDispatcher does
# `getattr(parsl.providers, type_name, None)`, and getattr does not walk dots
# (#87). The name resolves because _register_with_parsl_providers() below puts
# the class there.
_PROVIDER_TYPE = "EphemeralComputeProvider"

# Indentation helpers for hand-rolled YAML (avoids a PyYAML import at module
# level and keeps the output human-readable with predictable ordering).
_INDENT2 = "  "
_INDENT4 = "    "

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
# reconstructed provider falls back to EphemeralProvider's parsl-only
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
# worker_init puts it there, and EphemeralProvider's DEFAULT_WORKER_INIT
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


# Directory holding the bootstrap ``sitecustomize.py``, relative to the endpoint
# directory. Named with a leading underscore so it sorts away from the four
# configuration files a site administrator actually edits.
_BOOTSTRAP_DIRNAME = "_bootstrap"

# Placed on the user endpoint's PYTHONPATH via ``user_environment.yaml``. Python
# imports ``sitecustomize`` during ``site`` initialisation, so this runs before
# the first line of ``globus-compute-endpoint``'s own code -- which is the only
# window there is: the user endpoint process receives its configuration on stdin
# and parses it immediately.
_SITECUSTOMIZE = '''\
"""Import hook generated by parsl-ephemeral-provider. Do not edit.

The user endpoint process is a fresh interpreter (the manager forks and
``execvpe``s it), and it resolves the ``provider: type:`` key in the rendered
template by ``getattr(parsl.providers, "EphemeralComputeProvider", None)``. Nothing
in that process imports this package, so the attribute is absent and the lookup
fails with "is not a valid provider" (#196).

Python imports ``sitecustomize`` automatically during ``site`` initialisation
if it is anywhere on ``sys.path``. ``user_environment.yaml`` puts this
directory there, so the registration happens before the configuration is
parsed. Delete this file only together with that PYTHONPATH entry.
"""

try:
    import parsl_ephemeral_provider  # noqa: F401  registers EphemeralComputeProvider
except Exception as exc:  # pragma: no cover - diagnostic path
    # A bare traceback here would be attributed to the interpreter rather than
    # to the endpoint, so say where it came from. Not re-raised: breaking every
    # interpreter that happens to inherit this PYTHONPATH would be worse than
    # the "not a valid provider" error that follows.
    import sys

    print(
        "parsl-ephemeral-provider: could not import parsl_ephemeral_provider"
        f" ({type(exc).__name__}: {exc}). The endpoint will fail to resolve"
        " EphemeralComputeProvider. Is the package installed in this interpreter?",
        file=sys.stderr,
    )
'''


def _register_with_parsl_providers() -> None:
    """Make ``EphemeralComputeProvider`` resolvable from a Globus Compute config.

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
    setattr(parsl.providers, "EphemeralComputeProvider", EphemeralComputeProvider)
    exported = getattr(parsl.providers, "__all__", None)
    if isinstance(exported, list) and "EphemeralComputeProvider" not in exported:
        exported.append("EphemeralComputeProvider")


# ---------------------------------------------------------------------------
# EphemeralComputeProvider
# ---------------------------------------------------------------------------


class EphemeralComputeProvider(EphemeralProvider):
    """Globus Compute-aware wrapper around ``EphemeralProvider``.

    Extends ``EphemeralProvider`` with Globus Compute endpoint metadata
    and a helper that generates a ready-to-start endpoint directory for the
    ``globus-compute-endpoint`` daemon.

    All ``EphemeralProvider`` constructor parameters are accepted as-is
    (forwarded via ``**kwargs``).

    Parameters
    ----------
    endpoint_id : str, optional
        Globus Compute endpoint UUID. Optional, and it is not a configuration
        key -- Globus Compute's ``BaseConfig`` rejects ``endpoint_id``, and the
        UUID lives in ``endpoint.json`` written during registration. When set,
        the generated ``config.yaml`` records it as a comment together with the
        ``--endpoint-uuid`` invocation that adopts it; when unset, ``start``
        registers the endpoint and assigns one.
    container_image : str, optional
        Container image URI to run Parsl workers inside a container.
        Examples: ``"python:3.11-slim"``, ``"123456789.dkr.ecr.us-east-1.amazonaws.com/my-image:latest"``.
        When set, the generated ``user_config_template.yaml.j2`` includes
        ``container_type: docker`` and ``container_uri: <image>`` under the
        ``engine`` block.
    display_name : str, optional
        Human-readable name for the Globus Compute endpoint.
        Default is ``"Ephemeral AWS Endpoint"``.
    encrypted : bool, optional
        Whether the engine encrypts worker traffic with CurveZMQ. Default is
        ``False``, and that default is deliberate: ``HighThroughputExecutor``
        generates the certificates under its own ``run_dir`` on the *endpoint
        host* and passes that path to workers as ``--cert_dir``, so an EC2
        worker is handed a directory that does not exist on it and dies with
        ``FileNotFoundError``. So ``True`` alone does not work for remote
        workers; the default relies on VPC isolation instead. This was hardcoded
        ``true`` before #138, which made every generated config unusable.

        To run encrypted, pass ``encrypted=True`` **and**
        ``distribute_certificates=True``, which ships the certificates to each
        worker through Parameter Store (#62). That includes the endpoint's server
        secret key, which is inherent to Parsl's file layout rather than a choice
        -- see ``security/curvezmq.py``. It is standard mode only and needs an
        instance profile.

        High-Assurance endpoints have no choice in the matter:
        ``assert_ha_compliant()`` rejects ``encrypted=False``, so they need both
        flags set.
    \\*\\*kwargs
        All keyword arguments accepted by ``EphemeralProvider``.
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
        """Write a startable Globus Compute endpoint configuration to *path*.

        Creates the directory at *path* if it does not exist, then writes four
        files into it:

        ``config.yaml``
            Manager configuration: ``display_name``, and nothing else.
            Deliberately thin -- upstream's own packaged default is the single
            line ``display_name: null``.
        ``user_config_template.yaml.j2``
            The ``engine:`` block and its ``provider:`` sub-block, which is
            where all the AWS configuration lives. The file to edit.
        ``user_environment.yaml``
            A ``PYTHONPATH`` pointing at ``_bootstrap/``.
        ``_bootstrap/sitecustomize.py``
            ``import parsl_ephemeral_provider``, which registers
            ``EphemeralComputeProvider`` on ``parsl.providers``.

        The split between the first two is not cosmetic: an ``engine:`` key in
        ``config.yaml`` makes ``load_config_yaml()`` return a
        ``UserEndpointConfig``, and ``start`` rejects anything that is not a
        ``ManagerEndpointConfig`` -- so the previous single-file output could
        never be started (#196). The last two exist because the process that
        loads the template is a fresh interpreter that never imports this
        package; see the module docstring.

        Returns the absolute path to ``user_config_template.yaml.j2`` -- the
        file a caller would want to read or edit, and the one holding everything
        this class configures.

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
            Absolute path to the written ``user_config_template.yaml.j2``.
        """
        endpoint_dir = Path(os.path.expanduser(path)).resolve()
        endpoint_dir.mkdir(parents=True, exist_ok=True)

        manager_path = endpoint_dir / "config.yaml"
        manager_path.write_text(self._build_manager_config_yaml(), encoding="utf-8")

        template_path = endpoint_dir / "user_config_template.yaml.j2"
        template_path.write_text(self._build_user_config_template(), encoding="utf-8")

        bootstrap_dir = endpoint_dir / _BOOTSTRAP_DIRNAME
        bootstrap_dir.mkdir(exist_ok=True)
        (bootstrap_dir / "sitecustomize.py").write_text(
            _SITECUSTOMIZE, encoding="utf-8"
        )

        env_path = endpoint_dir / "user_environment.yaml"
        env_path.write_text(
            self._build_user_environment_yaml(bootstrap_dir), encoding="utf-8"
        )

        # A config.py left over from a pre-#196 generation would win over
        # config.yaml in get_config() and reinstate the unstartable shape, so
        # remove it rather than leaving the directory in a state where the fix
        # is present but inert.
        legacy_shim = endpoint_dir / "config.py"
        if legacy_shim.exists():
            legacy_shim.unlink()
            logger.info(
                "Removed the pre-#196 config.py shim at %s: get_config() prefers it"
                " over config.yaml, so leaving it would keep loading the"
                " single-file layout that `start` rejects",
                legacy_shim,
            )

        logger.info(
            "Globus Compute endpoint config written to %s"
            " (manager %s, environment %s, bootstrap %s)",
            template_path,
            manager_path,
            env_path,
            bootstrap_dir,
        )
        return str(template_path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_manager_config_yaml(self) -> str:
        """Render the manager ``config.yaml`` content as a string.

        Thin by requirement, not by choice. ``load_config_yaml()`` decides which
        config class to build from one thing -- whether an ``engine`` key is
        present -- and ``start`` accepts only ``ManagerEndpointConfig``. So
        anything engine-shaped here makes the endpoint unstartable (#196), and
        the keys that remain are the handful ``BaseConfig`` and
        ``ManagerEndpointConfig`` accept.

        ``endpoint_id`` is *not* among them: ``BaseConfig`` rejects it as an
        unexpected keyword argument. Globus Compute keeps the endpoint UUID in
        ``endpoint.json``, written during registration, so it is emitted as a
        comment and as ``--endpoint-uuid`` guidance instead.

        Neither is ``user_config_template_path``, though it *is* accepted. Its
        setter resolves the value against the process working directory and
        raises ``ValueError`` if the result does not exist, so a relative path
        breaks whenever the daemon is started from anywhere but the endpoint
        directory -- and an absolute one hard-codes the generating machine's
        layout. Omitting it leaves
        ``Endpoint.user_config_template_path()`` to fall back to
        ``<endpoint_dir>/user_config_template.yaml.j2``, which is where the
        template is written and how upstream's own ``configure`` leaves it.
        """
        lines: list[str] = []

        lines.append(
            "# Globus Compute *manager* endpoint configuration, generated by"
            " EphemeralComputeProvider."
        )
        lines.append(
            "# The AWS configuration is in user_config_template.yaml.j2 -- edit that."
        )
        lines.append("# Then:")
        lines.append("#   globus-compute-endpoint start <endpoint-name>")
        lines.append("")

        lines.append(_yaml_line("display_name", self.display_name))

        lines.append("")
        lines.append(
            "# The engine and provider configuration is in"
            " user_config_template.yaml.j2,"
        )
        lines.append(
            "# alongside this file. There is deliberately no"
            " `user_config_template_path`"
        )
        lines.append(
            "# key: it is resolved against the working directory, so it breaks when the"
        )
        lines.append(
            "# daemon starts from elsewhere. The default location is this directory."
        )

        lines.append("")
        if self.endpoint_id:
            lines.append(
                "# This endpoint's UUID. It is not a config key -- BaseConfig rejects"
            )
            lines.append(
                "# `endpoint_id` -- so pass it on the command line the first time:"
            )
            lines.append(
                f"#   globus-compute-endpoint start <name> --endpoint-uuid"
                f" {self.endpoint_id}"
            )
            lines.append(
                "# After that it is recorded in endpoint.json and read from there."
            )
        else:
            lines.append(
                "# No endpoint UUID was supplied. `start` registers the endpoint and"
            )
            lines.append(
                "# writes the UUID it is assigned to endpoint.json, so there is nothing"
            )
            lines.append(
                "# to do here; `globus-compute-endpoint list` will show it afterwards."
            )

        lines.append("")  # trailing newline
        return "\n".join(lines)

    def _build_user_config_template(self) -> str:
        """Render ``user_config_template.yaml.j2`` as a string.

        This is where the ``engine:`` block lives, and with it everything this
        class configures. The manager renders it per user endpoint and hands the
        result to a forked-and-``execvpe``d child, which parses it with
        ``load_config_yaml()``.

        It is a Jinja template by extension and by upstream contract, but this
        one contains no Jinja tags: every value is fixed at generation time from
        the provider's own configuration. A site that wants per-user variation
        can add ``{{ ... }}`` placeholders by hand, which is why the file is
        written as a template rather than the ``.yaml`` fallback
        ``load_user_config_template()`` also accepts.
        """
        lines: list[str] = []

        lines.append(
            "# User endpoint configuration template, generated by EphemeralComputeProvider."
        )
        lines.append("# Edit this file to customise the endpoint, then run:")
        lines.append("#   globus-compute-endpoint start <endpoint-name>")
        lines.append("#")
        lines.append(
            "# The engine block lives here rather than in config.yaml because an"
            " `engine:`"
        )
        lines.append(
            "# key there makes the endpoint a *user* endpoint config, which `start`"
        )
        lines.append("# refuses to run (#196).")
        lines.append("")

        lines.append("engine:")
        lines.append(f"{_INDENT2}type: GlobusComputeEngine")
        if not self.encrypted:
            lines.append(
                f"{_INDENT2}# CurveZMQ certificates live in the endpoint host's run_dir,"
                " which an EC2"
            )
            lines.append(
                f"{_INDENT2}# worker cannot read. To set this true, also set"
                " distribute_certificates:"
            )
            lines.append(
                f"{_INDENT2}# true on the provider below -- it ships the certificates"
                " through Parameter"
            )
            lines.append(
                f"{_INDENT2}# Store, including the endpoint's server secret key (#62)."
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

        lines.append("")  # trailing newline
        return "\n".join(lines)

    def _build_user_environment_yaml(self, bootstrap_dir: Path) -> str:
        """Render ``user_environment.yaml``, whose only job is ``PYTHONPATH``.

        The manager reads this file and merges it into the child's environment
        just before ``execvpe`` (``endpoint_manager.py:1069``), which makes it
        the one seam that reaches the interpreter where the template is parsed.
        Pointing ``PYTHONPATH`` at a directory holding ``sitecustomize.py`` gets
        this package imported during ``site`` initialisation, so
        ``EphemeralComputeProvider`` is on ``parsl.providers`` before the first
        ``getattr`` looks for it.

        An absolute path, because the child ``chdir``s to the mapped user's home
        (or ``/``) before exec.
        """
        lines: list[str] = []

        lines.append(
            "# Environment variables injected into user endpoint processes,"
            " generated by"
        )
        lines.append("# EphemeralComputeProvider.")
        lines.append("#")
        lines.append(
            "# PYTHONPATH is load-bearing: the user endpoint process is a fresh"
        )
        lines.append(
            "# interpreter that resolves `provider: type: EphemeralComputeProvider` with"
        )
        lines.append(
            "# getattr(parsl.providers, ...), and nothing in it imports this package."
        )
        lines.append(
            f"# {_BOOTSTRAP_DIRNAME}/sitecustomize.py does that import, and Python runs"
            " it during"
        )
        lines.append("# site initialisation. Remove this and the endpoint fails with")
        lines.append('# "EphemeralComputeProvider is not a valid provider" (#196).')
        lines.append("#")
        lines.append(
            "# To add your own variables, append them here; anything set overrides the"
        )
        lines.append(
            "# defaults, so keep this directory on PYTHONPATH if you extend it."
        )
        lines.append("")
        lines.append(_yaml_line("PYTHONPATH", str(bootstrap_dir)))
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
        signature = inspect.signature(EphemeralProvider.__init__)

        for name, parameter in signature.parameters.items():
            if name in _SKIP_PARAMS or parameter.kind is parameter.VAR_KEYWORD:
                continue
            if name not in _ALWAYS_EMIT and name not in self._explicit_params:
                continue

            attribute = _ATTRIBUTE_OVERRIDES.get(name, name)
            if not hasattr(self, attribute):  # pragma: no cover - defensive
                continue

            lines.append(_yaml_line(name, getattr(self, attribute), indent=_INDENT4))

        # These two are EphemeralComputeProvider's own, so the signature loop above
        # cannot see them -- it reads EphemeralProvider.__init__. Both are real
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

        Scope: the ``mode="standard"`` path, which is what a generated endpoint
        config uses, with either file-backed or Parameter Store state. Actions
        were derived from the package's actual API calls, so the set is narrower
        than it was before v0.7.0 -- network resources are caller-supplied since
        #69, so no VPC/subnet/security-group/NAT/gateway create or delete grant
        appears, and Spot Fleet was replaced by EC2 Fleet in #86. See the module
        docstring for what is deliberately *not* covered (the S3 state backend,
        and detached and serverless modes).

        Every action here has a call site in the package, and the teardown
        actions matter as much as the create ones: cleanup logs rather than
        raises, so a missing delete permission leaks resources silently. That is
        what #195 found -- this method granted ``iam:CreateRole`` and
        ``iam:CreateInstanceProfile`` with no corresponding deletes, so a user on
        this policy reproduced the very leak #132 had just fixed.

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
            # state_store_type="parameter_store", which detached mode needs so
            # the client and the bastion can read the same state. Both the
            # singular and plural deletes appear because they are distinct IAM
            # actions and the code calls both: delete_parameter() per key, and
            # delete_parameters() for the batched cleanup (#195).
            "ssm:PutParameter",
            "ssm:DeleteParameter",
            "ssm:DeleteParameters",
        ]
        # Deliberately absent: ssm:StartSession, TerminateSession,
        # ResumeSession, DescribeSessions, GetConnectionStatus. These were
        # granted for "Session Manager tunnels to reach workers in a private
        # subnet", but no such transport exists in this package -- grep finds no
        # call to any of them, and the bastion is an autonomous orchestrator
        # rather than a network tunnel. Granting a session-opening permission
        # nothing uses is exactly the over-grant this method exists to avoid
        # (#195).

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

        # Only needed for auto_create_instance_profile=True.
        #
        # The deletes are load-bearing, not symmetry for its own sake. This list
        # once ended at PassRole, on the rationale that "the provider does not
        # tear the profile down (#132)". v0.8.0's #132 fix made that false: the
        # teardown in utils/aws.py runs on every cleanup_infrastructure(). And
        # because cleanup logs rather than raises, an AccessDenied there is
        # silent -- so a user on this policy leaked exactly the roles and
        # profiles #132 was filed to stop, the failure that accumulated 94
        # orphaned roles and 94 orphaned profiles in a real account. The policy
        # was quietly reverting the fix (#195).
        iam_actions = [
            "iam:CreateRole",
            "iam:GetRole",
            "iam:AttachRolePolicy",
            "iam:CreateInstanceProfile",
            "iam:GetInstanceProfile",
            "iam:AddRoleToInstanceProfile",
            "iam:PassRole",
            # Teardown, in the order IAM requires -- it rejects any other.
            "iam:RemoveRoleFromInstanceProfile",
            "iam:DeleteInstanceProfile",
            # The teardown detaches whatever is actually attached rather than
            # assuming only AmazonSSMManagedInstanceCore, since delete_role
            # refuses while any policy remains.
            "iam:ListAttachedRolePolicies",
            "iam:DetachRolePolicy",
            # Inline policies are a separate list under separate calls, and
            # delete_role refuses while one remains just as it does for a managed
            # one. The worker role only ever carries managed policies, so these
            # two are strictly speaking unexercised on this policy's scope -- but
            # the teardown is shared with the bastion role, whose permissions are
            # inline, and an AccessDenied on ListRolePolicies aborts the whole
            # teardown before DeleteRole. Granting them is what makes the shared
            # path fail closed rather than leak (#229).
            "iam:ListRolePolicies",
            "iam:DeleteRolePolicy",
            "iam:DeleteRole",
        ]

        statements = [
            {
                # First, because it is the first call the provider makes:
                # create_session() verifies the session with
                # sts:GetCallerIdentity unconditionally, on the construction
                # path. Omitting it meant a user on this exact policy failed at
                # `EphemeralProvider(...)` before reaching any AWS work
                # (#195).
                "Sid": "SessionValidation",
                "Effect": "Allow",
                "Action": ["sts:GetCallerIdentity"],
                "Resource": "*",
            },
            {
                "Sid": "EC2Management",
                "Effect": "Allow",
                "Action": ec2_actions,
                "Resource": "*",
            },
            {
                # Not "SSMTunneling", which this was called: nothing here opens
                # a tunnel. SSM is used for AMI resolution, command dispatch,
                # and the Parameter Store state backend (#195).
                "Sid": "SSMCommandsAndParameters",
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
# Compute config load: the generated ``_bootstrap/sitecustomize.py`` imports this
# package during the user endpoint interpreter's ``site`` initialisation, before
# anything parses the rendered template. Defined above the class, called here,
# because it references the class by name.
_register_with_parsl_providers()

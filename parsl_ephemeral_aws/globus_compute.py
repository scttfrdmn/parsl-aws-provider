"""Globus Compute integration for EphemeralAWSProvider.

Exposes ``GlobusComputeProvider``, a thin subclass of ``EphemeralAWSProvider``
that:

* Carries ``endpoint_id`` and ``container_image`` metadata for Globus Compute.
* Provides ``generate_endpoint_config(path)`` which writes a valid Globus Compute
  endpoint ``config.yaml`` to the given directory.

Usage::

    from parsl_ephemeral_aws import GlobusComputeProvider

    provider = GlobusComputeProvider(
        endpoint_id="<your-globus-endpoint-uuid>",
        region="us-east-1",
        instance_type="t3.medium",
        mode="standard",
        use_spot=True,
        auto_create_instance_profile=True,
        display_name="My Ephemeral AWS Endpoint",
    )
    provider.generate_endpoint_config("~/.globus_compute/my_aws_endpoint")

Minimum IAM permissions
-----------------------
The role attached to EC2 instances (``auto_create_instance_profile=True`` or a
manually specified ``iam_instance_profile_arn``) must include:

EC2 (always required)
    ec2:RunInstances, ec2:DescribeInstances, ec2:TerminateInstances,
    ec2:CreateVpc, ec2:DescribeVpcs, ec2:DeleteVpc,
    ec2:CreateSubnet, ec2:DescribeSubnets, ec2:DeleteSubnet,
    ec2:CreateSecurityGroup, ec2:DescribeSecurityGroups,
    ec2:DeleteSecurityGroup, ec2:AuthorizeSecurityGroupIngress,
    ec2:CreateInternetGateway, ec2:AttachInternetGateway,
    ec2:DetachInternetGateway, ec2:DeleteInternetGateway,
    ec2:CreateRouteTable, ec2:AssociateRouteTable, ec2:CreateRoute,
    ec2:DescribeRouteTables, ec2:DeleteRouteTable,
    ec2:CreateTags, ec2:DescribeTags,
    ec2:DescribeImages, ec2:DescribeInstanceTypes,
    ec2:DescribeSpotPriceHistory, ec2:RequestSpotFleet,
    ec2:DescribeSpotFleetRequests, ec2:CancelSpotFleetRequests

SSM (required for Session Manager tunneling)
    ssm:StartSession, ssm:TerminateSession, ssm:DescribeSessions,
    ssm:GetConnectionStatus, ssm:ResumeSession,
    ssm:SendCommand, ssm:ListCommandInvocations

IAM (required when auto_create_instance_profile=True)
    iam:CreateRole, iam:AttachRolePolicy, iam:CreateInstanceProfile,
    iam:AddRoleToInstanceProfile, iam:PassRole,
    iam:GetRole, iam:GetInstanceProfile

ECR (only when container_image references an ECR repository)
    ecr:GetAuthorizationToken, ecr:BatchGetImage,
    ecr:GetDownloadUrlForLayer

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from parsl_ephemeral_aws.provider import EphemeralAWSProvider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# YAML template helpers
# ---------------------------------------------------------------------------

_PROVIDER_TYPE = "parsl_ephemeral_aws.globus_compute.GlobusComputeProvider"

# Indentation helpers for hand-rolled YAML (avoids a PyYAML import at module
# level and keeps the output human-readable with predictable ordering).
_INDENT2 = "  "
_INDENT4 = "    "
_INDENT6 = "      "


def _yaml_str(value: str) -> str:
    """Quote a string value if it contains YAML-special characters.

    A bare colon is only a YAML mapping indicator when followed by a space or
    at end of line (e.g. ``"key: value"`` or trailing ``":"``).  Simple values
    such as Docker image tags (``python:3.11-slim``) do not need quoting.
    """
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
    if isinstance(value, str):
        return f"{indent}{key}: {_yaml_str(value)}"
    return f"{indent}{key}: {value}"


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
    **kwargs
        All keyword arguments accepted by ``EphemeralAWSProvider``.
    """

    label = "globus_compute_aws"

    def __init__(
        self,
        endpoint_id: Optional[str] = None,
        container_image: Optional[str] = None,
        display_name: str = "Ephemeral AWS Endpoint",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.endpoint_id: Optional[str] = endpoint_id
        self.container_image: Optional[str] = container_image
        self.display_name: str = display_name

    # ------------------------------------------------------------------
    # Config generation
    # ------------------------------------------------------------------

    def generate_endpoint_config(self, path: str) -> str:
        """Write a Globus Compute endpoint ``config.yaml`` to *path*.

        Creates the directory at *path* if it does not exist, then writes
        ``config.yaml`` into it.  Returns the absolute path to the written
        file.

        The generated file is suitable for use with the ``globus-compute-endpoint``
        daemon::

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

        logger.info("Globus Compute endpoint config written to %s", config_path)
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
        lines.append(f"{_INDENT2}encrypted: true")
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
        """Return YAML lines (with 4-space indent) for provider parameters."""
        lines: list[str] = []

        # Core compute parameters
        lines.append(_yaml_line("region", self.region, indent=_INDENT4))
        lines.append(_yaml_line("instance_type", self.instance_type, indent=_INDENT4))
        lines.append(_yaml_line("mode", self.mode_type.value, indent=_INDENT4))

        # Block sizing
        lines.append(_yaml_line("min_blocks", self.min_blocks, indent=_INDENT4))
        lines.append(_yaml_line("max_blocks", self.max_blocks, indent=_INDENT4))

        # Spot
        lines.append(_yaml_line("use_spot", self.use_spot, indent=_INDENT4))
        if self.use_spot:
            lines.append(
                _yaml_line(
                    "spot_interruption_handling",
                    self.spot_interruption_handling,
                    indent=_INDENT4,
                )
            )

        # IAM / connectivity
        lines.append(
            _yaml_line(
                "auto_create_instance_profile",
                self.auto_create_instance_profile,
                indent=_INDENT4,
            )
        )
        if self.iam_instance_profile_arn:
            lines.append(
                _yaml_line(
                    "iam_instance_profile_arn",
                    self.iam_instance_profile_arn,
                    indent=_INDENT4,
                )
            )

        # Container image forwarded to provider so workers can pull it
        if self.container_image:
            lines.append(
                _yaml_line("container_image", self.container_image, indent=_INDENT4)
            )

        # Tuning
        lines.append(
            _yaml_line(
                "status_polling_interval",
                self.status_polling_interval,
                indent=_INDENT4,
            )
        )
        lines.append(_yaml_line("waiter_delay", self.waiter_delay, indent=_INDENT4))
        lines.append(
            _yaml_line("waiter_max_attempts", self.waiter_max_attempts, indent=_INDENT4)
        )

        # Optional endpoint_id metadata
        if self.endpoint_id:
            lines.append(_yaml_line("endpoint_id", self.endpoint_id, indent=_INDENT4))

        return lines

    # ------------------------------------------------------------------
    # Minimum IAM policy document
    # ------------------------------------------------------------------

    @staticmethod
    def minimum_iam_policy(include_ecr: bool = False) -> Dict[str, Any]:
        """Return the minimum IAM policy document as a Python dict.

        The returned dict can be serialised to JSON and attached to an IAM
        role or user to grant the least privileges needed by this provider.

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
            "ec2:RunInstances",
            "ec2:DescribeInstances",
            "ec2:TerminateInstances",
            "ec2:CreateVpc",
            "ec2:DescribeVpcs",
            "ec2:DeleteVpc",
            "ec2:CreateSubnet",
            "ec2:DescribeSubnets",
            "ec2:DeleteSubnet",
            "ec2:CreateSecurityGroup",
            "ec2:DescribeSecurityGroups",
            "ec2:DeleteSecurityGroup",
            "ec2:AuthorizeSecurityGroupIngress",
            "ec2:RevokeSecurityGroupIngress",
            "ec2:CreateInternetGateway",
            "ec2:AttachInternetGateway",
            "ec2:DetachInternetGateway",
            "ec2:DeleteInternetGateway",
            "ec2:CreateRouteTable",
            "ec2:AssociateRouteTable",
            "ec2:DisassociateRouteTable",
            "ec2:CreateRoute",
            "ec2:DescribeRouteTables",
            "ec2:DeleteRouteTable",
            "ec2:CreateTags",
            "ec2:DescribeTags",
            "ec2:DescribeImages",
            "ec2:DescribeInstanceTypes",
            "ec2:DescribeSpotPriceHistory",
            "ec2:RequestSpotFleet",
            "ec2:DescribeSpotFleetRequests",
            "ec2:CancelSpotFleetRequests",
            "ec2:AllocateAddress",
            "ec2:ReleaseAddress",
            "ec2:DescribeAddresses",
            "ec2:CreateNatGateway",
            "ec2:DeleteNatGateway",
            "ec2:DescribeNatGateways",
            "ec2:DescribeNetworkInterfaces",
            "ec2:DeleteNetworkInterface",
        ]

        ssm_actions = [
            "ssm:StartSession",
            "ssm:TerminateSession",
            "ssm:DescribeSessions",
            "ssm:GetConnectionStatus",
            "ssm:ResumeSession",
            "ssm:SendCommand",
            "ssm:ListCommandInvocations",
            "ssm:DescribeInstanceInformation",
        ]

        iam_actions = [
            "iam:CreateRole",
            "iam:AttachRolePolicy",
            "iam:GetRole",
            "iam:CreateInstanceProfile",
            "iam:AddRoleToInstanceProfile",
            "iam:GetInstanceProfile",
            "iam:PassRole",
            "iam:DeleteRole",
            "iam:DetachRolePolicy",
            "iam:DeleteInstanceProfile",
            "iam:RemoveRoleFromInstanceProfile",
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

"""Parsl Ephemeral AWS Provider.

A modern, flexible AWS provider for the Parsl parallel scripting library that leverages
ephemeral resources for cost-effective, scalable scientific computation.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

__version__ = "0.6.0"

from .globus_compute import GlobusComputeProvider
from .provider import EphemeralAWSProvider

__all__ = ["EphemeralAWSProvider", "GlobusComputeProvider"]

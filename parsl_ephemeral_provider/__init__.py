"""Parsl Ephemeral Provider for AWS.

An independent Parsl provider for running ephemeral compute on AWS -- EC2, EC2
Fleet, Lambda, or Fargate -- scaled from zero and torn down when the work is
done.

The distribution, this import package, and the CLI carry no AWS mark: the AWS
Trademark Guidelines (s7) forbid combining or hyphenating a mark into a product
name, while s13 permits plain-text factual reference in the relational form used
above. See NOTICE.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

__version__ = "0.9.0"

from .globus_compute import EphemeralComputeProvider
from .provider import EphemeralProvider

__all__ = ["EphemeralProvider", "EphemeralComputeProvider"]

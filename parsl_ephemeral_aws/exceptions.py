"""Custom exceptions for Parsl Ephemeral AWS Provider.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""


class EphemeralAWSError(Exception):
    """Base exception for all Ephemeral AWS Provider errors."""
    pass


class ResourceCreationError(EphemeralAWSError):
    """Error creating AWS resources."""
    pass


class ResourceCleanupError(EphemeralAWSError):
    """Error cleaning up AWS resources."""
    pass


class StateError(EphemeralAWSError):
    """Error handling provider state."""
    pass


class ConfigurationError(EphemeralAWSError):
    """Error in provider configuration."""
    pass


class NetworkingError(EphemeralAWSError):
    """Error in network configuration or connectivity."""
    pass


class WorkerInitializationError(EphemeralAWSError):
    """Error initializing worker."""
    pass


class JobSubmissionError(EphemeralAWSError):
    """Error submitting job."""
    pass


class SpotInterruptionError(EphemeralAWSError):
    """Error related to spot instance interruption."""
    pass
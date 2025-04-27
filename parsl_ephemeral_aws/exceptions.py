"""
Custom exceptions for the EphemeralAWSProvider.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""


class EphemeralAWSError(Exception):
    """Base class for all EphemeralAWSProvider exceptions."""

    pass


class ProviderError(EphemeralAWSError):
    """General provider error."""

    pass


class ProviderConfigurationError(ProviderError):
    """Error in provider configuration."""

    pass


class AWSConnectionError(ProviderError):
    """Error connecting to AWS services."""

    pass


class AWSAuthenticationError(AWSConnectionError):
    """AWS authentication failure."""

    pass


class ResourceCreationError(ProviderError):
    """Error creating AWS resources."""

    pass


class ResourceDeletionError(ProviderError):
    """Error deleting AWS resources."""

    pass


class ResourceNotFoundError(ProviderError):
    """Requested AWS resource not found."""

    pass


class StateStoreError(ProviderError):
    """Error in state store operations."""

    pass


class StateSerializationError(StateStoreError):
    """Error serializing state data."""

    pass


class StateDeserializationError(StateStoreError):
    """Error deserializing state data."""

    pass


class JobExecutionError(ProviderError):
    """Error executing a job."""

    pass


class JobCancellationError(ProviderError):
    """Error cancelling a job."""

    pass


class NetworkCreationError(ResourceCreationError):
    """Error creating network resources."""

    pass


class SecurityGroupError(ResourceCreationError):
    """Error managing security groups."""

    pass


class EC2InstanceError(ResourceCreationError):
    """Error managing EC2 instances."""

    pass


class LambdaFunctionError(ResourceCreationError):
    """Error managing Lambda functions."""

    pass


class ECSTaskError(ResourceCreationError):
    """Error managing ECS tasks."""

    pass


class BastionHostError(ResourceCreationError):
    """Error managing bastion hosts."""

    pass


class SpotInstanceError(EC2InstanceError):
    """Error managing spot instances."""

    pass


class SpotFleetError(EC2InstanceError):
    """Error managing EC2 Spot Fleet."""

    pass


class SpotFleetRequestError(SpotFleetError):
    """Error creating or managing Spot Fleet requests."""

    pass


class SpotFleetThrottlingError(SpotFleetError):
    """AWS API throttling for Spot Fleet operations."""
    
    def __init__(self, message="AWS Spot Fleet API request was throttled", operation=None, retry_after=None):
        self.operation = operation
        self.retry_after = retry_after
        super().__init__(message)


class OperatingModeError(ProviderError):
    """Error in operating mode functionality."""

    pass


class CloudFormationError(ResourceCreationError):
    """Error in CloudFormation stack operations."""

    pass


class TaskTimeoutError(JobExecutionError):
    """Task execution timeout."""

    pass


class InvalidStateError(ProviderError):
    """Provider is in an invalid state for the requested operation."""

    pass


class TaggingError(ProviderError):
    """Error tagging AWS resources."""

    pass


class CleanupError(ProviderError):
    """Error cleaning up resources."""

    pass


class AMINotFoundError(ResourceCreationError):
    """Specified AMI not found."""

    pass
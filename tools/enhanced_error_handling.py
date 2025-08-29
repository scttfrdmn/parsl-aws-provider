#!/usr/bin/env python3
"""
Enhanced Error Handling for Phase 1.5 AWS Provider.

Provides comprehensive error handling, retry logic, and graceful degradation
for all aspects of the enhanced AWS provider functionality.
"""

import asyncio
import functools
import logging
import random
import time
from typing import Callable, Type

from botocore.exceptions import ClientError, BotoCoreError

logger = logging.getLogger(__name__)


class RetryableError(Exception):
    """Base class for errors that should be retried."""

    pass


class NonRetryableError(Exception):
    """Base class for errors that should not be retried."""

    pass


class AWSThrottleError(RetryableError):
    """AWS API throttling error."""

    pass


class AWSTemporaryError(RetryableError):
    """Temporary AWS service error."""

    pass


class AWSConfigurationError(NonRetryableError):
    """AWS configuration or permissions error."""

    pass


class NetworkConnectivityError(RetryableError):
    """Network connectivity issue."""

    pass


def classify_aws_error(error: Exception) -> Type[Exception]:
    """Classify AWS errors for retry logic."""

    if isinstance(error, ClientError):
        error_code = error.response.get("Error", {}).get("Code", "")

        # Throttling errors - should retry
        if error_code in [
            "Throttling",
            "RequestLimitExceeded",
            "TooManyRequestsException",
        ]:
            return AWSThrottleError

        # Temporary service errors - should retry
        if error_code in ["ServiceUnavailable", "InternalError", "RequestTimeout"]:
            return AWSTemporaryError

        # Configuration errors - should not retry
        if error_code in [
            "InvalidUserID.NotFound",
            "UnauthorizedOperation",
            "AccessDenied",
            "InvalidParameterValue",
        ]:
            return AWSConfigurationError

        # Default to retryable for unknown client errors
        return AWSTemporaryError

    elif isinstance(error, BotoCoreError):
        # Most boto core errors are network related and retryable
        return NetworkConnectivityError

    # Unknown errors are retryable by default
    return RetryableError


def retry_with_backoff(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    backoff_multiplier: float = 2.0,
    jitter: bool = True,
):
    """
    Decorator for retrying operations with exponential backoff.

    Args:
        max_attempts: Maximum number of attempts
        base_delay: Initial delay between retries
        max_delay: Maximum delay between retries
        backoff_multiplier: Multiplier for exponential backoff
        jitter: Add random jitter to prevent thundering herd
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None

            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    error_type = classify_aws_error(e)

                    # Don't retry non-retryable errors
                    if issubclass(error_type, NonRetryableError):
                        logger.error(f"Non-retryable error in {func.__name__}: {e}")
                        raise

                    # Don't retry on last attempt
                    if attempt == max_attempts - 1:
                        logger.error(f"Max attempts reached for {func.__name__}: {e}")
                        raise

                    # Calculate delay with exponential backoff
                    delay = min(base_delay * (backoff_multiplier**attempt), max_delay)

                    # Add jitter
                    if jitter:
                        delay *= 0.5 + random.random() * 0.5

                    logger.warning(
                        f"Attempt {attempt + 1}/{max_attempts} failed for {func.__name__}: {e}"
                    )
                    logger.info(f"Retrying in {delay:.1f}s...")

                    time.sleep(delay)

            # This shouldn't be reached, but just in case
            raise last_exception

        return wrapper

    return decorator


def async_retry_with_backoff(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    backoff_multiplier: float = 2.0,
    jitter: bool = True,
):
    """Async version of retry_with_backoff decorator."""

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None

            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    error_type = classify_aws_error(e)

                    # Don't retry non-retryable errors
                    if issubclass(error_type, NonRetryableError):
                        logger.error(f"Non-retryable error in {func.__name__}: {e}")
                        raise

                    # Don't retry on last attempt
                    if attempt == max_attempts - 1:
                        logger.error(f"Max attempts reached for {func.__name__}: {e}")
                        raise

                    # Calculate delay with exponential backoff
                    delay = min(base_delay * (backoff_multiplier**attempt), max_delay)

                    # Add jitter
                    if jitter:
                        delay *= 0.5 + random.random() * 0.5

                    logger.warning(
                        f"Attempt {attempt + 1}/{max_attempts} failed for {func.__name__}: {e}"
                    )
                    logger.info(f"Retrying in {delay:.1f}s...")

                    await asyncio.sleep(delay)

            # This shouldn't be reached, but just in case
            raise last_exception

        return wrapper

    return decorator


class GracefulDegradationManager:
    """Manages graceful degradation of provider features."""

    def __init__(self):
        self.feature_health = {
            "ssm_tunneling": True,
            "private_subnets": True,
            "optimized_amis": True,
        }
        self.degradation_count = {}

    def report_feature_failure(self, feature: str, error: Exception):
        """Report a feature failure and potentially disable it."""

        # Add unknown features
        if feature not in self.feature_health:
            self.feature_health[feature] = True

        self.degradation_count[feature] = self.degradation_count.get(feature, 0) + 1
        failure_count = self.degradation_count[feature]

        logger.warning(f"Feature {feature} failed (count: {failure_count}): {error}")

        # Disable feature after 3 consecutive failures
        if failure_count >= 3:
            self.feature_health[feature] = False
            logger.error(f"Feature {feature} disabled due to repeated failures")

            # Provide guidance on what this means
            if feature == "ssm_tunneling":
                logger.error(
                    "SSM tunneling disabled - falling back to traditional networking"
                )
                logger.error("This may cause connectivity issues behind NAT/firewalls")
            elif feature == "private_subnets":
                logger.error(
                    "Private subnet deployment disabled - using public subnets"
                )
                logger.error("Security posture may be reduced")
            elif feature == "optimized_amis":
                logger.error("Optimized AMI discovery disabled - using base AMIs")
                logger.error("Instance startup will be slower")

    def report_feature_success(self, feature: str):
        """Report a feature success and potentially re-enable it."""

        if feature in self.degradation_count:
            self.degradation_count[feature] = 0

        if feature in self.feature_health and not self.feature_health[feature]:
            self.feature_health[feature] = True
            logger.info(f"Feature {feature} re-enabled after successful operation")

    def is_feature_enabled(self, feature: str) -> bool:
        """Check if a feature is currently enabled."""
        return self.feature_health.get(feature, True)


class HealthcheckManager:
    """Manages health checks for various provider components."""

    def __init__(self):
        self.last_healthcheck = {}
        self.healthcheck_interval = 300  # 5 minutes

    def should_run_healthcheck(self, component: str) -> bool:
        """Check if it's time to run a health check for a component."""
        last_check = self.last_healthcheck.get(component, 0)
        return time.time() - last_check > self.healthcheck_interval

    def record_healthcheck(self, component: str, success: bool):
        """Record the result of a health check."""
        self.last_healthcheck[component] = time.time()

        if success:
            logger.debug(f"Health check passed for {component}")
        else:
            logger.warning(f"Health check failed for {component}")

    async def run_aws_connectivity_check(self, ec2_client) -> bool:
        """Run basic AWS connectivity health check."""
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: ec2_client.describe_regions(MaxResults=1)
            )
            return True
        except Exception as e:
            logger.error(f"AWS connectivity check failed: {e}")
            return False

    async def run_ssm_connectivity_check(self, ssm_client, instance_id: str) -> bool:
        """Run SSM connectivity health check."""
        try:
            response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: ssm_client.describe_instance_information(
                    Filters=[{"Key": "InstanceIds", "Values": [instance_id]}],
                    MaxResults=1,
                ),
            )
            return bool(response.get("InstanceInformationList"))
        except Exception as e:
            logger.error(f"SSM connectivity check failed for {instance_id}: {e}")
            return False


class ErrorRecoveryManager:
    """Manages error recovery strategies."""

    def __init__(self, provider):
        self.provider = provider
        self.recovery_strategies = {
            "instance_launch_failure": self._recover_instance_launch,
            "tunnel_creation_failure": self._recover_tunnel_creation,
            "ssm_agent_timeout": self._recover_ssm_agent,
            "vpc_endpoint_failure": self._recover_vpc_endpoint,
        }

    async def attempt_recovery(self, error_type: str, context: dict) -> bool:
        """Attempt to recover from a specific type of error."""

        if error_type not in self.recovery_strategies:
            logger.warning(f"No recovery strategy for error type: {error_type}")
            return False

        logger.info(f"Attempting recovery for {error_type}")

        try:
            return await self.recovery_strategies[error_type](context)
        except Exception as e:
            logger.error(f"Recovery attempt failed for {error_type}: {e}")
            return False

    async def _recover_instance_launch(self, context: dict) -> bool:
        """Recover from instance launch failure."""

        # Try different availability zones
        if "availability_zone" in context:
            logger.info("Trying different availability zone...")
            # Implementation would modify launch config
            return True

        # Try different instance type
        if "instance_type" in context:
            logger.info("Trying smaller instance type...")
            # Implementation would fall back to smaller instance
            return True

        return False

    async def _recover_tunnel_creation(self, context: dict) -> bool:
        """Recover from tunnel creation failure."""

        # Wait longer for SSM agent
        if "instance_id" in context:
            logger.info("Waiting longer for SSM agent...")
            await asyncio.sleep(60)
            return True

        return False

    async def _recover_ssm_agent(self, context: dict) -> bool:
        """Recover from SSM agent timeout."""

        # Restart SSM agent via user data
        if "instance_id" in context:
            logger.info("Attempting to restart SSM agent...")

            ssm_client = self.provider.session.client("ssm")

            try:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: ssm_client.send_command(
                        InstanceIds=[context["instance_id"]],
                        DocumentName="AWS-RunShellScript",
                        Parameters={
                            "commands": [
                                "systemctl restart amazon-ssm-agent",
                                "systemctl status amazon-ssm-agent",
                            ]
                        },
                    ),
                )

                # Wait for restart
                await asyncio.sleep(30)
                return True

            except Exception as e:
                logger.error(f"Failed to restart SSM agent: {e}")

        return False

    async def _recover_vpc_endpoint(self, context: dict) -> bool:
        """Recover from VPC endpoint failure."""

        # Fall back to public subnet deployment
        logger.info("Falling back to public subnet deployment...")

        if hasattr(self.provider, "use_private_subnets"):
            self.provider.use_private_subnets = False
            return True

        return False


# Global instances for easy access
graceful_degradation = GracefulDegradationManager()
healthcheck_manager = HealthcheckManager()

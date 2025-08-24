"""Enhanced error handling and recovery framework for Parsl Ephemeral AWS Provider.

This module provides robust error handling, retry mechanisms, and recovery strategies
for AWS operations and provider state management.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import logging
import time
import random
from typing import Dict, List, Optional, Callable, Any, Type, Union
from dataclasses import dataclass, field
from enum import Enum
import functools
from botocore.exceptions import ClientError, NoCredentialsError, TokenRetrievalError, BotoCoreError

logger = logging.getLogger(__name__)


class ErrorSeverity(Enum):
    """Error severity levels."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RecoveryAction(Enum):
    """Recovery actions for error handling."""
    RETRY = "retry"
    FALLBACK = "fallback"
    CLEANUP = "cleanup"
    ABORT = "abort"
    IGNORE = "ignore"


@dataclass
class RetryConfig:
    """Configuration for retry behavior."""
    
    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0
    exponential_backoff: bool = True
    jitter: bool = True
    jitter_factor: float = 0.1
    
    # Retry conditions
    retry_on_exceptions: List[Type[Exception]] = field(default_factory=lambda: [
        ClientError, BotoCoreError, ConnectionError, TimeoutError
    ])
    retry_on_status_codes: List[int] = field(default_factory=lambda: [
        429,  # Too Many Requests
        500,  # Internal Server Error
        502,  # Bad Gateway
        503,  # Service Unavailable
        504,  # Gateway Timeout
    ])
    
    def should_retry(self, exception: Exception, attempt: int) -> bool:
        """Determine if an exception should trigger a retry.
        
        Parameters
        ----------
        exception : Exception
            Exception to evaluate
        attempt : int
            Current attempt number
            
        Returns
        -------
        bool
            True if should retry
        """
        if attempt >= self.max_attempts:
            return False
        
        # Check exception type
        if any(isinstance(exception, exc_type) for exc_type in self.retry_on_exceptions):
            # For ClientError, check status code
            if isinstance(exception, ClientError):
                error_code = exception.response.get('Error', {}).get('Code', '')
                status_code = exception.response.get('ResponseMetadata', {}).get('HTTPStatusCode', 0)
                
                # Don't retry on certain error codes
                non_retryable_codes = [
                    'AccessDenied',
                    'InvalidUserID.NotFound',
                    'InvalidGroup.NotFound',
                    'UnauthorizedOperation',
                    'ValidationException',
                    'InvalidParameterValue',
                ]
                
                if error_code in non_retryable_codes:
                    return False
                
                # Check HTTP status code
                return status_code in self.retry_on_status_codes
            
            return True
        
        return False
    
    def get_delay(self, attempt: int) -> float:
        """Calculate delay for retry attempt.
        
        Parameters
        ----------
        attempt : int
            Current attempt number (1-based)
            
        Returns
        -------
        float
            Delay in seconds
        """
        if self.exponential_backoff:
            delay = self.base_delay * (2 ** (attempt - 1))
        else:
            delay = self.base_delay
        
        # Cap at max delay
        delay = min(delay, self.max_delay)
        
        # Add jitter to avoid thundering herd
        if self.jitter:
            jitter_amount = delay * self.jitter_factor
            delay += random.uniform(-jitter_amount, jitter_amount)
        
        return max(0, delay)


@dataclass
class ErrorContext:
    """Context information for error handling."""
    
    operation: str
    resource_type: str
    resource_id: Optional[str] = None
    region: Optional[str] = None
    attempt: int = 1
    start_time: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def elapsed_time(self) -> float:
        """Get elapsed time since operation start.
        
        Returns
        -------
        float
            Elapsed time in seconds
        """
        return time.time() - self.start_time


@dataclass
class ErrorRecord:
    """Record of an error for analysis and reporting."""
    
    exception: Exception
    context: ErrorContext
    severity: ErrorSeverity
    recovery_action: RecoveryAction
    timestamp: float = field(default_factory=time.time)
    resolved: bool = False
    resolution_time: Optional[float] = None
    
    def mark_resolved(self) -> None:
        """Mark error as resolved."""
        self.resolved = True
        self.resolution_time = time.time()
    
    def resolution_duration(self) -> Optional[float]:
        """Get time taken to resolve error.
        
        Returns
        -------
        Optional[float]
            Resolution duration in seconds, None if not resolved
        """
        if self.resolution_time:
            return self.resolution_time - self.timestamp
        return None


class ErrorAnalyzer:
    """Analyzes errors and determines appropriate recovery actions."""
    
    def __init__(self):
        """Initialize error analyzer."""
        self.error_patterns = {
            # AWS API errors
            'Throttling': (ErrorSeverity.MEDIUM, RecoveryAction.RETRY),
            'RequestLimitExceeded': (ErrorSeverity.MEDIUM, RecoveryAction.RETRY),
            'InternalError': (ErrorSeverity.HIGH, RecoveryAction.RETRY),
            'ServiceUnavailable': (ErrorSeverity.HIGH, RecoveryAction.RETRY),
            'InsufficientInstanceCapacity': (ErrorSeverity.HIGH, RecoveryAction.FALLBACK),
            'SpotMaxPriceTooLow': (ErrorSeverity.MEDIUM, RecoveryAction.FALLBACK),
            'InvalidSpotFleetRequestConfig': (ErrorSeverity.LOW, RecoveryAction.ABORT),
            'UnauthorizedOperation': (ErrorSeverity.CRITICAL, RecoveryAction.ABORT),
            'AccessDenied': (ErrorSeverity.CRITICAL, RecoveryAction.ABORT),
            
            # Network and connectivity errors
            'EndpointConnectionError': (ErrorSeverity.MEDIUM, RecoveryAction.RETRY),
            'ConnectionClosedError': (ErrorSeverity.MEDIUM, RecoveryAction.RETRY),
            'ReadTimeoutError': (ErrorSeverity.MEDIUM, RecoveryAction.RETRY),
            
            # Credential errors
            'InvalidAccessKeyId': (ErrorSeverity.CRITICAL, RecoveryAction.ABORT),
            'TokenRefreshRequired': (ErrorSeverity.MEDIUM, RecoveryAction.RETRY),
            'ExpiredToken': (ErrorSeverity.MEDIUM, RecoveryAction.RETRY),
        }
    
    def analyze_error(self, exception: Exception, context: ErrorContext) -> tuple[ErrorSeverity, RecoveryAction]:
        """Analyze an error and determine appropriate response.
        
        Parameters
        ----------
        exception : Exception
            Exception to analyze
        context : ErrorContext
            Context of the error
            
        Returns
        -------
        tuple[ErrorSeverity, RecoveryAction]
            Error severity and recommended recovery action
        """
        # Handle AWS ClientError specifically
        if isinstance(exception, ClientError):
            error_code = exception.response.get('Error', {}).get('Code', '')
            if error_code in self.error_patterns:
                return self.error_patterns[error_code]
            
            # Default based on HTTP status code
            status_code = exception.response.get('ResponseMetadata', {}).get('HTTPStatusCode', 0)
            if status_code >= 500:
                return ErrorSeverity.HIGH, RecoveryAction.RETRY
            elif status_code >= 400:
                return ErrorSeverity.MEDIUM, RecoveryAction.ABORT
        
        # Handle credential errors
        if isinstance(exception, (NoCredentialsError, TokenRetrievalError)):
            return ErrorSeverity.CRITICAL, RecoveryAction.ABORT
        
        # Handle network errors
        if isinstance(exception, (ConnectionError, TimeoutError)):
            return ErrorSeverity.MEDIUM, RecoveryAction.RETRY
        
        # Default classification
        return ErrorSeverity.MEDIUM, RecoveryAction.RETRY
    
    def should_escalate(self, error_record: ErrorRecord, similar_errors: int) -> bool:
        """Determine if error should be escalated.
        
        Parameters
        ----------
        error_record : ErrorRecord
            Current error record
        similar_errors : int
            Number of similar errors recently
            
        Returns
        -------
        bool
            True if error should be escalated
        """
        # Escalate critical errors immediately
        if error_record.severity == ErrorSeverity.CRITICAL:
            return True
        
        # Escalate if too many similar errors
        if similar_errors >= 5:
            return True
        
        # Escalate long-running unresolved errors
        if not error_record.resolved and time.time() - error_record.timestamp > 300:  # 5 minutes
            return True
        
        return False


class ErrorRecoveryHandler:
    """Handles error recovery and fallback strategies."""
    
    def __init__(self):
        """Initialize recovery handler."""
        self.recovery_strategies = {
            'ec2_instance_launch': self._recover_instance_launch,
            'spot_fleet_request': self._recover_spot_fleet,
            'vpc_creation': self._recover_vpc_creation,
            'security_group_creation': self._recover_security_group,
        }
    
    def attempt_recovery(self, error_record: ErrorRecord, fallback_params: Dict[str, Any] = None) -> bool:
        """Attempt to recover from an error.
        
        Parameters
        ----------
        error_record : ErrorRecord
            Error to recover from
        fallback_params : Dict[str, Any], optional
            Parameters for fallback strategies
            
        Returns
        -------
        bool
            True if recovery was successful
        """
        operation = error_record.context.operation
        
        if operation in self.recovery_strategies:
            try:
                recovery_func = self.recovery_strategies[operation]
                success = recovery_func(error_record, fallback_params or {})
                
                if success:
                    error_record.mark_resolved()
                    logger.info(f"Successfully recovered from error in {operation}")
                
                return success
                
            except Exception as e:
                logger.error(f"Recovery attempt failed for {operation}: {e}")
                return False
        
        return False
    
    def _recover_instance_launch(self, error_record: ErrorRecord, fallback_params: Dict[str, Any]) -> bool:
        """Recover from EC2 instance launch failures."""
        exception = error_record.exception
        
        if isinstance(exception, ClientError):
            error_code = exception.response.get('Error', {}).get('Code', '')
            
            if error_code == 'InsufficientInstanceCapacity':
                # Try different instance types or availability zones
                logger.info("Attempting instance type fallback for capacity issue")
                return self._try_alternative_instance_types(fallback_params)
            
            elif error_code == 'SpotMaxPriceTooLow':
                # Increase spot price or switch to on-demand
                logger.info("Attempting spot price adjustment or on-demand fallback")
                return self._adjust_spot_pricing(fallback_params)
        
        return False
    
    def _recover_spot_fleet(self, error_record: ErrorRecord, fallback_params: Dict[str, Any]) -> bool:
        """Recover from Spot Fleet request failures."""
        exception = error_record.exception
        
        if isinstance(exception, ClientError):
            error_code = exception.response.get('Error', {}).get('Code', '')
            
            if 'SpotFleet' in error_code:
                # Try simpler spot fleet configuration or individual spot instances
                logger.info("Attempting simplified spot fleet configuration")
                return self._simplify_spot_fleet_config(fallback_params)
        
        return False
    
    def _recover_vpc_creation(self, error_record: ErrorRecord, fallback_params: Dict[str, Any]) -> bool:
        """Recover from VPC creation failures."""
        # Try different CIDR blocks or use existing VPC
        logger.info("Attempting VPC creation recovery")
        return self._try_alternative_vpc_config(fallback_params)
    
    def _recover_security_group(self, error_record: ErrorRecord, fallback_params: Dict[str, Any]) -> bool:
        """Recover from security group creation failures."""
        # Try different security group rules or use existing groups
        logger.info("Attempting security group recovery")
        return self._try_alternative_security_config(fallback_params)
    
    def _try_alternative_instance_types(self, params: Dict[str, Any]) -> bool:
        """Try alternative instance types for capacity issues."""
        # This would be implemented by the calling code
        # Return True if alternative succeeded
        return params.get('alternative_instance_types_available', False)
    
    def _adjust_spot_pricing(self, params: Dict[str, Any]) -> bool:
        """Adjust spot pricing or fallback to on-demand."""
        return params.get('pricing_adjustment_available', False)
    
    def _simplify_spot_fleet_config(self, params: Dict[str, Any]) -> bool:
        """Simplify spot fleet configuration."""
        return params.get('simplified_config_available', False)
    
    def _try_alternative_vpc_config(self, params: Dict[str, Any]) -> bool:
        """Try alternative VPC configuration."""
        return params.get('alternative_vpc_available', False)
    
    def _try_alternative_security_config(self, params: Dict[str, Any]) -> bool:
        """Try alternative security group configuration."""
        return params.get('alternative_security_available', False)


class RobustErrorHandler:
    """Main error handling coordinator."""
    
    def __init__(self, retry_config: Optional[RetryConfig] = None):
        """Initialize robust error handler.
        
        Parameters
        ----------
        retry_config : Optional[RetryConfig]
            Retry configuration, uses default if None
        """
        self.retry_config = retry_config or RetryConfig()
        self.analyzer = ErrorAnalyzer()
        self.recovery_handler = ErrorRecoveryHandler()
        self.error_history: List[ErrorRecord] = []
    
    def handle_error(self, exception: Exception, context: ErrorContext, 
                    fallback_params: Optional[Dict[str, Any]] = None) -> ErrorRecord:
        """Handle an error with analysis and recovery.
        
        Parameters
        ----------
        exception : Exception
            Exception that occurred
        context : ErrorContext
            Context of the operation
        fallback_params : Optional[Dict[str, Any]]
            Parameters for fallback strategies
            
        Returns
        -------
        ErrorRecord
            Record of the error and handling
        """
        # Analyze the error
        severity, recovery_action = self.analyzer.analyze_error(exception, context)
        
        # Create error record
        error_record = ErrorRecord(
            exception=exception,
            context=context,
            severity=severity,
            recovery_action=recovery_action
        )
        
        # Log the error
        logger.error(
            f"Error in {context.operation} (attempt {context.attempt}): {exception}",
            extra={
                'error_severity': severity.value,
                'recovery_action': recovery_action.value,
                'resource_type': context.resource_type,
                'resource_id': context.resource_id,
            }
        )
        
        # Attempt recovery if appropriate
        if recovery_action in [RecoveryAction.RETRY, RecoveryAction.FALLBACK]:
            if self.recovery_handler.attempt_recovery(error_record, fallback_params):
                logger.info(f"Error recovery successful for {context.operation}")
            else:
                logger.warning(f"Error recovery failed for {context.operation}")
        
        # Store error record
        self.error_history.append(error_record)
        
        # Clean up old error records (keep last 1000)
        if len(self.error_history) > 1000:
            self.error_history = self.error_history[-1000:]
        
        return error_record
    
    def get_error_statistics(self, time_window: float = 3600) -> Dict[str, Any]:
        """Get error statistics for a time window.
        
        Parameters
        ----------
        time_window : float
            Time window in seconds (default: 1 hour)
            
        Returns
        -------
        Dict[str, Any]
            Error statistics
        """
        cutoff_time = time.time() - time_window
        recent_errors = [
            error for error in self.error_history 
            if error.timestamp >= cutoff_time
        ]
        
        if not recent_errors:
            return {
                'total_errors': 0,
                'error_rate': 0.0,
                'most_common_errors': [],
                'avg_resolution_time': 0.0,
                'unresolved_count': 0,
            }
        
        # Calculate statistics
        total_errors = len(recent_errors)
        resolved_errors = [e for e in recent_errors if e.resolved]
        
        error_types = {}
        for error in recent_errors:
            error_type = type(error.exception).__name__
            error_types[error_type] = error_types.get(error_type, 0) + 1
        
        most_common = sorted(error_types.items(), key=lambda x: x[1], reverse=True)[:5]
        
        avg_resolution_time = 0.0
        if resolved_errors:
            resolution_times = [e.resolution_duration() for e in resolved_errors if e.resolution_duration()]
            if resolution_times:
                avg_resolution_time = sum(resolution_times) / len(resolution_times)
        
        return {
            'total_errors': total_errors,
            'error_rate': total_errors / (time_window / 60),  # errors per minute
            'most_common_errors': most_common,
            'avg_resolution_time': avg_resolution_time,
            'unresolved_count': total_errors - len(resolved_errors),
            'resolution_rate': len(resolved_errors) / total_errors if total_errors > 0 else 0.0,
        }


def retry_with_backoff(retry_config: Optional[RetryConfig] = None, 
                      error_handler: Optional[RobustErrorHandler] = None):
    """Decorator for adding retry behavior with exponential backoff.
    
    Parameters
    ----------
    retry_config : Optional[RetryConfig]
        Retry configuration
    error_handler : Optional[RobustErrorHandler]
        Error handler for comprehensive error management
        
    Returns
    -------
    Callable
        Decorated function with retry behavior
    """
    config = retry_config or RetryConfig()
    
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            
            for attempt in range(1, config.max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                
                except Exception as e:
                    last_exception = e
                    
                    # Use error handler if provided
                    if error_handler:
                        context = ErrorContext(
                            operation=func.__name__,
                            resource_type=kwargs.get('resource_type', 'unknown'),
                            resource_id=kwargs.get('resource_id'),
                            attempt=attempt
                        )
                        error_record = error_handler.handle_error(e, context)
                        
                        if error_record.resolved:
                            continue  # Try again after recovery
                    
                    # Check if should retry
                    if not config.should_retry(e, attempt):
                        break
                    
                    if attempt < config.max_attempts:
                        delay = config.get_delay(attempt)
                        logger.warning(
                            f"Attempt {attempt} failed, retrying in {delay:.2f}s: {e}"
                        )
                        time.sleep(delay)
            
            # All attempts failed
            logger.error(f"All {config.max_attempts} attempts failed for {func.__name__}")
            raise last_exception
        
        return wrapper
    return decorator
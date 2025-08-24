"""Tests for robust error handling framework.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import pytest
import time
from unittest.mock import Mock, patch
from botocore.exceptions import ClientError, NoCredentialsError

from parsl_ephemeral_aws.error_handling import (
    ErrorSeverity,
    RecoveryAction,
    RetryConfig,
    ErrorContext,
    ErrorRecord,
    ErrorAnalyzer,
    ErrorRecoveryHandler,
    RobustErrorHandler,
    retry_with_backoff
)


class TestRetryConfig:
    """Tests for retry configuration."""

    def test_default_config(self):
        """Test default retry configuration."""
        config = RetryConfig()
        
        assert config.max_attempts == 3
        assert config.base_delay == 1.0
        assert config.exponential_backoff is True
        assert config.jitter is True

    def test_should_retry_client_error(self):
        """Test retry decision for ClientError."""
        config = RetryConfig()
        
        # Create a retryable ClientError (500 status)
        error = ClientError(
            error_response={
                'Error': {'Code': 'InternalError', 'Message': 'Server error'},
                'ResponseMetadata': {'HTTPStatusCode': 500}
            },
            operation_name='TestOperation'
        )
        
        assert config.should_retry(error, 1) is True
        assert config.should_retry(error, config.max_attempts) is False

    def test_should_not_retry_non_retryable_error(self):
        """Test retry decision for non-retryable errors."""
        config = RetryConfig()
        
        # Create a non-retryable ClientError (AccessDenied)
        error = ClientError(
            error_response={
                'Error': {'Code': 'AccessDenied', 'Message': 'Access denied'},
                'ResponseMetadata': {'HTTPStatusCode': 403}
            },
            operation_name='TestOperation'
        )
        
        assert config.should_retry(error, 1) is False

    def test_get_delay_exponential(self):
        """Test exponential backoff delay calculation."""
        config = RetryConfig(base_delay=1.0, exponential_backoff=True, jitter=False)
        
        assert config.get_delay(1) == 1.0
        assert config.get_delay(2) == 2.0
        assert config.get_delay(3) == 4.0

    def test_get_delay_linear(self):
        """Test linear delay calculation."""
        config = RetryConfig(base_delay=2.0, exponential_backoff=False, jitter=False)
        
        assert config.get_delay(1) == 2.0
        assert config.get_delay(2) == 2.0
        assert config.get_delay(3) == 2.0

    def test_get_delay_with_max_delay(self):
        """Test delay capping at max_delay."""
        config = RetryConfig(base_delay=10.0, max_delay=15.0, exponential_backoff=True, jitter=False)
        
        assert config.get_delay(1) == 10.0
        assert config.get_delay(2) == 15.0  # Capped at max_delay
        assert config.get_delay(3) == 15.0  # Still capped

    def test_get_delay_with_jitter(self):
        """Test delay calculation with jitter."""
        config = RetryConfig(base_delay=1.0, exponential_backoff=False, jitter=True, jitter_factor=0.1)
        
        # With jitter, delay should vary around base_delay
        delays = [config.get_delay(1) for _ in range(10)]
        assert all(0.9 <= delay <= 1.1 for delay in delays)  # Within jitter range
        assert len(set(delays)) > 1  # Should produce different values


class TestErrorContext:
    """Tests for error context."""

    def test_initialization(self):
        """Test error context initialization."""
        context = ErrorContext(
            operation="test_operation",
            resource_type="ec2_instance",
            resource_id="i-123456",
            region="us-east-1"
        )
        
        assert context.operation == "test_operation"
        assert context.resource_type == "ec2_instance"
        assert context.resource_id == "i-123456"
        assert context.region == "us-east-1"
        assert context.attempt == 1

    def test_elapsed_time(self):
        """Test elapsed time calculation."""
        start_time = time.time()
        context = ErrorContext(
            operation="test",
            resource_type="test",
            start_time=start_time
        )
        
        time.sleep(0.01)  # Small delay
        elapsed = context.elapsed_time()
        assert elapsed > 0
        assert elapsed < 1  # Should be small


class TestErrorRecord:
    """Tests for error record."""

    def test_initialization(self):
        """Test error record initialization."""
        exception = ValueError("test error")
        context = ErrorContext(operation="test", resource_type="test")
        
        record = ErrorRecord(
            exception=exception,
            context=context,
            severity=ErrorSeverity.MEDIUM,
            recovery_action=RecoveryAction.RETRY
        )
        
        assert record.exception == exception
        assert record.context == context
        assert record.severity == ErrorSeverity.MEDIUM
        assert record.recovery_action == RecoveryAction.RETRY
        assert record.resolved is False

    def test_mark_resolved(self):
        """Test marking error as resolved."""
        exception = ValueError("test error")
        context = ErrorContext(operation="test", resource_type="test")
        
        record = ErrorRecord(
            exception=exception,
            context=context,
            severity=ErrorSeverity.LOW,
            recovery_action=RecoveryAction.RETRY
        )
        
        assert record.resolved is False
        assert record.resolution_time is None
        
        record.mark_resolved()
        
        assert record.resolved is True
        assert record.resolution_time is not None

    def test_resolution_duration(self):
        """Test resolution duration calculation."""
        exception = ValueError("test error")
        context = ErrorContext(operation="test", resource_type="test")
        
        record = ErrorRecord(
            exception=exception,
            context=context,
            severity=ErrorSeverity.LOW,
            recovery_action=RecoveryAction.RETRY
        )
        
        # Before resolution
        assert record.resolution_duration() is None
        
        # After resolution
        time.sleep(0.01)
        record.mark_resolved()
        duration = record.resolution_duration()
        
        assert duration is not None
        assert duration > 0


class TestErrorAnalyzer:
    """Tests for error analyzer."""

    def test_analyze_client_error(self):
        """Test analysis of AWS ClientError."""
        analyzer = ErrorAnalyzer()
        context = ErrorContext(operation="test", resource_type="test")
        
        # Test throttling error
        throttling_error = ClientError(
            error_response={
                'Error': {'Code': 'Throttling', 'Message': 'Request rate exceeded'},
                'ResponseMetadata': {'HTTPStatusCode': 429}
            },
            operation_name='TestOperation'
        )
        
        severity, action = analyzer.analyze_error(throttling_error, context)
        assert severity == ErrorSeverity.MEDIUM
        assert action == RecoveryAction.RETRY

    def test_analyze_access_denied(self):
        """Test analysis of access denied error."""
        analyzer = ErrorAnalyzer()
        context = ErrorContext(operation="test", resource_type="test")
        
        access_denied = ClientError(
            error_response={
                'Error': {'Code': 'AccessDenied', 'Message': 'Access denied'},
                'ResponseMetadata': {'HTTPStatusCode': 403}
            },
            operation_name='TestOperation'
        )
        
        severity, action = analyzer.analyze_error(access_denied, context)
        assert severity == ErrorSeverity.CRITICAL
        assert action == RecoveryAction.ABORT

    def test_analyze_credential_error(self):
        """Test analysis of credential errors."""
        analyzer = ErrorAnalyzer()
        context = ErrorContext(operation="test", resource_type="test")
        
        cred_error = NoCredentialsError()
        severity, action = analyzer.analyze_error(cred_error, context)
        
        assert severity == ErrorSeverity.CRITICAL
        assert action == RecoveryAction.ABORT

    def test_analyze_unknown_error(self):
        """Test analysis of unknown errors."""
        analyzer = ErrorAnalyzer()
        context = ErrorContext(operation="test", resource_type="test")
        
        unknown_error = RuntimeError("Unknown error")
        severity, action = analyzer.analyze_error(unknown_error, context)
        
        assert severity == ErrorSeverity.MEDIUM
        assert action == RecoveryAction.RETRY

    def test_should_escalate_critical(self):
        """Test escalation of critical errors."""
        analyzer = ErrorAnalyzer()
        
        exception = ValueError("test")
        context = ErrorContext(operation="test", resource_type="test")
        record = ErrorRecord(
            exception=exception,
            context=context,
            severity=ErrorSeverity.CRITICAL,
            recovery_action=RecoveryAction.ABORT
        )
        
        assert analyzer.should_escalate(record, 1) is True

    def test_should_escalate_many_similar(self):
        """Test escalation when many similar errors occur."""
        analyzer = ErrorAnalyzer()
        
        exception = ValueError("test")
        context = ErrorContext(operation="test", resource_type="test")
        record = ErrorRecord(
            exception=exception,
            context=context,
            severity=ErrorSeverity.LOW,
            recovery_action=RecoveryAction.RETRY
        )
        
        assert analyzer.should_escalate(record, 5) is True
        assert analyzer.should_escalate(record, 3) is False


class TestErrorRecoveryHandler:
    """Tests for error recovery handler."""

    def test_initialization(self):
        """Test recovery handler initialization."""
        handler = ErrorRecoveryHandler()
        assert len(handler.recovery_strategies) > 0
        assert 'ec2_instance_launch' in handler.recovery_strategies

    def test_attempt_recovery_unknown_operation(self):
        """Test recovery attempt for unknown operation."""
        handler = ErrorRecoveryHandler()
        
        exception = ValueError("test")
        context = ErrorContext(operation="unknown_operation", resource_type="test")
        record = ErrorRecord(
            exception=exception,
            context=context,
            severity=ErrorSeverity.MEDIUM,
            recovery_action=RecoveryAction.RETRY
        )
        
        success = handler.attempt_recovery(record)
        assert success is False

    def test_recovery_strategy_methods_exist(self):
        """Test that recovery strategy methods exist."""
        handler = ErrorRecoveryHandler()
        
        # Test that private methods exist for recovery strategies
        assert hasattr(handler, '_recover_instance_launch')
        assert hasattr(handler, '_recover_spot_fleet')
        assert hasattr(handler, '_recover_vpc_creation')
        assert hasattr(handler, '_recover_security_group')


class TestRobustErrorHandler:
    """Tests for robust error handler."""

    def test_initialization(self):
        """Test error handler initialization."""
        handler = RobustErrorHandler()
        
        assert handler.retry_config is not None
        assert handler.analyzer is not None
        assert handler.recovery_handler is not None
        assert handler.error_history == []

    def test_handle_error(self):
        """Test error handling workflow."""
        handler = RobustErrorHandler()
        
        exception = ValueError("test error")
        context = ErrorContext(operation="test_op", resource_type="test_resource")
        
        error_record = handler.handle_error(exception, context)
        
        assert error_record.exception == exception
        assert error_record.context == context
        assert len(handler.error_history) == 1
        assert handler.error_history[0] == error_record

    def test_error_history_cleanup(self):
        """Test error history cleanup when it gets too large."""
        handler = RobustErrorHandler()
        
        # Add more than 1000 errors
        for i in range(1100):
            exception = ValueError(f"error {i}")
            context = ErrorContext(operation="test", resource_type="test")
            handler.handle_error(exception, context)
        
        # Should be capped at 1000
        assert len(handler.error_history) == 1000

    def test_get_error_statistics_empty(self):
        """Test error statistics with no errors."""
        handler = RobustErrorHandler()
        
        stats = handler.get_error_statistics()
        
        assert stats['total_errors'] == 0
        assert stats['error_rate'] == 0.0
        assert stats['most_common_errors'] == []
        assert stats['avg_resolution_time'] == 0.0
        assert stats['unresolved_count'] == 0

    def test_get_error_statistics_with_errors(self):
        """Test error statistics with some errors."""
        handler = RobustErrorHandler()
        
        # Add some test errors
        for i in range(5):
            exception = ValueError("test error")
            context = ErrorContext(operation="test", resource_type="test")
            error_record = handler.handle_error(exception, context)
            
            if i < 3:  # Mark some as resolved
                error_record.mark_resolved()
        
        stats = handler.get_error_statistics()
        
        assert stats['total_errors'] == 5
        assert stats['error_rate'] > 0
        assert len(stats['most_common_errors']) > 0
        assert stats['unresolved_count'] == 2
        assert stats['resolution_rate'] == 0.6  # 3 out of 5 resolved


class TestRetryDecorator:
    """Tests for retry decorator."""

    def test_successful_function(self):
        """Test decorator with successful function."""
        @retry_with_backoff()
        def successful_function():
            return "success"
        
        result = successful_function()
        assert result == "success"

    def test_function_with_retryable_error(self):
        """Test decorator with function that fails then succeeds."""
        call_count = 0
        
        @retry_with_backoff(RetryConfig(max_attempts=3, base_delay=0.01))
        def failing_then_success():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ConnectionError("Network error")
            return "success"
        
        result = failing_then_success()
        assert result == "success"
        assert call_count == 2

    def test_function_with_non_retryable_error(self):
        """Test decorator with non-retryable error."""
        @retry_with_backoff()
        def non_retryable_error():
            raise ValueError("Non-retryable error")
        
        with pytest.raises(ValueError):
            non_retryable_error()

    def test_function_exceeds_max_attempts(self):
        """Test decorator when function exceeds max attempts."""
        @retry_with_backoff(RetryConfig(max_attempts=2, base_delay=0.01))
        def always_fails():
            raise ConnectionError("Always fails")
        
        with pytest.raises(ConnectionError):
            always_fails()

    def test_decorator_with_error_handler(self):
        """Test decorator with error handler integration."""
        error_handler = RobustErrorHandler()
        
        @retry_with_backoff(error_handler=error_handler)
        def test_function():
            raise ConnectionError("Test error")
        
        with pytest.raises(ConnectionError):
            test_function()
        
        # Error should be recorded in handler
        assert len(error_handler.error_history) > 0
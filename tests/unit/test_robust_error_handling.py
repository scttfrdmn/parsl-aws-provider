"""Tests for robust error handling framework.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import pytest
import time
from botocore.exceptions import ClientError, NoCredentialsError

from parsl_ephemeral_provider.error_handling import (
    ErrorSeverity,
    RecoveryAction,
    RetryConfig,
    ErrorContext,
    ErrorRecord,
    ErrorAnalyzer,
    ErrorRecoveryHandler,
    RobustErrorHandler,
    poll_until,
    retry_with_backoff,
)

pytestmark = pytest.mark.unit


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
                "Error": {"Code": "InternalError", "Message": "Server error"},
                "ResponseMetadata": {"HTTPStatusCode": 500},
            },
            operation_name="TestOperation",
        )

        assert config.should_retry(error, 1) is True
        assert config.should_retry(error, config.max_attempts) is False

    def test_should_not_retry_non_retryable_error(self):
        """Test retry decision for non-retryable errors."""
        config = RetryConfig()

        # Create a non-retryable ClientError (AccessDenied)
        error = ClientError(
            error_response={
                "Error": {"Code": "AccessDenied", "Message": "Access denied"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            operation_name="TestOperation",
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
        config = RetryConfig(
            base_delay=10.0, max_delay=15.0, exponential_backoff=True, jitter=False
        )

        assert config.get_delay(1) == 10.0
        assert config.get_delay(2) == 15.0  # Capped at max_delay
        assert config.get_delay(3) == 15.0  # Still capped

    def test_get_delay_with_jitter(self):
        """Test delay calculation with jitter."""
        config = RetryConfig(
            base_delay=1.0, exponential_backoff=False, jitter=True, jitter_factor=0.1
        )

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
            region="us-east-1",
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
            operation="test", resource_type="test", start_time=start_time
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
            recovery_action=RecoveryAction.RETRY,
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
            recovery_action=RecoveryAction.RETRY,
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
            recovery_action=RecoveryAction.RETRY,
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
                "Error": {"Code": "Throttling", "Message": "Request rate exceeded"},
                "ResponseMetadata": {"HTTPStatusCode": 429},
            },
            operation_name="TestOperation",
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
                "Error": {"Code": "AccessDenied", "Message": "Access denied"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            operation_name="TestOperation",
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
            recovery_action=RecoveryAction.ABORT,
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
            recovery_action=RecoveryAction.RETRY,
        )

        assert analyzer.should_escalate(record, 5) is True
        assert analyzer.should_escalate(record, 3) is False


class TestErrorRecoveryHandler:
    """Tests for error recovery handler."""

    def test_initialization(self):
        """Test recovery handler initialization."""
        handler = ErrorRecoveryHandler()
        assert len(handler.recovery_strategies) > 0
        assert "ec2_instance_launch" in handler.recovery_strategies

    def test_attempt_recovery_unknown_operation(self):
        """Test recovery attempt for unknown operation."""
        handler = ErrorRecoveryHandler()

        exception = ValueError("test")
        context = ErrorContext(operation="unknown_operation", resource_type="test")
        record = ErrorRecord(
            exception=exception,
            context=context,
            severity=ErrorSeverity.MEDIUM,
            recovery_action=RecoveryAction.RETRY,
        )

        success = handler.attempt_recovery(record)
        assert success is False

    def test_recovery_strategy_methods_exist(self):
        """Test that recovery strategy methods exist."""
        handler = ErrorRecoveryHandler()

        # Test that private methods exist for recovery strategies
        assert hasattr(handler, "_recover_instance_launch")
        assert hasattr(handler, "_recover_spot_fleet")
        assert hasattr(handler, "_recover_vpc_creation")
        assert hasattr(handler, "_recover_security_group")


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

        assert stats["total_errors"] == 0
        assert stats["error_rate"] == 0.0
        assert stats["most_common_errors"] == []
        assert stats["avg_resolution_time"] == 0.0
        assert stats["unresolved_count"] == 0

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

        assert stats["total_errors"] == 5
        assert stats["error_rate"] > 0
        assert len(stats["most_common_errors"]) > 0
        assert stats["unresolved_count"] == 2
        assert stats["resolution_rate"] == 0.6  # 3 out of 5 resolved


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


class TestPollUntil:
    """Tests for the success-poll primitive (#91).

    ``poll_until`` exists because ``retry_with_backoff`` cannot express these
    waits: the predicate *succeeds* and returns a not-yet answer rather than
    raising, so the decorator would fire zero times. Several tests below pin
    exactly that distinction.
    """

    # A schedule short enough that the whole class runs in well under a second.
    FAST = RetryConfig(base_delay=0.01, max_delay=0.05)

    def test_returns_the_first_truthy_value(self):
        """The predicate's value is what comes back, not just True."""
        result = poll_until(
            lambda: "i-abc123",
            timeout=1.0,
            description="an instance ID",
            retry_config=self.FAST,
        )
        assert result == "i-abc123"

    def test_polls_until_the_predicate_flips(self):
        """A falsey answer means keep waiting, and is not an error."""
        calls = 0

        def ready():
            nonlocal calls
            calls += 1
            return calls >= 3

        assert (
            poll_until(
                ready,
                timeout=1.0,
                description="readiness",
                retry_config=self.FAST,
            )
            is True
        )
        assert calls == 3

    def test_a_falsey_predicate_is_not_an_exception(self):
        """The case ``retry_with_backoff`` cannot serve.

        The predicate never raises, so a retry decorator would call it once and
        return the not-yet answer as if it were the result. ``poll_until`` must
        keep going instead -- this is the whole reason #91 needed a second
        primitive rather than the existing one.
        """
        answers = iter([None, False, 0, "", "i-final"])
        result = poll_until(
            lambda: next(answers),
            timeout=1.0,
            description="a value after four falsey answers",
            retry_config=self.FAST,
        )
        assert result == "i-final"

    def test_timeout_raises_with_the_description(self):
        """The message must name what was being waited for, not just time out."""
        with pytest.raises(TimeoutError, match="SSM registration"):
            poll_until(
                lambda: False,
                timeout=0.05,
                description="SSM registration",
                retry_config=self.FAST,
            )

    def test_timeout_is_respected_despite_a_long_delay_schedule(self):
        """A delay larger than the remaining budget must not overshoot.

        With ``base_delay=10`` the first sleep would be ten seconds, so a 0.1s
        timeout has to clamp it. Without the clamp the call would block for the
        full delay and report a 0.1s timeout ten seconds late.
        """
        start = time.time()
        with pytest.raises(TimeoutError):
            poll_until(
                lambda: False,
                timeout=0.1,
                description="something that never happens",
                retry_config=RetryConfig(base_delay=10.0, max_delay=60.0),
            )
        assert time.time() - start < 2.0

    def test_a_raising_predicate_is_treated_as_not_yet(self):
        """Early attempts are expected to fail; the poll absorbs that."""
        calls = 0

        def flaky():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise ClientError(
                    error_response={
                        "Error": {"Code": "InvalidInstanceId", "Message": "nope"}
                    },
                    operation_name="DescribeInstanceInformation",
                )
            return "online"

        assert (
            poll_until(
                flaky,
                timeout=1.0,
                description="an instance that 404s twice",
                retry_config=self.FAST,
            )
            == "online"
        )
        assert calls == 3

    def test_on_error_sees_every_exception(self):
        """The hook is how a caller narrows which errors are tolerable."""
        seen = []
        calls = 0

        def flaky():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise ConnectionError(f"attempt {calls}")
            return True

        poll_until(
            flaky,
            timeout=1.0,
            description="a flaky check",
            retry_config=self.FAST,
            on_error=seen.append,
        )
        assert [str(e) for e in seen] == ["attempt 1", "attempt 2"]

    def test_on_error_can_reraise_to_fail_fast(self):
        """An ``on_error`` that raises must abort the poll immediately.

        This is how ``modes/standard.py`` keeps a credentials failure from being
        retried silently for five minutes: tolerate ``ClientError``, re-raise
        anything else. If the raise were swallowed, the poll would burn the
        whole timeout on an error that could never resolve.
        """

        def fatal(exc):
            raise exc

        def always_raises():
            raise NoCredentialsError()

        with pytest.raises(NoCredentialsError):
            poll_until(
                always_raises,
                timeout=5.0,
                description="a poll that should fail fast",
                retry_config=self.FAST,
                on_error=fatal,
            )

    def test_max_attempts_does_not_bound_the_poll(self):
        """Only *timeout* bounds a poll -- ``RetryConfig.max_attempts`` is unused.

        ``RetryConfig`` is shared with the retry decorator, where
        ``max_attempts`` is the bound. Reusing the dataclass for its delay math
        must not import that bound: a boot that takes 40 polls is normal, and a
        default ``max_attempts=3`` would abort it after three.
        """
        calls = 0

        def ready_on_tenth():
            nonlocal calls
            calls += 1
            return calls >= 10

        assert poll_until(
            ready_on_tenth,
            timeout=5.0,
            description="the tenth attempt",
            retry_config=RetryConfig(max_attempts=3, base_delay=0.001, max_delay=0.01),
        )
        assert calls == 10

    def test_default_retry_config_is_usable(self):
        """Omitting *retry_config* must not require the caller to build one."""
        assert poll_until(lambda: True, timeout=1.0, description="an immediate answer")

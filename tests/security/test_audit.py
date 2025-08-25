"""Tests for security audit and monitoring framework.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import pytest
import json
import tempfile
import os

from parsl_ephemeral_aws.security.audit import (
    SecurityEventType,
    SecurityEventSeverity,
    SecurityEvent,
    SecurityMonitor,
    AuditLogger,
    ComplianceFramework,
)


class TestSecurityEvent:
    """Tests for security event class."""

    def test_initialization(self):
        """Test security event initialization."""
        event = SecurityEvent(
            event_type=SecurityEventType.RESOURCE_CREATE,
            severity=SecurityEventSeverity.INFO,
            message="Test resource created",
            resource_type="ec2_instance",
            resource_id="i-123456",
            user_identity="test-user",
            workflow_id="test-workflow",
        )

        assert event.event_type == SecurityEventType.RESOURCE_CREATE
        assert event.severity == SecurityEventSeverity.INFO
        assert event.message == "Test resource created"
        assert event.resource_type == "ec2_instance"
        assert event.resource_id == "i-123456"
        assert event.user_identity == "test-user"
        assert event.workflow_id == "test-workflow"
        assert event.event_id is not None
        assert event.timestamp > 0

    def test_to_dict(self):
        """Test security event to dict conversion."""
        event = SecurityEvent(
            event_type=SecurityEventType.CREDENTIAL_ACCESS,
            severity=SecurityEventSeverity.MEDIUM,
            message="Credential access attempt",
            metadata={"source": "test"},
            tags=["authentication"],
        )

        event_dict = event.to_dict()

        assert event_dict["event_type"] == "credential_access"
        assert event_dict["severity"] == "medium"
        assert event_dict["message"] == "Credential access attempt"
        assert event_dict["metadata"]["source"] == "test"
        assert "authentication" in event_dict["tags"]
        assert "iso_timestamp" in event_dict

    def test_to_json(self):
        """Test security event to JSON conversion."""
        event = SecurityEvent(
            event_type=SecurityEventType.ACCESS_DENIED,
            severity=SecurityEventSeverity.HIGH,
            message="Access denied",
        )

        json_str = event.to_json()
        parsed = json.loads(json_str)

        assert parsed["event_type"] == "access_denied"
        assert parsed["severity"] == "high"
        assert parsed["message"] == "Access denied"


class TestSecurityMonitor:
    """Tests for security monitor."""

    def test_initialization(self):
        """Test security monitor initialization."""
        monitor = SecurityMonitor()

        assert len(monitor.event_patterns) > 0
        assert "repeated_access_denied" in monitor.event_patterns
        assert len(monitor.event_history) == 0

    def test_analyze_normal_event(self):
        """Test analysis of normal security event."""
        monitor = SecurityMonitor()

        event = SecurityEvent(
            event_type=SecurityEventType.RESOURCE_CREATE,
            severity=SecurityEventSeverity.INFO,
            message="Resource created",
        )

        alerts = monitor.analyze_event(event)

        # No alerts should be generated for single normal event
        assert len(alerts) == 0
        assert len(monitor.event_history) == 1

    def test_detect_suspicious_pattern(self):
        """Test detection of suspicious activity patterns."""
        monitor = SecurityMonitor()

        # Generate multiple access denied events to trigger pattern
        alerts_generated = []
        for i in range(6):  # Threshold is 5
            event = SecurityEvent(
                event_type=SecurityEventType.ACCESS_DENIED,
                severity=SecurityEventSeverity.MEDIUM,
                message=f"Access denied {i}",
                user_identity="test-user",
            )

            alerts = monitor.analyze_event(event)
            alerts_generated.extend(alerts)

        # Should generate one alert when threshold is exceeded
        assert len(alerts_generated) == 1
        alert = alerts_generated[0]
        assert alert.event_type == SecurityEventType.SUSPICIOUS_ACTIVITY
        assert alert.severity == SecurityEventSeverity.HIGH
        assert "repeated_access_denied" in alert.message

    def test_get_security_metrics_empty(self):
        """Test security metrics with no events."""
        monitor = SecurityMonitor()

        metrics = monitor.get_security_metrics()

        assert metrics["total_events"] == 0
        assert metrics["events_by_type"] == {}
        assert metrics["events_by_severity"] == {}
        assert metrics["suspicious_activities"] == 0

    def test_get_security_metrics_with_events(self):
        """Test security metrics with events."""
        monitor = SecurityMonitor()

        # Add various events
        events = [
            SecurityEvent(
                event_type=SecurityEventType.RESOURCE_CREATE,
                severity=SecurityEventSeverity.INFO,
                message="Resource created",
                resource_id="i-123",
            ),
            SecurityEvent(
                event_type=SecurityEventType.ACCESS_DENIED,
                severity=SecurityEventSeverity.MEDIUM,
                message="Access denied",
                resource_id="i-123",
            ),
            SecurityEvent(
                event_type=SecurityEventType.SUSPICIOUS_ACTIVITY,
                severity=SecurityEventSeverity.HIGH,
                message="Suspicious activity detected",
            ),
        ]

        for event in events:
            monitor.analyze_event(event)

        metrics = monitor.get_security_metrics()

        assert metrics["total_events"] == 3
        assert metrics["events_by_type"]["resource_create"] == 1
        assert metrics["events_by_type"]["access_denied"] == 1
        assert metrics["events_by_severity"]["info"] == 1
        assert metrics["events_by_severity"]["medium"] == 1
        assert metrics["events_by_severity"]["high"] == 1
        assert metrics["suspicious_activities"] == 1
        assert len(metrics["top_resources"]) == 1
        assert metrics["top_resources"][0] == ("i-123", 2)


class TestAuditLogger:
    """Tests for audit logger."""

    def test_initialization_console_only(self):
        """Test audit logger initialization with console logging."""
        logger = AuditLogger(enable_console=True)

        assert logger.enable_console is True
        assert logger.log_file is None
        assert len(logger.audit_logger.handlers) >= 1

    def test_initialization_with_file(self):
        """Test audit logger initialization with file logging."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as tmp_file:
            log_file = tmp_file.name

        try:
            logger = AuditLogger(log_file=log_file, enable_console=False)

            assert logger.log_file == log_file
            assert logger.enable_console is False

            # Test logging to file
            event = SecurityEvent(
                event_type=SecurityEventType.RESOURCE_CREATE,
                severity=SecurityEventSeverity.INFO,
                message="Test log message",
            )

            logger.log_event(event)

            # Check file content
            with open(log_file, "r") as f:
                content = f.read()
                assert "Test log message" in content
                assert "resource_create" in content

        finally:
            if os.path.exists(log_file):
                os.unlink(log_file)

    def test_log_resource_operation(self):
        """Test logging resource operations."""
        logger = AuditLogger(enable_console=False)

        # Mock the log_event method to capture events
        logged_events = []
        original_log_event = logger.log_event

        def mock_log_event(event):
            logged_events.append(event)
            return original_log_event(event)

        logger.log_event = mock_log_event

        # Test successful resource creation
        logger.log_resource_operation(
            operation="create",
            resource_type="ec2_instance",
            resource_id="i-123456",
            success=True,
            additional_info="test metadata",
        )

        assert len(logged_events) == 1
        event = logged_events[0]
        assert event.event_type == SecurityEventType.RESOURCE_CREATE
        assert event.severity == SecurityEventSeverity.INFO
        assert event.resource_type == "ec2_instance"
        assert event.resource_id == "i-123456"
        assert event.metadata["additional_info"] == "test metadata"

    def test_log_credential_access(self):
        """Test logging credential access events."""
        logger = AuditLogger(enable_console=False)

        logged_events = []
        original_log_event = logger.log_event

        def mock_log_event(event):
            logged_events.append(event)
            return original_log_event(event)

        logger.log_event = mock_log_event

        # Test successful role assumption
        logger.log_credential_access(
            access_type="assume_role", identity="test-role", success=True
        )

        # Test failed access
        logger.log_credential_access(
            access_type="credential_access", identity="test-user", success=False
        )

        assert len(logged_events) == 2

        # Check successful event
        success_event = logged_events[0]
        assert success_event.event_type == SecurityEventType.ROLE_ASSUMPTION
        assert success_event.severity == SecurityEventSeverity.INFO
        assert success_event.user_identity == "test-role"

        # Check failed event
        fail_event = logged_events[1]
        assert fail_event.event_type == SecurityEventType.ACCESS_DENIED
        assert fail_event.severity == SecurityEventSeverity.MEDIUM
        assert fail_event.user_identity == "test-user"

    def test_log_security_violation(self):
        """Test logging security violation events."""
        logger = AuditLogger(enable_console=False)

        logged_events = []
        original_log_event = logger.log_event

        def mock_log_event(event):
            logged_events.append(event)
            return original_log_event(event)

        logger.log_event = mock_log_event

        logger.log_security_violation(
            violation_type="unauthorized_access",
            description="Attempted access to restricted resource",
            severity=SecurityEventSeverity.CRITICAL,
            source_ip="192.168.1.100",
        )

        assert len(logged_events) == 1
        event = logged_events[0]
        assert event.event_type == SecurityEventType.SECURITY_VIOLATION
        assert event.severity == SecurityEventSeverity.CRITICAL
        assert "unauthorized_access" in event.message
        assert "unauthorized_access" in event.tags
        assert event.metadata["source_ip"] == "192.168.1.100"

    def test_get_audit_summary(self):
        """Test getting audit summary."""
        logger = AuditLogger(enable_console=False)

        # Add some events
        event = SecurityEvent(
            event_type=SecurityEventType.RESOURCE_CREATE,
            severity=SecurityEventSeverity.INFO,
            message="Test event",
        )
        logger.log_event(event)

        summary = logger.get_audit_summary()

        assert "audit_summary" in summary
        assert "monitoring_status" in summary
        assert "last_updated" in summary
        assert summary["monitoring_status"] == "active"
        assert summary["audit_summary"]["total_events"] >= 1


class TestComplianceFramework:
    """Tests for compliance framework."""

    def test_initialization(self):
        """Test compliance framework initialization."""
        framework = ComplianceFramework()

        assert "aws_security" in framework.compliance_checks
        assert "soc2" in framework.compliance_checks
        assert "nist" in framework.compliance_checks

        # Check that check functions exist
        aws_checks = framework.compliance_checks["aws_security"]
        assert "encryption_at_rest" in aws_checks
        assert "least_privilege_access" in aws_checks

    def test_run_compliance_check_aws_security(self):
        """Test running AWS security compliance check."""
        framework = ComplianceFramework()

        # Test with encryption enabled
        config_with_encryption = {
            "enable_state_encryption": True,
            "strict_mode": True,
            "credential_config": {
                "role_arn": "arn:aws:iam::123456789012:role/test-role"
            },
        }

        result = framework.run_compliance_check("aws_security", config_with_encryption)

        assert result["framework"] == "aws_security"
        assert result["compliance_score"] > 0
        assert result["passed_checks"] > 0
        assert result["total_checks"] > 0
        assert "checks" in result
        assert "timestamp" in result

        # Check specific checks
        checks = result["checks"]
        assert checks["encryption_at_rest"]["passed"] is True
        assert checks["encryption_in_transit"]["passed"] is True
        assert checks["least_privilege_access"]["passed"] is True
        assert checks["secure_configurations"]["passed"] is True

    def test_run_compliance_check_without_config(self):
        """Test running compliance check without configuration."""
        framework = ComplianceFramework()

        result = framework.run_compliance_check("aws_security", None)

        assert result["framework"] == "aws_security"
        assert result["compliance_score"] >= 0

        # Some checks should fail without configuration
        checks = result["checks"]
        assert checks["encryption_at_rest"]["passed"] is False
        assert checks["least_privilege_access"]["passed"] is False
        assert checks["secure_configurations"]["passed"] is False

        # But some should pass (like encryption in transit)
        assert checks["encryption_in_transit"]["passed"] is True

    def test_run_compliance_check_soc2(self):
        """Test running SOC 2 compliance check."""
        framework = ComplianceFramework()

        config = {
            "enable_state_encryption": True,
            "credential_config": {
                "role_arn": "arn:aws:iam::123456789012:role/test-role"
            },
        }

        result = framework.run_compliance_check("soc2", config)

        assert result["framework"] == "soc2"
        assert result["compliance_score"] > 0
        assert "access_control" in result["checks"]
        assert "data_protection" in result["checks"]
        assert "system_monitoring" in result["checks"]
        assert "incident_response" in result["checks"]

    def test_run_compliance_check_nist(self):
        """Test running NIST compliance check."""
        framework = ComplianceFramework()

        result = framework.run_compliance_check("nist")

        assert result["framework"] == "nist"
        assert result["compliance_score"] > 0
        assert "identify" in result["checks"]
        assert "protect" in result["checks"]
        assert "detect" in result["checks"]
        assert "respond" in result["checks"]
        assert "recover" in result["checks"]

    def test_unknown_framework_error(self):
        """Test error handling for unknown compliance framework."""
        framework = ComplianceFramework()

        with pytest.raises(ValueError, match="Unknown compliance framework"):
            framework.run_compliance_check("unknown_framework")

    def test_compliance_check_error_handling(self):
        """Test error handling in compliance checks."""
        framework = ComplianceFramework()

        # Mock a check function to raise an error
        def failing_check(config):
            raise RuntimeError("Test error")

        framework.compliance_checks["test"] = {"failing_check": failing_check}

        result = framework.run_compliance_check("test")

        assert result["framework"] == "test"
        assert result["compliance_score"] == 0
        assert result["passed_checks"] == 0
        assert result["total_checks"] == 1
        assert result["checks"]["failing_check"]["passed"] is False
        assert "Test error" in result["checks"]["failing_check"]["error"]

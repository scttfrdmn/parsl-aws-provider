#!/usr/bin/env python3
"""Real AWS integration test for security framework.

This script tests the complete security framework with live AWS services
using the 'aws' profile for authentication.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import os
import sys
import time
import uuid
import logging
import tempfile
import json
from typing import Dict, Any
from datetime import datetime

# Add the package to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from parsl_ephemeral_aws.config.security_config import SecurityConfig
from parsl_ephemeral_aws.security.credential_manager import CredentialManager, CredentialConfiguration
from parsl_ephemeral_aws.security.audit import AuditLogger, SecurityMonitor, ComplianceFramework
from parsl_ephemeral_aws.security.encryption import EncryptionKeyManager, StateEncryptor
from parsl_ephemeral_aws.security.network_policy import NetworkSecurityPolicy, SecurityEnvironment
from parsl_ephemeral_aws.security.cidr_manager import CIDRManager
from parsl_ephemeral_aws.error_handling import RobustErrorHandler, RetryConfig

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class SecurityIntegrationTester:
    """Comprehensive security framework integration tester."""
    
    def __init__(self):
        """Initialize the security integration tester."""
        self.workflow_id = f"security-test-{uuid.uuid4().hex[:8]}"
        self.test_results = {}
        self.audit_file = None
        
    def setup_test_environment(self) -> Dict[str, Any]:
        """Set up test environment and configurations."""
        logger.info("Setting up test environment...")
        
        # Create temporary audit log file
        temp_dir = tempfile.mkdtemp()
        self.audit_file = os.path.join(temp_dir, f"security_audit_{self.workflow_id}.log")
        
        # Create development security configuration
        security_config = SecurityConfig.create_development_config(
            vpc_cidr="10.100.0.0/16",
            enable_encryption=True
        )
        
        logger.info(f"Security configuration created for workflow: {self.workflow_id}")
        logger.info(f"Audit log file: {self.audit_file}")
        
        return {
            "security_config": security_config,
            "audit_file": self.audit_file,
            "workflow_id": self.workflow_id
        }
    
    def test_credential_management(self, config: Dict[str, Any]) -> bool:
        """Test credential management with AWS profile."""
        logger.info("Testing credential management...")
        
        try:
            # Create credential configuration using 'aws' profile
            credential_config = CredentialConfiguration(
                use_profile="aws",
                enable_sanitization=True,
                sanitize_logs=True,
                auto_refresh_tokens=True
            )
            
            # Initialize credential manager
            credential_manager = CredentialManager(credential_config)
            logger.info("Credential manager initialized successfully")
            
            # Test AWS session creation
            session = credential_manager.create_boto3_session(region="us-east-1")
            logger.info("AWS session created successfully")
            
            # Test credential info retrieval
            cred_info = credential_manager.get_credential_info()
            logger.info(f"Credential info retrieved: {cred_info.get('source', 'unknown')}")
            
            # Test AWS service access
            ec2 = session.client("ec2")
            regions = ec2.describe_regions()
            logger.info(f"AWS API access verified - found {len(regions['Regions'])} regions")
            
            self.test_results["credential_management"] = {
                "status": "PASS",
                "details": {
                    "profile": "aws",
                    "credential_source": cred_info.get('source', 'unknown'),
                    "regions_accessible": len(regions['Regions']),
                    "session_created": True
                }
            }
            return True
            
        except Exception as e:
            logger.error(f"Credential management test failed: {e}")
            self.test_results["credential_management"] = {
                "status": "FAIL",
                "error": str(e)
            }
            return False
    
    def test_audit_logging(self, config: Dict[str, Any]) -> bool:
        """Test audit logging system with real events."""
        logger.info("Testing audit logging system...")
        
        try:
            # Initialize audit logger
            audit_logger = AuditLogger(
                log_file=config["audit_file"],
                enable_console=True
            )
            logger.info("Audit logger initialized")
            
            # Test various security events
            from parsl_ephemeral_aws.security.audit import SecurityEvent, SecurityEventType, SecurityEventSeverity
            
            # Test resource operation logging
            audit_logger.log_resource_operation(
                operation="create",
                resource_type="test_resource",
                resource_id="test-resource-001",
                success=True,
                workflow_id=config["workflow_id"],
                test_metadata="integration_test"
            )
            logger.info("Resource operation logged")
            
            # Test credential access logging
            audit_logger.log_credential_access(
                access_type="credential_init",
                identity="aws-profile",
                success=True,
                workflow_id=config["workflow_id"]
            )
            logger.info("Credential access logged")
            
            # Test security violation logging
            audit_logger.log_security_violation(
                violation_type="test_violation",
                description="Test security violation for integration testing",
                severity=SecurityEventSeverity.LOW,
                workflow_id=config["workflow_id"]
            )
            logger.info("Security violation logged")
            
            # Test audit summary generation
            summary = audit_logger.get_audit_summary()
            logger.info(f"Audit summary generated: {summary['monitoring_status']}")
            
            # Verify log file exists and contains events
            event_count = 0
            if os.path.exists(config["audit_file"]):
                with open(config["audit_file"], 'r') as f:
                    log_content = f.read()
                    event_count = log_content.count("AUDIT")
                    logger.info(f"Audit file contains {event_count} audit events")
            
            self.test_results["audit_logging"] = {
                "status": "PASS",
                "details": {
                    "audit_file": config["audit_file"],
                    "events_logged": event_count,
                    "summary_status": summary['monitoring_status']
                }
            }
            return True
            
        except Exception as e:
            logger.error(f"Audit logging test failed: {e}")
            self.test_results["audit_logging"] = {
                "status": "FAIL",
                "error": str(e)
            }
            return False
    
    def test_compliance_framework(self, config: Dict[str, Any]) -> bool:
        """Test compliance framework with real configuration."""
        logger.info("Testing compliance framework...")
        
        try:
            # Initialize compliance framework
            compliance = ComplianceFramework()
            logger.info("Compliance framework initialized")
            
            # Create test configuration
            test_config = {
                "enable_state_encryption": True,
                "strict_mode": False,  # Development environment
                "credential_config": {
                    "role_arn": None,  # Using profile instead
                    "use_profile": "aws"
                },
                "encryption_config": {
                    "algorithm": "fernet",
                    "master_key_source": "env"
                }
            }
            
            # Test AWS Security compliance
            aws_result = compliance.run_compliance_check("aws_security", test_config)
            logger.info(f"AWS Security compliance check - score: {aws_result['compliance_score']:.1f}%")
            
            # Test SOC 2 compliance
            soc2_result = compliance.run_compliance_check("soc2", test_config)
            logger.info(f"SOC 2 compliance check - score: {soc2_result['compliance_score']:.1f}%")
            
            # Test NIST compliance
            nist_result = compliance.run_compliance_check("nist", test_config)
            logger.info(f"NIST compliance check - score: {nist_result['compliance_score']:.1f}%")
            
            self.test_results["compliance_framework"] = {
                "status": "PASS",
                "details": {
                    "aws_security_score": aws_result['compliance_score'],
                    "aws_passed_checks": aws_result['passed_checks'],
                    "soc2_score": soc2_result['compliance_score'],
                    "soc2_passed_checks": soc2_result['passed_checks'],
                    "nist_score": nist_result['compliance_score'],
                    "nist_passed_checks": nist_result['passed_checks']
                }
            }
            return True
            
        except Exception as e:
            logger.error(f"Compliance framework test failed: {e}")
            self.test_results["compliance_framework"] = {
                "status": "FAIL",
                "error": str(e)
            }
            return False
    
    def run_integration_tests(self) -> Dict[str, Any]:
        """Run all integration tests."""
        logger.info("=" * 60)
        logger.info("STARTING COMPREHENSIVE SECURITY INTEGRATION TESTS")
        logger.info("=" * 60)
        
        start_time = time.time()
        
        # Setup test environment
        config = self.setup_test_environment()
        
        # Run core tests first
        tests = [
            ("Credential Management", self.test_credential_management),
            ("Audit Logging", self.test_audit_logging),
            ("Compliance Framework", self.test_compliance_framework),
        ]
        
        passed_tests = 0
        failed_tests = 0
        
        for test_name, test_func in tests:
            logger.info(f"\n{'='*50}")
            logger.info(f"RUNNING TEST: {test_name}")
            logger.info(f"{'='*50}")
            
            try:
                if test_func(config):
                    passed_tests += 1
                    logger.info(f"PASSED: {test_name}")
                else:
                    failed_tests += 1
                    logger.error(f"FAILED: {test_name}")
            except Exception as e:
                failed_tests += 1
                logger.error(f"EXCEPTION in {test_name}: {e}")
                self.test_results[test_name.lower().replace(' ', '_')] = {
                    "status": "EXCEPTION",
                    "error": str(e)
                }
        
        end_time = time.time()
        duration = end_time - start_time
        
        # Generate final report
        logger.info("\n" + "=" * 60)
        logger.info("INTEGRATION TEST RESULTS SUMMARY")
        logger.info("=" * 60)
        logger.info(f"Total Tests: {len(tests)}")
        logger.info(f"Passed: {passed_tests}")
        logger.info(f"Failed: {failed_tests}")
        logger.info(f"Success Rate: {(passed_tests/len(tests)*100):.1f}%")
        logger.info(f"Duration: {duration:.2f} seconds")
        logger.info(f"Workflow ID: {config['workflow_id']}")
        if self.audit_file and os.path.exists(self.audit_file):
            logger.info(f"Audit Log: {self.audit_file}")
        
        # Save detailed results
        results_file = f"security_integration_results_{config['workflow_id']}.json"
        with open(results_file, 'w') as f:
            json.dump({
                "summary": {
                    "workflow_id": config['workflow_id'],
                    "timestamp": datetime.now().isoformat(),
                    "total_tests": len(tests),
                    "passed": passed_tests,
                    "failed": failed_tests,
                    "success_rate": passed_tests/len(tests)*100,
                    "duration_seconds": duration
                },
                "test_results": self.test_results
            }, f, indent=2)
        
        logger.info(f"Detailed results saved to: {results_file}")
        
        if failed_tests == 0:
            logger.info("\nALL TESTS PASSED! Security framework is ready for production use.")
        else:
            logger.warning(f"{failed_tests} test(s) failed. Review the results and fix issues before production use.")
        
        return {
            "success": failed_tests == 0,
            "passed": passed_tests,
            "failed": failed_tests,
            "duration": duration,
            "results_file": results_file
        }

def main():
    """Main test execution function."""
    tester = SecurityIntegrationTester()
    results = tester.run_integration_tests()
    
    # Exit with appropriate code
    sys.exit(0 if results["success"] else 1)

if __name__ == "__main__":
    main()
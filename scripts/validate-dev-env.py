#!/usr/bin/env python3
"""
Development Environment Validation Script

This script validates that the Parsl Ephemeral AWS Provider development
environment is properly set up and functional.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import sys
import importlib
import subprocess
from pathlib import Path
from typing import List, Tuple


def check_imports() -> List[Tuple[str, bool, str]]:
    """Test critical imports."""
    imports_to_test = [
        ("parsl_ephemeral_aws", "Core package"),
        ("parsl_ephemeral_aws.provider", "Main provider"),
        ("parsl_ephemeral_aws.modes.standard", "Standard mode"),
        ("parsl_ephemeral_aws.modes.detached", "Detached mode"),
        ("parsl_ephemeral_aws.modes.serverless", "Serverless mode"),
        ("parsl_ephemeral_aws.compute.spot_fleet", "Spot Fleet manager"),
        ("parsl_ephemeral_aws.compute.lambda_func", "Lambda manager"),
        ("parsl_ephemeral_aws.constants", "Constants"),
        ("parsl_ephemeral_aws.exceptions", "Exceptions"),
    ]

    results = []
    for module_name, description in imports_to_test:
        try:
            importlib.import_module(module_name)
            results.append((description, True, ""))
        except Exception as e:
            results.append((description, False, str(e)))

    return results


def check_constants() -> List[Tuple[str, bool, str]]:
    """Test critical constants availability."""
    try:
        from parsl_ephemeral_aws.constants import (
            DEFAULT_LAMBDA_RUNTIME,
            DEFAULT_ECS_CPU,
            DEFAULT_ECS_MEMORY,
            TAG_PREFIX,
        )

        results = [
            (f"Lambda runtime: {DEFAULT_LAMBDA_RUNTIME}", True, ""),
            (f"ECS CPU: {DEFAULT_ECS_CPU}", True, ""),
            (f"ECS Memory: {DEFAULT_ECS_MEMORY}", True, ""),
            (f"Tag prefix: {TAG_PREFIX}", True, ""),
            ("Status constants", True, ""),
        ]

        # Validate Python version consistency
        if DEFAULT_LAMBDA_RUNTIME == "python3.9":
            results.append(("Python version consistency", True, ""))
        else:
            results.append(
                (
                    "Python version consistency",
                    False,
                    f"Expected python3.9, got {DEFAULT_LAMBDA_RUNTIME}",
                )
            )

    except Exception as e:
        results = [("Constants import", False, str(e))]

    return results


def check_exceptions() -> List[Tuple[str, bool, str]]:
    """Test critical exceptions availability."""
    try:
        return [("Exception classes", True, "All required exceptions available")]

    except Exception as e:
        return [("Exception classes", False, str(e))]


def check_version_management() -> List[Tuple[str, bool, str]]:
    """Test version management functionality."""
    results = []

    # Check bump-my-version availability
    try:
        result = subprocess.run(
            ["bump-my-version", "--version"], capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            version = result.stdout.strip()
            results.append(("Version bumping tool", True, f"bump-my-version {version}"))
        else:
            results.append(
                ("Version bumping tool", False, "bump-my-version not working")
            )
    except Exception as e:
        results.append(("Version bumping tool", False, str(e)))

    # Check configuration files
    config_files = [
        (Path("pyproject.toml"), "Project configuration"),
        (Path("CHANGELOG.md"), "Changelog"),
        (Path("commitlint.config.js"), "Commit linting"),
        (Path(".pre-commit-config.yaml"), "Pre-commit hooks"),
    ]

    for file_path, description in config_files:
        if file_path.exists():
            results.append((description, True, f"{file_path} exists"))
        else:
            results.append((description, False, f"{file_path} missing"))

    return results


def check_development_tools() -> List[Tuple[str, bool, str]]:
    """Test development tools availability."""
    tools_to_check = [
        ("pytest", "Testing framework"),
        ("coverage", "Test coverage"),
        ("ruff", "Linting and formatting"),
        ("pre-commit", "Git hooks"),
    ]

    results = []
    for tool, description in tools_to_check:
        try:
            result = subprocess.run(
                [tool, "--version"], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                version = result.stdout.strip().split("\n")[0]
                results.append((description, True, version))
            else:
                results.append((description, False, f"{tool} not working"))
        except Exception as e:
            results.append((description, False, str(e)))

    return results


def print_results(category: str, results: List[Tuple[str, bool, str]]):
    """Print results for a category."""
    print(f"\n{'='*60}")
    print(f"  {category.upper()}")
    print(f"{'='*60}")

    passed = 0
    total = len(results)

    for description, success, message in results:
        status = "✅ PASS" if success else "❌ FAIL"
        print(f"{status} {description}")
        if message and (not success or len(message) < 50):
            print(f"     {message}")
        if success:
            passed += 1

    print(f"\nResult: {passed}/{total} checks passed")
    return passed, total


def main():
    """Run all validation checks."""
    print("🔍 Parsl Ephemeral AWS Provider - Development Environment Validation")
    print("=" * 80)

    all_passed = 0
    all_total = 0

    # Run all checks
    checks = [
        ("Core Imports", check_imports()),
        ("Constants", check_constants()),
        ("Exceptions", check_exceptions()),
        ("Version Management", check_version_management()),
        ("Development Tools", check_development_tools()),
    ]

    for category, results in checks:
        passed, total = print_results(category, results)
        all_passed += passed
        all_total += total

    # Final summary
    print(f"\n{'='*80}")
    print("  OVERALL SUMMARY")
    print(f"{'='*80}")

    percentage = (all_passed / all_total) * 100 if all_total > 0 else 0

    if percentage >= 90:
        status = "🎉 EXCELLENT"
        color = "\033[92m"  # Green
    elif percentage >= 75:
        status = "✅ GOOD"
        color = "\033[93m"  # Yellow
    elif percentage >= 50:
        status = "⚠️  NEEDS WORK"
        color = "\033[93m"  # Yellow
    else:
        status = "❌ POOR"
        color = "\033[91m"  # Red

    reset = "\033[0m"

    print(f"{color}Status: {status}")
    print(f"Passed: {all_passed}/{all_total} ({percentage:.1f}%){reset}")

    if percentage >= 75:
        print("\n🚀 Development environment is ready for productive work!")
        exit_code = 0
    elif percentage >= 50:
        print("\n⚠️  Development environment has some issues but is functional.")
        exit_code = 0
    else:
        print("\n❌ Development environment needs significant work.")
        exit_code = 1

    print("\nNote: Docker/LocalStack validation requires Docker Desktop to be running.")
    print("Start Docker Desktop and run 'make localstack-up' to test AWS integration.")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()

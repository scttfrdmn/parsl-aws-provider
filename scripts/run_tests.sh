#!/usr/bin/env bash
# Script to run tests with coverage reporting
# Supports running unit tests, integration tests, or all tests
#
# NOTE: this duplicates `make test-unit` / `make test-integration`, is referenced
# by nothing, and installs pytest with pip rather than uv (contra CLAUDE.md).
# Tracked for removal in v0.8.0. The emulator references below were updated in
# #125 only so it does not point at a dead LocalStack endpoint.
#
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"

# Default settings
COVERAGE=true
TEST_TYPE="all"
SKIP_SUBSTRATE_CHECK=false
SUBSTRATE_ENDPOINT="${SUBSTRATE_ENDPOINT:-http://localhost:4566}"

# Help message
function show_help {
  echo "Usage: $0 [OPTIONS]"
  echo "Run tests for Parsl Ephemeral AWS Provider with coverage reporting."
  echo
  echo "Options:"
  echo "  -h, --help               Show this help message"
  echo "  -t, --type TYPE          Type of tests to run: unit, integration, all (default: all)"
  echo "  --no-coverage            Run tests without coverage reporting"
  echo "  --skip-substrate-check   Skip checking if substrate is running for integration tests"
  echo "  --substrate-endpoint     Specify custom substrate endpoint (default: http://localhost:4566)"
  echo
  echo "Examples:"
  echo "  $0 --type unit           Run unit tests with coverage reporting"
  echo "  $0 --type integration    Run integration tests with coverage reporting"
  echo "  $0 --no-coverage         Run all tests without coverage reporting"
}

# Parse arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      show_help
      exit 0
      ;;
    -t|--type)
      TEST_TYPE="$2"
      shift 2
      ;;
    --no-coverage)
      COVERAGE=false
      shift
      ;;
    --skip-substrate-check)
      SKIP_SUBSTRATE_CHECK=true
      shift
      ;;
    --substrate-endpoint)
      SUBSTRATE_ENDPOINT="$2"
      shift 2
      ;;
    *)
      echo "Unknown option: $1"
      show_help
      exit 1
      ;;
  esac
done

# Validate test type
if [[ "$TEST_TYPE" != "unit" && "$TEST_TYPE" != "integration" && "$TEST_TYPE" != "all" ]]; then
  echo "Error: Invalid test type '$TEST_TYPE'. Must be 'unit', 'integration', or 'all'."
  exit 1
fi

# Check if Python and pytest are available
if ! command -v python3 &>/dev/null; then
  echo "Error: Python 3 is required but not found in PATH."
  exit 1
fi

# Find Python package manager
if command -v pip3 &>/dev/null; then
  PIP_CMD="pip3"
elif command -v pip &>/dev/null; then
  PIP_CMD="pip"
else
  echo "Error: pip or pip3 not found in PATH."
  exit 1
fi

# Install test dependencies if needed
echo "Checking for test dependencies..."
"$PIP_CMD" install -q pytest pytest-cov

# Check if running integration tests and the emulator is needed
if [[ "$TEST_TYPE" == "integration" || "$TEST_TYPE" == "all" ]] && [[ "$SKIP_SUBSTRATE_CHECK" == "false" ]]; then
  echo "Checking substrate availability at $SUBSTRATE_ENDPOINT..."
  if ! curl -fsS -m 5 "$SUBSTRATE_ENDPOINT/health" >/dev/null 2>&1; then
    echo "substrate does not appear to be running at $SUBSTRATE_ENDPOINT."
    echo "Integration tests may fail or be skipped."
    echo "To start substrate:"
    echo "  make substrate-up"
    echo "To skip this check, use --skip-substrate-check"

    read -r -p "Do you want to continue anyway? (y/N) " response
    if [[ ! "$response" =~ ^[Yy]$ ]]; then
      exit 1
    fi
  else
    echo "substrate is running."
  fi
fi

# Set up test paths
if [[ "$TEST_TYPE" == "unit" ]]; then
  TEST_PATH="$PROJECT_ROOT/tests/unit"
  echo "Running unit tests..."
elif [[ "$TEST_TYPE" == "integration" ]]; then
  TEST_PATH="$PROJECT_ROOT/tests/integration"
  echo "Running integration tests..."
else
  TEST_PATH="$PROJECT_ROOT/tests"
  echo "Running all tests..."
fi

# Run tests with or without coverage
if [[ "$COVERAGE" == "true" ]]; then
  echo "Collecting coverage information..."
  python3 -m pytest "$TEST_PATH" -v --cov=parsl_ephemeral_aws --cov-report=term --cov-report=html --cov-config="$PROJECT_ROOT/.coveragerc"

  echo "Coverage report written to $PROJECT_ROOT/coverage_html_report/"

  # Print coverage summary
  echo "Coverage summary:"
  python3 -m coverage report
else
  python3 -m pytest "$TEST_PATH" -v
fi

echo "All tests completed."

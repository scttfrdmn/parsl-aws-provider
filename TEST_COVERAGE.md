# Test Coverage for Parsl Ephemeral AWS Provider

This document tracks the test coverage for the Parsl Ephemeral AWS Provider project.

## Current Test Status

| Test Type | Status | Coverage |
|-----------|--------|----------|
| Unit Tests | ✅ Complete | TBD |
| Integration Tests | ✅ Complete | TBD |
| Error Handling Tests | ✅ Complete | TBD |
| Shell Script Tests (BATS) | 🚧 In Progress | N/A |

## Key Components Coverage

| Component | Unit Tests | Integration Tests | Notes |
|-----------|------------|-------------------|-------|
| Provider Class | ✅ | ✅ | Full lifecycle testing |
| Standard Mode | ✅ | ✅ | All operations covered |
| Detached Mode | ✅ | ✅ | All operations covered |
| Serverless Mode | ✅ | ✅ | All worker types tested |
| EC2 Manager | ✅ | ✅ | |
| Lambda Manager | ✅ | ✅ | |
| ECS Manager | ✅ | ✅ | |
| Spot Fleet Manager | ✅ | ✅ | |
| Spot Interruption Handling | ✅ | ✅ | |
| VPC Manager | ✅ | ✅ | |
| Security Group Manager | ✅ | ✅ | |
| State Persistence | ✅ | ✅ | File/S3/Parameter Store |
| Error Handling | ✅ | ✅ | All error scenarios |

## Coverage Details

Last coverage report run: TBD

```
# Coverage details will be updated after running the test coverage script
```

## Running Test Coverage

Use the provided script to run tests with coverage reporting:

```bash
# Run all tests with coverage reporting
./scripts/run_tests.sh

# Run only unit tests with coverage
./scripts/run_tests.sh --type unit

# Run only integration tests with coverage
./scripts/run_tests.sh --type integration

# Run without coverage reporting
./scripts/run_tests.sh --no-coverage
```

## Integration with LocalStack

Integration tests can use [LocalStack](https://localstack.cloud/) to emulate AWS services. To run integration tests with LocalStack:

1. Start LocalStack:
   ```bash
   docker run -d --name localstack -p 4566:4566 -p 4571:4571 localstack/localstack
   ```

2. Run integration tests:
   ```bash
   ./scripts/run_tests.sh --type integration
   ```

## Test Improvement Roadmap

1. ✅ Create comprehensive unit tests
2. ✅ Create integration tests for each operating mode
3. ✅ Implement error handling tests
4. ✅ Add spot interruption handling tests
5. ✅ Add multi-node workflow tests
6. 🚧 Set up BATS testing for shell scripts
7. 📝 Add moto-based tests for AWS API mocking
8. 📝 Set up CI/CD integration for testing

## Code Quality Metrics

| Metric | Tool | Status |
|--------|------|--------|
| Test Coverage | pytest-cov | TBD |
| Type Checking | mypy | Pending |
| Code Linting | flake8/ruff | Pending |
| Shell Script Linting | shellcheck | Pending |
| Security Analysis | bandit | Pending |

---

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
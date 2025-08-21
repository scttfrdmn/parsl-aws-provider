# Testing the Parsl Ephemeral AWS Provider

This document outlines the testing approach for the Parsl Ephemeral AWS Provider, including unit tests, integration tests, and testing with both mocked and real AWS services.

## Testing Tools

The provider can be tested using several approaches:

1. **Unit Tests with Moto**: Fast, isolated tests using the Moto library to mock AWS services
2. **Integration Tests with LocalStack**: More comprehensive tests using LocalStack to emulate AWS services
3. **Integration Tests with Real AWS**: End-to-end tests using actual AWS services

## Test Dependencies

Install test dependencies:

```bash
pip install -e ".[test]"
```

This installs:
- pytest
- pytest-cov
- moto
- coverage
- bats-core (for shell script testing)

To run tests with LocalStack, you'll need to install and run LocalStack:

```bash
pip install localstack
localstack start
```

## Running Tests

### Unit Tests

Run unit tests with Moto mocking:

```bash
# Run all unit tests
pytest tests/unit/

# Run specific test file
pytest tests/unit/test_aws_mocking.py
```

### Integration Tests with LocalStack

Run integration tests using LocalStack:

```bash
# Make sure LocalStack is running
localstack start

# Run all integration tests
pytest tests/integration/

# Run specific integration test file
pytest tests/integration/test_state_persistence_integration.py
```

### All Tests with Coverage

Run all tests and generate a coverage report:

```bash
# Run the test script
./scripts/run_tests.sh --coverage

# Or run pytest directly with coverage
pytest --cov=parsl_ephemeral_aws --cov-report=term --cov-report=html
```

## AWS Mocking with Moto

The `tests/unit/test_aws_mocking.py` file provides comprehensive tests that use the Moto library to mock AWS services. These tests:

1. Mock AWS services like EC2, S3, Parameter Store, Lambda, ECS, etc.
2. Test the full lifecycle of AWS resources (create, use, delete)
3. Verify proper error handling and edge cases
4. Can run without external dependencies or AWS credentials

Moto tests are marked to be skipped if the Moto library is not available:

```python
try:
    from moto import mock_ec2, mock_s3, mock_ssm, mock_iam, mock_lambda, mock_ecs, mock_cloudformation
    MOTO_AVAILABLE = True
except ImportError:
    MOTO_AVAILABLE = False
    # Create placeholder decorators if moto is not available
    def mock_decorator(func):
        return pytest.mark.skip(reason="Moto library not available")(func)
    mock_ec2 = mock_s3 = mock_ssm = mock_iam = mock_lambda = mock_ecs = mock_cloudformation = mock_decorator

# Mark tests as requiring moto
pytestmark = pytest.mark.skipif(
    not MOTO_AVAILABLE,
    reason="Moto library not available. Install with: pip install moto"
)
```

## Integration Testing with LocalStack

The integration tests in `tests/integration/` use LocalStack to provide a more realistic emulation of AWS services. These tests:

1. Test interactions between multiple AWS services
2. Verify complete workflow scenarios
3. Test state persistence and recovery mechanisms
4. Test error handling and recovery in realistic scenarios

Integration tests are marked to be skipped if LocalStack is not available:

```python
# Skip if LocalStack is not running
@pytest.mark.skipif(
    not is_localstack_running(),
    reason="LocalStack is not running. Start with: localstack start"
)
def test_s3_state_persistence():
    # Test code here
```

## Testing Spot Fleet and Interruption Handling

The provider includes comprehensive tests for Spot Fleet management and interruption handling:

1. **Mock-based tests**: Test spot interruption handling logic without actual AWS
2. **Synthetic interruption tests**: Simulate spot interruptions in test environments
3. **State recovery tests**: Verify workflow recovery after interruptions

## BATS Testing for Shell Scripts

For testing shell scripts (like `run_tests.sh`), the project uses BATS (Bash Automated Testing System):

```bash
# Install BATS
npm install -g bats

# Run BATS tests
bats tests/bats/
```

## Continuous Integration

The CI workflow in GitHub Actions runs:

1. Unit tests with Moto on multiple Python versions
2. Integration tests with LocalStack
3. Linting and type checking
4. Coverage reporting to Codecov

## Test Coverage Tracking

The project maintains a test coverage report that's updated with each test run:

```bash
# Generate coverage report
pytest --cov=parsl_ephemeral_aws --cov-report=term --cov-report=html

# View HTML coverage report
open coverage_html_report/index.html
```

The coverage report tracks:
- Overall code coverage percentage
- Coverage by module and function
- Missed lines and branches

## Testing Best Practices

When adding new features:

1. Add unit tests with Moto mocking first
2. Add integration tests with LocalStack
3. For complex AWS interactions, consider manual testing with real AWS
4. Ensure spot interruption handling is tested for all modes
5. Test edge cases and error scenarios
6. Verify resource cleanup after tests

# Parsl Ephemeral AWS Provider Tests

This directory contains the test suite for the Parsl Ephemeral AWS Provider.

## Test Structure

The tests are organized into the following categories:

- `unit/`: Unit tests for individual components
  - `test_standard_mode.py`: Tests for StandardMode
  - `test_detached_mode.py`: Tests for DetachedMode
  - `test_serverless_mode.py`: Tests for ServerlessMode
  - Other unit tests for various components

- `integration/`: Integration tests that test the interactions between components
  - `test_localstack_modes.py`: Tests using LocalStack for AWS service emulation

## Running Tests

### Unit Tests

To run all unit tests:

```bash
pytest tests/unit
```

To run tests for a specific mode:

```bash
pytest tests/unit/test_standard_mode.py
pytest tests/unit/test_detached_mode.py
pytest tests/unit/test_serverless_mode.py
```

### Integration Tests

Integration tests require LocalStack to be running. To run LocalStack:

```bash
pip install localstack
localstack start
```

Then, in a separate terminal, run the integration tests:

```bash
pytest tests/integration
```

### Code Coverage

To run tests with code coverage:

```bash
pytest --cov=parsl_ephemeral_aws tests
```

To generate a coverage report:

```bash
pytest --cov=parsl_ephemeral_aws --cov-report=html tests
```

This will create an HTML report in the `htmlcov` directory that you can view in a browser.

## Writing Tests

When adding new features or fixing bugs, please follow these guidelines for writing tests:

1. **Unit Tests**: Write unit tests for each new function or class
   - Focus on testing individual components in isolation
   - Use mocks for external dependencies like AWS services
   - Test both successful paths and error handling

2. **Integration Tests**: Write integration tests for interactions between components
   - Use LocalStack for AWS service emulation
   - Test realistic workflows that span multiple components
   - Clean up any resources created during tests

## CI Pipeline

The CI pipeline runs the following checks:

1. **Unit Tests**: Run all unit tests on multiple Python versions
2. **Integration Tests**: Run integration tests using LocalStack
3. **Linting**: Check code style with flake8
4. **Type Checking**: Verify type annotations with mypy
5. **Code Coverage**: Generate coverage report and upload to Codecov
6. **Package Building**: Verify the package can be built correctly

## Additional Resources

- [pytest Documentation](https://docs.pytest.org/)
- [LocalStack Documentation](https://docs.localstack.cloud/)
- [moto Documentation](https://docs.getmoto.org/) (for AWS service mocking)
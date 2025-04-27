# CI/CD Pipeline Configuration

This document describes the continuous integration and continuous deployment (CI/CD) pipeline for the Parsl Ephemeral AWS Provider.

## Overview

The CI/CD pipeline automates testing, validation, and deployment processes, ensuring code quality and reliability. The pipeline is configured to run on both pull requests and merges to the main branch.

## Pipeline Components

### 1. Code Validation

The first stage validates code quality and style:

* **Linting**: Ensures code adheres to PEP 8 style guidelines
* **Type Checking**: Verifies type annotations with mypy
* **Security Scanning**: Identifies potential security issues
* **Formatting Check**: Verifies code formatting with black

### 2. Unit Testing

The second stage runs unit tests:

* **Test Execution**: Runs pytest test suite
* **Coverage Analysis**: Generates code coverage reports
* **Mocked AWS Tests**: Tests AWS interactions using moto

### 3. Integration Testing with LocalStack

The third stage tests with LocalStack:

* **LocalStack Environment**: Spins up LocalStack container
* **Service Verification**: Validates services are operating correctly
* **Integration Tests**: Runs tests against LocalStack services

### 4. Cross-Platform Testing

The fourth stage tests across operating systems:

* **Linux Testing**: Ubuntu latest
* **macOS Testing**: Latest macOS version
* **Windows Testing**: Windows Server

### 5. Python Version Testing

The fifth stage tests across Python versions:

* **Python 3.8**: Minimum supported version
* **Python 3.9**: Established version
* **Python 3.10**: Established version
* **Python 3.11**: Established version
* **Python 3.12**: Latest version

### 6. Documentation Building

The sixth stage builds and validates documentation:

* **API Docs**: Generates API documentation
* **User Guides**: Builds user guides
* **Example Validation**: Verifies examples are correct
* **Link Checking**: Ensures all links are valid

### 7. Package Building and Publishing

The final stage builds and publishes the package:

* **Package Building**: Creates distribution packages
* **Validation**: Verifies package contents
* **Publication**: Publishes to PyPI (only on release)

## GitHub Actions Configuration

Below is the GitHub Actions workflow configuration:

```yaml
name: CI/CD Pipeline

on:
  push:
    branches: [ main ]
  pull_request:
    branches: [ main ]
  release:
    types: [ published ]

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -e ".[dev]"
      - name: Lint with ruff
        run: ruff check .
      - name: Check types with mypy
        run: mypy parsl_ephemeral_aws
      - name: Security scan with bandit
        run: bandit -r parsl_ephemeral_aws
      - name: Check formatting with black
        run: black --check parsl_ephemeral_aws tests

  unit-test:
    runs-on: ubuntu-latest
    needs: validate
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -e ".[dev,test]"
      - name: Run unit tests
        run: pytest -xvs tests/unit
      - name: Generate coverage report
        run: |
          pytest --cov=parsl_ephemeral_aws tests/unit
          python -m coverage xml
      - name: Upload coverage to Codecov
        uses: codecov/codecov-action@v3
        with:
          file: ./coverage.xml
          fail_ci_if_error: false

  localstack-test:
    runs-on: ubuntu-latest
    needs: unit-test
    services:
      localstack:
        image: localstack/localstack:latest
        ports:
          - 4566:4566
        env:
          SERVICES: ec2,s3,ssm,lambda,ecs,cloudformation
          DEFAULT_REGION: us-east-1
          AWS_DEFAULT_REGION: us-east-1
          LOCALSTACK_PERSISTENCE: 1
        options: >-
          --health-cmd "curl -s http://localhost:4566/_localstack/health | grep -q '\"ec2\":\"running\"'"
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -e ".[dev,test]"
      - name: Verify LocalStack
        run: |
          # Install AWS CLI v2
          curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
          unzip awscliv2.zip
          sudo ./aws/install
          
          # Configure AWS CLI to use LocalStack
          aws configure set aws_access_key_id test
          aws configure set aws_secret_access_key test
          aws configure set region us-east-1
          aws configure set output json
          
          # Verify LocalStack is running correctly
          aws --endpoint-url=http://localhost:4566 ec2 describe-regions
      - name: Run integration tests with LocalStack
        run: |
          # Set environment variables for LocalStack
          export AWS_ENDPOINT_URL=http://localhost:4566
          export AWS_ACCESS_KEY_ID=test
          export AWS_SECRET_ACCESS_KEY=test
          export AWS_DEFAULT_REGION=us-east-1
          
          # Run the tests
          pytest -xvs tests/integration

  cross-platform:
    needs: unit-test
    strategy:
      matrix:
        os: [ubuntu-latest, macos-latest, windows-latest]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -e ".[dev,test]"
      - name: Run platform-specific tests
        run: pytest -xvs tests/unit

  python-versions:
    needs: unit-test
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.8", "3.9", "3.10", "3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python ${{ matrix.python-version }}
        uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -e ".[dev,test]"
      - name: Run tests with Python ${{ matrix.python-version }}
        run: pytest -xvs tests/unit

  docs:
    needs: validate
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -e ".[dev,docs]"
      - name: Build documentation
        run: |
          cd docs
          make html
      - name: Check links
        run: |
          pip install linkchecker
          linkchecker docs/_build/html/index.html
      - name: Deploy documentation (on main only)
        if: github.ref == 'refs/heads/main'
        uses: peaceiris/actions-gh-pages@v3
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          publish_dir: ./docs/_build/html

  build-and-publish:
    needs: [unit-test, localstack-test, cross-platform, python-versions, docs]
    runs-on: ubuntu-latest
    if: github.event_name == 'release'
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install build dependencies
        run: |
          python -m pip install --upgrade pip
          pip install build twine
      - name: Build package
        run: python -m build
      - name: Check package
        run: twine check dist/*
      - name: Publish to PyPI
        if: startsWith(github.ref, 'refs/tags')
        uses: pypa/gh-action-pypi-publish@release/v1
        with:
          password: ${{ secrets.PYPI_API_TOKEN }}
```

## Setting Up the Pipeline

To set up the CI/CD pipeline for your fork or deployment:

1. **Set Up GitHub Repository**:
   - Enable GitHub Actions in your repository settings
   - Configure branch protection rules for the main branch

2. **Configure Secrets**:
   - Add `PYPI_API_TOKEN` secret for PyPI publishing
   - Add any AWS test account credentials if needed

3. **Enable GitHub Pages**:
   - Configure GitHub Pages to publish from the gh-pages branch

4. **Configure Codecov**:
   - Set up a Codecov account and add your repository
   - Add the Codecov token as a secret if required

## Interpreting CI/CD Results

### Dashboard

The GitHub Actions dashboard provides a visual representation of pipeline runs. Key components include:

* **Workflow Status**: Overall success/failure of the run
* **Job Details**: Individual job success/failure
* **Logs**: Detailed logs for each step
* **Artifacts**: Build artifacts (if any)

### Badges

Add these badges to your README.md to show pipeline status:

```markdown
[![CI/CD Pipeline](https://github.com/owner/repo/actions/workflows/ci-cd.yml/badge.svg)](https://github.com/owner/repo/actions/workflows/ci-cd.yml)
[![codecov](https://codecov.io/gh/owner/repo/branch/main/graph/badge.svg)](https://codecov.io/gh/owner/repo)
[![Documentation](https://img.shields.io/badge/docs-latest-blue.svg)](https://owner.github.io/repo/)
[![PyPI version](https://badge.fury.io/py/parsl-ephemeral-aws.svg)](https://badge.fury.io/py/parsl-ephemeral-aws)
```

## Custom Test Environments

### LocalStack Configuration

The LocalStack configuration in the CI/CD pipeline ensures proper testing of AWS interactions:

```yaml
services:
  localstack:
    image: localstack/localstack:latest
    ports:
      - 4566:4566
    env:
      SERVICES: ec2,s3,ssm,lambda,ecs,cloudformation
      DEFAULT_REGION: us-east-1
      AWS_DEFAULT_REGION: us-east-1
      LOCALSTACK_PERSISTENCE: 1
    options: >-
      --health-cmd "curl -s http://localhost:4566/_localstack/health | grep -q '\"ec2\":\"running\"'"
      --health-interval 10s
      --health-timeout 5s
      --health-retries 5
```

This configuration:
- Starts LocalStack with the required AWS services
- Configures health checks to ensure LocalStack is running properly
- Sets up persistence to maintain state during tests

## Extending the Pipeline

To extend the pipeline for additional functionality:

1. **Add Custom Jobs**:
   - Add new job definitions to the workflow file
   - Specify dependencies using the `needs` keyword

2. **Add Environment-Specific Testing**:
   - Use matrix strategies for testing across environments
   - Configure environment variables for different test scenarios

3. **Add Deployment Steps**:
   - Configure deployment to test/staging environments
   - Add approval steps for production deployment

## Best Practices

For optimal CI/CD performance:

1. **Keep Tests Fast**:
   - Optimize tests to run quickly
   - Use parallelization where possible

2. **Manage Dependencies**:
   - Cache dependencies to speed up builds
   - Use dependency locking for reproducible builds

3. **Secure Secrets**:
   - Use GitHub Secrets for sensitive information
   - Rotate credentials regularly

4. **Monitor Performance**:
   - Track pipeline execution times
   - Optimize slow-running jobs

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
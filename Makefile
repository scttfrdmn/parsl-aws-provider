# Makefile for Parsl Ephemeral AWS Provider

.PHONY: clean test lint type-check test-unit test-integration test-bats docs build install install-dev

# Default target
all: lint type-check test build

# Clean build artifacts
clean:
	rm -rf build/ dist/ *.egg-info/ .pytest_cache/ .coverage htmlcov/
	find . -name "*.pyc" -o -name "*.pyo" -o -name "__pycache__" | xargs rm -rf
	find . -name "*.egg-info" | xargs rm -rf

# Install for development
install-dev:
	pip install -e ".[dev,test]"

# Install for production
install:
	pip install .

# Run tests
test: test-unit test-integration test-bats

# Run unit tests
test-unit:
	pytest tests/unit/ -v

# Run integration tests
test-integration:
	pytest tests/integration/ -v

# Run BATS tests for shell scripts
test-bats:
	@if command -v bats >/dev/null 2>&1; then \
		echo "Running BATS tests..."; \
		bats tests/bats/; \
	else \
		echo "BATS not installed. Skipping shell script tests."; \
		echo "Install with: brew install bats-core (macOS) or apt-get install bats (Ubuntu)"; \
	fi

# Run linting
lint: lint-python lint-shell

# Run Python linting
lint-python:
	flake8 parsl_ephemeral_aws tests

# Run shell script linting
lint-shell:
	@if command -v shellcheck >/dev/null 2>&1; then \
		echo "Running shellcheck..."; \
		shellcheck scripts/*.sh; \
	else \
		echo "shellcheck not installed. Skipping shell script linting."; \
		echo "Install with: brew install shellcheck (macOS) or apt-get install shellcheck (Ubuntu)"; \
	fi

# Run type checking
type-check:
	mypy parsl_ephemeral_aws

# Build documentation
docs:
	cd docs && make html

# Build package
build: clean
	python setup.py sdist bdist_wheel

# Create a release
release: lint type-check test build
	@echo "Package ready for release. Use the following commands to publish:"
	@echo "  twine check dist/*"
	@echo "  twine upload dist/*"

# Run security checks
security:
	bandit -r parsl_ephemeral_aws -x tests/

# Code coverage
coverage:
	pytest --cov=parsl_ephemeral_aws --cov-report=html tests/

# Setup development environment
setup:
	./scripts/setup_environment.sh
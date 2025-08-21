# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors

# Makefile for Parsl Ephemeral AWS Provider

.PHONY: clean test lint type-check test-unit test-integration test-bats docs build install install-dev help
.PHONY: localstack-up localstack-down localstack-status test-aws coverage format pre-commit version-check

# Colors for output
BLUE := \033[36m
YELLOW := \033[33m
GREEN := \033[32m
RED := \033[31m
RESET := \033[0m

# Configuration
PYTHON := python3
COVERAGE_MIN := 10

# Auto-detect container runtime (prefer podman, fallback to docker)
PODMAN_AVAILABLE := $(shell which podman > /dev/null 2>&1 && echo "yes")
DOCKER_AVAILABLE := $(shell which docker > /dev/null 2>&1 && echo "yes")

ifeq ($(PODMAN_AVAILABLE),yes)
    CONTAINER_CMD := podman
    COMPOSE_CMD := podman compose
else ifeq ($(DOCKER_AVAILABLE),yes)
    CONTAINER_CMD := docker
    COMPOSE_CMD := docker compose
else
    $(error Neither podman nor docker found. Please install one of them.)
endif

LOCALSTACK_COMPOSE := $(COMPOSE_CMD) -f docker-compose.localstack.yml

# Default target
all: lint type-check test build

help: ## Show this help message
	@echo "$(BLUE)Parsl Ephemeral AWS Provider Development Commands$(RESET)"
	@echo ""
	@egrep '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "$(BLUE)%-25s$(RESET) %s\n", $$1, $$2}'

# Clean build artifacts
clean: ## Clean build artifacts and cache
	@echo "$(YELLOW)Cleaning build artifacts...$(RESET)"
	rm -rf build/ dist/ *.egg-info/ .pytest_cache/ .coverage htmlcov/ .ruff_cache/ .mypy_cache/
	find . -name "*.pyc" -o -name "*.pyo" -o -name "__pycache__" | xargs rm -rf
	find . -name "*.egg-info" | xargs rm -rf
	@echo "$(GREEN)Clean complete$(RESET)"

# Install for development
install-dev: ## Install package with development dependencies
	@echo "$(YELLOW)Installing development dependencies...$(RESET)"
	pip install -e ".[dev,test]"
	pre-commit install
	pre-commit install --hook-type commit-msg
	@echo "$(GREEN)Development environment ready$(RESET)"

# Install for production
install: ## Install package in production mode
	@echo "$(YELLOW)Installing package...$(RESET)"
	pip install .
	@echo "$(GREEN)Package installed$(RESET)"

# Run tests
test: test-unit test-integration test-bats ## Run all tests

# Run unit tests
test-unit: ## Run unit tests only
	@echo "$(YELLOW)Running unit tests...$(RESET)"
	pytest tests/unit/ -v -m "unit"

# Run integration tests with LocalStack
test-integration: localstack-up ## Run integration tests with LocalStack
	@echo "$(YELLOW)Running integration tests with LocalStack...$(RESET)"
	@$(MAKE) localstack-wait
	pytest tests/integration/ -v -m "integration or localstack"

# Run integration tests against real AWS
test-aws: ## Run integration tests against real AWS (costs money!)
	@echo "$(YELLOW)Running integration tests against real AWS...$(RESET)"
	@echo "$(RED)WARNING: This will create real AWS resources and may incur costs!$(RESET)"
	@read -p "Continue? [y/N] " response && [ "$$response" = "y" ] || (echo "Aborted" && exit 1)
	AWS_PROFILE=aws pytest tests/integration/ -v -m "aws"

# Run BATS tests for shell scripts
test-bats: ## Run BATS tests for shell scripts
	@if command -v bats >/dev/null 2>&1; then \
		echo "$(YELLOW)Running BATS tests...$(RESET)"; \
		bats tests/bats/; \
	else \
		echo "$(YELLOW)BATS not installed. Skipping shell script tests.$(RESET)"; \
		echo "Install with: brew install bats-core (macOS) or apt-get install bats (Ubuntu)"; \
	fi

# LocalStack management
localstack-up: ## Start LocalStack for testing
	@echo "$(YELLOW)Starting LocalStack...$(RESET)"
	$(LOCALSTACK_COMPOSE) up -d
	@$(MAKE) localstack-wait

localstack-wait: ## Wait for LocalStack to be ready
	@echo "$(YELLOW)Waiting for LocalStack to be ready...$(RESET)"
	./scripts/localstack-wait.sh

localstack-down: ## Stop LocalStack
	@echo "$(YELLOW)Stopping LocalStack...$(RESET)"
	$(LOCALSTACK_COMPOSE) down

localstack-status: ## Check LocalStack status
	@echo "$(YELLOW)LocalStack status:$(RESET)"
	@$(LOCALSTACK_COMPOSE) ps
	@echo ""
	@echo "$(YELLOW)Health check:$(RESET)"
	@curl -s http://localhost:4566/health | python3 -m json.tool || echo "LocalStack not responding"

# Run linting
lint: lint-python lint-shell ## Run all linting checks

# Run Python linting
lint-python: ## Run Python linting with ruff
	@echo "$(YELLOW)Running Python linting...$(RESET)"
	ruff check .
	ruff format --check .

# Run shell script linting
lint-shell: ## Run shell script linting
	@if command -v shellcheck >/dev/null 2>&1; then \
		echo "$(YELLOW)Running shellcheck...$(RESET)"; \
		shellcheck scripts/*.sh; \
	else \
		echo "$(YELLOW)shellcheck not installed. Skipping shell script linting.$(RESET)"; \
		echo "Install with: brew install shellcheck (macOS) or apt-get install shellcheck (Ubuntu)"; \
	fi

# Format code
format: ## Format code with ruff
	@echo "$(YELLOW)Formatting code...$(RESET)"
	ruff format .
	ruff check --fix .

# Run type checking
type-check: ## Run type checking with mypy
	@echo "$(YELLOW)Running type checks...$(RESET)"
	mypy parsl_ephemeral_aws

# Run pre-commit hooks
pre-commit: ## Run all pre-commit hooks
	@echo "$(YELLOW)Running pre-commit hooks...$(RESET)"
	pre-commit run --all-files

# Build documentation
docs: ## Generate documentation
	@echo "$(YELLOW)Generating documentation...$(RESET)"
	cd docs && make html
	@echo "$(GREEN)Documentation generated in docs/_build/html/$(RESET)"

# Build package
build: clean ## Build package for distribution
	@echo "$(YELLOW)Building package...$(RESET)"
	python setup.py sdist bdist_wheel
	@echo "$(GREEN)Package built$(RESET)"

# Create a release
release: lint type-check test build ## Prepare package for release
	@echo "$(GREEN)Package ready for release. Use the following commands to publish:$(RESET)"
	@echo "  twine check dist/*"
	@echo "  twine upload dist/*"

# Run security checks
security: ## Run security scan with bandit
	@echo "$(YELLOW)Running security scan...$(RESET)"
	bandit -r parsl_ephemeral_aws -c pyproject.toml

# Code coverage
coverage: ## Generate test coverage report (LocalStack only)
	@echo "$(YELLOW)Generating coverage report...$(RESET)"
	coverage run -m pytest -m "not aws"
	coverage report --fail-under=$(COVERAGE_MIN)
	coverage html
	@echo "$(GREEN)Coverage report generated in htmlcov/$(RESET)"

# Coverage including AWS tests
coverage-aws: ## Generate coverage including AWS tests (costs money!)
	@echo "$(YELLOW)Generating coverage with AWS tests...$(RESET)"
	@echo "$(RED)WARNING: This will create real AWS resources!$(RESET)"
	@read -p "Continue? [y/N] " response && [ "$$response" = "y" ] || (echo "Aborted" && exit 1)
	AWS_PROFILE=aws coverage run -m pytest
	coverage report --fail-under=$(COVERAGE_MIN)
	coverage html

# Version management
version-check: ## Check current version information
	@echo "$(YELLOW)Current version information:$(RESET)"
	@echo "Package version: $$(grep '^version = ' pyproject.toml | cut -d'"' -f2)"
	@echo "Python version: $$($(PYTHON) --version)"
	@echo "Git branch: $$(git branch --show-current 2>/dev/null || echo 'Not a git repo')"

version-bump-patch: ## Bump patch version (0.1.0 -> 0.1.1)
	@echo "$(YELLOW)Bumping patch version...$(RESET)"
	bump-my-version bump patch

version-bump-minor: ## Bump minor version (0.1.0 -> 0.2.0)
	@echo "$(YELLOW)Bumping minor version...$(RESET)"
	bump-my-version bump minor

version-bump-major: ## Bump major version (0.1.0 -> 1.0.0)
	@echo "$(YELLOW)Bumping major version...$(RESET)"
	bump-my-version bump major

# Development workflows
dev-setup: install-dev localstack-up ## Complete development environment setup
	@echo "$(GREEN)Development environment setup complete!$(RESET)"

dev-test: lint test-unit test-integration coverage ## Full development test suite
	@echo "$(GREEN)All development tests passed!$(RESET)"

pre-release: clean lint type-check security test-unit test-integration coverage ## Pre-release checks
	@echo "$(GREEN)Pre-release checks passed!$(RESET)"

# Setup development environment
setup: ## Setup development environment (legacy)
	./scripts/setup_environment.sh

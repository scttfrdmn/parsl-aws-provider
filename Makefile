# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors

# Makefile for Parsl Ephemeral AWS Provider

.PHONY: clean test lint type-check test-unit test-integration test-bats docs build install install-dev help
.PHONY: substrate-up substrate-down substrate-wait substrate-status substrate-reset test-aws coverage format pre-commit version-check
.PHONY: version-verify lint-python lint-shell security coverage-aws release

# Colors for output
BLUE := \033[36m
YELLOW := \033[33m
GREEN := \033[32m
RED := \033[31m
RESET := \033[0m

# Configuration
# Everything runs through `uv run`, per CLAUDE.md -- never bare pytest/ruff/mypy,
# which resolve against whatever happens to be on PATH rather than .venv.
UV := uv
RUN := $(UV) run
PYTHON := $(RUN) python
# Matches the gate on the CI unit-tests job; tests/unit + tests/security together
# measure 68%. pyproject's --cov-fail-under is a lower smoke floor because it
# applies to narrow invocations too.
COVERAGE_MIN := 65

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

SUBSTRATE_COMPOSE := $(COMPOSE_CMD) -f docker-compose.substrate.yml
SUBSTRATE_URL ?= http://localhost:4566

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
	$(UV) sync --extra dev --extra test
	$(RUN) pre-commit install
	$(RUN) pre-commit install --hook-type commit-msg
	@echo "$(GREEN)Development environment ready$(RESET)"

# Install for production
install: ## Install package in production mode
	@echo "$(YELLOW)Installing package...$(RESET)"
	$(UV) sync --no-dev
	@echo "$(GREEN)Package installed$(RESET)"

# Run tests
test: test-unit test-integration test-bats ## Run all tests

# Run unit tests
test-unit: ## Run unit and security tests
	@echo "$(YELLOW)Running unit tests...$(RESET)"
	# Selected by path, not `-m unit`, to match CI. The old `-m unit` collected 88
	# of 295 tests here, so this target passed while CI ran the full set and
	# failed. tests/security is pure-mock and marked `unit`, so it belongs here.
	$(RUN) pytest tests/unit/ tests/security/ -v --cov-fail-under=$(COVERAGE_MIN)

# Run integration tests against the substrate emulator
test-integration: substrate-up ## Run integration tests against substrate
	@echo "$(YELLOW)Running integration tests against substrate...$(RESET)"
	$(RUN) pytest tests/integration/ -v

# Run E2E tests against real AWS
test-aws: ## Run E2E tests against real AWS (costs money!)
	@echo "$(YELLOW)Running E2E tests against real AWS...$(RESET)"
	@echo "$(RED)WARNING: This will create real AWS resources and may incur costs!$(RESET)"
	@read -p "Continue? [y/N] " response && [ "$$response" = "y" ] || (echo "Aborted" && exit 1)
	# tests/aws, not tests/integration: the real-AWS suite lives in tests/aws and
	# nothing under tests/integration carries the `aws` marker, so this target
	# collected zero tests and reported success.
	AWS_PROFILE=aws $(RUN) pytest tests/aws/ -v -m "aws" --no-cov

# Run BATS tests for shell scripts
test-bats: ## Run BATS tests for shell scripts
	@if command -v bats >/dev/null 2>&1; then \
		echo "$(YELLOW)Running BATS tests...$(RESET)"; \
		bats tests/bats/; \
	else \
		echo "$(YELLOW)BATS not installed. Skipping shell script tests.$(RESET)"; \
		echo "Install with: brew install bats-core (macOS) or apt-get install bats (Ubuntu)"; \
	fi

# Substrate emulator management (replaces the localstack-* targets, #125)
substrate-up: ## Start the substrate AWS emulator
	@echo "$(YELLOW)Starting substrate...$(RESET)"
	$(SUBSTRATE_COMPOSE) up -d
	@$(MAKE) substrate-wait

substrate-wait: ## Wait for substrate to be ready
	@echo "$(YELLOW)Waiting for substrate to be ready...$(RESET)"
	SUBSTRATE_URL=$(SUBSTRATE_URL) ./scripts/substrate-wait.sh

substrate-down: ## Stop substrate
	@echo "$(YELLOW)Stopping substrate...$(RESET)"
	$(SUBSTRATE_COMPOSE) down

substrate-status: ## Check substrate status
	@echo "$(YELLOW)Substrate status:$(RESET)"
	@$(SUBSTRATE_COMPOSE) ps
	@echo ""
	@echo "$(YELLOW)Health check:$(RESET)"
	@curl -fsS -m 5 $(SUBSTRATE_URL)/health | python3 -m json.tool || echo "substrate not responding"

substrate-reset: ## Wipe all substrate state (no LocalStack equivalent)
	@echo "$(YELLOW)Resetting substrate state...$(RESET)"
	@curl -fsS -m 5 -X POST $(SUBSTRATE_URL)/v1/state/reset && echo "" || echo "reset unavailable"

# Run linting
lint: lint-python lint-shell ## Run all linting checks

# Run Python linting
lint-python: ## Run Python linting with ruff
	@echo "$(YELLOW)Running Python linting...$(RESET)"
	# Whole repo. The 107 pre-existing errors that forced a narrower scope lived
	# entirely in the tools/ one-off debug scripts #93 removed, so `ruff check .`
	# now passes and nothing outside the package can drift unchecked.
	$(RUN) ruff check .
	# Same scope as `format` below and as the pre-commit hook, which formats
	# whatever is staged: a narrower check here lets a file drift out of format
	# and only fail on the next commit that happens to stage it.
	$(RUN) ruff format --check .

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
	$(RUN) ruff format .
	$(RUN) ruff check --fix .

# Run type checking
type-check: ## Run type checking with mypy
	@echo "$(YELLOW)Running type checks...$(RESET)"
	$(RUN) mypy parsl_aws_provider

# Run pre-commit hooks
pre-commit: ## Run all pre-commit hooks
	@echo "$(YELLOW)Running pre-commit hooks...$(RESET)"
	$(RUN) pre-commit run --all-files

# Build documentation
docs: ## Generate documentation
	@echo "$(YELLOW)Generating documentation...$(RESET)"
	cd docs && make html
	@echo "$(GREEN)Documentation generated in docs/_build/html/$(RESET)"

# Build package
build: clean ## Build package for distribution
	@echo "$(YELLOW)Building package...$(RESET)"
	# `uv build` reads pyproject.toml directly. The setup.py shim it used to go
	# through was removed in #93.
	$(UV) build
	@echo "$(GREEN)Package built$(RESET)"

# Create a release
release: lint type-check test build ## Prepare package for release
	@echo "$(GREEN)Package ready for release. Push a v* tag to publish:$(RESET)"
	@echo "  $(RUN) twine check dist/*"
	@echo "  git tag vX.Y.Z && git push origin vX.Y.Z"

# Run security checks
security: ## Run security scan with bandit
	@echo "$(YELLOW)Running security scan...$(RESET)"
	$(RUN) bandit -r parsl_aws_provider -c pyproject.toml

# Code coverage
coverage: ## Generate test coverage report (excludes real-AWS tests)
	@echo "$(YELLOW)Generating coverage report...$(RESET)"
	$(RUN) coverage run -m pytest -m "not aws"
	$(RUN) coverage report --fail-under=$(COVERAGE_MIN)
	$(RUN) coverage html
	@echo "$(GREEN)Coverage report generated in htmlcov/$(RESET)"

# Coverage including AWS tests
coverage-aws: ## Generate coverage including AWS tests (costs money!)
	@echo "$(YELLOW)Generating coverage with AWS tests...$(RESET)"
	@echo "$(RED)WARNING: This will create real AWS resources!$(RESET)"
	@read -p "Continue? [y/N] " response && [ "$$response" = "y" ] || (echo "Aborted" && exit 1)
	AWS_PROFILE=aws $(RUN) coverage run -m pytest
	$(RUN) coverage report --fail-under=$(COVERAGE_MIN)
	$(RUN) coverage html

# Version management
version-check: ## Check current version information
	@echo "$(YELLOW)Current version information:$(RESET)"
	@echo "Package version: $$(grep '^version = ' pyproject.toml | cut -d'"' -f2)"
	@echo "Python version: $$($(PYTHON) --version)"
	@echo "Git branch: $$(git branch --show-current 2>/dev/null || echo 'Not a git repo')"

version-bump-patch: ## Bump patch version (0.1.0 -> 0.1.1)
	@echo "$(YELLOW)Bumping patch version...$(RESET)"
	$(RUN) bump-my-version bump patch
	@$(MAKE) version-verify

version-bump-minor: ## Bump minor version (0.1.0 -> 0.2.0)
	@echo "$(YELLOW)Bumping minor version...$(RESET)"
	$(RUN) bump-my-version bump minor
	@$(MAKE) version-verify

version-bump-major: ## Bump major version (0.1.0 -> 1.0.0)
	@echo "$(YELLOW)Bumping major version...$(RESET)"
	$(RUN) bump-my-version bump major
	@$(MAKE) version-verify

version-verify: ## Check pyproject and __init__ versions agree
	@toml="$$(grep '^version = ' pyproject.toml | head -1 | cut -d'"' -f2)"; \
	pkg="$$($(RUN) python -c 'import parsl_aws_provider as p; print(p.__version__)')"; \
	if [ "$$toml" != "$$pkg" ]; then \
		echo "$(RED)version mismatch: pyproject=$$toml __init__=$$pkg$(RESET)"; \
		echo "$(RED)bump-my-version's search string has drifted; edit __init__.py by hand$(RESET)"; \
		exit 1; \
	fi; \
	echo "$(GREEN)Versions agree: $$pkg$(RESET)"

# Development workflows
dev-setup: install-dev substrate-up ## Complete development environment setup
	@echo "$(GREEN)Development environment setup complete!$(RESET)"

dev-test: lint test-unit test-integration coverage ## Full development test suite
	@echo "$(GREEN)All development tests passed!$(RESET)"

pre-release: clean lint type-check security test-unit test-integration coverage ## Pre-release checks
	@echo "$(GREEN)Pre-release checks passed!$(RESET)"

# Setup development environment
setup: ## Setup development environment (legacy)
	./scripts/setup_environment.sh

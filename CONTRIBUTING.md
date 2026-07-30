# Contributing to Parsl Ephemeral AWS Provider

Thank you for your interest in contributing to this project! Here's how you can help.

## Setting Up Your Development Environment

1. Fork the repository on GitHub
2. Clone your fork locally
   ```bash
   git clone https://github.com/your-username/parsl-aws-provider.git
   cd parsl-aws-provider
   ```
3. Install [uv](https://docs.astral.sh/uv/), which manages both the Python
   interpreter and the dependencies for this project. Do not use `pip`,
   `python -m venv`, or `pyenv` directly — the Python version comes from
   `requires-python` in `pyproject.toml` and `.python-version`, and uv installs
   it for you. Note the project requires Python 3.10+ (Parsl 2026.x dropped 3.9).
   ```bash
   # MacOS / Linux
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
4. Create the environment and install development dependencies
   ```bash
   # Creates .venv/ and installs from the committed uv.lock
   uv sync --extra dev --extra test

   # Install the git hooks (format, lint, commit-message linting)
   make install-dev
   ```
   Prefix commands with `uv run` rather than activating the venv, so they always
   resolve against `.venv` instead of whatever is on `PATH`.

## Development Workflow

1. Create a branch for your work
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. Make your changes and ensure they follow the project's coding standards
   ```bash
   # Format code and sort imports (ruff covers both)
   uv run ruff format parsl_ephemeral_aws
   uv run ruff check --fix parsl_ephemeral_aws tests

   # Run linting
   uv run ruff check parsl_ephemeral_aws tests

   # Run type checking
   uv run mypy parsl_ephemeral_aws
   ```
   Or all at once, exactly as CI runs them: `make lint-python type-check`.

3. Add tests for your changes
   ```bash
   # Unit + security tests, with the same coverage gate CI applies
   make test-unit

   # Integration tests (starts LocalStack; they skip without it)
   make test-integration

   # A single file or test
   uv run pytest tests/unit/test_provider_interface.py -v --no-cov
   ```

4. Commit your changes
   ```bash
   git add .
   git commit -m "Brief description of your changes"
   ```

5. Push your changes to your fork
   ```bash
   git push origin feature/your-feature-name
   ```

6. Open a pull request on GitHub

## Pull Request Guidelines

- **All work goes on a feature branch and merges via a PR. Never commit directly
  to `main`.** This holds for maintainers too.
- Ensure your code passes all tests, linting, and type checking
- Include tests for new functionality
- Update documentation as needed
- Keep pull requests focused on a single topic
- Follow the project's coding style (PEP 8, formatted with ruff-format)
- Add SPDX license headers to all new files
- Reference the issue in your commit subject: `fix: correct spot fleet config (closes #N)`.
  Commit messages are linted by commitlint (conventional commits), and the
  subject line must be 72 characters or fewer.

## Code Style

This project follows:
- [PEP 8](https://www.python.org/dev/peps/pep-0008/) style guide for Python code
- [ruff-format](https://docs.astral.sh/ruff/formatter/) at its default 88-character
  line length, for both formatting and import sorting
- [mypy](https://mypy.readthedocs.io/) for static type checking

## Adding New Features

When adding new features, please:

1. Start by opening an issue describing the feature
2. Begin with interface definitions and test cases
3. Implement the feature with comprehensive error handling
4. Add thorough documentation, including docstrings and examples
5. Ensure full test coverage

## Reporting Bugs

When reporting bugs, please include:

- The exact steps to reproduce the bug
- What you expected to happen
- What actually happened
- Your environment details (Python version, OS, etc.)
- Any relevant logs or screenshots

## License

By contributing to this project, you agree that your contributions will be licensed under the project's [Apache License 2.0](LICENSE).

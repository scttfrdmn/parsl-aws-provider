# Contributing to Parsl Ephemeral AWS Provider

Thank you for your interest in contributing to this project! Here's how you can help.

## Setting Up Your Development Environment

1. Fork the repository on GitHub
2. Clone your fork locally
   ```bash
   git clone https://github.com/your-username/parsl-aws-provider.git
   cd parsl-aws-provider
   ```
3. Set up your Python environment (we recommend using pyenv)
   ```bash
   # Install pyenv (if not already installed)
   # MacOS: brew install pyenv
   # Linux: curl https://pyenv.run | bash
   # Windows: See https://github.com/pyenv-win/pyenv-win#installation

   # Install the appropriate Python version
   pyenv install 3.9.16
   
   # Set the local Python version for this project
   pyenv local 3.9.16
   ```
4. Create a virtual environment and install development dependencies
   ```bash
   # Create a virtual environment in the project directory
   python -m venv .venv
   
   # Activate the virtual environment
   source .venv/bin/activate  # On Linux/macOS
   # OR
   .venv\Scripts\activate     # On Windows
   
   # Install development dependencies
   pip install -e ".[dev,test]"
   ```

## Development Workflow

1. Create a branch for your work
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. Make your changes and ensure they follow the project's coding standards
   ```bash
   # Format code with black
   black parsl_ephemeral_aws tests
   
   # Sort imports with isort
   isort parsl_ephemeral_aws tests
   
   # Run linting
   flake8 parsl_ephemeral_aws tests
   
   # Run type checking
   mypy parsl_ephemeral_aws
   ```

3. Add tests for your changes
   ```bash
   # Run tests
   pytest tests/
   
   # Run tests with coverage
   pytest --cov=parsl_ephemeral_aws tests/
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

- Ensure your code passes all tests, linting, and type checking
- Include tests for new functionality
- Update documentation as needed
- Keep pull requests focused on a single topic
- Follow the project's coding style (PEP 8 with Black formatting)
- Add SPDX license headers to all new files

## Code Style

This project follows:
- [PEP 8](https://www.python.org/dev/peps/pep-0008/) style guide for Python code
- [Black](https://black.readthedocs.io/) code formatter with 100-character line length
- [isort](https://pycqa.github.io/isort/) for import sorting
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
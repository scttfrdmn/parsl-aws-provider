# Parsl Ephemeral AWS Provider Documentation

This directory contains the documentation for the Parsl Ephemeral AWS Provider.

## Building Documentation

To build the documentation:

1. Install documentation dependencies:
   ```bash
   pip install -e ".[docs]"
   ```

2. Build the documentation:
   ```bash
   cd docs
   make html
   ```

3. View the documentation:
   ```bash
   open _build/html/index.html
   ```

## Documentation Structure

- `conf.py`: Sphinx configuration
- `index.rst`: Main documentation page
- `installation.rst`: Installation instructions
- `configuration.rst`: Configuration reference
- `examples/`: Example usage scenarios
- `api/`: API documentation

## Contributing to Documentation

To contribute:

1. Make your changes to the appropriate files
2. Build the documentation to ensure it renders correctly
3. Submit a pull request with your changes

Please follow the existing documentation style and formatting.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors

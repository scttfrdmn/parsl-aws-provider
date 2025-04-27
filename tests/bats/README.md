# BATS Tests for Parsl Ephemeral AWS Provider

This directory contains [Bash Automated Testing System (BATS)](https://github.com/bats-core/bats-core) tests for shell scripts in the Parsl Ephemeral AWS Provider.

## Prerequisites

To run these tests, you need to install:

1. BATS core: https://github.com/bats-core/bats-core
2. BATS support libraries:
   - bats-support: https://github.com/bats-core/bats-support
   - bats-assert: https://github.com/bats-core/bats-assert
   - bats-file: https://github.com/bats-core/bats-file

You can install these prerequisites with:

```bash
# Install BATS core
git clone https://github.com/bats-core/bats-core.git
cd bats-core
./install.sh /usr/local

# Install BATS support libraries
mkdir -p /usr/local/lib/bats
git clone https://github.com/bats-core/bats-support.git /usr/local/lib/bats/bats-support
git clone https://github.com/bats-core/bats-assert.git /usr/local/lib/bats/bats-assert
git clone https://github.com/bats-core/bats-file.git /usr/local/lib/bats/bats-file
```

Or, if you prefer to use package managers:

```bash
# macOS with Homebrew
brew install bats-core

# Ubuntu/Debian
sudo apt-get install bats
```

## Running Tests

To run all BATS tests:

```bash
bats tests/bats/
```

To run a specific test file:

```bash
bats tests/bats/test_setup_environment.bats
```

## Test Structure

Each BATS test file follows this structure:

```bash
#!/usr/bin/env bats

# Load BATS libraries
load '/usr/local/lib/bats/bats-support/load.bash'
load '/usr/local/lib/bats/bats-assert/load.bash'
load '/usr/local/lib/bats/bats-file/load.bash'

# Setup function runs before each test
setup() {
    # Create temporary directory for test files
    TEST_TEMP_DIR="$(mktemp -d)"
    
    # Source the script being tested
    source "${BATS_TEST_DIRNAME}/../../scripts/script_to_test.sh"
}

# Teardown function runs after each test
teardown() {
    # Remove temporary directory
    rm -rf "$TEST_TEMP_DIR"
}

# Example test
@test "Test that script function works" {
    # Run the function
    run function_to_test "argument"
    
    # Assert expectations
    assert_success
    assert_output --partial "expected output"
}
```

## ShellCheck Integration

All shell scripts should also be verified with [ShellCheck](https://www.shellcheck.net/), which can be run with:

```bash
shellcheck scripts/*.sh
```

## Best Practices

1. Mock external commands when testing shell scripts
2. Use BATS fixtures for complex setup/teardown
3. Test both success and failure cases
4. Test with edge cases and unexpected inputs
5. Keep tests isolated and independent
6. Use meaningful test descriptions
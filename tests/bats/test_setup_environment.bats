#!/usr/bin/env bats

# Load the BATS test helper
load setup_helper

# Load BATS libraries if they exist
BATS_SUPPORT="/usr/local/lib/bats/bats-support/load.bash"
BATS_ASSERT="/usr/local/lib/bats/bats-assert/load.bash"
BATS_FILE="/usr/local/lib/bats/bats-file/load.bash"

if [[ -f "$BATS_SUPPORT" ]]; then
  load "$BATS_SUPPORT"
fi
if [[ -f "$BATS_ASSERT" ]]; then
  load "$BATS_ASSERT"
fi
if [[ -f "$BATS_FILE" ]]; then
  load "$BATS_FILE"
fi

SCRIPT_PATH="${PROJ_ROOT}/scripts/setup_environment.sh"

# Setup function runs before each test
setup() {
  setup_temp_dir
  mock_aws
  
  # Make a copy of the script to test in our temp directory
  cp "$SCRIPT_PATH" "${TEST_TEMP_DIR}/setup_environment.sh"
  chmod +x "${TEST_TEMP_DIR}/setup_environment.sh"
  
  # Create a mock requirements.txt file
  echo "boto3>=1.28.0" > "${TEST_TEMP_DIR}/requirements.txt"
  
  # Create a mock Python virtual environment
  mkdir -p "${TEST_TEMP_DIR}/.venv/bin"
  echo "#!/bin/bash" > "${TEST_TEMP_DIR}/.venv/bin/activate"
  chmod +x "${TEST_TEMP_DIR}/.venv/bin/activate"
  
  # Create a mock pip for testing
  mkdir -p "${TEST_TEMP_DIR}/bin"
  cat > "${TEST_TEMP_DIR}/bin/pip" <<EOF
#!/usr/bin/env bash
echo "MOCK PIP CALLED WITH: \$@" >&2
exit 0
EOF
  chmod +x "${TEST_TEMP_DIR}/bin/pip"
  
  # Create a mock python3 that reports version 3.9
  cat > "${TEST_TEMP_DIR}/bin/python3" <<EOF
#!/usr/bin/env bash
if [[ "\$*" == *"version_info"* ]]; then
  echo "3.9"
elif [[ "\$*" == *"-m venv"* ]]; then
  mkdir -p "\${@: -1}/bin"
  echo "#!/bin/bash" > "\${@: -1}/bin/activate"
  chmod +x "\${@: -1}/bin/activate"
elif [[ "\$*" == *"-m pip"* ]]; then
  echo "MOCK PIP CALLED WITH: \$@" >&2
else
  echo "Mock Python 3.9.0"
fi
exit 0
EOF
  chmod +x "${TEST_TEMP_DIR}/bin/python3"
  
  # Add our mock binaries to the PATH
  export PATH="${TEST_TEMP_DIR}/bin:$PATH"
  
  # Set up environment for the script
  export ORIGINAL_PATH="$PATH"
  cd "$TEST_TEMP_DIR" || exit 1
}

# Teardown function runs after each test
teardown() {
  export PATH="$ORIGINAL_PATH"
  cleanup_temp_dir
}

# Test help option
@test "Script displays help message" {
  run ./setup_environment.sh --help
  
  [ "$status" -eq 0 ]
  [[ "$output" == *"Usage:"* ]]
  [[ "$output" == *"--region"* ]]
  [[ "$output" == *"--profile"* ]]
  [[ "$output" == *"--env-file"* ]]
}

# Test invalid option
@test "Script handles invalid options" {
  run ./setup_environment.sh --invalid-option
  
  [ "$status" -eq 1 ]
  [[ "$output" == *"Unknown option"* ]]
}

# Test basic execution
@test "Script runs successfully with default options" {
  run ./setup_environment.sh
  
  [ "$status" -eq 0 ]
  [[ "$output" == *"Environment setup complete"* ]]
}

# Test region option
@test "Script accepts custom region" {
  run ./setup_environment.sh --region=us-west-2
  
  [ "$status" -eq 0 ]
  [[ "$output" == *"Environment setup complete"* ]]
  
  # Check if .env file contains the specified region
  [ -f ".env" ]
  run cat .env
  [[ "$output" == *"AWS_REGION=us-west-2"* ]]
}

# Test profile option
@test "Script accepts AWS profile" {
  run ./setup_environment.sh --profile=test-profile
  
  [ "$status" -eq 0 ]
  [[ "$output" == *"Environment setup complete"* ]]
  
  # Check if .env file contains the specified profile
  [ -f ".env" ]
  run cat .env
  [[ "$output" == *"AWS_PROFILE=test-profile"* ]]
}

# Test env file option
@test "Script uses custom env file" {
  run ./setup_environment.sh --env-file=custom.env
  
  [ "$status" -eq 0 ]
  [[ "$output" == *"Environment setup complete"* ]]
  
  # Check if custom.env file was created
  [ -f "custom.env" ]
}

# Test with existing env file
@test "Script preserves existing env file" {
  # Create an existing .env file
  echo "EXISTING=VALUE" > .env
  
  run ./setup_environment.sh
  
  [ "$status" -eq 0 ]
  [[ "$output" == *"file already exists"* ]]
  
  # Check if .env file still exists and wasn't overwritten
  [ -f ".env" ]
  run cat .env
  [[ "$output" == *"EXISTING=VALUE"* ]]
}
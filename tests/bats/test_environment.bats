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

# Setup function runs before each test
setup() {
  setup_temp_dir
  mock_aws
}

# Teardown function runs after each test
teardown() {
  cleanup_temp_dir
}

# Test environment variables
@test "Required environment variables are set" {
  # Skip if not in a CI environment to avoid failing local tests
  if [[ -z "$CI" ]]; then
    skip "Not in CI environment"
  fi
  
  # Check for required environment variables
  [[ -n "$AWS_REGION" ]] || (echo "AWS_REGION is not set" && false)
  [[ -n "$AWS_ACCESS_KEY_ID" || -n "$AWS_PROFILE" ]] || (echo "Neither AWS_ACCESS_KEY_ID nor AWS_PROFILE is set" && false)
}

# Test AWS CLI is installed and configured
@test "AWS CLI is installed and configured" {
  # Check if AWS CLI is installed
  run which aws
  [ "$status" -eq 0 ]
  
  # Try a basic AWS command using our mock
  run aws ec2 describe-instances
  [ "$status" -eq 0 ]
  [[ "$output" == *"Instances"* ]]
}

# Test Python environment
@test "Python environment has required packages" {
  # Check Python version is at least 3.9
  run python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"
  [ "$status" -eq 0 ]
  
  # Check boto3 is installed
  run python3 -c "import boto3; print('boto3 version:', boto3.__version__)"
  [ "$status" -eq 0 ]
  [[ "$output" == *"boto3 version"* ]]
}

# Test the project structure
@test "Project has expected directory structure" {
  # Check for key project directories
  [ -d "$PROJ_ROOT/parsl_ephemeral_aws" ]
  [ -d "$PROJ_ROOT/parsl_ephemeral_aws/modes" ]
  [ -d "$PROJ_ROOT/parsl_ephemeral_aws/compute" ]
  [ -d "$PROJ_ROOT/parsl_ephemeral_aws/state" ]
  [ -d "$PROJ_ROOT/tests" ]
  
  # Check for essential files
  [ -f "$PROJ_ROOT/setup.py" ]
  [ -f "$PROJ_ROOT/requirements.txt" ]
}

# Test mock functions
@test "Mock AWS CLI functions correctly" {
  # Test EC2 instance commands
  run aws ec2 describe-instances
  [ "$status" -eq 0 ]
  [[ "$output" == *"i-0123456789abcdef0"* ]]
  
  run aws ec2 run-instances
  [ "$status" -eq 0 ]
  [[ "$output" == *"i-0123456789abcdef0"* ]]
  
  # Test S3 commands
  run aws s3 ls
  [ "$status" -eq 0 ]
  [[ "$output" == *"s3://bucket/path/"* ]]
}
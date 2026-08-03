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

# The "Required environment variables are set" test that used to sit here
# asserted that AWS_REGION and AWS_ACCESS_KEY_ID/AWS_PROFILE were set in the
# ambient environment, guarded by `if [[ -z "$CI" ]]; then skip`. So it ran only
# in CI -- and it failed on every CI run since it was written, taking the whole
# job with it even though the other 11 tests passed.
#
# It could not have done otherwise: this suite exercises shell scripts against
# the mocked AWS CLI that `mock_aws` installs, so it needs no credentials and the
# workflow supplies none. Exporting the variables in the job would have made it
# pass while asserting nothing but that the workflow sets what the workflow sets.
# The env-file contents the scripts actually depend on are covered by
# test_setup_environment.bats.

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
  [ -d "$PROJ_ROOT/parsl_ephemeral_provider" ]
  [ -d "$PROJ_ROOT/parsl_ephemeral_provider/modes" ]
  [ -d "$PROJ_ROOT/parsl_ephemeral_provider/compute" ]
  [ -d "$PROJ_ROOT/parsl_ephemeral_provider/state" ]
  [ -d "$PROJ_ROOT/tests" ]

  # Check for essential files. pyproject.toml is the single source of both
  # metadata and dependencies (#93): setup.py was a two-line setuptools shim and
  # requirements.txt duplicated the dependency list badly enough to drift, still
  # naming black and a Python floor two releases behind.
  [ -f "$PROJ_ROOT/pyproject.toml" ]
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

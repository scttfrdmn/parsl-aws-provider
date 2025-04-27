#!/usr/bin/env bash
# Helper functions for BATS tests

# Directory variables
PROJ_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPTS_DIR="${PROJ_ROOT}/scripts"

# Create a temporary directory for each test
setup_temp_dir() {
  TEST_TEMP_DIR="$(mktemp -d)"
  export TEST_TEMP_DIR
}

# Clean up the temporary directory
cleanup_temp_dir() {
  if [[ -d "$TEST_TEMP_DIR" ]]; then
    rm -rf "$TEST_TEMP_DIR"
  fi
}

# Mock AWS CLI command
mock_aws() {
  mkdir -p "${TEST_TEMP_DIR}/bin"
  cat > "${TEST_TEMP_DIR}/bin/aws" <<EOF
#!/usr/bin/env bash
echo "MOCK AWS CLI CALLED WITH: \$@" >&2
case "\$1" in
  "ec2")
    case "\$2" in
      "describe-instances")
        echo '{"Reservations":[{"Instances":[{"InstanceId":"i-0123456789abcdef0","State":{"Name":"running"}}]}]}'
        ;;
      "run-instances")
        echo '{"Instances":[{"InstanceId":"i-0123456789abcdef0"}]}'
        ;;
      *)
        echo '{"Error":"Unimplemented mock"}'
        ;;
    esac
    ;;
  "s3")
    case "\$2" in
      "ls")
        echo "MOCK: s3://bucket/path/"
        ;;
      *)
        echo '{"Error":"Unimplemented mock"}'
        ;;
    esac
    ;;
  *)
    echo '{"Error":"Unimplemented mock"}'
    ;;
esac
EOF
  chmod +x "${TEST_TEMP_DIR}/bin/aws"
  export PATH="${TEST_TEMP_DIR}/bin:$PATH"
}

# Mock boto3 library for Python scripts
mock_boto3() {
  mkdir -p "${TEST_TEMP_DIR}/lib/python"
  cat > "${TEST_TEMP_DIR}/lib/python/boto3_mock.py" <<EOF
def client(*args, **kwargs):
    return MockClient()

def resource(*args, **kwargs):
    return MockResource()

class MockClient:
    def __init__(self):
        pass
        
    def describe_instances(self, **kwargs):
        return {'Reservations': [{'Instances': [{'InstanceId': 'i-0123456789abcdef0', 'State': {'Name': 'running'}}]}]}
        
    def run_instances(self, **kwargs):
        return {'Instances': [{'InstanceId': 'i-0123456789abcdef0'}]}
        
    def create_tags(self, **kwargs):
        return {}

class MockResource:
    def __init__(self):
        pass
        
    def Instance(self, instance_id):
        return MockInstance(instance_id)

class MockInstance:
    def __init__(self, instance_id):
        self.id = instance_id
        self.state = {'Name': 'running'}
EOF
  export PYTHONPATH="${TEST_TEMP_DIR}/lib/python:$PYTHONPATH"
}

# Create a mock environment variables file
create_mock_env_file() {
  cat > "${TEST_TEMP_DIR}/.env" <<EOF
# Mock .env file for testing
AWS_REGION=us-east-1
AWS_PROFILE=testing
PARSL_IMAGE_ID=ami-12345678
PARSL_INSTANCE_TYPE=t3.micro
EOF
}

# Create a mock config file
create_mock_config_file() {
  cat > "${TEST_TEMP_DIR}/config.json" <<EOF
{
  "region": "us-east-1",
  "instance_type": "t3.micro",
  "image_id": "ami-12345678",
  "max_blocks": 10,
  "min_blocks": 0,
  "init_blocks": 1,
  "use_spot": true,
  "spot_max_price": 0.1
}
EOF
}

# Check if a command exists
command_exists() {
  command -v "$1" >/dev/null 2>&1
}

# Test if the environment has the necessary tools
check_required_tools() {
  local missing_tools=()
  
  for tool in aws python3 jq curl; do
    if ! command_exists "$tool"; then
      missing_tools+=("$tool")
    fi
  done
  
  if [ ${#missing_tools[@]} -gt 0 ]; then
    echo "Missing required tools: ${missing_tools[*]}" >&2
    return 1
  fi
  
  return 0
}
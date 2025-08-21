#!/usr/bin/env bash
# Setup environment for Parsl Ephemeral AWS Provider
# This is a sample script that will be tested with BATS

set -e

# Default values
DEFAULT_REGION="us-east-1"
PYTHON_MIN_VERSION="3.9"
REQUIREMENTS_FILE="requirements.txt"
ENV_FILE=".env"

# Parse command line arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --region=*)
      REGION="${1#*=}"
      shift
      ;;
    --profile=*)
      AWS_PROFILE="${1#*=}"
      shift
      ;;
    --env-file=*)
      ENV_FILE="${1#*=}"
      shift
      ;;
    --help)
      echo "Usage: $0 [OPTIONS]"
      echo "Setup the environment for Parsl Ephemeral AWS Provider"
      echo ""
      echo "Options:"
      echo "  --region=REGION    AWS region (default: us-east-1)"
      echo "  --profile=PROFILE  AWS profile to use"
      echo "  --env-file=FILE    Environment file (default: .env)"
      echo "  --help             Show this help message"
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      echo "Use --help for usage information"
      exit 1
      ;;
  esac
done

# Check for Python
if ! command -v python3 &> /dev/null; then
  echo "Error: Python 3 is required but not found"
  exit 1
fi

# Check Python version
PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
if [[ "$(printf '%s\n' "$PYTHON_MIN_VERSION" "$PYTHON_VERSION" | sort -V | head -n1)" != "$PYTHON_MIN_VERSION" ]]; then
  echo "Error: Python $PYTHON_MIN_VERSION or higher is required (found $PYTHON_VERSION)"
  exit 1
fi

# Check for pip
if ! command -v pip3 &> /dev/null; then
  echo "Error: pip3 is required but not found"
  exit 1
fi

# Check for AWS CLI
if ! command -v aws &> /dev/null; then
  echo "Error: AWS CLI is required but not found"
  exit 1
fi

# Set region
REGION=${REGION:-$DEFAULT_REGION}

# Create virtual environment if it doesn't exist
if [[ ! -d ".venv" ]]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi

# Activate virtual environment
if [[ -f ".venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
else
  echo "Error: Failed to create virtual environment"
  exit 1
fi

# Install requirements
if [[ -f "$REQUIREMENTS_FILE" ]]; then
  echo "Installing requirements..."
  pip install -r "$REQUIREMENTS_FILE"
else
  echo "Warning: Requirements file not found"
fi

# Install the package in development mode
echo "Installing package in development mode..."
pip install -e .

# Create or update .env file
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Creating $ENV_FILE file..."
  cat > "$ENV_FILE" <<EOF
# Parsl Ephemeral AWS Provider Environment
AWS_REGION=$REGION
EOF

  # Add AWS profile if specified
  if [[ -n "$AWS_PROFILE" ]]; then
    echo "AWS_PROFILE=$AWS_PROFILE" >> "$ENV_FILE"
  fi
else
  echo "$ENV_FILE file already exists"
fi

# Verify AWS access
echo "Verifying AWS access..."
if [[ -n "$AWS_PROFILE" ]]; then
  aws sts get-caller-identity --profile "$AWS_PROFILE"
else
  aws sts get-caller-identity
fi

echo ""
echo "Environment setup complete!"
echo "Activate the virtual environment with: source .venv/bin/activate"
echo "Set environment variables with: source $ENV_FILE"

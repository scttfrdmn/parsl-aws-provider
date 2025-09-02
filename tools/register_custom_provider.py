#!/usr/bin/env python3
"""Register our custom provider with Parsl's provider system."""

import sys
import os

# Add current directory to Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import our provider
from phase15_enhanced import AWSProvider as EphemeralAWSProvider

# Register it in the parsl.providers namespace
import parsl.providers
parsl.providers.EphemeralAWSProvider = EphemeralAWSProvider

print("✅ EphemeralAWSProvider registered with Parsl")
print(f"Available providers: {[name for name in dir(parsl.providers) if 'Provider' in name]}")

# Test that it's accessible
from parsl.providers import EphemeralAWSProvider
print("✅ EphemeralAWSProvider can be imported from parsl.providers")

# Create test provider
provider = EphemeralAWSProvider(
    label="test",
    region="us-east-1", 
    instance_type="t3.small"
)
print(f"✅ Provider instantiated: {provider}")
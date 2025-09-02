#!/usr/bin/env python3
"""Test if we can make our provider work with Globus Compute."""

import sys
import os

# Add current directory to Python path so our provider is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Test importing our provider
try:
    from phase15_enhanced import AWSProvider
    print("✅ Our AWSProvider is importable")
    
    # Test basic instantiation
    provider = AWSProvider(
        label="test",
        region="us-east-1",
        instance_type="t3.small"
    )
    print("✅ AWSProvider can be instantiated")
    print(f"Provider type: {type(provider)}")
    print(f"Provider module: {provider.__module__}")
    
except Exception as e:
    print(f"❌ Provider import failed: {e}")

# Test if we can create Globus Compute engine configuration
try:
    # This is how Globus Compute would try to import our provider
    import importlib
    
    # Simulate how Globus Compute loads providers
    module_name = "phase15_enhanced"
    class_name = "AWSProvider"
    
    module = importlib.import_module(module_name)
    provider_class = getattr(module, class_name)
    
    print("✅ Provider can be imported via importlib (Globus Compute style)")
    print(f"Found class: {provider_class}")
    
except Exception as e:
    print(f"❌ Importlib loading failed: {e}")
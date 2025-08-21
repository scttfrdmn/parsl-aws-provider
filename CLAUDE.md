# Using Claude Code for Parsl Ephemeral AWS Provider Development

This document provides guidance on effectively using Claude Code to develop the Parsl Ephemeral AWS Provider project. Claude Code's capabilities are particularly well-suited for this type of infrastructure-as-code development.

## Overview

Claude Code can assist with:

1. **Code Generation**: Creating complete, functional Python modules
2. **API Integration**: Writing code that interacts with AWS services
3. **Testing Strategies**: Developing unit and integration tests
4. **Documentation**: Generating comprehensive docstrings and examples
5. **Architecture Design**: Providing suggestions for optimal code structure

## Project Structure

When working with Claude, it's helpful to maintain a clear project structure. Here's the recommended structure for this project:

```
parsl_ephemeral_aws/
├── __init__.py
├── provider.py               # Main provider implementation
├── constants.py              # AWS-related constants and defaults
├── exceptions.py             # Custom exception classes
├── modes/                    # Different operating modes
│   ├── __init__.py
│   ├── standard.py           # Standard mode implementation
│   ├── detached.py           # Detached mode implementation
│   └── serverless.py         # Serverless mode implementation
├── compute/                  # Compute resource implementations
│   ├── __init__.py
│   ├── ec2.py                # EC2 instance management
│   ├── lambda_func.py        # Lambda function management
│   └── ecs.py                # ECS/Fargate management
├── network/                  # Network resource management
│   ├── __init__.py
│   ├── vpc.py                # VPC and subnet management
│   └── security.py           # Security group management
├── state/                    # State persistence mechanisms
│   ├── __init__.py
│   ├── parameter_store.py    # AWS Parameter Store integration
│   ├── s3.py                 # S3-based state management
│   └── file.py               # File-based state management
├── utils/                    # Utility functions
│   ├── __init__.py
│   ├── aws.py                # AWS helper functions
│   ├── logging.py            # Logging utilities
│   └── serialization.py      # State serialization/deserialization
└── templates/                # CloudFormation/other templates
    ├── bastion.yml           # Bastion host template
    ├── lambda_worker.yml     # Lambda worker template
    └── ecs_worker.yml        # ECS worker template
```

## Working with Claude Code

### Effective Prompting

When asking Claude to generate code for this project, follow these guidelines:

1. **Provide context**: Before requesting code, explain the component's purpose and how it integrates with Parsl

   Example: "I'm developing an AWS provider for Parsl that supports ephemeral resources. I need a class to manage EC2 instance lifecycle."

2. **Specify requirements**: Clearly state functional requirements, constraints, and dependencies

   Example: "The EC2 manager needs to support spot instances, hibernation, and proper cleanup of all resources."

3. **Request iterative development**: For complex components, ask for code in stages

   Example: "First, let's implement the basic VPC creation. Then we'll add subnet management and security groups."

4. **Provide examples**: Show snippets of existing code or Parsl interfaces to maintain consistency

   Example: "The provider needs to implement the ExecutionProvider interface as shown here: [code snippet]"

### Code Generation Strategy

When developing the provider, use the following approach with Claude:

1. **Start with interfaces**: Ask Claude to generate interface definitions and abstract classes first
2. **Implement core functionality**: Focus on the primary execution provider interface
3. **Add AWS-specific features**: Extend with ephemeral resource management
4. **Develop operational modes**: Implement standard, detached, and serverless modes
5. **Add advanced features**: Incorporate spot management, MPI support, etc.

### Handling AWS SDK Integration

The AWS SDK (boto3) integration is a critical aspect of this project. Here's how to work with Claude effectively on AWS-related code:

1. **Session management**: Ask Claude to implement AWS session handling with credential management
2. **Error handling**: Request robust error handling for AWS API calls with appropriate retry logic
3. **Resource tracking**: Ensure all created AWS resources are properly tagged and tracked
4. **Clean separation**: Maintain separation between AWS-specific code and Parsl-specific code

Example prompt:
```
Let's implement AWS session management for our provider. We need to:
1. Support credential loading from environment variables, profiles, or explicit credentials
2. Handle session creation with appropriate region settings
3. Implement a mechanism to track all resources created in the session
4. Ensure proper error handling for API throttling and temporary failures
```

## Implementation Guide

### Core Provider Implementation

The main provider class should extend Parsl's ExecutionProvider interface. Ask Claude to implement:

1. **Constructor**: Initialize with configuration parameters
2. **submit**: Create resources and execute jobs
3. **status**: Check job status
4. **cancel**: Cancel jobs and clean up resources
5. **scale_in/scale_out**: Adjust resources based on workload

Example prompt:
```
I need to implement the core EphemeralAWSProvider class that extends Parsl's ExecutionProvider.
The class should handle:
- Configuration processing with sensible defaults
- Resource provisioning based on blocks
- Job submission through the Parsl interface
- Status reporting and job cancellation
- Auto-scaling based on workload
```

### AWS Resource Management

For AWS resource management, request code that:

1. **Creates resources idempotently**: Handles resource creation with unique identifiers
2. **Implements proper cleanup**: Ensures all resources are terminated/deleted
3. **Provides status reporting**: Accurately reports resource status
4. **Handles errors gracefully**: Manages AWS API errors and limitations

Example prompt:
```
I need to implement a module that manages EC2 resources for our provider. It should:
- Create instances from a specified AMI
- Support both on-demand and spot instances
- Configure security groups and networking
- Track all created resources with tags
- Support termination, stopping, or hibernation
- Ensure complete cleanup of all resources
```

### State Management

For state persistence, ask Claude to create code that:

1. **Serializes provider state**: Converts internal state to serializable format
2. **Persists state securely**: Stores state in Parameter Store, S3, or files
3. **Restores state reliably**: Reconstructs provider state from persisted data
4. **Handles version compatibility**: Manages state format changes across versions

Example prompt:
```
I need a state management system for the provider that can:
- Serialize the provider's current state (running instances, jobs, etc.)
- Store this state in AWS Parameter Store with appropriate structure
- Restore the provider state when initializing a new provider instance
- Handle cases where state format changes between versions
```

## Testing Strategy

When developing tests with Claude, request:

1. **Unit tests**: For individual components like state managers, resource handlers
2. **Mock-based tests**: For AWS interactions without actual AWS calls
3. **Integration tests**: For end-to-end functionality with actual AWS resources
4. **Test fixtures**: Reusable test setup and teardown logic

Example prompt:
```
Let's create unit tests for the EC2 resource manager. We should:
1. Use moto to mock AWS interactions
2. Test instance creation with various configurations
3. Verify proper tagging and resource tracking
4. Test cleanup procedures and error handling
5. Ensure spot instance behavior is correctly tested
```

## Code Review Assistance

Claude can help review code for:

1. **AWS best practices**: Ensuring AWS resources are managed effectively
2. **Error handling**: Checking for comprehensive error handling
3. **Resource cleanup**: Verifying all resources are properly cleaned up
4. **Performance optimization**: Identifying potential performance issues
5. **Security concerns**: Spotting potential security risks

Example prompt:
```
Please review this EC2 manager implementation for:
- Proper error handling and retries
- Complete resource cleanup
- Security best practices
- Optimization opportunities
- AWS API usage patterns
```

## Documentation Generation

Claude excels at generating documentation. Request:

1. **Docstrings**: Comprehensive function and class documentation
2. **Code examples**: Usage examples for each component
3. **Configuration guide**: Documentation of all configuration options
4. **Architectural overview**: High-level documentation of system design

Example prompt:
```
Please generate comprehensive docstrings for the EphemeralAWSProvider class, including:
- Purpose and overall behavior
- Parameters with types and descriptions
- Return values and exceptions
- Usage examples for different configurations
- Notes on AWS permissions and requirements
```

## AWS Permission Management

For working with AWS permissions, ask Claude to:

1. **Define required permissions**: List IAM permissions needed for the provider
2. **Create IAM policy documents**: Generate policy JSON for different use cases
3. **Implement permission checking**: Validate required permissions at runtime
4. **Provide least-privilege examples**: Demonstrate minimal required permissions

Example prompt:
```
Let's create an IAM policy that provides the minimum permissions required for the provider.
We need to include permissions for:
- EC2 instance management
- VPC and network configuration
- Parameter Store access
- Lambda function management (if using serverless mode)
- ECS task execution (if using container mode)
```

## Implementation Timeline

Here's a suggested development timeline with Claude:

1. **Week 1**: Core provider interface and basic EC2 implementation
2. **Week 2**: State management and VPC/network configuration
3. **Week 3**: Detached mode with bastion host
4. **Week 4**: Spot instance management and hibernation support
5. **Week 5**: Serverless mode with Lambda and ECS
6. **Week 6**: MPI support and advanced features
7. **Week 7**: Testing and documentation
8. **Week 8**: Performance optimization and final polish

## Limitation Awareness

Be aware of these limitations when working with Claude Code:

1. **AWS API versioning**: Claude may not be aware of the very latest AWS API changes
2. **Complex architecture visualization**: Claude cannot create visual architecture diagrams
3. **Performance testing**: Claude cannot actually execute performance tests on AWS
4. **Local testing**: Claude cannot execute the provider locally to verify behavior

For these limitations, use Claude to generate test plans or templates that you can execute yourself.

## Python Environment Setup

When working on this project, follow these Python environment management practices:

1. **Using pyenv**: Use pyenv to manage Python versions
   ```bash
   # Install the appropriate Python version
   pyenv install 3.9.16

   # Set the local Python version for this project
   cd /path/to/parsl-aws-provider
   pyenv local 3.9.16
   ```

2. **Virtual environments**: Always use virtual environments to isolate dependencies
   ```bash
   # Create a virtual environment in the project directory
   python -m venv .venv

   # Activate the virtual environment
   source .venv/bin/activate  # On Linux/macOS
   .venv\Scripts\activate     # On Windows

   # Install development dependencies
   pip install -e ".[dev,test]"
   ```

3. **Requirements management**:
   - Use `requirements.txt` for production dependencies
   - Use `requirements-dev.txt` for development and testing dependencies
   - Use `setup.py` or `pyproject.toml` for package metadata

4. **Linting and formatting**:
   ```bash
   # Run linting checks
   flake8 parsl_ephemeral_aws tests

   # Run type checking
   mypy parsl_ephemeral_aws

   # Format code
   black parsl_ephemeral_aws tests
   ```

## Best Practices

When developing this project with Claude Code:

1. **Iterative development**: Break large components into smaller, focused iterations
2. **Code review**: Ask Claude to review your code or its own generated code
3. **Documentation first**: Consider requesting documentation before implementation
4. **Testing focus**: Emphasize test development alongside feature development
5. **Error handling**: Pay special attention to AWS error scenarios
6. **Resource tracking**: Ensure all created resources are traceable and cleanup-able
7. **Environment consistency**: Ensure all development uses the same Python environment

## Example Complete Function Request

Here's an example of a complete function request to Claude:

```
Please implement the EC2ResourceManager class for our ephemeral AWS provider. This class should:

1. Handle EC2 instance lifecycle (create, terminate, stop, hibernate)
2. Support both on-demand and spot instances
3. Track all created resources with tags
4. Provide status reporting compatible with Parsl's job status
5. Implement complete cleanup of all EC2 resources

The class should have the following interface:

- __init__(self, session, config): Initialize with AWS session and configuration
- create_instances(self, count, job_id): Create the requested number of instances
- get_instance_status(self, instance_ids): Get status information for instances
- terminate_instances(self, instance_ids): Terminate specified instances
- stop_instances(self, instance_ids, hibernate=False): Stop or hibernate instances
- cleanup_all_resources(self): Clean up all resources created by this manager

Include comprehensive error handling, logging, and docstrings.
```

## Conclusion

Claude Code is a powerful tool for developing the Parsl Ephemeral AWS Provider. By following these guidelines, you can maximize productivity and code quality throughout the development process.

Remember to:
- Provide clear context for code generation
- Break complex tasks into manageable pieces
- Request documentation and tests alongside implementation
- Review generated code carefully, especially AWS interactions
- Ensure proper error handling and resource cleanup

With this approach, you can leverage Claude Code effectively to build a robust, flexible, and efficient ephemeral AWS provider for Parsl.

"""Conformance tests for the substrate AWS emulator itself.

Renamed from ``tests/test_localstack.py`` in #125.

Nothing here imports ``parsl_ephemeral_provider``: every test drives raw boto3 against
the emulator. That makes this the suite that answers "does the emulator still
support what the provider relies on?" -- worth keeping distinct from the
integration tests, which exercise provider code and would attribute an emulator
regression to the provider.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import pytest
import os
import uuid

from botocore.exceptions import ClientError

pytestmark = pytest.mark.skipif(
    os.environ.get("SKIP_SUBSTRATE_TESTS", "False").lower() == "true",
    reason="Skipping substrate emulator tests",
)


@pytest.fixture
def ec2_client(boto3_substrate_session, substrate_endpoint):
    """Create an EC2 client connected to substrate."""
    return boto3_substrate_session.client("ec2", endpoint_url=substrate_endpoint)


@pytest.fixture
def s3_client(boto3_substrate_session, substrate_endpoint):
    """Create an S3 client connected to substrate."""
    return boto3_substrate_session.client("s3", endpoint_url=substrate_endpoint)


@pytest.fixture
def ssm_client(boto3_substrate_session, substrate_endpoint):
    """Create an SSM client connected to substrate."""
    return boto3_substrate_session.client("ssm", endpoint_url=substrate_endpoint)


@pytest.fixture
def lambda_client(boto3_substrate_session, substrate_endpoint):
    """Create a Lambda client connected to substrate."""
    return boto3_substrate_session.client("lambda", endpoint_url=substrate_endpoint)


@pytest.fixture
def setup_vpc(ec2_client):
    """Set up a VPC in substrate for testing."""
    # Create a VPC
    vpc_response = ec2_client.create_vpc(CidrBlock="10.0.0.0/16")
    vpc_id = vpc_response["Vpc"]["VpcId"]

    # Tag the VPC
    ec2_client.create_tags(
        Resources=[vpc_id],
        Tags=[
            {"Key": "Name", "Value": "test-vpc"},
            {"Key": "ParslResource", "Value": "true"},
            {"Key": "ParslWorkflowId", "Value": "test-workflow"},
        ],
    )

    # Create a subnet
    subnet_response = ec2_client.create_subnet(VpcId=vpc_id, CidrBlock="10.0.0.0/24")
    subnet_id = subnet_response["Subnet"]["SubnetId"]

    # Create an internet gateway
    igw_response = ec2_client.create_internet_gateway()
    igw_id = igw_response["InternetGateway"]["InternetGatewayId"]

    # Attach the internet gateway to the VPC
    ec2_client.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)

    # Create a security group
    sg_response = ec2_client.create_security_group(
        GroupName="test-sg", Description="Test security group", VpcId=vpc_id
    )
    sg_id = sg_response["GroupId"]

    # Add inbound rules
    ec2_client.authorize_security_group_ingress(
        GroupId=sg_id,
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
            },
            {
                "IpProtocol": "tcp",
                "FromPort": 54000,
                "ToPort": 55000,
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
            },
        ],
    )

    yield {
        "vpc_id": vpc_id,
        "subnet_id": subnet_id,
        "security_group_id": sg_id,
        "internet_gateway_id": igw_id,
    }

    # Clean up resources
    try:
        ec2_client.delete_security_group(GroupId=sg_id)
        ec2_client.detach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
        ec2_client.delete_internet_gateway(InternetGatewayId=igw_id)
        ec2_client.delete_subnet(SubnetId=subnet_id)
        ec2_client.delete_vpc(VpcId=vpc_id)
    except Exception as e:
        print(f"Error cleaning up resources: {e}")


@pytest.fixture
def setup_s3_bucket(s3_client):
    """Set up an S3 bucket in substrate for testing."""
    # Create a unique bucket name
    bucket_name = f"test-bucket-{uuid.uuid4().hex[:8]}"

    # Create the bucket
    s3_client.create_bucket(Bucket=bucket_name)

    yield bucket_name

    # Clean up the bucket
    try:
        # Delete all objects in the bucket
        response = s3_client.list_objects_v2(Bucket=bucket_name)
        if "Contents" in response:
            for obj in response["Contents"]:
                s3_client.delete_object(Bucket=bucket_name, Key=obj["Key"])

        # Delete the bucket
        s3_client.delete_bucket(Bucket=bucket_name)
    except Exception as e:
        print(f"Error cleaning up S3 bucket: {e}")


@pytest.mark.substrate
def test_vpc_creation(ec2_client):
    """Test VPC creation in substrate."""
    # Create a VPC
    vpc_response = ec2_client.create_vpc(CidrBlock="10.0.0.0/16")
    vpc_id = vpc_response["Vpc"]["VpcId"]

    # Verify VPC was created
    describe_response = ec2_client.describe_vpcs(VpcIds=[vpc_id])
    assert len(describe_response["Vpcs"]) == 1
    assert describe_response["Vpcs"][0]["VpcId"] == vpc_id
    assert describe_response["Vpcs"][0]["CidrBlock"] == "10.0.0.0/16"


@pytest.mark.substrate
def test_s3_state_storage(s3_client, setup_s3_bucket):
    """Test S3 state storage using substrate."""
    bucket_name = setup_s3_bucket
    test_key = "test/state.json"
    test_data = '{"workflow_id": "test", "status": "running"}'

    # Upload test data
    s3_client.put_object(
        Bucket=bucket_name, Key=test_key, Body=test_data, ContentType="application/json"
    )

    # Get the object
    response = s3_client.get_object(Bucket=bucket_name, Key=test_key)

    # Read the data
    data = response["Body"].read().decode("utf-8")

    # Verify data
    assert data == test_data


@pytest.mark.substrate
def test_ssm_parameter_store(ssm_client):
    """Test Parameter Store using substrate."""
    parameter_name = "/parsl/workflows/test-workflow/state"
    parameter_value = '{"status": "running", "blocks": 2}'

    # Put parameter
    ssm_client.put_parameter(Name=parameter_name, Value=parameter_value, Type="String")

    # Get parameter
    response = ssm_client.get_parameter(Name=parameter_name)

    # Verify parameter
    assert response["Parameter"]["Name"] == parameter_name
    assert response["Parameter"]["Value"] == parameter_value

    # Delete parameter
    ssm_client.delete_parameter(Name=parameter_name)

    # Verify deletion. Asserted on the symbolic code now that substrate#392 has
    # landed -- it used to return Error.Code == "404" where AWS returns
    # "ParameterNotFound", and this assertion could only check the HTTP status.
    #
    # The code is what matters, not just the failure:
    # ParameterStoreState.save_state() branches on exactly "ParameterNotFound" to
    # choose between put-with-Overwrite and create-with-Tags, so under the old
    # behaviour its create path could not be covered against this emulator at
    # all. The HTTP status is still asserted alongside, since that is what the
    # 0.84.0-and-earlier gap showed up as.
    with pytest.raises(ClientError) as excinfo:
        ssm_client.get_parameter(Name=parameter_name)
    assert excinfo.value.response["Error"]["Code"] == "ParameterNotFound"
    assert excinfo.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404


@pytest.mark.substrate
def test_ec2_instance_lifecycle(ec2_client, setup_vpc):
    """Test EC2 instance lifecycle using substrate."""
    vpc_resources = setup_vpc

    # Register an AMI (mock)
    ami_response = ec2_client.register_image(
        # Unique name: AMI names are account-unique and substrate keeps state for
        # the server's lifetime, so a fixed name breaks a second run.
        Name=f"test-ami-{uuid.uuid4().hex[:8]}",
        RootDeviceName="/dev/xvda",
        BlockDeviceMappings=[{"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": 8}}],
        Architecture="x86_64",
    )
    ami_id = ami_response["ImageId"]

    # Launch an instance
    instance_response = ec2_client.run_instances(
        ImageId=ami_id,
        InstanceType="t3.micro",
        MinCount=1,
        MaxCount=1,
        SubnetId=vpc_resources["subnet_id"],
        SecurityGroupIds=[vpc_resources["security_group_id"]],
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Name", "Value": "test-instance"},
                    {"Key": "ParslResource", "Value": "true"},
                    {"Key": "ParslWorkflowId", "Value": "test-workflow"},
                ],
            }
        ],
    )

    instance_id = instance_response["Instances"][0]["InstanceId"]

    # Verify instance was created
    describe_response = ec2_client.describe_instances(InstanceIds=[instance_id])
    assert len(describe_response["Reservations"]) > 0
    assert len(describe_response["Reservations"][0]["Instances"]) > 0
    assert (
        describe_response["Reservations"][0]["Instances"][0]["InstanceId"]
        == instance_id
    )

    # Terminate instance
    ec2_client.terminate_instances(InstanceIds=[instance_id])

    # Substrate *does* transition instance state, which LocalStack did not -- the
    # old version of this test stopped here with a comment saying termination
    # status could not be checked. It matters to this project specifically:
    # EC2_STATUS_MAPPING keys off exactly these strings, and #66's one-shot mode
    # depends on an instance reaching `terminated` rather than `stopped`.
    final_state = ec2_client.describe_instances(InstanceIds=[instance_id])[
        "Reservations"
    ][0]["Instances"][0]["State"]["Name"]
    assert final_state in ("shutting-down", "terminated"), final_state


@pytest.mark.substrate
def test_lambda_function(lambda_client):
    """Create and invoke a Lambda function.

    Un-skipped in #125: this carried ``@pytest.mark.skip(reason="LocalStack
    doesn't fully support Lambda function creation and invocation")``. Substrate
    does support both, so the test now asserts instead of merely calling.

    ``python3.12``, not the original ``python3.9`` -- AWS deprecated that runtime,
    and this package requires Python >= 3.10 anyway.
    """
    # Unique per run: substrate state persists for the life of the server process,
    # so a fixed name collides with ResourceConflictException on a second run
    # against an already-running emulator.
    function_name = f"test-lambda-{uuid.uuid4().hex[:8]}"

    import io
    import zipfile

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as zip_file:
        zip_file.writestr(
            "index.py",
            """
def handler(event, context):
    return {
        'statusCode': 200,
        'body': 'Hello from Lambda!'
    }
""",
        )

    created = lambda_client.create_function(
        FunctionName=function_name,
        Runtime="python3.12",
        Role="arn:aws:iam::123456789012:role/test-role",
        Handler="index.handler",
        Code={"ZipFile": zip_buffer.getvalue()},
        Description="Test Lambda function",
        Timeout=30,
        MemorySize=128,
    )
    assert created["FunctionArn"].endswith(f"function:{function_name}")

    response = lambda_client.invoke(
        FunctionName=function_name, InvocationType="RequestResponse"
    )
    assert response["StatusCode"] == 200
    # The emulator does not execute the handler body, so assert on the envelope
    # rather than on 'Hello from Lambda!'. LambdaManager reads StatusCode and
    # FunctionError to decide job state, and those are what this covers.
    #
    # Truthiness via `.get()`, which is also what lambda_func.py:458 does, so
    # this covers the provider's own spelling. Substrate omits the key entirely
    # on success now, matching AWS -- it used to set it to "" (substrate#393),
    # which is why the assertion was written to accept either. Both spellings
    # agree that the invocation did not error, so the looser form is kept
    # deliberately rather than tightened to `not in response`.
    assert not response.get("FunctionError")

    lambda_client.delete_function(FunctionName=function_name)

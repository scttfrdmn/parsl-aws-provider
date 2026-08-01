"""Substrate-backed AWS emulation helpers for the integration suite.

This replaces ``parsl_aws_provider/utils/localstack.py``, which lived inside the
shipped package despite being imported only by tests (#125).

LocalStack OSS is end-of-life: the upstream repository was archived read-only in
March 2026, ``4.14.0`` is the last community image, and ``localstack/localstack``
now publishes the Pro build under its community tags — byte-identical digest to
``localstack/localstack-pro``, which refuses to start without
``LOCALSTACK_AUTH_TOKEN``. So the emulator is
[substrate](https://github.com/scttfrdmn/substrate): a single Go binary, no
license, no Docker requirement, serving the same ``:4566`` endpoint.

Substrate is a deliberate drop-in here — it implements
``GET /_localstack/health`` with a LocalStack-shaped payload and adds
``POST /v1/state/reset`` for per-test isolation, which LocalStack never offered.

Two gaps are worth knowing, neither of which this suite hits:

* CloudFormation is not exposed over HTTP (substrate has a Go ``StackDeployer``
  but no ``cloudformation`` plugin), so ``create_stack`` returns
  ``ServiceNotAvailable``. Only ``test_spot_interruption_integration.py``
  references it, and there ``_create_cloudformation_stack`` is patched out.
  ``DetachedMode``'s bastion stack is covered in ``tests/aws/`` against real AWS.
* ``RequestSpotFleet``/``RequestSpotInstances``/``CreateFleet`` are
  unimplemented. Every spot-fleet test uses moto rather than an endpoint, so
  nothing here depends on them.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import logging
import os
from typing import Any, Dict, Optional

import boto3

logger = logging.getLogger(__name__)

#: Substrate's default listen address, and the port LocalStack used, so an
#: endpoint already exported for the old setup keeps working.
DEFAULT_ENDPOINT = "http://localhost:4566"

#: Credentials are structural, not secret: substrate authenticates against
#: nothing. They must merely be present, since botocore refuses to sign without
#: them.
TEST_ACCESS_KEY_ID = "substrate-test"  # nosec B105
TEST_SECRET_ACCESS_KEY = "substrate-test-secret"  # nosec B105


def get_substrate_endpoint() -> str:
    """Return the substrate endpoint URL.

    ``SUBSTRATE_ENDPOINT`` wins; ``LOCALSTACK_ENDPOINT`` is still honoured so
    existing CI configuration and developer shells keep working through the
    transition.
    """
    for var in ("SUBSTRATE_ENDPOINT", "LOCALSTACK_ENDPOINT"):
        if endpoint := os.environ.get(var):
            return endpoint
    return DEFAULT_ENDPOINT


def is_substrate_running(endpoint: Optional[str] = None) -> bool:
    """Return whether a substrate server answers at ``endpoint``.

    Polls ``GET /health``, which substrate and LocalStack both serve, so this
    detects either one.
    """
    endpoint = endpoint or get_substrate_endpoint()

    try:
        import requests

        response = requests.get(f"{endpoint}/health", timeout=1)
        return response.status_code == 200
    except Exception as exc:
        logger.debug("substrate is not reachable at %s: %s", endpoint, exc)
        return False


def is_substrate_available() -> bool:
    """Return whether the emulator is usable, swallowing any probe error.

    Integration modules gate whole suites on this, so it must never raise —
    a missing ``requests``, a DNS failure, and a refused connection all mean the
    same thing to a caller: skip.
    """
    try:
        return is_substrate_running()
    except Exception:
        return False


def reset_substrate_state(endpoint: Optional[str] = None) -> bool:
    """Wipe all emulator state, returning whether the reset was performed.

    This has no LocalStack equivalent — it is why tests can share one server
    process instead of restarting a container between cases. Substrate answers
    501 when its state manager cannot snapshot, which is a legitimate
    configuration rather than an error, so that is reported as ``False`` rather
    than raised.
    """
    endpoint = endpoint or get_substrate_endpoint()

    try:
        import requests

        response = requests.post(f"{endpoint}/v1/state/reset", timeout=5)
    except Exception as exc:
        logger.debug("substrate state reset failed: %s", exc)
        return False

    if response.status_code == 501:
        logger.debug("substrate state manager does not support reset")
        return False
    return response.status_code == 200


def create_substrate_session(
    region: str = "us-east-1", endpoint: Optional[str] = None
) -> boto3.Session:
    """Return a boto3 session pointed at substrate.

    Raises
    ------
    RuntimeError
        If no emulator answers at the endpoint. Callers that would rather skip
        than fail should gate on :func:`is_substrate_available` first.
    """
    endpoint = endpoint or get_substrate_endpoint()

    if not is_substrate_running(endpoint):
        raise RuntimeError(f"substrate is not running at {endpoint}")

    return boto3.Session(  # nosec B106
        aws_access_key_id=TEST_ACCESS_KEY_ID,
        aws_secret_access_key=TEST_SECRET_ACCESS_KEY,
        region_name=region,
    )


#: The name every integration module imports. ``create_*`` reads better for a
#: factory, but the suite settled on ``get_*``, so both exist rather than
#: churning ten call sites over a verb.
get_substrate_session = create_substrate_session


def get_substrate_client(
    service_name: str,
    session: Optional[boto3.Session] = None,
    region: str = "us-east-1",
    endpoint: Optional[str] = None,
) -> Any:
    """Return a boto3 client for ``service_name`` bound to substrate."""
    endpoint = endpoint or get_substrate_endpoint()
    session = session or create_substrate_session(region, endpoint)

    return session.client(service_name, endpoint_url=endpoint)


def get_substrate_resource(
    service_name: str,
    session: Optional[boto3.Session] = None,
    region: str = "us-east-1",
    endpoint: Optional[str] = None,
) -> Any:
    """Return a boto3 resource for ``service_name`` bound to substrate."""
    endpoint = endpoint or get_substrate_endpoint()
    session = session or create_substrate_session(region, endpoint)

    return session.resource(service_name, endpoint_url=endpoint)


def setup_substrate_vpc(session: Optional[boto3.Session] = None) -> Dict[str, str]:
    """Provision a VPC, subnet, gateway, route table and security group.

    Since #69 the provider creates no network resources, so every mode requires
    ``vpc_id``/``subnet_id``/``security_group_id`` to exist beforehand. This is
    what supplies them in the emulator.

    Pass ``session`` when the caller has one, so the resources land in the region
    that session uses. Substrate partitions by region, and the default here is
    ``us-east-1`` while the suite's ``AWS_TEST_REGION`` defaults to ``us-west-2``
    -- creating in one and describing from the other reports the resource as
    missing immediately after it was created successfully.

    The subnet's availability zone is read from ``describe_availability_zones``
    rather than built as ``f"{region}a"``: that string is a guess about zone
    naming, and a wrong guess is how the real-AWS suite ended up pinned to a zone
    that does not offer ``t3.micro``.
    """
    ec2 = get_substrate_client("ec2", session=session)

    def tag(resource_id: str, name: str) -> None:
        ec2.create_tags(
            Resources=[resource_id],
            Tags=[
                {"Key": "Name", "Value": name},
                {"Key": "ParslResource", "Value": "true"},
            ],
        )

    vpc_id = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})
    tag(vpc_id, "parsl-test-vpc")

    zones = ec2.describe_availability_zones()["AvailabilityZones"]
    subnet_id = ec2.create_subnet(
        VpcId=vpc_id,
        CidrBlock="10.0.0.0/24",
        AvailabilityZone=zones[0]["ZoneName"],
    )["Subnet"]["SubnetId"]
    tag(subnet_id, "parsl-test-subnet")

    igw_id = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
    tag(igw_id, "parsl-test-igw")
    ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)

    route_table_id = ec2.create_route_table(VpcId=vpc_id)["RouteTable"]["RouteTableId"]
    tag(route_table_id, "parsl-test-rt")
    ec2.create_route(
        RouteTableId=route_table_id,
        DestinationCidrBlock="0.0.0.0/0",
        GatewayId=igw_id,
    )
    ec2.associate_route_table(RouteTableId=route_table_id, SubnetId=subnet_id)

    sg_id = ec2.create_security_group(
        GroupName="parsl-test-sg",
        Description="Parsl test security group",
        VpcId=vpc_id,
    )["GroupId"]
    tag(sg_id, "parsl-test-sg")

    ec2.authorize_security_group_ingress(
        GroupId=sg_id,
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
            },
            # The HTEX interchange range: workers dial back to the driver on
            # these ports, so without them a submitted job never connects.
            {
                "IpProtocol": "tcp",
                "FromPort": 54000,
                "ToPort": 55000,
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
            },
        ],
    )
    ec2.authorize_security_group_ingress(
        GroupId=sg_id,
        IpPermissions=[
            {
                "IpProtocol": "-1",
                "FromPort": -1,
                "ToPort": -1,
                "UserIdGroupPairs": [{"GroupId": sg_id}],
            }
        ],
    )

    return {
        "vpc_id": vpc_id,
        "subnet_id": subnet_id,
        "security_group_id": sg_id,
        "route_table_id": route_table_id,
        "internet_gateway_id": igw_id,
    }


def cleanup_substrate_vpc(vpc_id: str, session: Optional[boto3.Session] = None) -> None:
    """Delete a VPC and everything attached to it, best-effort.

    Every step is individually guarded: this runs in teardown, where raising
    would mask the actual test result. Order matters — dependents before
    dependencies, or the VPC delete fails with ``DependencyViolation``.

    Pass the same ``session`` used to create the VPC — see
    :func:`setup_substrate_vpc` on why the region has to match.

    Prefer :func:`reset_substrate_state` where the whole emulator can be wiped;
    this exists for tests that share a server with others.
    """
    ec2 = get_substrate_client("ec2", session=session)

    try:
        vpcs = ec2.describe_vpcs(VpcIds=[vpc_id]).get("Vpcs")
        if not vpcs:
            logger.warning("VPC %s not found", vpc_id)
            return
    except Exception as exc:
        logger.error("Error describing VPC %s: %s", vpc_id, exc)
        return

    in_vpc = [{"Name": "vpc-id", "Values": [vpc_id]}]

    def delete_each(items, id_key, delete, label):
        for item in items:
            resource_id = item[id_key]
            try:
                delete(resource_id)
                logger.debug("Deleted %s %s", label, resource_id)
            except Exception as exc:
                logger.error("Error deleting %s %s: %s", label, resource_id, exc)

    groups = ec2.describe_security_groups(Filters=in_vpc).get("SecurityGroups", [])
    delete_each(
        [g for g in groups if g["GroupName"] != "default"],
        "GroupId",
        lambda i: ec2.delete_security_group(GroupId=i),
        "security group",
    )

    subnets = ec2.describe_subnets(Filters=in_vpc).get("Subnets", [])
    delete_each(subnets, "SubnetId", lambda i: ec2.delete_subnet(SubnetId=i), "subnet")

    tables = ec2.describe_route_tables(Filters=in_vpc).get("RouteTables", [])
    delete_each(
        # The main route table belongs to the VPC and goes with it; deleting it
        # directly is rejected.
        [
            t
            for t in tables
            if not any(a.get("Main") for a in t.get("Associations", []))
        ],
        "RouteTableId",
        lambda i: ec2.delete_route_table(RouteTableId=i),
        "route table",
    )

    gateways = ec2.describe_internet_gateways(
        Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}]
    ).get("InternetGateways", [])
    delete_each(
        gateways,
        "InternetGatewayId",
        lambda i: (
            ec2.detach_internet_gateway(InternetGatewayId=i, VpcId=vpc_id),
            ec2.delete_internet_gateway(InternetGatewayId=i),
        ),
        "internet gateway",
    )

    try:
        ec2.delete_vpc(VpcId=vpc_id)
        logger.debug("Deleted VPC %s", vpc_id)
    except Exception as exc:
        logger.error("Error deleting VPC %s: %s", vpc_id, exc)

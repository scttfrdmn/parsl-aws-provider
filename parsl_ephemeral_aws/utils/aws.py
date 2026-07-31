"""
AWS utility functions for the EphemeralAWSProvider.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import base64
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple, Union

import boto3
from botocore.exceptions import (
    ClientError,
    CredentialRetrievalError,
    NoCredentialsError,
    PartialCredentialsError,
    ProfileNotFound,
    TokenRetrievalError,
)

from parsl_ephemeral_aws.constants import (
    AMI_SSM_PARAMETER_TEMPLATE,
    ARCHITECTURE_ARM64,
    ARCHITECTURE_X86_64,
    ARM64_INSTANCE_FAMILIES,
    DEFAULT_AMI_MAPPING,
    DEFAULT_ARCHITECTURE,
    DEFAULT_REGION,
    EC2_FLEET_ALLOCATION_STRATEGIES,
    EC2_FLEET_TERMINATE_INSTANCES,
    EC2_FLEET_TYPE_INSTANT,
    IMDSV2_METADATA_OPTIONS,
    RESOURCE_TYPE_FLEET,
    RESOURCE_TYPE_LAUNCH_TEMPLATE,
    SPOT_FLEET_ALLOCATION_STRATEGIES,
    SPOT_INTERRUPTION_EVENT_DETAIL_TYPE,
    SPOT_INTERRUPTION_EVENT_SOURCE,
    SPOT_INTERRUPTION_QUEUE_RETENTION_SECONDS,
    TAG_AWS_FLEET_ID,
)
from parsl_ephemeral_aws.exceptions import (
    AMINotFoundError,
    AWSAuthenticationError,
    AWSConnectionError,
    ResourceCreationError,
    ResourceDeletionError,
    ResourceNotFoundError,
)


logger = logging.getLogger(__name__)

# STS error codes that mean "your credentials are bad", as opposed to "AWS could
# not be reached". Both map to AWSConnectionError subclasses, but only the former
# is worth telling the user to go fix their credentials over — the latter is
# worth retrying.
_AUTH_ERROR_CODES = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "AuthFailure",
        "ExpiredToken",
        "ExpiredTokenException",
        "InvalidAccessKeyId",
        "InvalidClientTokenId",
        "MissingAuthenticationToken",
        "SignatureDoesNotMatch",
        "UnauthorizedOperation",
        "UnrecognizedClientException",
    }
)

# botocore exceptions raised when credentials are absent, incomplete, or
# unresolvable. These are never transient, so they must not be reported as a
# connection failure that a caller might retry.
_CREDENTIAL_EXCEPTIONS = (
    CredentialRetrievalError,
    NoCredentialsError,
    PartialCredentialsError,
    ProfileNotFound,
    TokenRetrievalError,
)


def resolve_manager_session(provider: Any, credential_manager: Any) -> boto3.Session:
    """Return the session a compute manager should use.

    The caller's own session wins. Only when the provider has none does this fall
    back to building one from the credential manager.

    All four compute managers previously went straight to
    ``credential_manager.create_boto3_session()``, discarding
    ``provider.session`` entirely — so a caller who passed an explicitly
    configured session (temporary role credentials, a chosen profile, a
    LocalStack ``endpoint_url``) had it silently replaced by one built from
    ambient environment credentials, possibly pointing at a different account
    (#117). It also meant an injected test double was ignored and the manager
    reached real AWS; a unit test created a live ECS cluster this way.

    This mirrors the fix already applied to the state stores, which had the same
    defect.

    Parameters
    ----------
    provider : Any
        The provider (or operating mode) the manager was constructed with.
    credential_manager : Any
        Fallback used when the provider carries no session.

    Returns
    -------
    boto3.Session
        The caller's session if it has one, else a newly built session.
    """
    session = getattr(provider, "session", None)
    if session is not None:
        return session

    return credential_manager.create_boto3_session(
        region=getattr(provider, "region", None) or DEFAULT_REGION
    )


def create_session(
    region: Optional[str] = None,
    profile_name: Optional[str] = None,
    aws_access_key_id: Optional[str] = None,
    aws_secret_access_key: Optional[str] = None,
    aws_session_token: Optional[str] = None,
    endpoint_url: Optional[str] = None,
) -> boto3.Session:
    """Create a boto3 session with the given parameters.

    Parameters
    ----------
    region : Optional[str], optional
        AWS region to use, by default None
    profile_name : Optional[str], optional
        AWS profile name to use, by default None
    aws_access_key_id : Optional[str], optional
        AWS access key ID, by default None
    aws_secret_access_key : Optional[str], optional
        AWS secret access key, by default None
    aws_session_token : Optional[str], optional
        AWS session token, by default None
    endpoint_url : Optional[str], optional
        Custom endpoint URL for AWS services (e.g., for LocalStack), by default None

    Returns
    -------
    boto3.Session
        The created boto3 session

    Raises
    ------
    AWSAuthenticationError
        If authentication fails
    AWSConnectionError
        If connection to AWS services fails
    """
    try:
        session = boto3.Session(
            region_name=region or DEFAULT_REGION,
            profile_name=profile_name or None,  # treat "" same as None
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            aws_session_token=aws_session_token,
        )

        # Verify that the session is valid by calling a simple operation
        sts = session.client("sts", endpoint_url=endpoint_url)
        sts.get_caller_identity()

        logger.debug(f"Created AWS session for region {session.region_name}")
        return session

    except _CREDENTIAL_EXCEPTIONS as e:
        # Absent or unresolvable credentials. Not transient, so don't let this
        # reach the generic handler and be reported as a connection failure the
        # caller might retry.
        logger.error(f"AWS authentication failed: {e}")
        raise AWSAuthenticationError(f"AWS authentication failed: {e}") from e
    except ClientError as e:
        # Classify on the error *code*, not on str(e) -- a message that merely
        # mentions AccessDenied is not an auth failure, and matching the string
        # missed AuthFailure, SignatureDoesNotMatch, ExpiredToken, and
        # UnrecognizedClientException, all of which plainly are.
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code in _AUTH_ERROR_CODES:
            logger.error(f"AWS authentication failed: {e}")
            raise AWSAuthenticationError(f"AWS authentication failed: {e}") from e
        else:
            logger.error(f"AWS connection failed: {e}")
            raise AWSConnectionError(f"AWS connection failed: {e}") from e
    except Exception as e:
        logger.error(f"Failed to create AWS session: {e}")
        raise AWSConnectionError(f"Failed to create AWS session: {e}") from e


def architecture_for_instance_type(instance_type: str) -> str:
    """Return the CPU architecture an instance type needs an AMI for.

    An AMI is architecture-specific, so launching a Graviton instance with an
    x86_64 image fails. Nothing in this package distinguished the two before
    #84, which made every arm64 instance type unusable.

    The family suffix is the signal: AWS appends ``g`` to the generation of
    every Graviton family (``c7g``, ``m8g``, ``r7gd``, ``c8gn``, ...) and to no
    x86_64 family. Validated against ``describe_instance_types`` for all 1,346
    types AWS offers in us-east-1: 396 arm64 and 950 x86_64, zero mistakes.

    The only exceptions are the eight ``mac*.metal`` types, which report
    ``arm64_mac`` and need a macOS AMI rather than AL2023 -- so classifying
    them as x86_64 is no worse than the arm64 answer would be. A caller wanting
    a Mac instance must pass ``image_id`` explicitly either way.

    Parameters
    ----------
    instance_type : str
        EC2 instance type, e.g. ``"c7g.xlarge"`` or ``"t3.micro"``.

    Returns
    -------
    str
        ``"arm64"`` or ``"x86_64"``.
    """
    family = instance_type.split(".")[0].lower()

    if family in ARM64_INSTANCE_FAMILIES:
        return ARCHITECTURE_ARM64

    # Split "c7gd" into prefix "c", generation "7", suffix "gd". A type we
    # cannot parse is assumed x86_64, which is what it was before #84.
    match = re.match(r"^([a-z]+)(\d+)([a-z]*)$", family)
    if match is None:
        logger.debug(
            f"Unrecognised instance family {family!r}; assuming {DEFAULT_ARCHITECTURE}"
        )
        return DEFAULT_ARCHITECTURE

    return ARCHITECTURE_ARM64 if "g" in match.group(3) else ARCHITECTURE_X86_64


def normalize_spot_fleet_allocation_strategy(strategy: str) -> str:
    """Translate an allocation strategy to the spelling RequestSpotFleet takes.

    The two fleet APIs disagree on the casing of the same enum, and each
    rejects the other's spelling. Verified against real EC2 in us-east-1 --
    ``RequestSpotFleet`` with ``"price-capacity-optimized"`` returns
    ``InvalidParameterValue``, and ``CreateFleet`` with
    ``"priceCapacityOptimized"`` returns ``InvalidParameter``.

    The provider's ``spot_allocation_strategy`` kwarg is documented in
    kebab-case, so it needs converting at the RequestSpotFleet boundary. Values
    already in camelCase pass through, so a caller who supplied the API-native
    spelling is not punished for it.

    Parameters
    ----------
    strategy : str
        Allocation strategy in either spelling, e.g.
        ``"price-capacity-optimized"`` or ``"priceCapacityOptimized"``.

    Returns
    -------
    str
        The camelCase spelling ``RequestSpotFleet`` accepts.

    Raises
    ------
    ValueError
        If the strategy is not one EC2 recognises, or is not a string. Raised
        here rather than letting EC2 reject it, because a spot fleet request
        fails several seconds and one IAM role later.
    """
    if strategy in SPOT_FLEET_ALLOCATION_STRATEGIES:
        return strategy

    # Checked explicitly: a non-string reaches ``.split()`` and, for a MagicMock,
    # returns another mock that unpacks to nothing -- "not enough values to
    # unpack", which names neither the argument nor the caller.
    if not isinstance(strategy, str):
        raise ValueError(
            f"Spot allocation strategy must be a string, got "
            f"{type(strategy).__name__}: {strategy!r}"
        )

    # "price-capacity-optimized" -> "priceCapacityOptimized"
    head, *rest = strategy.split("-")
    camel = head + "".join(word.capitalize() for word in rest)
    if camel in SPOT_FLEET_ALLOCATION_STRATEGIES:
        return camel

    raise ValueError(
        f"Unsupported spot allocation strategy {strategy!r}. Expected one of "
        f"{sorted(SPOT_FLEET_ALLOCATION_STRATEGIES)} or their kebab-case "
        f"equivalents."
    )


def normalize_ec2_fleet_allocation_strategy(strategy: str) -> str:
    """Translate an allocation strategy to the spelling CreateFleet takes.

    The mirror image of :func:`normalize_spot_fleet_allocation_strategy`:
    ``CreateFleet`` accepts only kebab-case, and rejects the camelCase spelling
    ``RequestSpotFleet`` demands. The provider's ``spot_allocation_strategy``
    kwarg is documented in kebab-case, so the common case is a pass-through --
    but a caller who supplied the camelCase form (or read it off the legacy
    constant) is converted rather than punished.

    Validating here matters more than it does on the legacy path, because EC2
    does *not* catch this for you until real capacity is requested: verified
    against real EC2 that both ``DryRun=True`` and
    ``TotalTargetCapacity=0`` accept ``"priceCapacityOptimized"``, and
    ``describe_fleets`` then shows the bad value stored verbatim.

    Parameters
    ----------
    strategy : str
        Allocation strategy in either spelling.

    Returns
    -------
    str
        The kebab-case spelling ``CreateFleet`` accepts.

    Raises
    ------
    ValueError
        If the strategy is not one EC2 recognises, or is not a string.
    """
    if strategy in EC2_FLEET_ALLOCATION_STRATEGIES:
        return strategy

    if not isinstance(strategy, str):
        raise ValueError(
            f"Spot allocation strategy must be a string, got "
            f"{type(strategy).__name__}: {strategy!r}"
        )

    # "priceCapacityOptimized" -> "price-capacity-optimized"
    kebab = re.sub(r"(?<!^)(?=[A-Z])", "-", strategy).lower()
    if kebab in EC2_FLEET_ALLOCATION_STRATEGIES:
        return kebab

    raise ValueError(
        f"Unsupported spot allocation strategy {strategy!r}. Expected one of "
        f"{sorted(EC2_FLEET_ALLOCATION_STRATEGIES)} or their camelCase "
        f"equivalents."
    )


def build_fleet_launch_template_configs(
    template_id: str,
    template_version: str,
    instance_types: List[str],
    subnet_id: str,
) -> List[Dict[str, Any]]:
    """Build the ``LaunchTemplateConfigs`` for a CreateFleet request (#86).

    One config referencing one template, with an override per instance type so a
    single template still covers every pool the fleet may draw from.

    ``CreateFleet``'s override shape is richer than Spot Fleet's -- it also
    accepts ``ImageId``, ``MaxPrice``, and ``BlockDeviceMappings`` -- but it
    still has no ``UserData``, which is why the per-block user data has to live
    in the template rather than here.

    Parameters
    ----------
    template_id : str
        Launch template to draw the baseline definition from.
    template_version : str
        Pinned version. Not ``$Latest``: a fleet must launch the definition the
        caller built, not whatever a concurrent provider added afterwards.
    instance_types : List[str]
        Types to emit as overrides, in preference order.
    subnet_id : str
        Subnet every override launches into.

    Returns
    -------
    List[Dict[str, Any]]
        A single-element ``LaunchTemplateConfigs`` list.
    """
    return [
        {
            "LaunchTemplateSpecification": {
                "LaunchTemplateId": template_id,
                "Version": template_version,
            },
            "Overrides": [
                {"InstanceType": instance_type, "SubnetId": subnet_id}
                for instance_type in instance_types
            ],
        }
    ]


def create_ec2_fleet(
    ec2_client: Any,
    launch_template_configs: List[Dict[str, Any]],
    target_capacity: int,
    allocation_strategy: str,
    client_token: Optional[str] = None,
    tags: Optional[List[Dict[str, str]]] = None,
    max_total_price: Optional[str] = None,
) -> Tuple[str, List[str]]:
    """Create an ``instant`` EC2 Fleet and return its ID and instance IDs.

    Replaces ``RequestSpotFleet``, which AWS describes as "a legacy API with no
    planned investment" (#86).

    Fleet type ``instant`` is deliberate: it places a synchronous request and
    returns the launched instance IDs in the response, so a block knows its
    instances without polling. That is what the rest of this package assumes,
    and the asynchronous types cannot provide it.

    Several parameters the legacy path sent are *rejected* for this fleet type --
    verified against real EC2, with ``InvalidParameter`` rather than silent
    acceptance -- so they are deliberately absent here:

    * ``ReplaceUnhealthyInstances`` and ``TerminateInstancesWithExpiration``
      ("not supported for given fleet type")
    * ``SpotOptions.MaintenanceStrategies``, i.e. Capacity Rebalance ("only
      compatible with fleet type maintain")

    Parameters
    ----------
    ec2_client : Any
        A boto3 EC2 client.
    launch_template_configs : List[Dict[str, Any]]
        As returned by :func:`build_fleet_launch_template_configs`.
    target_capacity : int
        Number of instances to request, all spot.
    allocation_strategy : str
        Spot allocation strategy; normalised to the kebab-case spelling
        ``CreateFleet`` requires.
    client_token : Optional[str]
        Idempotency token. EC2 generates one when omitted.
    tags : Optional[List[Dict[str, str]]]
        Applied to both the fleet resource and the instances it launches, so
        either can be found by the cleanup sweep.
    max_total_price : Optional[str]
        Maximum hourly spot price for the whole fleet. Left unset by default:
        AWS warns that capping the price increases interruptions.

    Returns
    -------
    Tuple[str, List[str]]
        The fleet ID, and the IDs of the instances it launched. The instance
        list may be shorter than *target_capacity*, or empty, if EC2 could not
        fill the request; the caller decides whether that is fatal.

    Raises
    ------
    ClientError
        Propagated unchanged from ``CreateFleet``, so the caller can discriminate
        on the EC2 error code. See the note at the call itself.
    ValueError
        If *allocation_strategy* is not one EC2 recognises.
    """
    spot_options: Dict[str, Any] = {
        "AllocationStrategy": normalize_ec2_fleet_allocation_strategy(
            allocation_strategy
        ),
        # An interrupted worker's instance should go away, not linger stopped
        # with a billed EBS volume that nothing is tracking.
        "InstanceInterruptionBehavior": "terminate",
    }
    if max_total_price is not None:
        spot_options["MaxTotalPrice"] = max_total_price

    kwargs: Dict[str, Any] = {
        "Type": EC2_FLEET_TYPE_INSTANT,
        "LaunchTemplateConfigs": launch_template_configs,
        "TargetCapacitySpecification": {
            "TotalTargetCapacity": target_capacity,
            "DefaultTargetCapacityType": "spot",
        },
        "SpotOptions": spot_options,
    }
    if client_token:
        kwargs["ClientToken"] = client_token
    if tags:
        kwargs["TagSpecifications"] = [
            {"ResourceType": RESOURCE_TYPE_FLEET, "Tags": tags},
            {"ResourceType": "instance", "Tags": tags},
        ]

    # ClientError is deliberately *not* caught and wrapped here. Callers
    # discriminate on the EC2 error code -- SpotFleetManager._create_fleet maps it
    # onto this package's exception hierarchy, audit-logs the failure, and deletes
    # the launch template the fleet was built for. Wrapping it in
    # ResourceCreationError made all three unreachable, since the caller's
    # ``except ClientError`` no longer matched.
    response = ec2_client.create_fleet(**kwargs)

    fleet_id = str(response["FleetId"])
    instance_ids = [
        instance_id
        for entry in response.get("Instances", [])
        for instance_id in entry.get("InstanceIds", [])
    ]

    # An instant fleet reports per-instance failures inline instead of failing
    # the call, so a partially-filled fleet looks like success. Surface them:
    # "no capacity in this pool" is the single most common fleet outcome and is
    # otherwise invisible.
    for error in response.get("Errors", []):
        logger.warning(
            f"EC2 Fleet {fleet_id} could not launch an instance: "
            f"{error.get('ErrorCode')} - {error.get('ErrorMessage')}"
        )

    logger.info(
        f"Created EC2 Fleet {fleet_id} with {len(instance_ids)}/{target_capacity} "
        f"instances: {instance_ids}"
    )
    return fleet_id, instance_ids


def describe_ec2_fleet(ec2_client: Any, fleet_id: str) -> Optional[Dict[str, Any]]:
    """Return the fleet's ``FleetData``, or None if EC2 has forgotten it.

    The ID is always passed explicitly, and not merely as an optimisation: AWS
    documents that "if a fleet is of type instant, you must specify the fleet ID
    in the request, otherwise the fleet does not appear in the response".
    Verified -- ``describe_fleets()`` with no ``FleetIds`` returned an empty list
    while an instant fleet was active.

    Parameters
    ----------
    ec2_client : Any
        A boto3 EC2 client.
    fleet_id : str
        Fleet to describe.

    Returns
    -------
    Optional[Dict[str, Any]]
        The ``FleetData`` document, or None when the fleet no longer exists.
    """
    try:
        fleets = ec2_client.describe_fleets(FleetIds=[fleet_id]).get("Fleets", [])
    except ClientError as e:
        if e.response["Error"]["Code"] == "InvalidFleetId.NotFound":
            logger.debug(f"EC2 Fleet {fleet_id} no longer exists")
            return None
        raise
    return fleets[0] if fleets else None


def get_ec2_fleet_instance_ids(ec2_client: Any, fleet_id: str) -> List[str]:
    """Return the IDs of instances belonging to *fleet_id*.

    Goes through ``describe_instances`` filtered on the ``aws:ec2:fleet-id`` tag
    EC2 applies to every fleet-launched instance, because the two obvious
    routes do not work for an instant fleet -- verified against real EC2:

    * ``describe_fleet_instances`` refuses it outright with ``Unsupported``:
      "Describe fleet instances is not supported by this type of fleet."
    * ``describe_fleets`` does return an ``Instances`` list, but only reflects
      the original launch; it does not drop instances that have since
      terminated.

    Terminated instances are excluded, so the result answers "what is this fleet
    still running" rather than "what did it ever launch".

    Parameters
    ----------
    ec2_client : Any
        A boto3 EC2 client.
    fleet_id : str
        Fleet whose instances to list.

    Returns
    -------
    List[str]
        Instance IDs that are not terminated. Empty if the fleet launched
        nothing, or everything it launched is gone.
    """
    instance_ids: List[str] = []
    try:
        paginator = ec2_client.get_paginator("describe_instances")
        pages = paginator.paginate(
            Filters=[
                {"Name": f"tag:{TAG_AWS_FLEET_ID}", "Values": [fleet_id]},
                {
                    "Name": "instance-state-name",
                    "Values": ["pending", "running", "stopping", "stopped"],
                },
            ]
        )
        for page in pages:
            for reservation in page.get("Reservations", []):
                for instance in reservation.get("Instances", []):
                    instance_ids.append(instance["InstanceId"])
    except ClientError as e:
        logger.warning(f"Could not list instances for EC2 Fleet {fleet_id}: {e}")
    return instance_ids


def delete_ec2_fleet(ec2_client: Any, fleet_id: str) -> None:
    """Delete an EC2 Fleet, terminating its instances.

    Instance termination is not optional for an instant fleet: AWS rejects
    ``NoTerminateInstances`` for this type, and "a deleted instant fleet with
    running instances is not supported".

    Tolerates a fleet that is already gone, and a repeat call on one already
    deleting -- verified that deleting twice succeeds both times, reporting
    ``deleted_terminating``, and that an unknown ID comes back as a
    ``fleetIdDoesNotExist`` entry in ``UnsuccessfulFleetDeletions`` rather than
    as a raised error. Both are the desired outcome for a cleanup path, so
    neither raises.

    Parameters
    ----------
    ec2_client : Any
        A boto3 EC2 client.
    fleet_id : str
        Fleet to delete.

    Raises
    ------
    ResourceDeletionError
        If EC2 refuses the deletion for any reason other than the fleet not
        existing.
    """
    try:
        response = ec2_client.delete_fleets(
            FleetIds=[fleet_id],
            TerminateInstances=EC2_FLEET_TERMINATE_INSTANCES,
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "InvalidFleetId.NotFound":
            logger.debug(f"EC2 Fleet {fleet_id} already deleted")
            return
        raise ResourceDeletionError(
            f"Failed to delete EC2 Fleet {fleet_id}: {e}"
        ) from e

    for failure in response.get("UnsuccessfulFleetDeletions", []):
        code = failure.get("Error", {}).get("Code")
        if code == "fleetIdDoesNotExist":
            logger.debug(f"EC2 Fleet {fleet_id} already deleted")
            continue
        raise ResourceDeletionError(
            f"Failed to delete EC2 Fleet {fleet_id}: {code} - "
            f"{failure.get('Error', {}).get('Message')}"
        )

    for success in response.get("SuccessfulFleetDeletions", []):
        logger.info(
            f"Deleted EC2 Fleet {fleet_id}: "
            f"{success.get('PreviousFleetState')} -> "
            f"{success.get('CurrentFleetState')}"
        )


def create_spot_interruption_notifier(
    events_client: Any,
    sqs_client: Any,
    name: str,
    tags: Optional[List[Dict[str, str]]] = None,
) -> Tuple[str, str, str]:
    """Wire an EventBridge spot-interruption rule to a fresh SQS queue (#86).

    This is what supplies the two-minute advance warning. Polling EC2 state
    cannot: an interrupted instance is only observable once it reaches
    ``shutting-down``, which is after the reclaim, far too late to checkpoint.
    An ``instant`` fleet also gets no Capacity Rebalance -- ``CreateFleet``
    rejects ``SpotOptions.MaintenanceStrategies`` for the type -- so EventBridge
    is the mechanism, and it has the further advantage of working for instances
    that are already running.

    **No IAM role is created, and none is needed.** Verified against real
    EventBridge: ``put_targets`` with an SQS ARN and no ``RoleArn`` returned
    ``FailedEntryCount=0``. Unlike most target types, delivery to SQS is
    authorised by the *queue's* resource policy, which is why this sets one
    granting ``events.amazonaws.com`` permission to ``sqs:SendMessage``,
    conditioned on ``aws:SourceArn`` being this rule -- so no other rule, in this
    account or any other, can post to the queue.

    Verified end to end with a Fault Injection Simulator experiment
    (``aws:ec2:send-spot-instance-interruptions``): the warning reached the queue
    15.2s after the experiment started, while the instance was still
    ``running``.

    Parameters
    ----------
    events_client : Any
        A boto3 EventBridge client.
    sqs_client : Any
        A boto3 SQS client.
    name : str
        Name for both the rule and the queue. Must be unique per provider.
    tags : Optional[List[Dict[str, str]]]
        Applied to the rule so a leaked one is traceable. Not applied to the
        queue: SQS takes tags as a flat mapping, and the queue is named after
        the same provider anyway.

    Returns
    -------
    Tuple[str, str, str]
        The rule name, the queue URL, and the queue ARN.

    Raises
    ------
    ResourceCreationError
        If the rule, the queue, or the wiring between them cannot be created.
    """
    pattern = json.dumps(
        {
            "source": [SPOT_INTERRUPTION_EVENT_SOURCE],
            "detail-type": [SPOT_INTERRUPTION_EVENT_DETAIL_TYPE],
        }
    )

    try:
        queue_url = sqs_client.create_queue(
            QueueName=name,
            Attributes={
                # A warning is worthless once its two minutes are up, so let an
                # undelivered message expire rather than be replayed against a
                # long-dead instance.
                "MessageRetentionPeriod": str(
                    SPOT_INTERRUPTION_QUEUE_RETENTION_SECONDS
                ),
            },
        )["QueueUrl"]
        queue_arn = sqs_client.get_queue_attributes(
            QueueUrl=queue_url, AttributeNames=["QueueArn"]
        )["Attributes"]["QueueArn"]

        rule_kwargs: Dict[str, Any] = {
            "Name": name,
            "EventPattern": pattern,
            "State": "ENABLED",
            "Description": "Parsl ephemeral AWS provider spot interruption warnings",
        }
        if tags:
            rule_kwargs["Tags"] = tags
        rule_arn = events_client.put_rule(**rule_kwargs)["RuleArn"]

        # Set the queue policy *before* adding the target, so no window exists in
        # which EventBridge has a target it cannot deliver to.
        sqs_client.set_queue_attributes(
            QueueUrl=queue_url,
            Attributes={
                "Policy": json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Sid": "AllowEventBridgeRule",
                                "Effect": "Allow",
                                "Principal": {"Service": "events.amazonaws.com"},
                                "Action": "sqs:SendMessage",
                                "Resource": queue_arn,
                                "Condition": {"ArnEquals": {"aws:SourceArn": rule_arn}},
                            }
                        ],
                    }
                )
            },
        )

        response = events_client.put_targets(
            Rule=name, Targets=[{"Id": "parsl-spot-warning-queue", "Arn": queue_arn}]
        )
        if response.get("FailedEntryCount"):
            failures = response.get("FailedEntries", [])
            raise ResourceCreationError(
                f"EventBridge refused the SQS target for rule {name}: {failures}"
            )
    except ClientError as e:
        raise ResourceCreationError(
            f"Failed to create spot interruption notifier {name}: {e}"
        ) from e

    logger.info(
        f"Created spot interruption notifier {name}: EventBridge rule -> {queue_arn}"
    )
    return name, queue_url, queue_arn


def delete_spot_interruption_notifier(
    events_client: Any,
    sqs_client: Any,
    rule_name: Optional[str],
    queue_url: Optional[str],
) -> None:
    """Tear down what :func:`create_spot_interruption_notifier` built.

    Order matters: the target has to go before the rule, because EventBridge
    refuses to delete a rule that still has one. Every step logs rather than
    raises -- this runs from cleanup paths, where a rule that is already gone is
    the desired end state and must not stop the queue from being deleted too.

    Parameters
    ----------
    events_client : Any
        A boto3 EventBridge client.
    sqs_client : Any
        A boto3 SQS client.
    rule_name : Optional[str]
        Rule to delete. Ignored when None.
    queue_url : Optional[str]
        Queue to delete. Ignored when None.
    """
    if rule_name:
        try:
            events_client.remove_targets(
                Rule=rule_name, Ids=["parsl-spot-warning-queue"]
            )
        except ClientError as e:
            logger.warning(f"Could not remove targets from rule {rule_name}: {e}")
        try:
            events_client.delete_rule(Name=rule_name)
            logger.info(f"Deleted spot interruption rule {rule_name}")
        except ClientError as e:
            logger.warning(f"Could not delete rule {rule_name}: {e}")

    if queue_url:
        try:
            sqs_client.delete_queue(QueueUrl=queue_url)
            logger.info(f"Deleted spot interruption queue {queue_url}")
        except ClientError as e:
            logger.warning(f"Could not delete queue {queue_url}: {e}")


def encode_user_data(user_data: str) -> str:
    """Base64-encode *user_data* for an API that does not do it for you.

    botocore installs ``base64_encode_user_data`` on
    ``before-parameter-build.ec2.RunInstances`` only -- verified by inspecting
    ``botocore.handlers.BUILTIN_HANDLERS``, where that operation and
    ``autoscaling.CreateLaunchConfiguration`` are the sole entries.
    ``CreateLaunchTemplate`` is *not* among them, so a plaintext script passed
    there is stored verbatim, handed to cloud-init base64-decoded, and produces
    garbage that fails silently -- the instance boots fine and simply never runs
    the worker.

    Encoding twice is the other half of the trap, so callers must not hand
    already-encoded data to ``RunInstances``.

    Parameters
    ----------
    user_data : str
        The plaintext user data script.

    Returns
    -------
    str
        The base64-encoded form.
    """
    return base64.b64encode(user_data.encode()).decode()


def build_launch_template_data(
    image_id: str,
    instance_type: str,
    subnet_id: Optional[str] = None,
    security_group_id: Optional[str] = None,
    associate_public_ip: bool = True,
    key_name: Optional[str] = None,
    iam_instance_profile_arn: Optional[str] = None,
    shutdown_behavior: str = "terminate",
    user_data: Optional[str] = None,
    monitoring: bool = False,
) -> Dict[str, Any]:
    """Build the ``LaunchTemplateData`` shared by every launch path (#85).

    One definition serves the on-demand, spot, and fleet paths, which is what
    makes the Phase 6 EC2 Fleet/ASG migration possible -- those APIs accept a
    template reference and nothing resembling ``RunInstances`` kwargs.

    Every field is optional except the image and instance type, because the
    template is a *baseline*: ``RunInstances`` overrides ``UserData`` and
    ``TagSpecifications`` per launch, and Spot Fleet overrides ``InstanceType``
    and ``SubnetId`` per pool.

    Parameters
    ----------
    image_id : str
        AMI to launch.
    instance_type : str
        Default instance type; fleet paths override it per pool.
    subnet_id : Optional[str]
        Subnet for the primary network interface.
    security_group_id : Optional[str]
        Security group for the primary network interface.
    associate_public_ip : bool
        Whether the primary interface gets a public IP.
    key_name : Optional[str]
        EC2 key pair for SSH access.
    iam_instance_profile_arn : Optional[str]
        Instance profile ARN; required for SSM command dispatch.
    shutdown_behavior : str
        ``"terminate"`` or ``"stop"`` for an instance-initiated shutdown.
    user_data : Optional[str]
        Plaintext user data; base64-encoded here, since
        ``CreateLaunchTemplate`` does not do it.
    monitoring : bool
        Whether to enable detailed CloudWatch monitoring.

    Returns
    -------
    Dict[str, Any]
        A ``LaunchTemplateData`` document.
    """
    data: Dict[str, Any] = {
        "ImageId": image_id,
        "InstanceType": instance_type,
        "InstanceInitiatedShutdownBehavior": shutdown_behavior,
        # Copied, not referenced: the caller must not be able to mutate the
        # module-level default through the document it gets back.
        "MetadataOptions": dict(IMDSV2_METADATA_OPTIONS),
    }

    # A network interface and top-level SecurityGroupIds are mutually exclusive
    # -- EC2 rejects a template carrying both. The interface form is required
    # for AssociatePublicIpAddress, so it wins whenever a subnet is known.
    if subnet_id:
        interface: Dict[str, Any] = {
            "DeviceIndex": 0,
            "SubnetId": subnet_id,
            "AssociatePublicIpAddress": associate_public_ip,
        }
        if security_group_id:
            interface["Groups"] = [security_group_id]
        data["NetworkInterfaces"] = [interface]
    elif security_group_id:
        data["SecurityGroupIds"] = [security_group_id]

    if key_name:
        data["KeyName"] = key_name
    if iam_instance_profile_arn:
        data["IamInstanceProfile"] = {"Arn": iam_instance_profile_arn}
    if user_data is not None:
        data["UserData"] = encode_user_data(user_data)
    if monitoring:
        data["Monitoring"] = {"Enabled": True}

    return data


def create_launch_template(
    ec2_client: Any,
    name: str,
    launch_template_data: Dict[str, Any],
    tags: Optional[List[Dict[str, str]]] = None,
) -> Tuple[str, str]:
    """Create a launch template, reusing one that already exists under *name*.

    Idempotent because ``initialize()`` may run again after a partial failure,
    and a second ``CreateLaunchTemplate`` under the same name is rejected with
    ``InvalidLaunchTemplateName.AlreadyExistsException``. Rather than fail, the
    existing template is adopted -- its name encodes the provider ID, so it can
    only be one this provider made.

    Parameters
    ----------
    ec2_client : Any
        A boto3 EC2 client.
    name : str
        Launch template name; must be unique within the account and region.
    launch_template_data : Dict[str, Any]
        As returned by :func:`build_launch_template_data`.
    tags : Optional[List[Dict[str, str]]]
        Tags applied to the template resource itself, for cleanup tracking.

    Returns
    -------
    Tuple[str, str]
        The template ID and version number, the latter as the string
        ``RunInstances`` expects.

    Raises
    ------
    ResourceCreationError
        If the template can neither be created nor found.
    """
    kwargs: Dict[str, Any] = {
        "LaunchTemplateName": name,
        "LaunchTemplateData": launch_template_data,
    }
    if tags:
        kwargs["TagSpecifications"] = [
            {"ResourceType": RESOURCE_TYPE_LAUNCH_TEMPLATE, "Tags": tags}
        ]

    try:
        template = ec2_client.create_launch_template(**kwargs)["LaunchTemplate"]
        logger.debug(
            f"Created launch template {template['LaunchTemplateId']} ({name}) "
            f"version {template['LatestVersionNumber']}"
        )
        return str(template["LaunchTemplateId"]), str(template["LatestVersionNumber"])
    except ClientError as e:
        if (
            e.response["Error"]["Code"]
            != "InvalidLaunchTemplateName.AlreadyExistsException"
        ):
            raise ResourceCreationError(
                f"Failed to create launch template {name}: {e}"
            ) from e

    # Adopt the existing one. A new version is added rather than the old one
    # reused, so a changed AMI or instance type actually takes effect -- a
    # resumed provider whose config has moved on must not silently keep
    # launching the previous definition.
    try:
        version = ec2_client.create_launch_template_version(
            LaunchTemplateName=name,
            LaunchTemplateData=launch_template_data,
        )["LaunchTemplateVersion"]
        logger.debug(
            f"Launch template {name} already existed; added version "
            f"{version['VersionNumber']}"
        )
        return str(version["LaunchTemplateId"]), str(version["VersionNumber"])
    except ClientError as e:
        raise ResourceCreationError(
            f"Launch template {name} exists but could not be updated: {e}"
        ) from e


def delete_launch_template(ec2_client: Any, template_id: str) -> None:
    """Delete a launch template, tolerating one that is already gone.

    Deleting the template does not affect instances launched from it, so this is
    safe to call before those instances have terminated.

    Parameters
    ----------
    ec2_client : Any
        A boto3 EC2 client.
    template_id : str
        ID of the template to delete.
    """
    try:
        ec2_client.delete_launch_template(LaunchTemplateId=template_id)
        logger.debug(f"Deleted launch template {template_id}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "InvalidLaunchTemplateId.NotFound":
            logger.debug(f"Launch template {template_id} already deleted")
            return
        raise ResourceDeletionError(
            f"Failed to delete launch template {template_id}: {e}"
        ) from e


def get_default_ami(
    region: str,
    architecture: str = DEFAULT_ARCHITECTURE,
    session: Optional[boto3.Session] = None,
) -> str:
    """Resolve the latest Amazon Linux 2023 AMI for a region and architecture.

    Resolution order:

    1. AWS's public SSM Parameter Store alias
       ``/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-<arch>``,
       which AWS repoints at every new AL2023 release. This is the only source
       that stays correct without maintenance.
    2. ``DEFAULT_AMI_MAPPING``, retained purely so offline test runs against
       moto or substrate need no network. It is *not* a reliable source of live
       AMIs -- see the note in ``constants.py`` -- and is x86_64-only, so it is
       skipped for arm64 rather than returning an image that cannot boot.

    Parameters
    ----------
    region : str
        AWS region.
    architecture : str, optional
        ``"x86_64"`` (default) or ``"arm64"``. Use
        ``architecture_for_instance_type()`` to derive it from an instance type.
    session : boto3.Session, optional
        Session to query SSM with. A new one is created for ``region`` when
        omitted; pass an existing session to reuse its credentials and any
        custom endpoint.

    Returns
    -------
    str
        AMI ID.

    Raises
    ------
    AMINotFoundError
        If SSM cannot be reached and no usable fallback exists for the region
        and architecture.
    """
    if architecture not in (ARCHITECTURE_X86_64, ARCHITECTURE_ARM64):
        raise AMINotFoundError(
            f"Unsupported architecture {architecture!r}: expected "
            f"{ARCHITECTURE_X86_64!r} or {ARCHITECTURE_ARM64!r}"
        )

    parameter_name = AMI_SSM_PARAMETER_TEMPLATE.format(architecture=architecture)

    try:
        if session is None:
            session = boto3.Session(region_name=region)
        value = session.client("ssm", region_name=region).get_parameter(
            Name=parameter_name
        )["Parameter"]["Value"]
        logger.debug(
            f"Resolved {architecture} AL2023 AMI {value} for {region} from "
            f"{parameter_name}"
        )
        return str(value)
    except Exception as e:
        # Any failure here is recoverable if a fallback exists, so log at debug
        # and fall through; the raise below carries the real diagnosis.
        logger.debug(f"SSM AMI lookup failed for {region}/{architecture}: {e}")
        ssm_error: Exception = e

    # The fallback table is x86_64-only. Handing an x86_64 AMI to an arm64
    # instance produces an opaque launch failure, so refuse instead.
    if architecture == ARCHITECTURE_X86_64 and region in DEFAULT_AMI_MAPPING:
        fallback = DEFAULT_AMI_MAPPING[region]
        logger.warning(
            f"Could not resolve the current AL2023 AMI for {region} from SSM "
            f"({ssm_error}); falling back to the offline table entry "
            f"{fallback}. This AMI may be deprecated or deleted -- set "
            f"image_id explicitly if the launch fails."
        )
        return fallback

    message = (
        f"No AMI found for region {region} architecture {architecture}: SSM "
        f"lookup of {parameter_name} failed ({ssm_error})"
    )
    if architecture == ARCHITECTURE_ARM64:
        message += " and the offline fallback table has no arm64 entries"
    logger.error(message)
    raise AMINotFoundError(message)


def describe_instance_capacity(
    session: boto3.Session, instance_type: str
) -> Tuple[Optional[int], Optional[float]]:
    """Look up an instance type's vCPU count and memory in GB.

    Parsl's ``ExecutionProvider`` declares ``cores_per_node`` and
    ``mem_per_node`` so an executor can size its worker pool: HTEX divides them
    by its per-worker requirements to pick ``workers_per_node``, and falls back
    to a hardcoded guess of 1 when both are ``None``. EC2 already knows the real
    numbers, so there is no reason to make the caller supply them.

    Failure is not an error. This is an optimisation hint, and a provider that
    cannot reach EC2 during ``__init__`` should still construct — so any
    exception yields ``(None, None)``, which is exactly the base class's default.

    Parameters
    ----------
    session : boto3.Session
        Session used to call ``ec2:DescribeInstanceTypes``.
    instance_type : str
        Instance type to describe, e.g. ``"t3.micro"``.

    Returns
    -------
    Tuple[Optional[int], Optional[float]]
        ``(vcpus, memory_gb)``, or ``(None, None)`` if the lookup failed.
    """
    try:
        response = session.client("ec2").describe_instance_types(
            InstanceTypes=[instance_type]
        )
        info = response["InstanceTypes"][0]
        vcpus = info["VCpuInfo"]["DefaultVCpus"]
        # AWS reports memory in MiB; Parsl documents mem_per_node in GB.
        memory_gb = info["MemoryInfo"]["SizeInMiB"] / 1024
        logger.debug(
            f"Instance type {instance_type} has {vcpus} vCPUs and {memory_gb:.2f} GB memory"
        )
        return vcpus, memory_gb
    except Exception as e:
        logger.debug(
            f"Could not describe instance type {instance_type}: {e}. "
            "cores_per_node/mem_per_node stay unset."
        )
        return None, None


def wait_for_resource(
    resource_id: str,
    waiter_name: str,
    service_client: Any,
    waiter_config: Optional[Dict[str, Any]] = None,
    resource_name: str = "resource",
    delay: int = 5,
    max_attempts: int = 60,
) -> None:
    """Wait for a resource to reach the desired state.

    Parameters
    ----------
    resource_id : str
        Resource ID to wait for
    waiter_name : str
        Name of the waiter to use
    service_client : Any
        Boto3 service client
    waiter_config : Optional[Dict[str, Any]], optional
        Waiter configuration, by default None
    resource_name : str, optional
        Name of the resource for logging purposes, by default "resource"
    delay : int, optional
        Seconds between waiter attempts, by default 5
    max_attempts : int, optional
        Maximum number of waiter attempts, by default 60

    Raises
    ------
    ResourceCreationError
        If the resource fails to reach the desired state
    """
    try:
        logger.debug(f"Waiting for {resource_name} {resource_id} ({waiter_name})")
        waiter = service_client.get_waiter(waiter_name)

        config = {
            "WaiterConfig": {
                "Delay": delay,
                "MaxAttempts": max_attempts,
            }
        }

        if waiter_config:
            config["WaiterConfig"].update(waiter_config)

        if waiter_name in ["instance_running", "instance_status_ok"]:
            waiter.wait(InstanceIds=[resource_id], **config)
        elif waiter_name in ["vpc_available", "vpc_exists"]:
            waiter.wait(VpcIds=[resource_id], **config)
        elif waiter_name in ["subnet_available"]:
            waiter.wait(SubnetIds=[resource_id], **config)
        elif waiter_name in ["security_group_exists"]:
            waiter.wait(GroupIds=[resource_id], **config)
        elif waiter_name in ["function_active", "function_exists"]:
            waiter.wait(FunctionName=resource_id, **config)
        elif waiter_name in ["task_running", "task_stopped"]:
            waiter.wait(Tasks=[resource_id], **config)
        elif "stack" in waiter_name:
            waiter.wait(StackName=resource_id, **config)
        else:
            # Generic wait for resources without specific waiter support
            logger.debug(f"Using generic wait for {resource_name} {resource_id}")
            waiter.wait(Id=resource_id, **config)

        logger.debug(
            f"{resource_name.capitalize()} {resource_id} reached desired state"
        )

    except Exception as e:
        logger.error(f"Error waiting for {resource_name} {resource_id}: {e}")
        raise ResourceCreationError(
            f"Error waiting for {resource_name} {resource_id}: {e}"
        ) from e


def create_tags(
    resource_ids: Union[str, List[str]],
    tags: Dict[str, str],
    session: boto3.Session,
    region: Optional[str] = None,
) -> None:
    """Create tags for AWS resources.

    Parameters
    ----------
    resource_ids : Union[str, List[str]]
        Resource ID or list of resource IDs to tag
    tags : Dict[str, str]
        Tags to apply to the resources
    session : boto3.Session
        Boto3 session to use
    region : Optional[str], optional
        AWS region, by default None

    Raises
    ------
    ResourceCreationError
        If tagging fails
    """
    if not isinstance(resource_ids, list):
        resource_ids = [resource_ids]

    if not resource_ids:
        logger.debug("No resources to tag")
        return

    if not tags:
        logger.debug("No tags to apply")
        return

    # Convert tags dictionary to AWS Tags format
    aws_tags = [{"Key": key, "Value": value} for key, value in tags.items()]

    try:
        ec2 = session.client("ec2", region_name=region)
        ec2.create_tags(Resources=resource_ids, Tags=aws_tags)
        logger.debug(f"Created tags for resources {resource_ids}: {tags}")
    except Exception as e:
        logger.error(f"Failed to create tags for resources {resource_ids}: {e}")
        # Don't raise an exception here, as tagging failure should not abort the operation
        logger.warning("Continuing despite tag creation failure")


def get_resources_by_tags(
    tags: Dict[str, str],
    session: boto3.Session,
    region: Optional[str] = None,
    resource_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Get AWS resources by tags.

    Parameters
    ----------
    tags : Dict[str, str]
        Tags to filter resources by
    session : boto3.Session
        Boto3 session to use
    region : Optional[str], optional
        AWS region, by default None
    resource_type : Optional[str], optional
        Resource type to filter by, by default None

    Returns
    -------
    List[Dict[str, Any]]
        List of resources matching the tags

    Raises
    ------
    AWSConnectionError
        If connection to AWS services fails
    """
    # Convert tags dictionary to AWS filter format
    filters = [{"Name": f"tag:{key}", "Values": [value]} for key, value in tags.items()]

    if resource_type:
        filters.append({"Name": "resource-type", "Values": [resource_type]})

    try:
        ec2 = session.client("ec2", region_name=region)
        response = ec2.describe_tags(Filters=filters)

        # Get unique resource IDs
        resource_ids = list(set(tag["ResourceId"] for tag in response["Tags"]))

        # Get resource details
        resources = []

        if resource_ids:
            if not resource_type or resource_type == "instance":
                try:
                    instance_response = ec2.describe_instances(
                        InstanceIds=[
                            rid for rid in resource_ids if rid.startswith("i-")
                        ]
                    )
                    for reservation in instance_response.get("Reservations", []):
                        resources.extend(reservation.get("Instances", []))
                except ClientError:
                    # Some resource IDs might not be instances
                    pass

            if not resource_type or resource_type == "vpc":
                try:
                    vpc_response = ec2.describe_vpcs(
                        VpcIds=[rid for rid in resource_ids if rid.startswith("vpc-")]
                    )
                    resources.extend(vpc_response.get("Vpcs", []))
                except ClientError:
                    pass

            if not resource_type or resource_type == "subnet":
                try:
                    subnet_response = ec2.describe_subnets(
                        SubnetIds=[
                            rid for rid in resource_ids if rid.startswith("subnet-")
                        ]
                    )
                    resources.extend(subnet_response.get("Subnets", []))
                except ClientError:
                    pass

            if not resource_type or resource_type == "security-group":
                try:
                    sg_response = ec2.describe_security_groups(
                        GroupIds=[rid for rid in resource_ids if rid.startswith("sg-")]
                    )
                    resources.extend(sg_response.get("SecurityGroups", []))
                except ClientError:
                    pass

        return resources

    except Exception as e:
        logger.error(f"Failed to get resources by tags: {e}")
        raise AWSConnectionError(f"Failed to get resources by tags: {e}") from e


def delete_resource(
    resource_id: str,
    session: boto3.Session,
    resource_type: str,
    region: Optional[str] = None,
    force: bool = False,
) -> bool:
    """Delete an AWS resource.

    Parameters
    ----------
    resource_id : str
        Resource ID to delete
    session : boto3.Session
        Boto3 session to use
    resource_type : str
        Type of resource to delete
    region : Optional[str], optional
        AWS region, by default None
    force : bool, optional
        Whether to force deletion even if resource is in use, by default False

    Returns
    -------
    bool
        True if the resource was deleted, False otherwise

    Raises
    ------
    ResourceDeletionError
        If deletion fails
    ResourceNotFoundError
        If the resource is not found
    """
    try:
        if resource_type == "instance":
            ec2 = session.client("ec2", region_name=region)
            ec2.terminate_instances(InstanceIds=[resource_id])
            logger.debug(f"Terminated EC2 instance {resource_id}")
            return True

        elif resource_type == "vpc":
            ec2 = session.client("ec2", region_name=region)

            # Delete all resources within the VPC
            if force:
                # 1. NAT Gateways must be deleted before subnets can be removed
                nat_gws = ec2.describe_nat_gateways(
                    Filters=[
                        {"Name": "vpc-id", "Values": [resource_id]},
                        {
                            "Name": "state",
                            "Values": ["available", "pending", "deleting"],
                        },
                    ]
                ).get("NatGateways", [])
                allocation_ids = [
                    a["AllocationId"]
                    for ngw in nat_gws
                    for a in ngw.get("NatGatewayAddresses", [])
                    if a.get("AllocationId")
                ]
                deleted_nat_gw_ids = []
                for ngw in nat_gws:
                    try:
                        ec2.delete_nat_gateway(NatGatewayId=ngw["NatGatewayId"])
                        deleted_nat_gw_ids.append(ngw["NatGatewayId"])
                        logger.debug(f"Deleting NAT gateway {ngw['NatGatewayId']}")
                    except ClientError as e:
                        logger.warning(
                            f"Could not delete NAT gateway {ngw['NatGatewayId']}: {e}"
                        )
                # Poll until all NAT gateways have finished deleting (max ~2 min).
                # Only enter the loop if we actually submitted deletion requests.
                if deleted_nat_gw_ids:
                    for _ in range(24):
                        still_deleting = ec2.describe_nat_gateways(
                            Filters=[
                                {"Name": "vpc-id", "Values": [resource_id]},
                                {"Name": "state", "Values": ["deleting"]},
                            ]
                        ).get("NatGateways", [])
                        if not still_deleting:
                            break
                        time.sleep(5)

                # 2. Release EIPs that backed the deleted NAT gateways
                for alloc_id in allocation_ids:
                    try:
                        ec2.release_address(AllocationId=alloc_id)
                        logger.debug(f"Released EIP {alloc_id}")
                    except ClientError as e:
                        logger.warning(f"Could not release EIP {alloc_id}: {e}")

                # 3. Delete detached ENIs (e.g. leftover Lambda/ECS interfaces)
                for eni in ec2.describe_network_interfaces(
                    Filters=[{"Name": "vpc-id", "Values": [resource_id]}]
                ).get("NetworkInterfaces", []):
                    if eni.get("Status") == "available":
                        try:
                            ec2.delete_network_interface(
                                NetworkInterfaceId=eni["NetworkInterfaceId"]
                            )
                            logger.debug(f"Deleted ENI {eni['NetworkInterfaceId']}")
                        except ClientError as e:
                            logger.warning(
                                f"Could not delete ENI {eni['NetworkInterfaceId']}: {e}"
                            )

                # Get all subnets in the VPC
                subnets = ec2.describe_subnets(
                    Filters=[{"Name": "vpc-id", "Values": [resource_id]}]
                )
                for subnet in subnets.get("Subnets", []):
                    delete_resource(
                        subnet["SubnetId"], session, "subnet", region, force
                    )

                # Get all security groups in the VPC
                security_groups = ec2.describe_security_groups(
                    Filters=[{"Name": "vpc-id", "Values": [resource_id]}]
                )
                for sg in security_groups.get("SecurityGroups", []):
                    if sg["GroupName"] != "default":  # Can't delete default SG
                        delete_resource(
                            sg["GroupId"], session, "security-group", region, force
                        )

                # Get internet gateways attached to the VPC
                igws = ec2.describe_internet_gateways(
                    Filters=[{"Name": "attachment.vpc-id", "Values": [resource_id]}]
                )
                for igw in igws.get("InternetGateways", []):
                    ec2.detach_internet_gateway(
                        InternetGatewayId=igw["InternetGatewayId"], VpcId=resource_id
                    )
                    delete_resource(
                        igw["InternetGatewayId"],
                        session,
                        "internet-gateway",
                        region,
                        force,
                    )

                # Delete non-main route tables (main RT is deleted with the VPC)
                route_tables = ec2.describe_route_tables(
                    Filters=[{"Name": "vpc-id", "Values": [resource_id]}]
                )
                for rt in route_tables.get("RouteTables", []):
                    is_main = any(
                        assoc.get("Main") for assoc in rt.get("Associations", [])
                    )
                    if is_main:
                        continue
                    # Disassociate all explicit associations first
                    for assoc in rt.get("Associations", []):
                        assoc_id = assoc.get("RouteTableAssociationId")
                        if assoc_id:
                            try:
                                ec2.disassociate_route_table(AssociationId=assoc_id)
                            except ClientError:
                                pass
                    try:
                        ec2.delete_route_table(RouteTableId=rt["RouteTableId"])
                        logger.debug(f"Deleted route table {rt['RouteTableId']}")
                    except ClientError as e:
                        logger.warning(
                            f"Could not delete route table {rt['RouteTableId']}: {e}"
                        )

            # Delete the VPC
            ec2.delete_vpc(VpcId=resource_id)
            logger.debug(f"Deleted VPC {resource_id}")
            return True

        elif resource_type == "subnet":
            ec2 = session.client("ec2", region_name=region)
            ec2.delete_subnet(SubnetId=resource_id)
            logger.debug(f"Deleted subnet {resource_id}")
            return True

        elif resource_type == "security-group":
            ec2 = session.client("ec2", region_name=region)
            ec2.delete_security_group(GroupId=resource_id)
            logger.debug(f"Deleted security group {resource_id}")
            return True

        elif resource_type == "internet-gateway":
            ec2 = session.client("ec2", region_name=region)
            ec2.delete_internet_gateway(InternetGatewayId=resource_id)
            logger.debug(f"Deleted internet gateway {resource_id}")
            return True

        elif resource_type == "function":
            lambda_client = session.client("lambda", region_name=region)
            lambda_client.delete_function(FunctionName=resource_id)
            logger.debug(f"Deleted Lambda function {resource_id}")
            return True

        elif resource_type == "task":
            ecs = session.client("ecs", region_name=region)
            ecs.stop_task(task=resource_id, cluster="default")
            logger.debug(f"Stopped ECS task {resource_id}")
            return True

        elif resource_type == "cloudformation-stack":
            cfn = session.client("cloudformation", region_name=region)
            cfn.delete_stack(StackName=resource_id)
            # Wait for stack deletion
            logger.debug(f"Initiated deletion of CloudFormation stack {resource_id}")
            logger.debug(f"Waiting for stack {resource_id} to be deleted...")
            waiter = cfn.get_waiter("stack_delete_complete")
            waiter.wait(
                StackName=resource_id, WaiterConfig={"Delay": 10, "MaxAttempts": 30}
            )
            logger.debug(f"Deleted CloudFormation stack {resource_id}")
            return True

        else:
            logger.warning(f"Unsupported resource type: {resource_type}")
            return False

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")

        if any(
            code in error_code
            for code in [
                "NotFound",
                "InvalidSubnetID.NotFound",
                "InvalidVpcID.NotFound",
                "InvalidGroup.NotFound",
                "InvalidInternetGatewayID.NotFound",
                "ResourceNotFoundException",
            ]
        ):
            logger.debug(f"Resource {resource_id} not found or already deleted")
            raise ResourceNotFoundError(
                f"Resource {resource_id} not found or already deleted"
            ) from e
        else:
            logger.error(f"Failed to delete {resource_type} {resource_id}: {e}")
            raise ResourceDeletionError(
                f"Failed to delete {resource_type} {resource_id}: {e}"
            ) from e

    except Exception as e:
        logger.error(f"Failed to delete {resource_type} {resource_id}: {e}")
        raise ResourceDeletionError(
            f"Failed to delete {resource_type} {resource_id}: {e}"
        ) from e


def get_cf_template(template_name: str) -> str:
    """
    Load a CloudFormation template from the templates directory.

    Uses ``importlib.resources`` so the template is found both in an installed
    wheel and in an editable/source checkout. The previous implementation called
    ``pkg_resources.resource_string`` with the import placed *outside* its own
    ``try`` — so once setuptools 81 removed ``pkg_resources`` the
    ``except ModuleNotFoundError`` fallback became unreachable and every call
    raised, taking down `DetachedMode.initialize()` with it. It also fell back to
    a placeholder template declaring no ``Outputs``, which the bastion path then
    indexed for ``BastionHostId`` — a confusing failure several steps removed
    from the missing file (#112).

    Parameters
    ----------
    template_name : str
        Name of the template file (e.g., 'bastion.yml')

    Returns
    -------
    str
        CloudFormation template content

    Raises
    ------
    FileNotFoundError
        If the template is not found in the package or on the filesystem
    """
    from importlib.resources import files

    try:
        resource = files("parsl_ephemeral_aws").joinpath(
            "templates", "cloudformation", template_name
        )
        return resource.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        # Fall back to the filesystem, for layouts importlib cannot address.
        current_dir = os.path.dirname(os.path.abspath(__file__))
        template_path = os.path.join(
            current_dir, "..", "templates", "cloudformation", template_name
        )

        if os.path.exists(template_path):
            with open(template_path, "r") as f:
                return f.read()

        raise FileNotFoundError(
            f"CloudFormation template {template_name!r} not found in "
            "parsl_ephemeral_aws/templates/cloudformation. If this is an "
            "installed package, the templates were not included in the "
            "distribution."
        )


def get_or_create_iam_role(
    iam_client: Any,
    role_name: str,
    assume_role_policy: Dict[str, Any],
    policy_arns: List[str],
    tags: Optional[List[Dict[str, str]]] = None,
    description: str = "",
) -> str:
    """Get or create an IAM role idempotently.

    Checks whether the role already exists and returns its ARN without
    modifying it.  If the role does not exist, creates it, attaches the
    supplied managed-policy ARNs, and returns the new ARN.

    Parameters
    ----------
    iam_client : Any
        A boto3 IAM client.
    role_name : str
        Name of the IAM role.
    assume_role_policy : Dict[str, Any]
        Trust-relationship policy document (Python dict, not JSON string).
    policy_arns : List[str]
        Managed policy ARNs to attach.
    tags : Optional[List[Dict[str, str]]], optional
        Tags to apply when creating the role.
    description : str, optional
        Role description.

    Returns
    -------
    str
        ARN of the existing or newly created role.

    Raises
    ------
    ResourceCreationError
        If role creation or retrieval fails.
    """
    try:
        response = iam_client.get_role(RoleName=role_name)
        logger.debug(f"Reusing existing IAM role: {role_name}")
        return response["Role"]["Arn"]
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("NoSuchEntity", "NoSuchEntityException"):
            raise ResourceCreationError(
                f"Failed to check IAM role {role_name}: {e}"
            ) from e

    # Role does not exist — create it
    try:
        create_kwargs: Dict[str, Any] = {
            "RoleName": role_name,
            "AssumeRolePolicyDocument": json.dumps(assume_role_policy),
            "Description": description,
        }
        if tags:
            create_kwargs["Tags"] = tags

        response = iam_client.create_role(**create_kwargs)
        role_arn: str = response["Role"]["Arn"]

        for policy_arn in policy_arns:
            iam_client.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)

        logger.info(f"Created IAM role: {role_name}")
        return role_arn

    except ClientError as e:
        if e.response["Error"]["Code"] in ("EntityAlreadyExists",):
            # Race condition — another process created it; fetch ARN
            try:
                response = iam_client.get_role(RoleName=role_name)
                logger.debug(f"IAM role created by concurrent caller: {role_name}")
                return response["Role"]["Arn"]
            except Exception as inner_e:
                raise ResourceCreationError(
                    f"Failed to retrieve IAM role {role_name} after concurrent creation: {inner_e}"
                ) from inner_e
        raise ResourceCreationError(
            f"Failed to create IAM role {role_name}: {e}"
        ) from e


def get_or_create_ssm_instance_profile(
    session: boto3.Session,
    name_suffix: str,
    iam_instance_profile_arn: Optional[str] = None,
    auto_create: bool = False,
) -> Optional[str]:
    """Resolve an IAM instance profile ARN granting SSM access, or None.

    SSM ``SendCommand`` — used by warm-pool and one-shot dispatch — needs the
    instance to carry a profile with the ``AmazonSSMManagedInstanceCore`` policy.
    Without it the SSM agent never registers and every command dispatch times out.

    Resolution order:

    1. ``iam_instance_profile_arn`` if supplied → used directly.
    2. ``auto_create`` → get-or-create a profile holding
       ``AmazonSSMManagedInstanceCore``.
    3. Otherwise → ``None``; the caller launches instances without a profile.

    Both the create and the attach steps are idempotent, so concurrent callers
    sharing a ``name_suffix`` converge on the same profile.

    Parameters
    ----------
    session : boto3.Session
        AWS session used to build the IAM client.
    name_suffix : str
        Discriminator appended to the role and profile names — normally the
        provider ID, so resources are traceable back to their provider.
    iam_instance_profile_arn : Optional[str], optional
        Pre-existing profile ARN to use verbatim, by default None.
    auto_create : bool, optional
        Whether to create a profile when none was supplied, by default False.

    Returns
    -------
    Optional[str]
        ARN of the resolved instance profile, or None when neither an explicit
        ARN was given nor auto-creation requested.

    Raises
    ------
    ResourceCreationError
        If the profile cannot be created or retrieved.
    """
    if iam_instance_profile_arn:
        return iam_instance_profile_arn

    if not auto_create:
        return None

    iam = session.client("iam")
    role_name = f"parsl-ephemeral-ssm-role-{name_suffix}"
    profile_name = f"parsl-ephemeral-ssm-profile-{name_suffix}"

    get_or_create_iam_role(
        iam_client=iam,
        role_name=role_name,
        assume_role_policy={
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "ec2.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        },
        policy_arns=["arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"],
        description=f"SSM instance role for Parsl ({name_suffix})",
    )

    # Ensure the instance profile exists and has the role attached
    try:
        response = iam.get_instance_profile(InstanceProfileName=profile_name)
        existing_arn: str = response["InstanceProfile"]["Arn"]
        _attach_role_to_instance_profile(iam, profile_name, role_name)
        return existing_arn
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("NoSuchEntity", "NoSuchEntityException"):
            raise ResourceCreationError(
                f"Failed to check instance profile {profile_name}: {e}"
            ) from e

    try:
        response = iam.create_instance_profile(InstanceProfileName=profile_name)
        arn: str = response["InstanceProfile"]["Arn"]
        _attach_role_to_instance_profile(iam, profile_name, role_name)
        logger.info(f"Created IAM instance profile: {profile_name}")
        _wait_for_instance_profile(session, arn)
        return arn
    except ClientError as e:
        if e.response["Error"]["Code"] == "EntityAlreadyExists":
            # Race condition — another caller created it; fetch the ARN
            response = iam.get_instance_profile(InstanceProfileName=profile_name)
            raced_arn: str = response["InstanceProfile"]["Arn"]
            _attach_role_to_instance_profile(iam, profile_name, role_name)
            _wait_for_instance_profile(session, raced_arn)
            return raced_arn
        raise ResourceCreationError(
            f"Failed to create instance profile {profile_name}: {e}"
        ) from e


#: How long to wait for a new instance profile to become visible to EC2.
_PROFILE_PROPAGATION_TIMEOUT_S = 60
_PROFILE_PROPAGATION_DELAY_S = 2


def _wait_for_instance_profile(
    session: boto3.Session,
    profile_arn: str,
    timeout: int = _PROFILE_PROPAGATION_TIMEOUT_S,
) -> None:
    """Block until EC2 will accept *profile_arn*, or give up after *timeout*.

    IAM is eventually consistent with respect to EC2: a profile that
    ``create_instance_profile`` has already returned an ARN for is rejected by
    ``RunInstances`` with ``InvalidParameterValue: Invalid IAM Instance Profile
    ARN`` for the first several seconds (measured at ~10s). A dry-run
    ``RunInstances`` is the only check that exercises the path that matters —
    ``get_instance_profile`` succeeds immediately and so proves nothing.

    A timeout is logged rather than raised: the launch that follows will report
    the real error, and failing here would turn a slow propagation into a hard
    error on a profile that is about to work.
    """
    ec2 = session.client("ec2")
    # t3.micro below is x86_64, so the default architecture is the right one.
    image_id = get_default_ami(session.region_name or DEFAULT_REGION, session=session)
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            ec2.run_instances(
                ImageId=image_id,
                MinCount=1,
                MaxCount=1,
                InstanceType="t3.micro",
                IamInstanceProfile={"Arn": profile_arn},
                DryRun=True,
            )
            return
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "DryRunOperation":
                # EC2 validated everything including the profile.
                logger.debug(f"Instance profile {profile_arn} is visible to EC2")
                return
            if code != "InvalidParameterValue":
                # Anything else (an unusable AMI here, say) is not what this
                # check is for; let the real launch report it.
                logger.debug(f"Instance profile propagation check skipped: {e}")
                return
            time.sleep(_PROFILE_PROPAGATION_DELAY_S)

    logger.warning(
        f"Instance profile {profile_arn} still not visible to EC2 after "
        f"{timeout}s; launching anyway"
    )


def _attach_role_to_instance_profile(
    iam_client: Any, profile_name: str, role_name: str
) -> None:
    """Attach a role to an instance profile, tolerating an existing attachment.

    A profile holds at most one role, so re-attaching the same role is a no-op
    that AWS reports as ``LimitExceeded``. Any other role already present is
    left alone: the caller supplied the profile name, so its contents are
    theirs to manage.
    """
    try:
        iam_client.add_role_to_instance_profile(
            InstanceProfileName=profile_name, RoleName=role_name
        )
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("LimitExceeded", "LimitExceededException"):
            logger.debug(f"Instance profile {profile_name} already holds a role")
            return
        raise ResourceCreationError(
            f"Failed to attach role {role_name} to profile {profile_name}: {e}"
        ) from e

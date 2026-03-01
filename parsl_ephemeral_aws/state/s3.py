"""S3 state implementation for Parsl Ephemeral AWS Provider.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import json
import logging
from typing import Dict, Any, Optional

import boto3
from botocore.exceptions import ClientError

from ..exceptions import StateError
from .base import StateStore


logger = logging.getLogger(__name__)


class S3State(StateStore):
    """AWS S3 implementation of state persistence."""

    def __init__(
        self,
        provider: Any,
        bucket_name: str,
        key_prefix: str = "parsl/workflows",
        create_bucket_if_not_exists: bool = False,
    ) -> None:
        """Initialize S3 state.

        Parameters
        ----------
        provider : EphemeralAWSProvider
            The provider instance
        bucket_name : str
            Name of the S3 bucket to use
        key_prefix : str, optional
            Prefix for S3 keys, by default 'parsl/workflows'
        create_bucket_if_not_exists : bool, optional
            Whether to create the bucket if it doesn't exist, by default False
        """
        self.provider = provider
        self.bucket_name = bucket_name
        self.key_prefix = key_prefix.rstrip("/")
        self.create_bucket_if_not_exists = create_bucket_if_not_exists

        # Initialize AWS session
        session_kwargs = {}
        if self.provider.aws_access_key_id and self.provider.aws_secret_access_key:
            session_kwargs["aws_access_key_id"] = self.provider.aws_access_key_id
            session_kwargs[
                "aws_secret_access_key"
            ] = self.provider.aws_secret_access_key

        if self.provider.aws_session_token:
            session_kwargs["aws_session_token"] = self.provider.aws_session_token

        if self.provider.aws_profile:
            session_kwargs["profile_name"] = self.provider.aws_profile

        self.aws_session = boto3.Session(
            region_name=self.provider.region, **session_kwargs
        )

        # Initialize clients
        self.s3_client = self.aws_session.client("s3")
        self.s3_resource = self.aws_session.resource("s3")

        # Ensure bucket exists if requested
        if self.create_bucket_if_not_exists:
            self._ensure_bucket_exists()

    def _ensure_bucket_exists(self) -> None:
        """Ensure S3 bucket exists, creating it if it doesn't."""
        try:
            # Check if bucket exists
            self.s3_client.head_bucket(Bucket=self.bucket_name)
            logger.debug(f"S3 bucket exists: {self.bucket_name}")
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "404":
                # Bucket doesn't exist, create it
                try:
                    # Create bucket in the current region
                    if self.provider.region == "us-east-1":
                        # us-east-1 requires special handling (no LocationConstraint)
                        self.s3_client.create_bucket(Bucket=self.bucket_name)
                    else:
                        self.s3_client.create_bucket(
                            Bucket=self.bucket_name,
                            CreateBucketConfiguration={
                                "LocationConstraint": self.provider.region
                            },
                        )

                    # Block all public access (replaces deprecated ACL="private")
                    self.s3_client.put_public_access_block(
                        Bucket=self.bucket_name,
                        PublicAccessBlockConfiguration={
                            "BlockPublicAcls": True,
                            "IgnorePublicAcls": True,
                            "BlockPublicPolicy": True,
                            "RestrictPublicBuckets": True,
                        },
                    )

                    # Add tags to the bucket
                    self.s3_client.put_bucket_tagging(
                        Bucket=self.bucket_name,
                        Tagging={
                            "TagSet": [
                                {"Key": "ParslManagedBucket", "Value": "true"},
                                {
                                    "Key": "ParslWorkflowId",
                                    "Value": self.provider.workflow_id,
                                },
                            ]
                        },
                    )

                    logger.info(f"Created S3 bucket: {self.bucket_name}")

                except Exception as create_e:
                    logger.error(f"Error creating S3 bucket: {create_e}")
                    raise StateError(f"Failed to create S3 bucket: {create_e}")
            else:
                # Other error
                logger.error(f"Error checking S3 bucket: {e}")
                raise StateError(f"Failed to check S3 bucket: {e}")

    def _get_object_key(self, state_key: str) -> str:
        """Get the full S3 object key.

        Parameters
        ----------
        state_key : str
            State key

        Returns
        -------
        str
            Full S3 object key
        """
        # Ensure state_key doesn't begin with a slash
        state_key = state_key.lstrip("/")
        return f"{self.key_prefix}/{state_key}"

    def save_state(self, state_key: str, state_data: Dict[str, Any]) -> None:
        """Save provider state in S3.

        Parameters
        ----------
        state_key : str
            Key to store the state under
        state_data : Dict[str, Any]
            State data to save
        """
        try:
            object_key = self._get_object_key(state_key)

            # Convert state data to JSON
            state_json = json.dumps(state_data)

            # Upload to S3
            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=object_key,
                Body=state_json,
                ContentType="application/json",
                Metadata={"ParslWorkflowId": self.provider.workflow_id},
            )

            logger.debug(f"Saved state to S3: {object_key}")

        except Exception as e:
            logger.error(f"Error saving state to S3: {e}")
            raise StateError(f"Failed to save state: {e}")

    def load_state(self, state_key: str) -> Optional[Dict[str, Any]]:
        """Load provider state from S3.

        Parameters
        ----------
        state_key : str
            Key to load the state from

        Returns
        -------
        Optional[Dict[str, Any]]
            Loaded state data, or None if not found
        """
        try:
            object_key = self._get_object_key(state_key)

            try:
                # Get object from S3
                response = self.s3_client.get_object(
                    Bucket=self.bucket_name, Key=object_key
                )

                # Read and parse JSON
                state_json = response["Body"].read().decode("utf-8")
                state_data = json.loads(state_json)

                logger.debug(f"Loaded state from S3: {object_key}")

                return state_data

            except ClientError as e:
                if e.response["Error"]["Code"] == "NoSuchKey":
                    logger.debug(f"State not found in S3: {object_key}")
                    return None
                raise

        except Exception as e:
            logger.error(f"Error loading state from S3: {e}")
            raise StateError(f"Failed to load state: {e}")

    def delete_state(self, state_key: str) -> None:
        """Delete provider state from S3.

        Parameters
        ----------
        state_key : str
            Key to delete the state for
        """
        try:
            object_key = self._get_object_key(state_key)

            try:
                # Delete object from S3
                self.s3_client.delete_object(Bucket=self.bucket_name, Key=object_key)

                logger.debug(f"Deleted state from S3: {object_key}")

            except ClientError as e:
                if e.response["Error"]["Code"] == "NoSuchKey":
                    logger.debug(f"State not found in S3: {object_key}")
                    return
                raise

        except Exception as e:
            logger.error(f"Error deleting state from S3: {e}")
            raise StateError(f"Failed to delete state: {e}")

    def list_states(self, prefix: str) -> Dict[str, Dict[str, Any]]:
        """List all states with a given prefix.

        Parameters
        ----------
        prefix : str
            Prefix to list states for

        Returns
        -------
        Dict[str, Dict[str, Any]]
            Dictionary mapping state keys to state data
        """
        try:
            object_prefix = self._get_object_key(prefix)

            # List objects in S3
            paginator = self.s3_client.get_paginator("list_objects_v2")
            page_iterator = paginator.paginate(
                Bucket=self.bucket_name, Prefix=object_prefix
            )

            # Collect objects
            states = {}
            for page in page_iterator:
                for obj in page.get("Contents", []):
                    # Extract the state key from the object key
                    object_key = obj["Key"]
                    state_key = object_key[
                        len(self.key_prefix) + 1 :
                    ]  # +1 for the slash

                    # Get and parse the object
                    try:
                        response = self.s3_client.get_object(
                            Bucket=self.bucket_name, Key=object_key
                        )

                        state_json = response["Body"].read().decode("utf-8")
                        state_data = json.loads(state_json)

                        states[state_key] = state_data

                    except Exception as e:
                        logger.warning(
                            f"Failed to load state data for {state_key}: {e}"
                        )

            return states

        except Exception as e:
            logger.error(f"Error listing states from S3: {e}")
            raise StateError(f"Failed to list states: {e}")

    def cleanup_workflow_states(self) -> None:
        """Clean up all states for the current workflow."""
        try:
            # List objects with the workflow prefix
            workflow_prefix = f"{self.key_prefix}/{self.provider.workflow_id}"

            objects_to_delete = []

            paginator = self.s3_client.get_paginator("list_objects_v2")
            page_iterator = paginator.paginate(
                Bucket=self.bucket_name, Prefix=workflow_prefix
            )

            for page in page_iterator:
                for obj in page.get("Contents", []):
                    objects_to_delete.append({"Key": obj["Key"]})

            # Delete objects in batches (S3 has a limit of 1000 objects per delete operation)
            batch_size = 1000
            for i in range(0, len(objects_to_delete), batch_size):
                batch = objects_to_delete[i : i + batch_size]
                if batch:
                    self.s3_client.delete_objects(
                        Bucket=self.bucket_name,
                        Delete={"Objects": batch, "Quiet": True},
                    )
                    logger.debug(f"Deleted {len(batch)} objects from S3")

            logger.info(f"Cleaned up {len(objects_to_delete)} workflow states from S3")

        except Exception as e:
            logger.error(f"Error cleaning up workflow states from S3: {e}")
            raise StateError(f"Failed to clean up workflow states: {e}")

    def delete_bucket_if_empty(self) -> bool:
        """Delete the S3 bucket if it's empty.

        Returns
        -------
        bool
            Whether the bucket was deleted
        """
        try:
            # Check if bucket is empty
            response = self.s3_client.list_objects_v2(
                Bucket=self.bucket_name, MaxKeys=1
            )

            if response.get("KeyCount", 0) > 0:
                logger.debug(
                    f"S3 bucket {self.bucket_name} is not empty, skipping deletion"
                )
                return False

            # Delete the bucket
            self.s3_client.delete_bucket(Bucket=self.bucket_name)
            logger.info(f"Deleted empty S3 bucket: {self.bucket_name}")

            return True

        except Exception as e:
            logger.error(f"Error deleting S3 bucket: {e}")
            return False


# Alias for backwards compatibility
S3StateStore = S3State

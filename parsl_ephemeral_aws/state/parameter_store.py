"""Parameter Store state implementation for Parsl Ephemeral AWS Provider.

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


class ParameterStoreState(StateStore):
    """AWS Parameter Store implementation of state persistence."""

    def __init__(
        self,
        provider: Any,
        prefix: str = "/parsl/workflows",
        use_secure_string: bool = False,
    ) -> None:
        """Initialize Parameter Store state.

        Parameters
        ----------
        provider : EphemeralAWSProvider
            The provider instance
        prefix : str, optional
            Prefix for parameter names, by default '/parsl/workflows'
        use_secure_string : bool, optional
            Whether to use SecureString parameter type, by default False
        """
        self.provider = provider
        self.prefix = prefix.rstrip("/")
        self.use_secure_string = use_secure_string

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
        self.ssm_client = self.aws_session.client("ssm")

    def _get_parameter_name(self, state_key: str) -> str:
        """Get the full parameter name.

        Parameters
        ----------
        state_key : str
            State key

        Returns
        -------
        str
            Full parameter name
        """
        # Ensure state_key doesn't begin with a slash if it's the first character
        state_key = state_key.lstrip("/")
        return f"{self.prefix}/{state_key}"

    def save_state(self, state_key: str, state_data: Dict[str, Any]) -> None:
        """Save provider state in Parameter Store.

        Parameters
        ----------
        state_key : str
            Key to store the state under
        state_data : Dict[str, Any]
            State data to save
        """
        try:
            parameter_name = self._get_parameter_name(state_key)
            parameter_type = "SecureString" if self.use_secure_string else "String"

            # Convert state data to JSON
            state_json = json.dumps(state_data)

            # Check if parameter already exists
            try:
                self.ssm_client.get_parameter(Name=parameter_name)
                # Parameter exists, update it
                self.ssm_client.put_parameter(
                    Name=parameter_name,
                    Value=state_json,
                    Type=parameter_type,
                    Overwrite=True,
                )
            except ClientError as e:
                if e.response["Error"]["Code"] == "ParameterNotFound":
                    # Parameter doesn't exist, create it
                    self.ssm_client.put_parameter(
                        Name=parameter_name,
                        Value=state_json,
                        Type=parameter_type,
                        Tags=[
                            {
                                "Key": "ParslWorkflowId",
                                "Value": self.provider.workflow_id,
                            }
                        ],
                    )
                else:
                    raise

            logger.debug(f"Saved state to Parameter Store: {parameter_name}")

        except Exception as e:
            logger.error(f"Error saving state to Parameter Store: {e}")
            raise StateError(f"Failed to save state: {e}")

    def load_state(self, state_key: str) -> Optional[Dict[str, Any]]:
        """Load provider state from Parameter Store.

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
            parameter_name = self._get_parameter_name(state_key)

            try:
                response = self.ssm_client.get_parameter(
                    Name=parameter_name, WithDecryption=True
                )

                # Parse JSON state data
                state_json = response["Parameter"]["Value"]
                state_data = json.loads(state_json)

                logger.debug(f"Loaded state from Parameter Store: {parameter_name}")

                return state_data

            except ClientError as e:
                if e.response["Error"]["Code"] == "ParameterNotFound":
                    logger.debug(
                        f"State not found in Parameter Store: {parameter_name}"
                    )
                    return None
                raise

        except Exception as e:
            logger.error(f"Error loading state from Parameter Store: {e}")
            raise StateError(f"Failed to load state: {e}")

    def delete_state(self, state_key: str) -> None:
        """Delete provider state from Parameter Store.

        Parameters
        ----------
        state_key : str
            Key to delete the state for
        """
        try:
            parameter_name = self._get_parameter_name(state_key)

            try:
                self.ssm_client.delete_parameter(Name=parameter_name)
                logger.debug(f"Deleted state from Parameter Store: {parameter_name}")
            except ClientError as e:
                if e.response["Error"]["Code"] == "ParameterNotFound":
                    logger.debug(
                        f"State not found in Parameter Store: {parameter_name}"
                    )
                    return
                raise

        except Exception as e:
            logger.error(f"Error deleting state from Parameter Store: {e}")
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
            parameter_path = self._get_parameter_name(prefix)

            # Get all parameters with the given path
            paginator = self.ssm_client.get_paginator("get_parameters_by_path")
            page_iterator = paginator.paginate(
                Path=parameter_path, Recursive=True, WithDecryption=True
            )

            # Collect parameters
            states = {}
            for page in page_iterator:
                for parameter in page["Parameters"]:
                    # Extract the state key from the parameter name
                    full_name = parameter["Name"]
                    state_key = full_name[len(self.prefix) + 1 :]  # +1 for the slash

                    # Parse the state data
                    try:
                        state_data = json.loads(parameter["Value"])
                        states[state_key] = state_data
                    except json.JSONDecodeError as e:
                        logger.warning(
                            f"Failed to parse state data for {state_key}: {e}"
                        )

            return states

        except Exception as e:
            logger.error(f"Error listing states from Parameter Store: {e}")
            raise StateError(f"Failed to list states: {e}")

    def cleanup_workflow_states(self) -> None:
        """Clean up all states for the current workflow."""
        try:
            # Get all parameters with workflow ID tag
            paginator = self.ssm_client.get_paginator("describe_parameters")
            page_iterator = paginator.paginate(
                ParameterFilters=[
                    {
                        "Key": "tag:ParslWorkflowId",
                        "Values": [self.provider.workflow_id],
                    }
                ]
            )

            parameters_to_delete = []
            for page in page_iterator:
                for parameter in page["Parameters"]:
                    parameters_to_delete.append(parameter["Name"])

            # Delete parameters in batches (SSM has a limit of 10 parameters per delete operation)
            batch_size = 10
            for i in range(0, len(parameters_to_delete), batch_size):
                batch = parameters_to_delete[i : i + batch_size]
                if batch:
                    self.ssm_client.delete_parameters(Names=batch)
                    logger.debug(
                        f"Deleted {len(batch)} parameters from Parameter Store"
                    )

            logger.info(
                f"Cleaned up {len(parameters_to_delete)} workflow states from Parameter Store"
            )

        except Exception as e:
            logger.error(f"Error cleaning up workflow states from Parameter Store: {e}")
            raise StateError(f"Failed to clean up workflow states: {e}")


# Alias for backwards compatibility
ParameterStoreStateStore = ParameterStoreState

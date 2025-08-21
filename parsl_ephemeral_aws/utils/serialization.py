"""Serialization utilities for Parsl Ephemeral AWS Provider.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import json
import logging
import uuid
import datetime
from typing import Dict, Any

from ..exceptions import StateError


logger = logging.getLogger(__name__)


class ParslStateEncoder(json.JSONEncoder):
    """JSON encoder for Parsl provider state.

    This encoder handles special types like UUIDs and datetimes.
    """

    def default(self, obj: Any) -> Any:
        """Encode special objects to JSON-compatible types.

        Parameters
        ----------
        obj : Any
            Object to encode

        Returns
        -------
        Any
            JSON-compatible representation
        """
        if isinstance(obj, uuid.UUID):
            return {"__uuid__": str(obj)}
        elif isinstance(obj, datetime.datetime):
            return {"__datetime__": obj.isoformat()}
        elif isinstance(obj, set):
            return {"__set__": list(obj)}
        elif hasattr(obj, "__dict__"):
            # For objects with __dict__, serialize the dict with class info
            return {
                "__object__": obj.__class__.__name__,
                "__module__": obj.__class__.__module__,
                "__state__": obj.__dict__,
            }
        return super().default(obj)


def object_hook(obj: Dict[str, Any]) -> Any:
    """Custom object hook for JSON deserialization.

    Parameters
    ----------
    obj : Dict[str, Any]
        Dictionary to deserialize

    Returns
    -------
    Any
        Deserialized object
    """
    if "__uuid__" in obj:
        return uuid.UUID(obj["__uuid__"])
    elif "__datetime__" in obj:
        return datetime.datetime.fromisoformat(obj["__datetime__"])
    elif "__set__" in obj:
        return set(obj["__set__"])
    elif "__object__" in obj and "__module__" in obj and "__state__" in obj:
        # For serialized objects, attempt to reconstruct them
        # For safety, we don't try to actually import and instantiate the classes
        # Instead, return a dict with class information
        return {
            "class": obj["__object__"],
            "module": obj["__module__"],
            "state": obj["__state__"],
        }
    return obj


def serialize_state(state: Dict[str, Any]) -> str:
    """Serialize state to JSON string.

    Parameters
    ----------
    state : Dict[str, Any]
        State dictionary

    Returns
    -------
    str
        JSON-serialized state
    """
    try:
        return json.dumps(state, cls=ParslStateEncoder)
    except Exception as e:
        logger.error(f"Error serializing state: {e}")
        raise StateError(f"Failed to serialize state: {e}")


def deserialize_state(state_json: str) -> Dict[str, Any]:
    """Deserialize state from JSON string.

    Parameters
    ----------
    state_json : str
        JSON-serialized state

    Returns
    -------
    Dict[str, Any]
        Deserialized state dictionary
    """
    try:
        return json.loads(state_json, object_hook=object_hook)
    except Exception as e:
        logger.error(f"Error deserializing state: {e}")
        raise StateError(f"Failed to deserialize state: {e}")


def extract_workflow_state(provider: Any) -> Dict[str, Any]:
    """Extract the state of a workflow from a provider.

    Parameters
    ----------
    provider : EphemeralAWSProvider
        Provider instance

    Returns
    -------
    Dict[str, Any]
        State dictionary
    """
    state = {
        "workflow_id": provider.workflow_id,
        "created_at": datetime.datetime.now().isoformat(),
        "configuration": {
            "image_id": provider.image_id,
            "instance_type": provider.instance_type,
            "region": provider.region,
            "mode": provider.mode,
            "worker_type": provider.worker_type,
            "use_spot_instances": provider.use_spot_instances,
        },
        "resources": {
            "blocks": {k: _clean_state_dict(v) for k, v in provider.blocks.items()},
            "vpc_id": getattr(provider, "vpc_id", None),
            "subnet_ids": getattr(provider, "subnet_ids", []),
            "security_group_ids": getattr(provider, "security_group_ids", []),
            "bastion_id": getattr(provider, "bastion_id", None),
        },
        "jobs": {
            # Extract relevant job information, but strip any unpicklable objects
            k: _clean_state_dict(v)
            for k, v in getattr(provider, "jobs", {}).items()
        },
    }

    return state


def _clean_state_dict(data: Any) -> Any:
    """Clean a dictionary or value for state storage.

    Parameters
    ----------
    data : Any
        Data to clean

    Returns
    -------
    Any
        Cleaned data
    """
    if isinstance(data, dict):
        return {
            k: _clean_state_dict(v) for k, v in data.items() if not k.startswith("_")
        }
    elif isinstance(data, list):
        return [_clean_state_dict(v) for v in data]
    elif isinstance(data, (str, int, float, bool, type(None))):
        return data
    elif isinstance(data, (datetime.datetime, uuid.UUID, set)):
        # These types are handled by the ParslStateEncoder
        return data
    else:
        # For other objects, convert to string representation
        return str(data)

"""Unit tests for the state serialization helpers.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import datetime
import uuid

import pytest

from parsl_ephemeral_aws.exceptions import StateError
from parsl_ephemeral_aws.utils.serialization import (
    deserialize_state,
    serialize_state,
)

pytestmark = pytest.mark.unit


class TestRoundTrip:
    """``serialize_state``/``deserialize_state`` must preserve the value.

    The whole point of ``ParslStateEncoder`` and ``object_hook`` is that state
    documents carry types plain JSON cannot: UUIDs (provider and block IDs),
    datetimes (creation timestamps), and sets. A round trip that silently
    degraded those to strings would corrupt state on every save.
    """

    def test_json_native_types_survive(self):
        state = {"string": "value", "number": 42, "nested": {"data": True}}

        assert deserialize_state(serialize_state(state)) == state

    @pytest.mark.parametrize(
        "key, value",
        [
            ("uuid", uuid.uuid4()),
            ("set", {"a", "b", "c"}),
        ],
    )
    def test_encoded_types_survive_by_value(self, key, value):
        """UUIDs and sets come back equal, and as the same type."""
        restored = deserialize_state(serialize_state({key: value}))[key]

        assert restored == value
        assert type(restored) is type(value)

    def test_datetime_survives(self):
        """Compared separately: isoformat/fromisoformat is lossless only to the
        microsecond, so assert the type and the value rather than identity of
        representation."""
        moment = datetime.datetime(2026, 7, 29, 12, 34, 56, 789012)

        restored = deserialize_state(serialize_state({"when": moment}))["when"]

        assert isinstance(restored, datetime.datetime)
        assert restored == moment

    def test_arbitrary_objects_degrade_to_a_description(self):
        """Anything with a ``__dict__`` round-trips as a description, not itself.

        ``ParslStateEncoder`` encodes unknown objects as class name + module +
        ``__dict__``, and ``object_hook`` deliberately does not import and
        reconstruct them ("for safety" — reinstantiating arbitrary classes named
        in a state document is a code-execution path). So a caller who stores an
        object gets a dict back. Pinned because it is a design decision that
        looks like a bug, and because it is why a lambda serializes rather than
        raising.
        """
        restored = deserialize_state(serialize_state({"callback": lambda: None}))

        assert restored["callback"] == {
            "class": "function",
            "module": "builtins",
            "state": {},
        }

    def test_unserializable_value_raises_state_error(self):
        """A value the encoder cannot handle must fail loudly.

        Needs an object with neither a registered type nor a ``__dict__`` to
        fall back on, hence ``__slots__``.
        """

        class Slotted:
            __slots__ = ("x",)

        value = Slotted()
        value.x = 1

        with pytest.raises(StateError):
            serialize_state({"opaque": value})

    def test_malformed_json_raises_state_error(self):
        with pytest.raises(StateError):
            deserialize_state("{not json")

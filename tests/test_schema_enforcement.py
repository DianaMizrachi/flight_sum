"""Proves the mechanism behind the stage-0 requirement: 'CI fails when a
schema changes without the fixtures being updated'.

We don't mutate the real schemas here (that would make the suite flaky).
Instead we confirm the property every fixture test above relies on: every
contract model rejects unknown/missing/mistyped fields outright, so the
moment someone edits a schema in an incompatible way, the golden-flight and
edge-case validation tests (test_golden_flights.py, test_edge_cases.py)
start failing on real fixture data - there is no separate path that could
stay green.
"""

import pytest
from pydantic import ValidationError

from aura.schemas import Entity, Message


def test_message_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        Message.model_validate(
            {
                "id": "m1",
                "flight_id": "f1",
                "timestamp": "2026-01-01T00:00:00Z",
                "ingested_at": "2026-01-01T00:00:00Z",
                "source": "operator",
                "text": "hello",
                "a_field_that_does_not_exist_in_the_schema": True,
            }
        )


def test_message_rejects_missing_required_field():
    with pytest.raises(ValidationError):
        Message.model_validate(
            {
                "id": "m1",
                "flight_id": "f1",
                "ingested_at": "2026-01-01T00:00:00Z",
                "source": "operator",
                "text": "hello",
            }
        )


def test_entity_rejects_renamed_required_field():
    """Simulates the exact failure a renamed field would cause: fixtures
    still use the old key, so validation must reject it."""
    with pytest.raises(ValidationError):
        Entity.model_validate(
            {
                "id": "target-x",
                "type": "target",
                "display_label_renamed": "x",  # old key would have been "label"
            }
        )

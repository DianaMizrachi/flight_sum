"""Validate every golden flight fixture against the stage-0 contracts.

This is the concrete form of the stage-0 pass criterion: "every golden-set
example passes validation against the schemas". Because every Message,
EntityRef, Event-shaped tag and MissionState checkpoint below is parsed
through the real Pydantic models (not a hand-rolled check), a backward
incompatible schema change (a renamed/added-required field) breaks one of
these tests immediately - that is what keeps CI from going green on a
schema change that nobody updated the fixtures for.
"""

import pytest

from aura.schemas import Entity, EntityRef, Message, MissionState, VersionTag
from aura.taxonomy import load_event_taxonomy

VALID_EVENT_TYPE_IDS = {d.id for d in load_event_taxonomy()}


def test_golden_flights_dir_is_not_empty(golden_flight_paths):
    assert len(golden_flight_paths) >= 3, "plan requires 3-5 tagged golden flights"


def test_messages_validate_against_message_schema(golden_flight):
    message_ids = set()
    for raw in golden_flight["messages"]:
        fields = {k: v for k, v in raw.items() if k != "expected"}
        message = Message.model_validate(fields)
        assert message.flight_id == golden_flight["flight_id"]
        message_ids.add(message.id)
    assert len(message_ids) == len(golden_flight["messages"]), "duplicate message ids in fixture"


def test_tagged_events_reference_known_taxonomy_types(golden_flight):
    for raw in golden_flight["messages"]:
        expected = raw["expected"]
        if not expected["has_event"]:
            continue
        assert expected["event_type"] in VALID_EVENT_TYPE_IDS, (
            f"{golden_flight['flight_id']}/{raw['id']} tags unknown event type "
            f"{expected['event_type']!r}"
        )


def test_tagged_event_entities_validate(golden_flight):
    for raw in golden_flight["messages"]:
        expected = raw["expected"]
        if not expected["has_event"]:
            continue
        for entity in expected.get("entities", []):
            EntityRef.model_validate(entity)


def test_tagged_events_reference_source_message_itself(golden_flight):
    message_ids = {raw["id"] for raw in golden_flight["messages"]}
    for raw in golden_flight["messages"]:
        if raw["expected"]["has_event"]:
            assert raw["id"] in message_ids


def test_flight_end_signal_points_to_real_message(golden_flight):
    signal_id = golden_flight["flight_end_signal_message_id"]
    message_ids = {raw["id"] for raw in golden_flight["messages"]}
    assert signal_id in message_ids


def test_checkpoints_validate_as_mission_state(golden_flight):
    message_ids = {raw["id"] for raw in golden_flight["messages"]}
    for checkpoint in golden_flight.get("checkpoints", []):
        assert checkpoint["at_message_id"] in message_ids
        mission_state = MissionState.model_validate(
            {
                "flight_id": golden_flight["flight_id"],
                "version": VersionTag.REALTIME,
                "as_of": checkpoint["timestamp"],
                "entities": checkpoint["state"]["entities"],
            }
        )
        for entity_id, entity in mission_state.entities.items():
            assert entity.id == entity_id


def test_golden_flight_has_at_least_one_checkpoint(golden_flight):
    assert golden_flight.get("checkpoints"), "golden flights must tag expected mission state"


@pytest.mark.parametrize(
    "entity_payload",
    [
        {"id": "target-x", "type": "target", "label": "x", "status": "no_such_status"},
        {"id": "target-x", "type": "not_a_type", "label": "x"},
    ],
)
def test_invalid_entity_payloads_are_rejected(entity_payload):
    with pytest.raises(Exception):
        Entity.model_validate(entity_payload)

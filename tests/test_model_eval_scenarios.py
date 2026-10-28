"""Validate the model-comparison scenarios against the stage-0 contracts.

These fixtures exist to compare two local LLMs (see the README beside
them), not to tag a full flight - so they deliberately
differ from tests/fixtures/golden_flights/: three minutes instead of a
whole sortie, no flight-end signal, no mission-state checkpoints, and
expectations stated as a flat list of events carrying ``source_ids``
rather than one tag per message. That shape is what the stage-4 pass
criterion is written against: "every event points at the correct source
sentences".

Everything below is checked against the real Pydantic models and the real
taxonomy, so a scenario that drifts from the contracts fails here rather
than silently skewing a model comparison.
"""

import pytest

from aura.schemas import EntityRef, InfoRelation, Message
from aura.taxonomy import load_event_taxonomy

TAXONOMY = {d.id: d for d in load_event_taxonomy()}


def all_expected(scenario: dict) -> list[dict]:
    return scenario.get("expected_events", []) + scenario.get("optional_events", [])


def test_scenario_dir_holds_the_agreed_set(model_eval_paths):
    assert len(model_eval_paths) >= 2, "model comparison needs at least 2 scenarios"


def test_messages_validate_against_message_schema(model_eval_scenario):
    ids = set()
    for raw in model_eval_scenario["messages"]:
        message = Message.model_validate(raw)
        assert message.flight_id == model_eval_scenario["flight_id"]
        ids.add(message.id)
    assert len(ids) == len(model_eval_scenario["messages"]), "duplicate message ids"


def test_messages_are_in_chronological_order(model_eval_scenario):
    stamps = [raw["timestamp"] for raw in model_eval_scenario["messages"]]
    assert stamps == sorted(stamps), "scenario messages must be listed in time order"


def test_declared_duration_matches_the_timestamps(model_eval_scenario):
    messages = [Message.model_validate(raw) for raw in model_eval_scenario["messages"]]
    span = (messages[-1].timestamp - messages[0].timestamp).total_seconds()
    assert span == pytest.approx(model_eval_scenario["duration_seconds"], abs=10)
    assert 150 <= span <= 195, "scenarios are specified as ~3 minutes"


def test_expected_event_count_matches_the_list(model_eval_scenario):
    assert model_eval_scenario["expected_event_count"] == len(
        model_eval_scenario["expected_events"]
    )


def test_expected_events_use_known_taxonomy_types(model_eval_scenario):
    for event in all_expected(model_eval_scenario):
        assert event["type"] in TAXONOMY, f"{event['key']} uses unknown type {event['type']!r}"


def test_expected_events_use_a_valid_relation(model_eval_scenario):
    for event in all_expected(model_eval_scenario):
        InfoRelation(event["relation_to_previous"])


def test_expected_event_entities_validate(model_eval_scenario):
    for event in all_expected(model_eval_scenario):
        for entity in event.get("entities", []):
            EntityRef.model_validate(entity)


def test_expected_events_satisfy_taxonomy_required_fields(model_eval_scenario):
    """A scenario must not expect an event the validator (stage 5) would reject."""
    for event in all_expected(model_eval_scenario):
        for field in TAXONOMY[event["type"]].required_fields:
            if field == "entities":
                assert event.get("entities"), f"{event['key']} needs entities"
            elif field == "new_info":
                assert event.get("new_info_he"), f"{event['key']} needs new_info"
            else:  # pragma: no cover - guards against a taxonomy change
                pytest.fail(f"unhandled required field {field!r} in taxonomy")


def test_expected_event_entity_types_are_applicable(model_eval_scenario):
    for event in all_expected(model_eval_scenario):
        applicable = set(TAXONOMY[event["type"]].applicable_entity_types)
        for entity in event.get("entities", []):
            assert entity["type"] in applicable, (
                f"{event['key']}: {entity['type']} is not applicable to {event['type']}"
            )


def test_source_ids_point_at_messages_in_the_scenario(model_eval_scenario):
    known = {raw["id"] for raw in model_eval_scenario["messages"]}
    for event in all_expected(model_eval_scenario):
        assert event["source_ids"], f"{event['key']} has no source_ids"
        assert len(event["source_ids"]) == len(set(event["source_ids"])), event["key"]
        for source_id in event["source_ids"]:
            assert source_id in known, f"{event['key']} cites unknown {source_id}"


def test_event_keys_are_unique(model_eval_scenario):
    keys = [event["key"] for event in all_expected(model_eval_scenario)]
    assert len(keys) == len(set(keys))


def test_negative_cases_point_at_messages_in_the_scenario(model_eval_scenario):
    known = {raw["id"] for raw in model_eval_scenario["messages"]}
    for case in model_eval_scenario["must_not_produce_events"]:
        assert case["message_id"] in known
        assert case["reason_he"].strip(), "a negative case must say why"


def test_negative_cases_are_not_also_expected_sources(model_eval_scenario):
    """The decisive property: a message is either evidence or a trap, never both."""
    cited = {
        source_id
        for event in model_eval_scenario["expected_events"]
        for source_id in event["source_ids"]
    }
    for case in model_eval_scenario["must_not_produce_events"]:
        assert case["message_id"] not in cited, (
            f"{case['message_id']} is both a trap and an expected source"
        )


def test_every_message_is_either_evidence_or_an_explicit_trap(model_eval_scenario):
    """No message may be left unjudged, or a model could not be scored on it."""
    cited = {
        source_id
        for event in all_expected(model_eval_scenario)
        for source_id in event["source_ids"]
    }
    traps = {case["message_id"] for case in model_eval_scenario["must_not_produce_events"]}
    for raw in model_eval_scenario["messages"]:
        assert raw["id"] in cited or raw["id"] in traps, f"{raw['id']} has no stated expectation"


def test_optional_events_explain_why_they_are_optional(model_eval_scenario):
    for event in model_eval_scenario.get("optional_events", []):
        assert event.get("reason_optional_he", "").strip(), event["key"]

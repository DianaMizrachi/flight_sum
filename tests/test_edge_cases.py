"""Validate the dedicated edge-case fixtures (stage 0, task 5).

These fixtures don't exercise real components yet (ingestion/validator are
stage 1 and 5) - they pin down, with runnable assertions, what each
component will be judged against once built: dedup, chronological
ordering by original timestamp, dead-lettering, and same-sentence
contradiction detection.
"""

from datetime import datetime
from pathlib import Path

import pytest
import yaml

from aura.schemas import LLMResult, Message

EDGE_CASES_DIR = Path(__file__).parent / "fixtures" / "edge_cases"


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def dedup_key(raw: dict) -> tuple:
    if raw.get("id"):
        return ("id", raw["id"])
    return ("hash", raw["flight_id"], raw["timestamp"], raw["source"], raw["text"])


def test_duplicates_fixture_counts_are_internally_consistent():
    data = load_yaml(EDGE_CASES_DIR / "duplicates.yaml")
    for case in data["cases"]:
        seen = set()
        accepted = 0
        duplicates = 0
        for raw in case["raw_inputs"]:
            key = dedup_key(raw)
            if key in seen:
                duplicates += 1
            else:
                seen.add(key)
                accepted += 1
        assert accepted == case["expected"]["accepted_count"], case["case_id"]
        assert duplicates == case["expected"]["duplicate_count"], case["case_id"]


def test_late_arrival_chronological_order_matches_timestamp_sort():
    data = load_yaml(EDGE_CASES_DIR / "late_arrival.yaml")
    ordered = sorted(data["raw_inputs"], key=lambda m: m["timestamp"])
    ordered_ids = [m["id"] for m in ordered]
    assert ordered_ids == data["expected"]["chronological_order_by_timestamp"]


def test_late_arrival_lateness_matches_ingested_minus_timestamp():
    data = load_yaml(EDGE_CASES_DIR / "late_arrival.yaml")
    late_id = data["expected"]["late_message_id"]
    late_msg = next(m for m in data["raw_inputs"] if m["id"] == late_id)
    delta = datetime.fromisoformat(late_msg["ingested_at"]) - datetime.fromisoformat(
        late_msg["timestamp"]
    )
    assert delta.total_seconds() == data["expected"]["late_by_seconds"]


def test_malformed_cases_missing_required_field_fail_message_validation():
    data = load_yaml(EDGE_CASES_DIR / "malformed_and_empty.yaml")
    for case in data["cases"]:
        if case["expected"]["reason"] != "missing_required_field":
            continue
        payload = {**case["raw_input"], "ingested_at": "2026-09-06T12:00:00Z"}
        with pytest.raises(Exception):
            Message.model_validate(payload)


def test_malformed_cases_empty_text_passes_schema_but_is_blank():
    """Schema allows an empty str - blankness is an ingestion business rule,
    not a contract violation, so Message validation must succeed here while
    still being flagged blank for the future dead-letter check."""
    data = load_yaml(EDGE_CASES_DIR / "malformed_and_empty.yaml")
    for case in data["cases"]:
        if case["expected"]["reason"] != "empty_text":
            continue
        payload = {**case["raw_input"], "ingested_at": "2026-09-06T12:00:00Z"}
        message = Message.model_validate(payload)
        assert message.text.strip() == ""


def test_same_sentence_contradiction_is_schema_valid_but_semantically_bad():
    """The two contradicting events are structurally fine (this is exactly
    why stage 5 needs an explicit contradiction check, not just schema
    validation)."""
    data = load_yaml(EDGE_CASES_DIR / "same_sentence_contradiction.yaml")
    Message.model_validate(data["source_message"])
    result = LLMResult.model_validate(data["llm_result"])
    assert len(result.events) == 2
    source_id_sets = [set(e.source_ids) for e in result.events]
    assert source_id_sets[0] == source_id_sets[1], "fixture must share the same source sentence"
    assert result.events[0].relation_to_previous != result.events[1].relation_to_previous or (
        result.events[0].type != result.events[1].type
    )

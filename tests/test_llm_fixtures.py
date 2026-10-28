"""Check the recorded real model answers (רכיב 4, משימה 3).

tests/fixtures/llm_responses/ holds answers captured from models actually
running on a GPU, via RecordingLLMClient. They exist so a contract change
is caught against real output rather than only against output we wrote
ourselves: if ``LLMEvent`` gains a required field or an enum value is
renamed, every recorded answer stops parsing here and the break is
visible without pulling 17GB of weights.

These tests assert the *shape* of what the models returned, never its
correctness. Whether an answer was right is scored by
scripts/evaluate_llm.py and written up in the README - and some of these
recordings are deliberately of answers that scored badly.
"""

import json
from pathlib import Path

import pytest

from aura.llm_engine import ModelEventsResponse

FIXTURE_FILES = sorted((Path(__file__).parent / "fixtures" / "llm_responses").glob("*.json"))


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(params=FIXTURE_FILES, ids=lambda p: p.stem)
def recorded(request):
    return load(request.param)


def test_recordings_exist():
    """Stage 4 asks for real answers kept as fixtures, not just mocks."""
    assert FIXTURE_FILES, "no recorded answers; run scripts/evaluate_llm.py --record"


def test_recording_has_everything_needed_to_replay_it(recorded):
    for key in ("model", "window_id", "system_prompt", "user_prompt", "response"):
        assert recorded.get(key), f"missing {key}"


def test_recorded_response_still_parses_against_the_contract(recorded):
    """The regression guard: a schema change breaks this, loudly."""
    ModelEventsResponse.model_validate_json(recorded["response"])


def test_recorded_prompt_carries_the_taxonomy_and_the_window(recorded):
    assert "contact_with_target" in recorded["system_prompt"]
    assert "source_ids" in recorded["system_prompt"]
    assert "[" in recorded["user_prompt"], "sentences must be labelled with their ids"


def test_every_cited_sentence_appears_in_the_prompt(recorded):
    """A model may cite the wrong sentence, but never one it was not shown.

    A citation that is not in the window at all would mean the engine sent
    a different prompt than the one recorded beside it - a harness bug, not
    a model error, and one no accuracy metric would reveal.
    """
    parsed = ModelEventsResponse.model_validate_json(recorded["response"])
    for event in parsed.events:
        for source_id in event.source_ids:
            assert f"[{source_id}]" in recorded["user_prompt"], (
                f"{recorded['model']} cited {source_id}, which is not in the window"
            )


def test_recorded_events_are_not_all_empty():
    """At least one recording must contain events, or these fixtures prove nothing."""
    total = sum(
        len(ModelEventsResponse.model_validate_json(load(path)["response"]).events)
        for path in FIXTURE_FILES
    )
    assert total > 0


def test_more_than_one_model_is_represented():
    """The point of stage 4 was a comparison, so both candidates are kept."""
    models = {load(path)["model"] for path in FIXTURE_FILES}
    assert len(models) >= 2, f"only {models} recorded; a comparison needs both"

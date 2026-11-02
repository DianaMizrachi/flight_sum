"""The tagged fixtures, run through output validation (רכיב 5).

A validator is only half-tested by broken input. Rejecting everything
satisfies "no malformed output reaches the state update" perfectly and is
worthless, so the other half has to be stated too: the events the project
has already decided are *correct* must pass.

Those events are the tags in ``tests/fixtures/`` - the golden flights'
per-sentence ``expected`` blocks and the model-comparison scenarios'
``expected_events``. They were written by hand as the right answer, long
before this component existed. Every one of them is rebuilt here as the
model would have emitted it and pushed through the real validator; a
single rejection means either the validator is wrong or a tag is, and
both are worth stopping for.

This is also what keeps the taxonomy honest. Adding a ``required_fields``
entry or narrowing ``applicable_entity_types`` would quietly invalidate
tagged events that the stage-4 model is still scored against; here that
shows up as a failure with the event's name in it.
"""

from datetime import UTC, datetime

import pytest

from aura.schemas import (
    ContextBundle,
    LLMEvent,
    LLMResult,
    Message,
    ValidationStatus,
    WindowCloseReason,
)
from aura.validator import OutputValidator, RejectionReason

FROZEN_NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)


@pytest.fixture
def validator() -> OutputValidator:
    return OutputValidator(clock=lambda: FROZEN_NOW)


def bundle_for(flight_id: str, raw_messages: list[dict]) -> ContextBundle:
    """חלון אחד שמחזיק את כל משפטי הפיקסצ'ר.

    בפועל רכיב 3 היה מחלק את הטיסה לכמה חלונות, אבל השאלה כאן היא אם
    האירוע המתויג עובר אימות - לא היכן עובר גבול החלון. חלון אחד גדול
    הוא הצורה שבה כל מזהי המקור המתויגים נמצאים באותו מקום.
    """
    return ContextBundle(
        window_id=f"{flight_id}-w-all",
        flight_id=flight_id,
        messages=[
            Message.model_validate({k: v for k, v in raw.items() if k != "expected"})
            for raw in raw_messages
        ],
        window_close_reason=WindowCloseReason.FLIGHT_END,
    )


def result_for(bundle: ContextBundle, events: list[LLMEvent]) -> LLMResult:
    return LLMResult(window_id=bundle.window_id, events=events, llm_model="tagged-fixture")


# --- golden flights ------------------------------------------------------


def golden_events(flight: dict) -> list[tuple[str, LLMEvent]]:
    """בונה מהתגיות את האירועים שהמודל היה אמור להפיק."""
    events: list[tuple[str, LLMEvent]] = []
    for raw in flight["messages"]:
        expected = raw["expected"]
        if not expected["has_event"]:
            continue
        events.append(
            (
                raw["id"],
                LLMEvent.model_validate(
                    {
                        "type": expected["event_type"],
                        "description": expected.get("new_info") or raw["text"],
                        "entities": expected.get("entities", []),
                        "new_info": expected.get("new_info", ""),
                        "relation_to_previous": expected["relation_to_previous"],
                        "confidence": expected.get("confidence_min", 0.8),
                        "source_ids": [raw["id"]],
                    }
                ),
            )
        )
    return events


def test_a_golden_flight_tags_at_least_one_event(golden_flight):
    assert golden_events(golden_flight), f"{golden_flight['flight_id']} tags no events"


def test_every_tagged_golden_event_passes_validation(golden_flight, validator):
    """התג הוא התשובה הנכונה; אימות שדוחה אותה שגוי."""
    bundle = bundle_for(golden_flight["flight_id"], golden_flight["messages"])
    tagged = golden_events(golden_flight)

    validated = validator.validate(result_for(bundle, [e for _, e in tagged]), bundle)

    assert not validated.rejected, (
        f"{golden_flight['flight_id']}: "
        + "; ".join(
            f"{r.raw.get('type')} -> {r.reason}: {r.detail}" for r in validated.rejected
        )
    )
    assert len(validated.accepted) == len(tagged)
    assert validated.status == ValidationStatus.OK


def test_each_tagged_golden_event_passes_on_its_own(golden_flight, validator):
    """אירוע-אירוע, כדי שכישלון יצביע על המשפט המדויק ולא על הטיסה."""
    bundle = bundle_for(golden_flight["flight_id"], golden_flight["messages"])

    for message_id, event in golden_events(golden_flight):
        validated = validator.validate(result_for(bundle, [event]), bundle)
        assert not validated.rejected, (
            f"{message_id}: {validated.rejected[0].reason} - {validated.rejected[0].detail}"
        )


def test_tagged_events_keep_their_source_sentence(golden_flight, validator):
    """הייחוס שורד את האימות: מאירוע מאומת חוזרים לתמלול המקורי."""
    bundle = bundle_for(golden_flight["flight_id"], golden_flight["messages"])
    tagged = golden_events(golden_flight)

    validated = validator.validate(result_for(bundle, [e for _, e in tagged]), bundle)

    returned = [event.source_ids for event in validated.accepted]
    assert returned == [[message_id] for message_id, _ in tagged]


# --- model-comparison scenarios ------------------------------------------


def scenario_events(scenario: dict) -> list[tuple[str, LLMEvent]]:
    events: list[tuple[str, LLMEvent]] = []
    for expected in scenario["expected_events"] + scenario.get("optional_events", []):
        events.append(
            (
                expected["key"],
                LLMEvent.model_validate(
                    {
                        "type": expected["type"],
                        "description": expected["new_info_he"],
                        "entities": expected.get("entities", []),
                        "new_info": expected["new_info_he"],
                        "relation_to_previous": expected["relation_to_previous"],
                        "confidence": expected.get("confidence_min", 0.8),
                        "source_ids": list(expected["source_ids"]),
                    }
                ),
            )
        )
    return events


def test_every_expected_scenario_event_passes_validation(model_eval_scenario, validator):
    """התרחישים האלה הם מה שמודל נמדד מולו; הם חייבים להיות קבילים."""
    bundle = bundle_for(model_eval_scenario["flight_id"], model_eval_scenario["messages"])
    tagged = scenario_events(model_eval_scenario)

    validated = validator.validate(result_for(bundle, [e for _, e in tagged]), bundle)

    assert not validated.rejected, (
        f"{model_eval_scenario['scenario_id']}: "
        + "; ".join(f"{r.raw.get('type')} -> {r.reason}: {r.detail}" for r in validated.rejected)
    )
    assert len(validated.accepted) == len(tagged)


def test_each_expected_scenario_event_passes_on_its_own(model_eval_scenario, validator):
    bundle = bundle_for(model_eval_scenario["flight_id"], model_eval_scenario["messages"])

    for key, event in scenario_events(model_eval_scenario):
        validated = validator.validate(result_for(bundle, [event]), bundle)
        assert not validated.rejected, (
            f"{key}: {validated.rejected[0].reason} - {validated.rejected[0].detail}"
        )


def test_a_scenario_with_contact_and_loss_over_time_is_not_a_contradiction(
    model_eval_scenario, validator
):
    """המקרה שהכלל כמעט תפס בטעות.

    ``eval-01`` מחזיק קשר, אובדן קשר וחידוש על אותה מטרה - שלושתם
    אירועים תקינים, כי כל אחד נגזר ממשפט אחר. אם כלל הסתירות היה בודק
    רק "אותה ישות, סוגים סותרים", התרחיש הזה היה נדחה כולו.
    """
    bundle = bundle_for(model_eval_scenario["flight_id"], model_eval_scenario["messages"])
    tagged = [e for _, e in scenario_events(model_eval_scenario)]
    types = {event.type for event in tagged}
    if not {"contact_with_target", "loss_of_contact"} <= types:
        pytest.skip("this scenario does not pair contact with loss of contact")

    validated = validator.validate(result_for(bundle, tagged), bundle)

    reasons = [r.reason for r in validated.rejected]
    assert RejectionReason.CONTRADICTING_EVENTS_SAME_SOURCE_SENTENCE not in reasons
    assert not validated.rejected


# --- the trap messages ---------------------------------------------------


def test_an_event_built_on_a_trap_message_is_structurally_valid(
    model_eval_scenario, validator
):
    """מלכודת אינה פלט *פגום*, ולכן רכיב 5 אינו אמור לתפוס אותה.

    ``must_not_produce_events`` מסמן משפטים שמודל טוב לא יפיק מהם אירוע
    - חזרה, אישור, או ספקולציה. אירוע כזה עדיין עומד בכל כלל מבני, והוא
    נספר כ-false positive בניקוד של שלב 4, לא כדחייה כאן. הגבול הזה
    נבדק כדי שלא יתפתה מישהו להעביר ניקוד איכות לתוך הוולידטור.
    """
    bundle = bundle_for(model_eval_scenario["flight_id"], model_eval_scenario["messages"])
    trap = model_eval_scenario["must_not_produce_events"][0]
    invented = LLMEvent.model_validate(
        {
            "type": "contact_with_target",
            "description": "אירוע שהומצא ממשפט מלכודת",
            "entities": [{"id": None, "type": "target", "label": "מטרה אלפא"}],
            "new_info": "המצאה.",
            "relation_to_previous": "new",
            "confidence": 0.9,
            "source_ids": [trap["message_id"]],
        }
    )

    validated = validator.validate(result_for(bundle, [invented]), bundle)

    assert validated.accepted, "component 5 checks form, not whether the model was right"
    assert not validated.rejected

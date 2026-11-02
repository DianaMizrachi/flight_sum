"""A golden flight, end to end into mission state (רכיב 6, קריטריון המעבר).

The stage's criterion is *"one golden flight runs end to end with a final
state matching the manual tagging"*. This file runs all three, from the
tagged events through component 5 and into component 6, and compares the
result with the ``checkpoints`` block each fixture carries.

It compares what the contracts can actually produce, which is less than
the fixtures imagined, and the gap is worth stating plainly rather than
hiding behind a looser assertion:

**Entities, types, labels and open/closed status** are reproduced exactly,
and that is the substance of the criterion - the system ends the flight
believing in the same things the human tagger did.

**Entity ids** are not compared. The fixtures use hand-chosen
transliterations (``target-alpha``), which no rule can derive from
``מטרה אלפא``; the manager derives its own stable id instead. Entities
are therefore matched by type and label.

**Attribute names and values** are not compared, because they cannot be
produced. The checkpoints expect ``location: "צומת צפוני"`` and
``fuel_status: "40%..."`` - structured extractions - but ``Event`` has no
attribute field at all. Its only content is a type, free-text
``new_info`` and entity references. Component 6 therefore records
``new_info`` under the attribute the taxonomy assigns to the event type.
Closing that gap is a contract change (an ``attributes`` field on
``LLMEvent``), not a change here, and it is called out in the README.
"""

from datetime import datetime

import pytest

from aura.schemas import (
    ContextBundle,
    EntityStatus,
    LLMEvent,
    LLMResult,
    Message,
    WindowCloseReason,
)
from aura.state_manager import MissionStateManager, entity_key
from aura.validator import OutputValidator


def message_times(flight: dict) -> dict[str, datetime]:
    return {raw["id"]: datetime.fromisoformat(raw["timestamp"]) for raw in flight["messages"]}


def tagged_llm_events(flight: dict) -> list[LLMEvent]:
    """התגיות, בצורה שבה המודל היה מפיק אותן."""
    events = []
    for raw in flight["messages"]:
        expected = raw["expected"]
        if not expected["has_event"]:
            continue
        events.append(
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
            )
        )
    return events


def run_flight(flight: dict):
    """התגיות -> רכיב 5 -> רכיב 6. בלי לדלג על האימות בדרך.

    העברת האירועים דרך הוולידטור ולא ישירות למנהל המצב היא חלק מהטענה:
    המצב נבנה רק ממה שעבר אימות, בדיוק כמו בצינור האמיתי.
    """
    flight_id = flight["flight_id"]
    bundle = ContextBundle(
        window_id=f"{flight_id}-w-all",
        flight_id=flight_id,
        messages=[
            Message.model_validate({k: v for k, v in raw.items() if k != "expected"})
            for raw in flight["messages"]
        ],
        window_close_reason=WindowCloseReason.FLIGHT_END,
    )
    result = LLMResult(
        window_id=bundle.window_id, events=tagged_llm_events(flight), llm_model="tagged"
    )
    validated = OutputValidator().validate(result, bundle)
    assert not validated.rejected, "the tagged events must survive component 5"

    update = MissionStateManager().apply(
        validated.accepted, message_times=message_times(flight)
    )
    return validated, update


def by_label(state) -> dict[tuple[str, str], object]:
    return {entity_key(e.type, e.label): e for e in state.entities.values()}


def expected_final(flight: dict) -> dict:
    return flight["checkpoints"][-1]["state"]["entities"]


# --- the criterion -------------------------------------------------------


def test_the_flight_reaches_the_entities_the_tagger_expected(golden_flight):
    """אותן ישויות, אותם סוגים, אותן תוויות."""
    _, update = run_flight(golden_flight)

    produced = set(by_label(update.state))
    expected = {
        entity_key(raw["type"], raw["label"]) for raw in expected_final(golden_flight).values()
    }
    assert produced == expected


def test_every_entity_ends_in_the_status_the_tagger_expected(golden_flight):
    """פתוח מול סגור הוא ההבדל המהותי במצב סופי של טיסה."""
    _, update = run_flight(golden_flight)

    produced = by_label(update.state)
    for raw in expected_final(golden_flight).values():
        entity = produced[entity_key(raw["type"], raw["label"])]
        assert entity.status is EntityStatus(raw["status"]), (
            f"{raw['label']}: expected {raw['status']}, got {entity.status.value}"
        )


def test_a_closed_entity_names_the_event_that_closed_it(golden_flight):
    """ייחוס שורד עד המצב: מישות סגורה חזרה לאירוע, ומשם לתמלול."""
    validated, update = run_flight(golden_flight)

    event_ids = {event.id for event in validated.accepted}
    closed = [e for e in update.state.entities.values() if e.status is EntityStatus.CLOSED]
    for entity in closed:
        assert entity.closed_by_event_id in event_ids
        assert entity.closed_at is not None


def test_every_accepted_event_is_either_applied_or_explained(golden_flight):
    """שום אירוע מאומת לא נעלם בשקט בין רכיב 5 לרכיב 6."""
    validated, update = run_flight(golden_flight)

    accounted = set(update.applied_event_ids) | {event_id for event_id, _ in update.skipped}
    assert accounted == {event.id for event in validated.accepted}


def test_the_change_log_can_rebuild_the_entities(golden_flight):
    """התשתית לשלב 7: רשומות השינוי מכסות כל ישות שבמצב."""
    _, update = run_flight(golden_flight)

    assert update.changes
    assert {change.entity_id for change in update.changes} == set(update.state.entities)
    assert all(change.applied_at is not None for change in update.changes)


def test_the_changes_are_in_the_order_they_were_applied(golden_flight):
    _, update = run_flight(golden_flight)

    stamps = [change.applied_at for change in update.changes]
    assert stamps == sorted(stamps)


def test_the_final_state_is_as_of_the_last_change(golden_flight):
    _, update = run_flight(golden_flight)

    assert update.state.as_of == max(change.applied_at for change in update.changes)


# --- the gap, stated as a test -------------------------------------------


def test_the_checkpoints_expect_attributes_the_event_contract_cannot_carry(golden_flight):
    """מתעד פער אמיתי בין הפיקסצ'רים של שלב 0 לחוזים של שלב 0.

    נקודות הביקורת מצפות למאפיינים חילוציים (``location``,
    ``fuel_status``), אבל ל-``Event`` אין שדה מאפיינים כלל - יש לו סוג,
    ``new_info`` חופשי והפניות ישות. לכן מנהל המצב כותב את ``new_info``
    תחת המאפיין שהטקסונומיה מצמידה לסוג האירוע.

    הבדיקה נכשלת ברגע שהפער ייסגר - כלומר כשיתווסף שדה ``attributes``
    ל-``LLMEvent`` ויתעדכנו הפיקסצ'רים - וזו בדיוק ההתראה הרצויה: אז
    יהיה אפשר להשוות גם ערכי מאפיינים, והבדיקה הזו צריכה להימחק.
    """
    _, update = run_flight(golden_flight)

    produced = by_label(update.state)
    taxonomy_attributes = {"contact_status", "mission", "malfunction_status", "status"}
    mismatched = []
    for raw in expected_final(golden_flight).values():
        entity = produced[entity_key(raw["type"], raw["label"])]
        assert set(entity.attributes) <= taxonomy_attributes
        if set(entity.attributes) != set(raw.get("attributes", {})):
            mismatched.append(raw["label"])

    assert mismatched, (
        "attribute names now line up with the checkpoints - the contract gap is closed, "
        "so this test should be deleted and the values compared properly"
    )


@pytest.mark.parametrize("checkpoint_index", [0])
def test_an_earlier_checkpoint_is_reachable_by_replaying_part_of_the_flight(
    golden_flight, checkpoint_index
):
    """מצב בנקודת זמן, לא רק בסוף - ההבטחה ששלב 7 בנוי עליה."""
    checkpoint = golden_flight["checkpoints"][checkpoint_index]
    cutoff = datetime.fromisoformat(checkpoint["timestamp"])
    validated, _ = run_flight(golden_flight)
    times = message_times(golden_flight)

    until_cutoff = [
        event
        for event in validated.accepted
        if min(times[sid] for sid in event.source_ids) <= cutoff
    ]
    update = MissionStateManager().apply(until_cutoff, message_times=times)

    expected = {
        entity_key(raw["type"], raw["label"])
        for raw in checkpoint["state"]["entities"].values()
    }
    assert set(by_label(update.state)) == expected

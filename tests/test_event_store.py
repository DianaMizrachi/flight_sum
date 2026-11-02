"""The event and mission-state store (רכיב 7, שלב 7).

The stage has two pass criteria and they pull in different directions, so
both are tested here directly.

*"From an event back to its source sentences always succeeds"* - that is
the property the whole system's traceability rests on. An event that is
stored without its sources is not evidence of anything.

*"State rebuilt at time T is 100% identical to the state that was live at
time T"* - tested by running each golden flight through components 5 and
6, snapshotting the live state after every single event, and then asking
the store to rebuild that same moment from the change log alone. If the
two ever diverge, the stored history is a story about what happened
rather than a record of it, and stages 9 and 11 are both built on sand.

The append-only tests matter for the same reason: the guarantee has to
hold against someone with a SQL prompt, not just against this module, so
they go through a raw connection.
"""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from aura.event_store import DuplicateEventError, SQLiteEventStore
from aura.schemas import (
    ContextBundle,
    Event,
    LLMEvent,
    LLMResult,
    Message,
    VersionTag,
    WindowCloseReason,
)
from aura.state_manager import MissionStateManager
from aura.validator import OutputValidator

FLIGHT = "flight-s7"
START = datetime(2026, 9, 1, 6, 0, tzinfo=UTC)


def at(minutes: int) -> datetime:
    return START + timedelta(minutes=minutes)


@pytest.fixture
def store() -> SQLiteEventStore:
    with SQLiteEventStore(":memory:") as opened:
        yield opened


def event(
    event_id: str,
    *,
    type: str = "contact_with_target",
    relation: str = "new",
    info: str = "קשר עין עם מטרה אלפא.",
    sources: list[str] | None = None,
    minutes: int = 0,
    version: VersionTag = VersionTag.REALTIME,
) -> Event:
    return Event(
        id=event_id,
        flight_id=FLIGHT,
        window_id="w01",
        type=type,
        description=info,
        entities=[{"id": None, "type": "target", "label": "מטרה אלפא"}],
        new_info=info,
        relation_to_previous=relation,
        confidence=0.9,
        source_ids=sources or ["m01"],
        version=version,
        created_at=at(minutes),
    )


# --- events and their sources --------------------------------------------


def test_an_event_round_trips(store):
    original = event("e1")

    store.append_event(original)
    loaded = store.get_event("e1", FLIGHT)

    assert loaded is not None
    assert loaded.model_dump() == original.model_dump()


def test_from_an_event_back_to_its_sentences_always_works(store):
    """קריטריון המעבר: ייחוס שאינו נשבר."""
    store.append_events(
        [
            event("e1", sources=["m01"]),
            event("e2", sources=["m02", "m03"], minutes=2),
        ]
    )

    for stored in store.events_for_flight(FLIGHT):
        assert store.source_ids_of(stored.id, FLIGHT), f"{stored.id} lost its sources"
    assert store.source_ids_of("e2", FLIGHT) == ["m02", "m03"]


def test_source_order_is_preserved(store):
    """``source_ids`` היא רשימה מסודרת, לא קבוצה."""
    store.append_event(event("e1", sources=["m09", "m02", "m05"]))

    assert store.get_event("e1", FLIGHT).source_ids == ["m09", "m02", "m05"]


def test_from_a_sentence_forward_to_its_events(store):
    """הכיוון ההפוך, שבלעדיו הקליינט אינו יכול לסמן משפט שהוליד אירוע."""
    store.append_events(
        [
            event("e1", sources=["m01"]),
            event("e2", sources=["m01", "m02"], minutes=1),
            event("e3", sources=["m09"], minutes=2),
        ]
    )

    assert [e.id for e in store.events_from_message("m01", FLIGHT)] == ["e1", "e2"]
    assert store.events_from_message("m-nothing", FLIGHT) == []


def test_the_timeline_comes_back_in_order(store):
    store.append_events(
        [event("e3", minutes=9), event("e1", minutes=0), event("e2", minutes=4)]
    )

    assert [e.id for e in store.events_for_flight(FLIGHT)] == ["e1", "e2", "e3"]


def test_the_timeline_can_be_cut_at_a_point_in_time(store):
    store.append_events([event("e1", minutes=0), event("e2", minutes=5)])

    assert [e.id for e in store.events_for_flight(FLIGHT, until=at(2))] == ["e1"]


def test_the_timeline_can_be_filtered_by_type(store):
    store.append_events(
        [event("e1"), event("e2", type="malfunction", minutes=2)]
    )

    assert [e.id for e in store.events_for_flight(FLIGHT, event_type="malfunction")] == ["e2"]


def test_the_same_event_cannot_be_stored_twice(store):
    store.append_event(event("e1"))

    with pytest.raises(DuplicateEventError):
        store.append_event(event("e1"))


def test_a_failed_batch_writes_nothing(store):
    """טרנזקציה אחת: אירוע וקישוריו נכתבים יחד או בכלל לא."""
    store.append_event(event("e1"))

    with pytest.raises(DuplicateEventError):
        store.append_events([event("e2", minutes=1), event("e1")])

    assert store.get_event("e2", FLIGHT) is None


# --- append-only, enforced by the engine ---------------------------------


@pytest.mark.parametrize("table", ["events", "state_changes"])
def test_the_engine_itself_blocks_updates_and_deletes(store, table):
    """הערובה חייבת להחזיק גם מול מי שיש לו גישת SQL ישירה."""
    store.append_event(event("e1"))
    store.append_changes(
        MissionStateManager().apply([event("e1")]).changes
    )
    raw = store._connection

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        raw.execute(f"UPDATE {table} SET flight_id = 'tampered'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        raw.execute(f"DELETE FROM {table}")


def test_the_snapshot_table_is_a_cache_and_may_be_replaced(store):
    """ההפך המכוון: תמונת המצב *כן* ניתנת לעדכון, כי אפשר לבנות אותה מחדש."""
    update = MissionStateManager().apply([event("e1")])
    store.save_state(update.state)
    store.save_state(update.state)  # שמירה חוזרת אינה שגיאה

    assert store.load_state(FLIGHT) is not None


# --- the two versions live side by side ----------------------------------


def test_realtime_and_final_do_not_collide(store):
    """שלב 11 מוסיף שורות, לא מתקן אותן - ולכן אפשר להשוות ביניהן."""
    store.append_event(event("e1", info="גרסת זמן אמת."))
    store.append_event(event("e1", info="גרסה סופית.", version=VersionTag.FINAL))

    assert store.get_event("e1", FLIGHT).new_info == "גרסת זמן אמת."
    assert store.get_event("e1", FLIGHT, VersionTag.FINAL).new_info == "גרסה סופית."


def test_a_version_sees_only_its_own_timeline(store):
    store.append_event(event("e1"))
    store.append_event(event("e2", version=VersionTag.FINAL, minutes=1))

    assert [e.id for e in store.events_for_flight(FLIGHT)] == ["e1"]
    assert [e.id for e in store.events_for_flight(FLIGHT, VersionTag.FINAL)] == ["e2"]


# --- rebuilding state ----------------------------------------------------


SEQUENCE = [
    event("e1", info="קשר עין עם מטרה אלפא.", sources=["m01"], minutes=0),
    event(
        "e2",
        type="loss_of_contact",
        relation="updates",
        info="איבדנו קשר עם מטרה אלפא.",
        sources=["m02"],
        minutes=2,
    ),
    event("e3", relation="updates", info="חידשנו קשר.", sources=["m03"], minutes=4),
]
TIMES = {"m01": at(0), "m02": at(2), "m03": at(4)}


def live_run():
    update = MissionStateManager().apply(SEQUENCE, message_times=TIMES)
    return update


def test_a_rebuilt_state_equals_the_live_one(store):
    """הטענה שמאפשרת להתייחס לתמונת המצב כאל מטמון."""
    update = live_run()
    store.append_events(SEQUENCE)
    store.append_changes(update.changes)
    store.save_state(update.state)

    rebuilt = store.rebuild_state(FLIGHT)
    saved = store.load_state(FLIGHT)

    assert set(rebuilt.entities) == set(saved.entities)
    for entity_id, entity in saved.entities.items():
        other = rebuilt.entities[entity_id]
        assert other.status == entity.status
        assert set(other.attributes) == set(entity.attributes)
        for name, attribute in entity.attributes.items():
            assert other.attributes[name].current.value == attribute.current.value
            assert [s.value for s in other.attributes[name].history] == [
                s.value for s in attribute.history
            ]


def test_the_rebuilt_history_is_the_full_history_not_just_the_last_value(store):
    update = live_run()
    store.append_changes(update.changes)

    attribute = next(
        iter(store.rebuild_state(FLIGHT).entities.values())
    ).attributes["contact_status"]

    assert attribute.current.value == "חידשנו קשר."
    assert [s.value for s in attribute.history] == [
        "קשר עין עם מטרה אלפא.",
        "איבדנו קשר עם מטרה אלפא.",
    ]


@pytest.mark.parametrize(
    "minutes, expected",
    [
        (0, "קשר עין עם מטרה אלפא."),
        (2, "איבדנו קשר עם מטרה אלפא."),
        (3, "איבדנו קשר עם מטרה אלפא."),
        (4, "חידשנו קשר."),
        (99, "חידשנו קשר."),
    ],
)
def test_the_state_can_be_rebuilt_at_any_moment(store, minutes, expected):
    """"מה ידענו ב-06:03" - השאלה ששלב 9 נבנה עליה."""
    store.append_changes(live_run().changes)

    rebuilt = store.rebuild_state(FLIGHT, until=at(minutes))

    assert next(iter(rebuilt.entities.values())).attributes["contact_status"].current.value == (
        expected
    )


def test_rebuilding_before_anything_happened_gives_an_empty_state(store):
    store.append_changes(live_run().changes)

    assert store.rebuild_state(FLIGHT, until=at(-5)).entities == {}


def test_a_closed_entity_comes_back_closed(store):
    events = [
        event("e1", minutes=0),
        event("e2", relation="closes", info="סיימנו מעקב.", sources=["m02"], minutes=5),
    ]
    update = MissionStateManager().apply(
        events, message_times={"m01": at(0), "m02": at(5)}
    )
    store.append_changes(update.changes)

    entity = next(iter(store.rebuild_state(FLIGHT).entities.values()))
    assert entity.status.value == "closed"
    assert entity.closed_by_event_id == "e2"


def test_a_contradiction_survives_the_rebuild(store):
    events = [
        event("e1", minutes=0),
        event("e2", relation="contradicts", info="לא, היא לא שם.", sources=["m02"], minutes=2),
    ]
    update = MissionStateManager().apply(
        events, message_times={"m01": at(0), "m02": at(2)}
    )
    store.append_changes(update.changes)

    entity = next(iter(store.rebuild_state(FLIGHT).entities.values()))
    assert entity.attributes["contact_status"].contradicted is True


def test_changes_at_the_same_instant_replay_in_the_order_they_were_applied(store):
    """חותמת זמן לבדה אינה מספיקה, ולכן נשמר גם מספר סידורי."""
    same_time = [
        event("e1", info="ראשון.", sources=["m01"], minutes=0),
        event("e2", relation="updates", info="שני.", sources=["m02"], minutes=0),
    ]
    update = MissionStateManager().apply(
        same_time, message_times={"m01": at(0), "m02": at(0)}
    )
    store.append_changes(update.changes)

    rebuilt = store.rebuild_state(FLIGHT)
    assert next(iter(rebuilt.entities.values())).attributes["contact_status"].current.value == (
        "שני."
    )


def test_the_snapshot_can_be_thrown_away_and_rebuilt(store):
    """מטמון במובן המלא: מחיקתו אינה מאבדת דבר."""
    update = live_run()
    store.append_changes(update.changes)
    store.save_state(update.state)
    before = store.load_state(FLIGHT)

    store._connection.execute("DELETE FROM entity_snapshots")
    store._connection.commit()
    assert store.load_state(FLIGHT) is None

    store.save_state(store.rebuild_state(FLIGHT))
    after = store.load_state(FLIGHT)

    assert set(after.entities) == set(before.entities)


# --- the golden flights, at every checkpoint -----------------------------


def run_golden(flight: dict):
    """טיסת זהב דרך רכיבים 5-6, עם צילום מצב אחרי כל אירוע."""
    flight_id = flight["flight_id"]
    messages = [
        Message.model_validate({k: v for k, v in raw.items() if k != "expected"})
        for raw in flight["messages"]
    ]
    times = {m.id: m.timestamp for m in messages}
    tagged = [
        LLMEvent.model_validate(
            {
                "type": raw["expected"]["event_type"],
                "description": raw["expected"].get("new_info") or raw["text"],
                "entities": raw["expected"].get("entities", []),
                "new_info": raw["expected"].get("new_info", ""),
                "relation_to_previous": raw["expected"]["relation_to_previous"],
                "confidence": raw["expected"].get("confidence_min", 0.8),
                "source_ids": [raw["id"]],
            }
        )
        for raw in flight["messages"]
        if raw["expected"]["has_event"]
    ]
    bundle = ContextBundle(
        window_id=f"{flight_id}-w-all",
        flight_id=flight_id,
        messages=messages,
        window_close_reason=WindowCloseReason.FLIGHT_END,
    )
    validated = OutputValidator().validate(
        LLMResult(window_id=bundle.window_id, events=tagged, llm_model="tagged"), bundle
    )

    # הרצה חיה אירוע-אירוע, עם צילום מצב אחרי כל אחד.
    manager = MissionStateManager()
    state = manager.empty_state(flight_id)
    changes = []
    snapshots = []
    for single in sorted(
        validated.accepted, key=lambda e: min(times[s] for s in e.source_ids)
    ):
        update = manager.apply([single], state, message_times=times)
        state = update.state
        changes.extend(update.changes)
        snapshots.append(
            (
                min(times[s] for s in single.source_ids),
                {
                    entity_id: (
                        entity.status.value,
                        {
                            name: attribute.current.value
                            for name, attribute in entity.attributes.items()
                        },
                    )
                    for entity_id, entity in state.entities.items()
                },
            )
        )
    return validated.accepted, changes, snapshots


def flatten(state) -> dict:
    return {
        entity_id: (
            entity.status.value,
            {name: attr.current.value for name, attr in entity.attributes.items()},
        )
        for entity_id, entity in state.entities.items()
    }


def test_a_golden_flight_rebuilds_identically_at_every_point(golden_flight):
    """קריטריון המעבר: 100% התאמה בכל נקודות הבדיקה."""
    events, changes, snapshots = run_golden(golden_flight)
    flight_id = golden_flight["flight_id"]

    with SQLiteEventStore(":memory:") as store:
        store.append_events(events)
        store.append_changes(changes)

        for moment, live in snapshots:
            rebuilt = flatten(store.rebuild_state(flight_id, until=moment))
            assert rebuilt == live, f"{flight_id} diverges at {moment}"


def test_every_golden_event_keeps_its_sources_in_the_store(golden_flight):
    events, changes, _ = run_golden(golden_flight)
    flight_id = golden_flight["flight_id"]

    with SQLiteEventStore(":memory:") as store:
        store.append_events(events)

        for stored in store.events_for_flight(flight_id):
            sources = store.source_ids_of(stored.id, flight_id)
            assert sources
            assert store.events_from_message(sources[0], flight_id)


def test_a_golden_flight_survives_a_restart(golden_flight, tmp_path):
    """המאגר על הדיסק: מה שנכתב נקרא אחרי פתיחה מחדש."""
    events, changes, _ = run_golden(golden_flight)
    flight_id = golden_flight["flight_id"]
    path = tmp_path / "events.db"

    with SQLiteEventStore(path) as store:
        store.append_events(events)
        store.append_changes(changes)
        store.save_state(store.rebuild_state(flight_id))
        expected = flatten(store.load_state(flight_id))

    with SQLiteEventStore(path) as reopened:
        assert flatten(reopened.load_state(flight_id)) == expected
        assert flatten(reopened.rebuild_state(flight_id)) == expected
        assert reopened.count_events(flight_id) == len(events)

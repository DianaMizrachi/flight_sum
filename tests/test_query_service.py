"""The query service (שלב 9, קריטריון המעבר).

The stage asks for two things that are tested differently. *"Every query
type returns correct results"* is tested against the golden flights'
``checkpoints`` blocks - the hand tagging - so the answers are compared
with a human's, not with the system's own output. *"An internal interface
that exposes neither the LLM engine's structures nor the update
mechanism's"* is a claim about shape, so it is tested as one: the read
models are inspected for the fields they must not have.

The response-time target lives in ``test_query_performance.py``, because
it needs a long flight that the golden fixtures deliberately are not.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime, timedelta

import pytest
from golden_stores import load_flight

from aura.event_store.store import SQLiteEventStore
from aura.query_service import (
    EventFilter,
    QueryService,
    SummariesUnavailableError,
    at_least,
)
from aura.query_service.models import StateView, TimelineEntry
from aura.raw_store.sqlite_store import SQLiteRawTranscriptStore
from aura.schemas import (
    CertaintyLevel,
    EntityRef,
    EntityType,
    Event,
    InfoRelation,
    Message,
    ReconciliationAction,
    VersionTag,
)


@pytest.fixture
def loaded(golden_flight):
    """כל טיסת זהב, במאגרים אמיתיים, בלי מודל."""
    flight = load_flight(golden_flight)
    yield flight
    flight.close()


@pytest.fixture
def service(loaded):
    return QueryService(loaded.events, loaded.raw, loaded.summaries)


def expected_entities(checkpoint: dict) -> set[tuple[str, str, str]]:
    """הישויות שהתיוג מצפה להן בנקודת בדיקה: סוג, תווית, מצב.

    מזהי ישויות ושמות מאפיינים אינם נשווים כאן, מאותה סיבה המתועדת ב-
    ``test_state_manager_golden.py``: הפיקסצ'ר מניח מזהים בתעתיק יד
    ומאפיינים מחולצים שחוזה ה-``Event`` אינו נושא. מה שכן נשווה הוא
    מה שהמערכת באמת טוענת - אילו ישויות קיימות ומה מצבן.
    """
    return {
        (raw["type"], raw["label"], raw["status"])
        for raw in checkpoint["state"]["entities"].values()
    }


def produced_entities(view: StateView) -> set[tuple[str, str, str]]:
    return {(e.type, e.label, e.status) for e in view.entities}


# --- query 1: the state now ----------------------------------------------


def test_the_state_now_is_the_one_the_tagger_expected(loaded, service):
    """קריטריון המעבר, בשאילתה הראשונה."""
    view = service.current_state(loaded.flight_id)
    assert produced_entities(view) == expected_entities(loaded.checkpoints[-1])


def test_the_state_now_comes_from_the_snapshot(loaded, service):
    """הדרך המהירה היא ברירת המחדל, ו-``rebuilt`` אומר זאת."""
    view = service.current_state(loaded.flight_id)
    assert view.rebuilt is False


def test_the_state_is_the_same_without_the_snapshot(loaded, service):
    """אותה תשובה בשתי הדרכים - אחרת המטמון אינו מטמון.

    תמונת המצב נזרקת ישירות ב-SQL: הטבלה היא היחידה בשלב 7 שאינה
    append-only, בדיוק כדי שתהיה זריקה כזו אפשרית.
    """
    cached = service.current_state(loaded.flight_id)
    loaded.events._connection.execute(
        "DELETE FROM entity_snapshots WHERE flight_id = ?", (loaded.flight_id,)
    )
    loaded.events._connection.commit()

    rebuilt = service.current_state(loaded.flight_id)
    assert rebuilt.rebuilt is True
    assert produced_entities(rebuilt) == produced_entities(cached)


def test_a_closed_entity_names_the_event_that_closed_it(loaded, service):
    view = service.current_state(loaded.flight_id)
    for entity in view.closed:
        assert entity.closed_by_event_id, f"{entity.label} is closed by nothing"
        assert entity.closed_at is not None


def test_an_attribute_keeps_what_came_before_it(loaded, service):
    """עדכון אינו מוחק: ההיסטוריה מגיעה עד שכבת הקריאה.

    אם ישות קיבלה יותר מעדכון אחד על אותו מאפיין, הערך הקודם חייב
    להיות שם - זה מה שהופך "מה ידענו קודם" לשאלה שאפשר לענות עליה.
    """
    view = service.current_state(loaded.flight_id)
    updated = [
        attribute
        for entity in view.entities
        for attribute in entity.attributes
        if attribute.history
    ]
    for attribute in updated:
        assert all(past.value for past in attribute.history)
        assert attribute.current.updated_at >= attribute.history[-1].updated_at


# --- query 2: the state at a past moment ---------------------------------


def test_the_state_at_every_checkpoint_matches_the_tagging(loaded, service):
    """קריטריון המעבר, בנקודות הזמן שהתיוג קבע."""
    for checkpoint in loaded.checkpoints:
        at = datetime.fromisoformat(checkpoint["timestamp"])
        view = service.state_at(loaded.flight_id, at)
        assert produced_entities(view) == expected_entities(checkpoint), (
            f"{loaded.flight_id} at {checkpoint['at_message_id']}"
        )


def test_the_state_before_the_flight_began_is_empty(loaded, service):
    first = min(loaded.times.values())
    view = service.state_at(loaded.flight_id, first - timedelta(hours=1))
    assert view.entities == ()
    assert view.rebuilt is True


def test_a_past_state_answers_as_of_the_moment_asked(loaded, service):
    """``as_of`` הוא הרגע שנשאל, ולא הרגע שבו קרה השינוי האחרון לפניו.

    קורא שמבקש את המצב ב-06:20 מקבל תשובה על 06:20. אם ``as_of`` היה
    חוזר כזמן השינוי האחרון, שתי שאילתות לשתי נקודות זמן שונות היו
    מחזירות אותו ``as_of`` - ונראות כאותה תשובה.
    """
    at = datetime.fromisoformat(loaded.checkpoints[0]["timestamp"]) + timedelta(
        seconds=30
    )
    assert service.state_at(loaded.flight_id, at).as_of == at


def test_the_past_does_not_change_when_the_flight_goes_on(loaded, service):
    """המצב בנקודה מוקדמת זהה בין אם נשאל אז ובין אם נשאל בסוף."""
    early = datetime.fromisoformat(loaded.checkpoints[0]["timestamp"])
    once = service.state_at(loaded.flight_id, early)
    again = service.state_at(loaded.flight_id, early)
    assert produced_entities(once) == produced_entities(again)
    assert produced_entities(once) == expected_entities(loaded.checkpoints[0])


# --- query 3: the timeline -----------------------------------------------


def test_the_timeline_holds_every_event_the_tagger_marked(loaded, service):
    marked = [
        raw["id"] for raw in loaded.fixture["messages"] if raw["expected"]["has_event"]
    ]
    entries = service.timeline(loaded.flight_id)
    covered = {sid for entry in entries for sid in entry.source_ids}
    assert covered == set(marked)


def test_the_timeline_is_in_the_order_the_sentences_were_said(loaded, service):
    entries = service.timeline(loaded.flight_id)
    assert [e.at for e in entries] == sorted(e.at for e in entries)
    for entry in entries:
        assert entry.spoken_at == min(loaded.times[sid] for sid in entry.source_ids)


def test_the_timeline_carries_the_type_and_relation_from_the_tagging(loaded, service):
    expected = {
        raw["id"]: (raw["expected"]["event_type"], raw["expected"]["relation_to_previous"])
        for raw in loaded.fixture["messages"]
        if raw["expected"]["has_event"]
    }
    for entry in service.timeline(loaded.flight_id):
        for sid in entry.source_ids:
            assert (entry.type, entry.relation) == expected[sid]


def test_filtering_by_type_keeps_only_that_type(loaded, service):
    types = {entry.type for entry in service.timeline(loaded.flight_id)}
    for event_type in types:
        found = service.timeline(loaded.flight_id, EventFilter.build(types=event_type))
        assert found, f"no events of type {event_type}"
        assert {entry.type for entry in found} == {event_type}


def test_filtering_by_relation_keeps_only_that_relation(loaded, service):
    relations = {entry.relation for entry in service.timeline(loaded.flight_id)}
    for relation in relations:
        found = service.timeline(
            loaded.flight_id, EventFilter.build(relations=relation)
        )
        assert {entry.relation for entry in found} == {relation}


def test_filtering_by_entity_label_finds_that_entity(loaded, service):
    state = service.current_state(loaded.flight_id)
    for entity in state.entities:
        found = service.timeline(
            loaded.flight_id, EventFilter.build(entity=entity.label)
        )
        assert found, f"no events mention {entity.label}"
        for entry in found:
            assert any(ref.label == entity.label for ref in entry.entities)


def test_filtering_by_entity_id_finds_the_same_events_as_its_label(loaded, service):
    """הרגרסיה: מזהה הישות אינו כתוב באירוע השמור.

    מנהל המצב מקצה את המזהה ואינו כותב אותו בחזרה ל-``EntityRef``,
    ולכן סינון תמים לפי המזהה היה מחזיר ריק דווקא לישויות שהטיסה
    חידשה - כלומר לכולן. השירות פותר מזהה לתווית לפני ההשוואה.
    """
    state = service.current_state(loaded.flight_id)
    for entity in state.entities:
        by_id = service.timeline(loaded.flight_id, EventFilter.build(entity=entity.id))
        by_label = service.timeline(
            loaded.flight_id, EventFilter.build(entity=entity.label)
        )
        assert [e.event_id for e in by_id] == [e.event_id for e in by_label]
        assert by_id, f"{entity.id} matched nothing"


def test_an_unknown_entity_matches_nothing(loaded, service):
    assert service.timeline(loaded.flight_id, EventFilter.build(entity="ישות שאינה")) == []


def test_filtering_by_certainty_uses_the_level_the_state_records(loaded, service):
    """אותו מיפוי ביטחון->ודאות שנכתב למצב, לא מיפוי שני.

    ``confidence_min`` בפיקסצ'רים הוא 0.8, שהוא מתחת לסף "מאומת"
    (0.85) ומעל לסף "מדווח" (0.5) - ולכן התיוג כולו "מדווח", וזו
    בדיקה על הסיווג ולא רק על הסינון.
    """
    verified = service.timeline(
        loaded.flight_id, EventFilter.build(certainty=CertaintyLevel.VERIFIED)
    )
    reported = service.timeline(
        loaded.flight_id, EventFilter.build(certainty=CertaintyLevel.REPORTED)
    )
    assert verified == []
    assert len(reported) == len(service.timeline(loaded.flight_id))
    assert {entry.certainty for entry in reported} == {"reported"}


def test_at_least_reported_includes_verified_too(loaded, service):
    wide = service.timeline(
        loaded.flight_id,
        EventFilter.build(certainty=at_least(CertaintyLevel.REPORTED)),
    )
    assert len(wide) == len(service.timeline(loaded.flight_id))


def test_a_time_window_keeps_what_was_said_inside_it(loaded, service):
    entries = service.timeline(loaded.flight_id)
    assert len(entries) >= 2
    start, end = entries[1].at, entries[-1].at

    inside = service.timeline(loaded.flight_id, EventFilter.build(start=start, end=end))
    assert [e.event_id for e in inside] == [e.event_id for e in entries[1:]]


def test_a_time_window_has_inclusive_edges(loaded, service):
    entries = service.timeline(loaded.flight_id)
    only_first = service.timeline(
        loaded.flight_id, EventFilter.build(start=entries[0].at, end=entries[0].at)
    )
    assert [e.event_id for e in only_first] == [entries[0].event_id]


def test_a_limit_keeps_the_earliest_events(loaded, service):
    entries = service.timeline(loaded.flight_id)
    limited = service.timeline(loaded.flight_id, EventFilter.build(limit=2))
    assert [e.event_id for e in limited] == [e.event_id for e in entries[:2]]


def test_a_single_event_can_be_fetched_by_id(loaded, service):
    first = service.timeline(loaded.flight_id)[0]
    assert service.event(first.event_id, loaded.flight_id) == first


def test_an_unknown_event_id_is_none_not_an_error(loaded, service):
    assert service.event("no-such-event", loaded.flight_id) is None


# --- query 4: from an event back to its sentences ------------------------


def test_every_event_resolves_to_the_sentences_it_came_from(loaded, service):
    """התכנית: "חייבת להצליח תמיד, לכל אירוע שמור"."""
    for entry in service.timeline(loaded.flight_id):
        sentences = service.sources_of(entry.event_id, loaded.flight_id)
        assert sentences, f"{entry.event_id} resolved to nothing"
        assert [s.message_id for s in sentences] == sorted(
            entry.source_ids, key=lambda sid: loaded.times[sid]
        )
        for sentence in sentences:
            assert sentence.missing is False
            assert sentence.text == loaded.message(sentence.message_id).text
            assert sentence.source == loaded.message(sentence.message_id).source


def test_the_sentences_of_an_event_are_the_ones_the_tagger_marked(loaded, service):
    for entry in service.timeline(loaded.flight_id):
        for sentence in service.sources_of(entry.event_id, loaded.flight_id):
            tagged = next(
                raw
                for raw in loaded.fixture["messages"]
                if raw["id"] == sentence.message_id
            )
            assert tagged["expected"]["has_event"] is True


def test_a_sentence_resolves_forward_to_its_events(loaded, service):
    for entry in service.timeline(loaded.flight_id):
        for sid in entry.source_ids:
            back = service.events_from_sentence(sid, loaded.flight_id)
            assert entry.event_id in [e.event_id for e in back]


def test_the_transcript_itself_can_be_read_for_a_window(loaded, service):
    sentences = service.sentences_of_flight(loaded.flight_id)
    assert [s.message_id for s in sentences] == [m.id for m in loaded.messages]
    assert all(s.text for s in sentences)


# --- query 5: the summary -------------------------------------------------


def test_the_latest_summary_is_the_last_version_written(loaded, service):
    summary = service.summary(loaded.flight_id)
    assert summary is not None
    assert summary.version_number == len(loaded.accepted)
    assert summary.trigger == "post_flight"


def test_the_summary_names_every_active_entity(loaded, service):
    """הקריטריון של שלב 8, נבדק דרך שכבת הקריאה."""
    summary = service.summary(loaded.flight_id)
    state = service.current_state(loaded.flight_id)
    for entity in state.active:
        assert entity.label in summary.text


def test_the_summary_up_to_a_moment_is_the_one_in_force_then(loaded, service):
    """תחקור של נקודת זמן רואה את ההקשר שהיה זמין אז.

    זו הסיבה שהשאילתה קיימת: התקציר האחרון כולל מידע שנודע אחר כך,
    והצגתו כהקשר של רגע מוקדם הופכת תחקור לאנכרוניזם.
    """
    for index, event in enumerate(loaded.accepted, start=1):
        spoken = loaded.spoken_at(event)
        summary = service.summary(loaded.flight_id, at=spoken)
        assert summary is not None
        assert summary.version_number == index
        assert summary.covers_up_to == spoken


def test_a_summary_written_later_is_not_returned_for_an_earlier_moment(loaded, service):
    first = loaded.spoken_at(loaded.accepted[0])
    latest = service.summary(loaded.flight_id)
    early = service.summary(loaded.flight_id, at=first)
    if len(loaded.accepted) > 1:
        assert early.version_number < latest.version_number


def test_before_the_first_summary_there_is_none(loaded, service):
    first = min(loaded.times.values())
    assert service.summary(loaded.flight_id, at=first - timedelta(hours=1)) is None


def test_the_summary_history_is_every_version_in_order(loaded, service):
    history = service.summary_history(loaded.flight_id)
    assert [s.version_number for s in history] == list(
        range(1, len(loaded.accepted) + 1)
    )


def test_a_summary_declares_the_sentence_it_covers_up_to(loaded, service):
    for summary in service.summary_history(loaded.flight_id):
        assert summary.covers_up_to_message_id in loaded.times
        assert summary.covers_up_to == loaded.times[summary.covers_up_to_message_id]


def test_asking_for_a_summary_without_a_summary_store_is_an_error(loaded):
    """תצורה חסרה אינה "אין תקציר": שתיהן היו מוחזרות כ-``None``."""
    bare = QueryService(loaded.events, loaded.raw)
    with pytest.raises(SummariesUnavailableError):
        bare.summary(loaded.flight_id)


# --- versions -------------------------------------------------------------


def test_without_a_final_version_the_default_is_the_realtime_one(loaded, service):
    assert service.resolve_version(loaded.flight_id) is VersionTag.REALTIME


def test_an_explicitly_requested_version_is_honoured_even_when_empty(loaded, service):
    """"הגרסה הסופית ריקה" היא תשובה, לא סיבה להחזיר טיוטה בשקט."""
    assert (
        service.resolve_version(loaded.flight_id, VersionTag.FINAL)
        is VersionTag.FINAL
    )
    assert service.timeline(loaded.flight_id, version=VersionTag.FINAL) == []


def test_the_diff_says_there_is_nothing_to_compare_yet(loaded, service):
    diff = service.version_diff(loaded.flight_id)
    assert diff.final_exists is False
    assert diff.realtime_count == len(loaded.accepted)
    assert diff.identical is False


# --- the boundary: what a read model must not expose ---------------------


def test_the_timeline_does_not_leak_the_engine_structures():
    """קריטריון המעבר: ממשק שאינו חושף מבנים של מנוע ה-LLM.

    ``window_id`` הוא תוצר של כללי הקיבוץ של רכיב 3 ולא עובדה על
    הטיסה, ו-``confidence`` הוא מספר שהמודל פלט - תצוגתו כ-"0.82"
    מבטיחה דיוק שלא נמדד. במקומו מגיעה רמת הוודאות, אותה שלישייה
    שהמצב עצמו רושם.
    """
    names = {field.name for field in fields(TimelineEntry)}
    assert "window_id" not in names
    assert "confidence" not in names
    assert "certainty" in names


def test_no_query_returns_a_stored_contract_object(loaded, service):
    """מה שחוזר הוא מודל קריאה, לא ``Event`` ולא ``StateChange``."""
    entries = service.timeline(loaded.flight_id)
    assert entries and not any(isinstance(entry, Event) for entry in entries)
    assert all(isinstance(entry, TimelineEntry) for entry in entries)

    view = service.current_state(loaded.flight_id)
    assert isinstance(view, StateView)
    assert all(not hasattr(entity, "model_dump") for entity in view.entities)


def test_the_read_models_are_immutable(loaded, service):
    """תוצאת שאילתה אינה דרך לכתוב למערכת."""
    entry = service.timeline(loaded.flight_id)[0]
    with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError
        entry.type = "changed"  # type: ignore[misc]


# --- cross-flight behaviour ----------------------------------------------


def base_time() -> datetime:
    return datetime(2026, 9, 1, 6, 0, tzinfo=UTC)


def a_message(flight_id: str, message_id: str, minute: int) -> Message:
    at = base_time() + timedelta(minutes=minute)
    return Message(
        id=message_id,
        flight_id=flight_id,
        timestamp=at,
        ingested_at=at,
        source="operator",
        text=f"{message_id} נאמר",
    )


def an_event(
    flight_id: str,
    event_id: str,
    source_ids: list[str],
    *,
    created_at: datetime | None = None,
    version: VersionTag = VersionTag.REALTIME,
    event_type: str = "contact_with_target",
    label: str = "מטרה אלפא",
    confidence: float = 0.9,
    reconciles: str | None = None,
    action: ReconciliationAction | None = None,
    reason: str | None = None,
) -> Event:
    return Event(
        id=event_id,
        flight_id=flight_id,
        window_id=f"{flight_id}-w1",
        type=event_type,
        description=f"{event_id} תיאור",
        entities=[EntityRef(type=EntityType.TARGET, label=label)],
        new_info=f"{event_id} מידע",
        relation_to_previous=InfoRelation.NEW,
        confidence=confidence,
        source_ids=source_ids,
        version=version,
        created_at=created_at if created_at is not None else base_time(),
        reconciles_event_id=reconciles,
        reconciliation_action=action,
        reconciliation_reason=reason,
    )


@pytest.fixture
def two_flights():
    raw = SQLiteRawTranscriptStore(":memory:")
    events = SQLiteEventStore(":memory:")
    raw.append_many(
        [
            a_message("flight-a", "m01", 0),
            a_message("flight-a", "m02", 5),
            a_message("flight-b", "m01", 2),
        ]
    )
    events.append_events(
        [
            an_event("flight-a", "a-evt-1", ["m01"]),
            an_event("flight-a", "a-evt-2", ["m02"], event_type="malfunction"),
            an_event("flight-b", "b-evt-1", ["m01"]),
        ]
    )
    yield QueryService(events, raw)
    raw.close()
    events.close()


def test_a_timeline_can_span_flights_in_spoken_order(two_flights):
    entries = two_flights.timeline()
    assert [e.event_id for e in entries] == ["a-evt-1", "b-evt-1", "a-evt-2"]


def test_a_cross_flight_timeline_can_be_narrowed_to_some_flights(two_flights):
    entries = two_flights.timeline(filter=EventFilter.build(flights="flight-b"))
    assert [e.event_id for e in entries] == ["b-evt-1"]


def test_the_same_message_id_in_two_flights_keeps_its_own_time(two_flights):
    """מזהה משפט ייחודי בתוך הטיסה ולא במאגר כולו.

    ``m01`` קיים בשתי הטיסות בשני זמנים שונים. אם זמני האמירה היו
    נפתרים ממילון גלובלי, אחד מהם היה דורס את השני - והאירוע של טיסה
    אחת היה מקבל את הזמן של השנייה.
    """
    entries = {e.event_id: e.spoken_at for e in two_flights.timeline()}
    assert entries["a-evt-1"] == base_time()
    assert entries["b-evt-1"] == base_time() + timedelta(minutes=2)


# --- spoken time versus processing time ----------------------------------


@pytest.fixture
def processed_out_of_order():
    """אירועים שסדר העיבוד שלהם הפוך לסדר האמירה.

    זה המצב הרגיל כשטיסה מנותחת בדיעבד, ולא קצה מיוחד: כל האירועים
    נבנים באותה דקה, ולכן ``created_at`` אינו מסדר אותם בכלל.
    """
    raw = SQLiteRawTranscriptStore(":memory:")
    events = SQLiteEventStore(":memory:")
    raw.append_many(
        [a_message("flight-c", "m01", 0), a_message("flight-c", "m02", 30)]
    )
    processed = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    events.append_events(
        [
            # המשפט המאוחר נעובד ראשון.
            an_event("flight-c", "c-evt-late", ["m02"], created_at=processed),
            an_event(
                "flight-c",
                "c-evt-early",
                ["m01"],
                created_at=processed + timedelta(seconds=5),
            ),
        ]
    )
    yield QueryService(events, raw)
    raw.close()
    events.close()


def test_the_timeline_follows_when_it_was_said_not_when_it_was_processed(
    processed_out_of_order,
):
    entries = processed_out_of_order.timeline("flight-c")
    assert [e.event_id for e in entries] == ["c-evt-early", "c-evt-late"]
    assert entries[0].recorded_at > entries[1].recorded_at


def test_a_time_window_uses_spoken_time(processed_out_of_order):
    """הרגרסיה שבגללה השירות פותר זמנים מהמאגר הגולמי.

    סינון על ``created_at`` היה מחזיר כאן את שני האירועים או אף אחד,
    כי שניהם נבנו באותו רגע. הטווח המבוקש הוא בזמן הטיסה.
    """
    window = processed_out_of_order.timeline(
        "flight-c",
        EventFilter.build(
            start=base_time() + timedelta(minutes=10),
            end=base_time() + timedelta(minutes=40),
        ),
    )
    assert [e.event_id for e in window] == ["c-evt-late"]


def test_an_event_whose_sentence_is_missing_is_not_placed_in_a_window():
    """אירוע בלי זמן אמירה אינו נכנס לטווח מבוקש.

    החלופה היא להחזיר אותו בכל טווח, וטווח שמחזיר גם את מה שאינו ידוע
    שהוא בתוכו אינו טווח. הוא כן מופיע בציר הזמן הלא-מסונן, לפי זמן
    העיבוד, כדי שלא ייעלם בשקט.
    """
    raw = SQLiteRawTranscriptStore(":memory:")
    events = SQLiteEventStore(":memory:")
    events.append_events([an_event("flight-d", "d-evt-1", ["gone"])])
    service = QueryService(events, raw)

    assert [e.event_id for e in service.timeline("flight-d")] == ["d-evt-1"]
    assert service.timeline("flight-d")[0].spoken_at is None
    assert service.timeline("flight-d", EventFilter.build(start=base_time())) == []

    sentences = service.sources_of("d-evt-1", "flight-d")
    assert len(sentences) == 1
    assert sentences[0].missing is True
    assert sentences[0].text is None

    raw.close()
    events.close()


# --- the version diff -----------------------------------------------------


@pytest.fixture
def reconciled():
    """טיסה שיש לה גם גרסת זמן אמת וגם גרסה סופית.

    מעבר התיקון (שלב 11) טרם נכתב, ולכן הגרסה הסופית כאן נבנית ביד -
    וזו בדיוק הנקודה: שאילתת ההבדלים מוגדרת ונבדקת לפי החוזה שהגרסה
    הסופית תישא, כך שלא יישאר מה להוסיף בשכבת הקריאה בשלב 11.
    """
    raw = SQLiteRawTranscriptStore(":memory:")
    events = SQLiteEventStore(":memory:")
    raw.append_many([a_message("flight-r", f"m0{i}", i) for i in range(1, 6)])
    events.append_events(
        [
            an_event("flight-r", "rt-1", ["m01"]),
            an_event("flight-r", "rt-2", ["m02"]),
            an_event("flight-r", "rt-3", ["m03"]),
            an_event("flight-r", "rt-4", ["m04"]),
        ]
    )
    events.append_events(
        [
            an_event(
                "flight-r",
                "fi-1",
                ["m01"],
                version=VersionTag.FINAL,
                reconciles="rt-1",
                action=ReconciliationAction.CONFIRMED,
            ),
            an_event(
                "flight-r",
                "fi-2",
                ["m02"],
                version=VersionTag.FINAL,
                reconciles="rt-2",
                action=ReconciliationAction.CORRECTED,
                reason="הישות הייתה שגויה",
            ),
            an_event(
                "flight-r",
                "fi-3",
                ["m03"],
                version=VersionTag.FINAL,
                reconciles="rt-3",
                action=ReconciliationAction.CANCELLED,
                reason="לא קרה",
            ),
            an_event(
                "flight-r",
                "fi-new",
                ["m05"],
                version=VersionTag.FINAL,
                action=ReconciliationAction.ADDED,
                reason="פוספס בזמן אמת",
            ),
        ]
    )
    yield QueryService(events, raw)
    raw.close()
    events.close()


def test_the_final_version_is_the_default_once_it_exists(reconciled):
    """ברירת המחדל מהתכנית: תחקור על הגרסה הסופית, לא על הטיוטה."""
    assert reconciled.resolve_version("flight-r") is VersionTag.FINAL
    assert {e.event_id for e in reconciled.timeline("flight-r")} == {
        "fi-1",
        "fi-2",
        "fi-3",
        "fi-new",
    }


def test_the_realtime_version_is_still_reachable_on_request(reconciled):
    entries = reconciled.timeline("flight-r", version=VersionTag.REALTIME)
    assert {e.event_id for e in entries} == {"rt-1", "rt-2", "rt-3", "rt-4"}


def test_the_diff_classifies_what_the_pass_did(reconciled):
    diff = reconciled.version_diff("flight-r")
    assert diff.final_exists is True
    assert diff.realtime_count == 4
    assert diff.final_count == 4
    assert [p.final.event_id for p in diff.confirmed] == ["fi-1"]
    assert [p.final.event_id for p in diff.corrected] == ["fi-2"]
    assert [p.final.event_id for p in diff.cancelled] == ["fi-3"]
    assert [p.final.event_id for p in diff.added] == ["fi-new"]


def test_a_corrected_event_keeps_both_sides_and_the_reason(reconciled):
    pair = reconciled.version_diff("flight-r").corrected[0]
    assert pair.realtime.event_id == "rt-2"
    assert pair.final.event_id == "fi-2"
    assert pair.reason == "הישות הייתה שגויה"


def test_an_added_event_has_no_realtime_side(reconciled):
    pair = reconciled.version_diff("flight-r").added[0]
    assert pair.realtime is None
    assert pair.final.event_id == "fi-new"


def test_a_realtime_event_the_pass_never_mentions_is_reported(reconciled):
    """שתיקה אינה ביטול, והבדל ביניהן חשוב לתחקור.

    ``rt-4`` לא אושר, לא תוקן ולא בוטל - הגרסה הסופית פשוט אינה
    מזכירה אותו. סיווגו כ"בוטל" היה שם בפי מעבר התיקון טענה שלא טען.
    """
    diff = reconciled.version_diff("flight-r")
    assert [p.realtime.event_id for p in diff.unmatched] == ["rt-4"]
    assert diff.identical is False


def test_the_changed_events_are_everything_but_the_confirmed_ones(reconciled):
    diff = reconciled.version_diff("flight-r")
    assert {p.final.event_id for p in diff.changed} == {"fi-2", "fi-3", "fi-new"}


def test_an_unclassified_final_event_is_read_from_its_link():
    """גרסה סופית בלי סיווג מפורש: מקושר = תיקון, לא מקושר = תוספת."""
    raw = SQLiteRawTranscriptStore(":memory:")
    events = SQLiteEventStore(":memory:")
    raw.append_many([a_message("flight-u", "m01", 0), a_message("flight-u", "m02", 1)])
    events.append_events([an_event("flight-u", "rt-1", ["m01"])])
    events.append_events(
        [
            an_event("flight-u", "fi-1", ["m01"], version=VersionTag.FINAL, reconciles="rt-1"),
            an_event("flight-u", "fi-2", ["m02"], version=VersionTag.FINAL),
        ]
    )
    diff = QueryService(events, raw).version_diff("flight-u")
    assert [p.final.event_id for p in diff.corrected] == ["fi-1"]
    assert [p.final.event_id for p in diff.added] == ["fi-2"]
    raw.close()
    events.close()


def test_a_pass_that_changed_nothing_says_so():
    raw = SQLiteRawTranscriptStore(":memory:")
    events = SQLiteEventStore(":memory:")
    raw.append_many([a_message("flight-s", "m01", 0)])
    events.append_events([an_event("flight-s", "rt-1", ["m01"])])
    events.append_events(
        [
            an_event(
                "flight-s",
                "fi-1",
                ["m01"],
                version=VersionTag.FINAL,
                reconciles="rt-1",
                action=ReconciliationAction.CONFIRMED,
            )
        ]
    )
    diff = QueryService(events, raw).version_diff("flight-s")
    assert diff.identical is True
    assert diff.changed == ()
    raw.close()
    events.close()


# --- filters in isolation -------------------------------------------------


def test_a_filter_with_nothing_set_filters_nothing(loaded, service):
    assert EventFilter().empty is True
    assert service.timeline(loaded.flight_id, EventFilter()) == service.timeline(
        loaded.flight_id
    )


def test_a_filter_accepts_one_value_or_many():
    single = EventFilter.build(types="malfunction")
    many = EventFilter.build(types=["malfunction"])
    assert single == many


def test_at_least_orders_certainty_from_strict_to_loose():
    assert at_least(CertaintyLevel.VERIFIED) == {CertaintyLevel.VERIFIED}
    assert at_least(CertaintyLevel.UNCERTAIN) == {
        CertaintyLevel.VERIFIED,
        CertaintyLevel.REPORTED,
        CertaintyLevel.UNCERTAIN,
    }


def test_an_unknown_flight_answers_empty_rather_than_failing(service):
    assert service.timeline("no-such-flight") == []
    assert service.current_state("no-such-flight").entities == ()
    assert service.version_diff("no-such-flight").final_exists is False

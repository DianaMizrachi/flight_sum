"""The full 1→6 pipeline (שלב 6, נקודת הביקורת המרכזית).

The plan calls this the project's main checkpoint, and the claim being
tested is one sentence: a sentence handed to ingestion ends up changing
mission state, through every component in between, with nothing skipped.

So these tests drive the pipeline the way the service will - one
sentence at a time, through a mock model - and then check the far end.
The model is mocked because the pipeline's correctness is about *wiring*,
not about whether a particular model is any good; that question belongs
to stage 4's scoring and has its own fixtures.

The failure tests matter as much as the happy path. A model call can time
out, and the sentences are already durable by then (component 2 runs
before the queue), so one unanalysable window must not take down the
ingest path for every other flight.
"""

import json

import pytest

from aura.ingestion import IngestionService
from aura.llm_engine import EventDetectionEngine, LLMTimeoutError, MockLLMClient
from aura.pipeline import FlightPipeline
from aura.raw_store import RawStoreDedupIndex, SQLiteRawTranscriptStore
from aura.schemas import EntityStatus, MessageStatus

FLIGHT = "pipe-01"


def answer(*events: dict) -> str:
    return json.dumps({"events": list(events)}, ensure_ascii=False)


def contact(source_ids, label="מטרה אלפא", relation="new", info="קשר עין עם מטרה אלפא."):
    return {
        "type": "contact_with_target",
        "description": info,
        "entities": [{"id": None, "type": "target", "label": label}],
        "new_info": info,
        "relation_to_previous": relation,
        "confidence": 0.9,
        "source_ids": list(source_ids),
    }


# דובר אחד ומרווחים קצרים, כדי שהשלושה ייפלו לחלון אחד: כללי הסגירה של
# רכיב 3 (שינוי דובר, שתיקה של 20 שניות) נבדקים אצלו, ולא כאן. כאן
# נבדקת ההשחלה בין הרכיבים, וחלון אחד הופך את הספירות לקריאות.
SENTENCES = [
    ("p-m01", "2026-09-01T06:00:00Z", "operator", "בקרה, קשר עין עם מטרה אלפא."),
    ("p-m02", "2026-09-01T06:00:08Z", "operator", "בקרה, ממשיכים מעקב."),
    ("p-m03", "2026-09-01T06:00:16Z", "operator", "בקרה, המטרה נעה מזרחה."),
]


@pytest.fixture
def store():
    with SQLiteRawTranscriptStore(":memory:") as opened:
        yield opened


def build(store, responses) -> FlightPipeline:
    service = IngestionService(store=store, dedup_index=RawStoreDedupIndex(store))
    engine = EventDetectionEngine(MockLLMClient(responses))
    return FlightPipeline(service, engine)


def feed(pipeline, sentences=SENTENCES, flight_id=FLIGHT):
    return [
        pipeline.ingest(
            {
                "id": message_id,
                "flight_id": flight_id,
                "timestamp": timestamp,
                "source": source,
                "channel": "command",
                "text": text,
            }
        )
        for message_id, timestamp, source, text in sentences
    ]


# --- the checkpoint ------------------------------------------------------


def test_a_sentence_becomes_mission_state(store):
    """הטענה המרכזית של שלב 6, מקצה לקצה."""
    pipeline = build(store, answer(contact(["p-m01"])))

    feed(pipeline)
    pipeline.close_flight(FLIGHT)

    state = pipeline.state_of(FLIGHT)
    assert state is not None
    entity = next(iter(state.entities.values()))
    assert entity.label == "מטרה אלפא"
    assert entity.attributes["contact_status"].current.value == "קשר עין עם מטרה אלפא."


def test_the_sentences_are_durable_before_any_analysis(store):
    """"שמירה לפני עיבוד": המאגר מלא עוד לפני שהמודל נקרא."""
    pipeline = build(store, answer())

    feed(pipeline)

    assert store.count(FLIGHT) == len(SENTENCES)
    assert pipeline.state_of(FLIGHT) is None, "nothing is analysed until a window closes"


def test_every_accepted_sentence_reaches_the_queue_and_the_windows(store):
    pipeline = build(store, answer(contact(["p-m01"])))

    results = feed(pipeline)
    run = pipeline.close_flight(FLIGHT)

    assert all(result.status is MessageStatus.ACCEPTED for result in results)
    seen = {m.id for window in run.windows for m in window.bundle.messages}
    assert seen == {message_id for message_id, *_ in SENTENCES}


def test_the_event_points_back_at_the_sentence_it_came_from(store):
    """ייחוס שורד את כל הצינור: מאירוע חזרה לתמלול במאגר."""
    pipeline = build(store, answer(contact(["p-m01"])))

    feed(pipeline)
    run = pipeline.close_flight(FLIGHT)

    event = run.events[0]
    assert event.source_ids == ["p-m01"]
    assert store.get("p-m01", flight_id=FLIGHT) is not None


def test_state_changes_are_recorded_for_stage_seven(store):
    pipeline = build(store, answer(contact(["p-m01"])))

    feed(pipeline)
    run = pipeline.close_flight(FLIGHT)

    assert run.changes
    assert {change.entity_id for change in run.changes} <= set(
        pipeline.state_of(FLIGHT).entities
    )
    assert all(change.flight_id == FLIGHT for change in run.changes)


def test_validation_still_refuses_bad_events_inside_the_pipeline(store):
    """רכיב 5 אינו מדולג כשהוא רץ בתוך הצינור."""
    invented = contact(["p-m99"])  # משפט שאינו בחלון
    pipeline = build(store, answer(invented))

    feed(pipeline)
    run = pipeline.close_flight(FLIGHT)

    assert run.events == []
    assert sum(window.rejected_count for window in run.windows) == 1
    assert pipeline.state_of(FLIGHT) is None


def test_the_pipeline_carries_rejections_not_just_a_count(store):
    """"לבדיקה ידנית" מחייב שיישאר מה לבדוק.

    רכיב 5 שומר את הפלט הגולמי של כל אירוע שנדחה; צינור שמעביר הלאה
    רק מונה מאבד בדיוק את זה, ואי אפשר לדעת *מה* נדחה ולמה.
    """
    pipeline = build(store, answer(contact(["p-m99"])))

    feed(pipeline)
    run = pipeline.close_flight(FLIGHT)

    assert run.rejected, "the rejected payloads must survive the pipeline"
    rejection = run.rejected[0]
    assert rejection.reason == "source_id_not_in_window"
    assert rejection.raw["source_ids"] == ["p-m99"]
    assert rejection.detail
    assert run.windows[0].rejected_count == len(run.windows[0].rejected)


def test_two_flights_keep_separate_state(store):
    pipeline = build(store, answer(contact(["p-m01"])))

    feed(pipeline)
    feed(
        pipeline,
        [("q-m01", "2026-09-01T07:00:00Z", "operator", "בקרה, קשר עם מטרה ברבו.")],
        flight_id="pipe-02",
    )
    pipeline.close_flight(FLIGHT)
    pipeline.close_flight("pipe-02")

    assert pipeline.state_of(FLIGHT) is not pipeline.state_of("pipe-02")
    assert pipeline.state_of(FLIGHT).flight_id == FLIGHT


def test_state_accumulates_across_windows(store):
    """חלון שני מעדכן את מה שהראשון קבע, ולא מתחיל מאפס."""
    first = answer(contact(["p-m01"]))
    second = answer(
        contact(["p-m03"], relation="updates", info="איבדנו קשר עם מטרה אלפא.")
    )
    pipeline = build(store, [first, second])

    feed(pipeline)
    pipeline.close_flight(FLIGHT)
    feed(
        pipeline,
        [("p-m04", "2026-09-01T06:05:00Z", "operator", "בקרה, איבדנו קשר.")],
    )
    pipeline.close_flight(FLIGHT)

    state = pipeline.state_of(FLIGHT)
    assert len(state.entities) == 1, "the second window must not create a second target"
    attribute = next(iter(state.entities.values())).attributes["contact_status"]
    assert attribute.current.value == "איבדנו קשר עם מטרה אלפא."
    assert attribute.history


def test_an_entity_id_from_an_earlier_window_is_accepted(store):
    """רכיב 5 מקבל את מצב הטיסה מהצינור, ולכן מזהה ישות מוכר אינו נדחה."""
    pipeline = build(store, answer(contact(["p-m01"])))
    feed(pipeline)
    pipeline.close_flight(FLIGHT)
    known_id = next(iter(pipeline.state_of(FLIGHT).entities))

    referring = dict(contact(["p-m04"], relation="updates", info="המטרה עצרה."))
    referring["entities"] = [{"id": known_id, "type": "target", "label": "מטרה אלפא"}]
    pipeline.engine.client._responses = answer(referring)
    pipeline.engine.client._index = 0
    feed(pipeline, [("p-m04", "2026-09-01T06:06:00Z", "operator", "בקרה, המטרה עצרה.")])
    run = pipeline.close_flight(FLIGHT)

    assert run.events, "a known entity id must survive validation"
    assert sum(window.rejected_count for window in run.windows) == 0


# --- failure does not spread ---------------------------------------------


def test_a_model_failure_is_contained_to_its_window(store):
    """המשפטים כבר שמורים; חלון שנכשל אינו מפיל את הקליטה."""
    pipeline = build(store, LLMTimeoutError("model took too long"))

    feed(pipeline)
    run = pipeline.close_flight(FLIGHT)

    assert run.failures, "the failure must be recorded, not swallowed"
    assert "LLMTimeoutError" in run.failures[0].error
    assert store.count(FLIGHT) == len(SENTENCES), "the transcript survives"
    assert pipeline.state_of(FLIGHT) is None


def test_ingestion_keeps_working_after_a_failed_window(store):
    pipeline = build(store, LLMTimeoutError("down"))

    feed(pipeline)
    pipeline.close_flight(FLIGHT)
    later = pipeline.ingest(
        {
            "id": "p-m09",
            "flight_id": FLIGHT,
            "timestamp": "2026-09-01T06:10:00Z",
            "source": "operator",
            "text": "בקרה, ממשיכים.",
        }
    )

    assert later.status is MessageStatus.ACCEPTED
    assert store.count(FLIGHT) == len(SENTENCES) + 1


def test_an_empty_answer_is_not_a_failure(store):
    """חלון בלי אירועים הוא תשובה לגיטימית."""
    pipeline = build(store, answer())

    feed(pipeline)
    run = pipeline.close_flight(FLIGHT)

    assert run.failures == []
    assert run.events == []
    assert run.windows and run.windows[0].ok


def test_closing_a_flight_with_nothing_pending_is_harmless(store):
    pipeline = build(store, answer())

    run = pipeline.close_flight("never-seen")

    assert run.windows == []


# --- ordering through the whole pipeline ---------------------------------


def test_a_late_sentence_becomes_an_orphan_and_disturbs_nothing(store):
    """מה שקורה בפועל להודעה שאחרה, ברמת הצינור.

    ברכיב 6 נבדק שמידע ישן אינו דורס מידע חדש יותר. כאן, רמה אחת
    למעלה, ההודעה המאחרת אפילו לא מגיעה לשם: החלון שלה כבר נסגר,
    ובהגדרה הנוכחית (``include_late_messages=False``) היא מדווחת
    כיתומה ונשארת למעבר התיקון של שלב 11.

    שלוש הטענות ביחד הן מה שהופך את זה להתנהגות ולא לאובדן: המשפט
    *נשמר* במאגר, הוא *מדווח* כיתום, והמצב *אינו* משתנה בגללו.
    """
    pipeline = build(store, answer(contact(["p-m01"])))
    feed(pipeline)
    pipeline.close_flight(FLIGHT)
    before = next(
        iter(pipeline.state_of(FLIGHT).entities.values())
    ).attributes["contact_status"].current.value

    feed(pipeline, [("p-m00", "2026-09-01T05:59:00Z", "operator", "בקרה, המטרה בתנועה.")])
    pipeline.drain()

    assert store.get("p-m00", flight_id=FLIGHT) is not None, "the sentence is still durable"
    assert "p-m00" in {message.id for message in pipeline.pump.orphans}
    after = next(
        iter(pipeline.state_of(FLIGHT).entities.values())
    ).attributes["contact_status"].current.value
    assert after == before


def test_closing_an_entity_through_the_pipeline(store):
    closing = contact(["p-m03"], relation="closes", info="סיימנו מעקב אחרי מטרה אלפא.")
    pipeline = build(store, answer(contact(["p-m01"]), closing))

    feed(pipeline)
    pipeline.close_flight(FLIGHT)

    entity = next(iter(pipeline.state_of(FLIGHT).entities.values()))
    assert entity.status is EntityStatus.CLOSED
    assert entity.closed_by_event_id


# --- persistence (component 7) -------------------------------------------


def test_the_pipeline_persists_events_and_state_when_given_a_store(store):
    """רכיב 7 מחובר: מה שרץ בצינור שורד אתחול."""
    from aura.event_store import SQLiteEventStore

    with SQLiteEventStore(":memory:") as events:
        pipeline = build(store, answer(contact(["p-m01"])))
        pipeline.event_store = events

        feed(pipeline)
        run = pipeline.close_flight(FLIGHT)

        assert events.count_events(FLIGHT) == len(run.events)
        stored = events.events_for_flight(FLIGHT)[0]
        assert events.source_ids_of(stored.id, FLIGHT) == ["p-m01"]
        assert events.load_state(FLIGHT) is not None
        rebuilt = events.rebuild_state(FLIGHT)
        assert set(rebuilt.entities) == set(pipeline.state_of(FLIGHT).entities)


def test_without_a_store_the_pipeline_still_runs(store):
    """המאגר אופציונלי: בלעדיו המצב חי בזיכרון ונעלם באתחול."""
    pipeline = build(store, answer(contact(["p-m01"])))

    feed(pipeline)
    run = pipeline.close_flight(FLIGHT)

    assert pipeline.event_store is None
    assert run.events and pipeline.state_of(FLIGHT) is not None


# --- summaries and highlights (component 8) ------------------------------


def test_the_pipeline_produces_a_summary_and_highlights(store):
    """הקצה שהמשתמש רואה: סיכום טיסה ונקודות ציון."""
    from aura.event_store import SQLiteEventStore
    from aura.summarizer import FlightSummarizer, SQLiteSummaryStore

    with SQLiteEventStore(":memory:") as events, SQLiteSummaryStore(":memory:") as sums:
        pipeline = build(store, answer(contact(["p-m01"])))
        pipeline.event_store = events
        pipeline.summarizer = FlightSummarizer(store=sums)

        feed(pipeline)
        pipeline.close_flight(FLIGHT)
        final = pipeline.finish_flight(FLIGHT)

        assert final is not None
        assert "מטרה אלפא" in final.text
        assert final.trigger_reason.value == "post_flight"
        assert final.entities_covered

        picked = pipeline.highlights_of(FLIGHT)
        assert picked
        assert all(h.source_ids for h in picked), "a highlight stays traceable"


def test_without_a_summarizer_the_pipeline_still_runs(store):
    pipeline = build(store, answer(contact(["p-m01"])))

    feed(pipeline)
    run = pipeline.close_flight(FLIGHT)

    assert pipeline.summarizer is None
    assert run.summaries == []
    assert pipeline.summary_of(FLIGHT) is None
    assert run.events

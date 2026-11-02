"""Response time on a long flight (שלב 9, קריטריון המעבר).

The stage's criterion is *"response time within target (for example p95 <
500ms for a single flight — to be confirmed)"*. The figure is marked
unconfirmed in the plan, so this file does two separate things: it asserts
the stated budget, and it reports what was actually measured, so the
number can be confirmed from evidence rather than kept as a guess.

The flight is built long on purpose - 3,000 sentences is about three and
a half hours of continuous radio - because the queries that could be slow
are exactly the ones whose cost grows with the flight. Short fixtures
would measure nothing: every golden flight answers in well under a
millisecond.

One measured result is worth stating, because it decided a design
question. The timeline query resolves every event's spoken time by
loading the flight's message timestamps on each call, with no cache. A
cache looked necessary before it was measured and turned out not to be:
the uncached query answers a 3,000-sentence flight in about 20ms, and a
12,000-sentence one in about 70ms. A cache would have bought an
invisible fraction of the budget in exchange for staleness on a live
flight, where sentences keep arriving - so there is no cache.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from aura.event_store.store import SQLiteEventStore
from aura.query_service import EventFilter, QueryService
from aura.raw_store.sqlite_store import SQLiteRawTranscriptStore
from aura.schemas import (
    ContextBundle,
    EntityRef,
    EntityType,
    InfoRelation,
    LLMEvent,
    LLMResult,
    Message,
    WindowCloseReason,
)
from aura.state_manager import MissionStateManager
from aura.validator import OutputValidator

# התקציב מהתכנית. מסומן שם "לאישור", ולכן נבדק *וגם* נמדד.
BUDGET_MS = 500.0

FLIGHT_ID = "long-01"
SENTENCES = 3_000
EVENT_EVERY = 5
SECONDS_APART = 4
TARGETS = 20
BASE = datetime(2026, 9, 1, 6, 0, tzinfo=UTC)
REPEATS = 12


@pytest.fixture(scope="module")
def long_flight():
    """טיסה ארוכה במאגרים אמיתיים: ~3.3 שעות קשר רצוף.

    נבנית פעם אחת למודול: הבנייה היא רוב הזמן כאן, והמדידה עצמה אמורה
    להיות הזולה שבשתיים.
    """
    messages = [
        Message(
            id=f"m{index:05d}",
            flight_id=FLIGHT_ID,
            timestamp=BASE + timedelta(seconds=SECONDS_APART * index),
            ingested_at=BASE + timedelta(seconds=SECONDS_APART * index),
            source="operator" if index % 2 else "ops",
            channel="command",
            text=f"משפט מספר {index} בטיסה הארוכה",
        )
        for index in range(SENTENCES)
    ]
    raw = SQLiteRawTranscriptStore(":memory:")
    raw.append_many(messages)

    labels = [f"מטרה {number}" for number in range(1, TARGETS + 1)]
    tagged = [
        LLMEvent(
            type="contact_with_target",
            description=f"תיאור {index}",
            entities=[
                EntityRef(
                    type=EntityType.TARGET,
                    label=labels[(index // EVENT_EVERY) % TARGETS],
                )
            ],
            new_info=f"מידע חדש {index}",
            relation_to_previous=InfoRelation.UPDATES,
            confidence=0.9,
            source_ids=[f"m{index:05d}"],
        )
        for index in range(0, SENTENCES, EVENT_EVERY)
    ]
    bundle = ContextBundle(
        window_id=f"{FLIGHT_ID}-w-all",
        flight_id=FLIGHT_ID,
        messages=messages,
        window_close_reason=WindowCloseReason.FLIGHT_END,
    )
    validated = OutputValidator().validate(
        LLMResult(window_id=bundle.window_id, events=tagged, llm_model="tagged"), bundle
    )
    assert not validated.rejected

    times = {message.id: message.timestamp for message in messages}
    update = MissionStateManager().apply(validated.accepted, message_times=times)

    events = SQLiteEventStore(":memory:")
    events.append_events(validated.accepted)
    events.append_changes(update.changes)
    events.save_state(update.state)

    service = QueryService(events, raw)
    yield service, validated.accepted, update
    raw.close()
    events.close()


def measure(call: Callable[[], object], repeats: int = REPEATS) -> tuple[float, float]:
    """p95 וחציון במילישניות."""
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        call()
        samples.append((time.perf_counter() - started) * 1000)
    samples.sort()
    index = max(int(0.95 * len(samples)) - 1, 0)
    return samples[index], statistics.median(samples)


def test_the_long_flight_is_actually_long(long_flight):
    """אם הקלט אינו ארוך, המדידה אינה אומרת דבר."""
    service, accepted, update = long_flight
    assert len(service.sentences_of_flight(FLIGHT_ID)) == SENTENCES
    assert len(accepted) == SENTENCES // EVENT_EVERY
    assert len(update.state.entities) == TARGETS
    assert len(service.timeline(FLIGHT_ID)) == len(accepted)


@pytest.mark.parametrize(
    "name",
    [
        "timeline",
        "timeline_by_type",
        "timeline_by_entity",
        "timeline_in_window",
        "timeline_limited",
        "current_state",
        "state_at_midflight",
        "state_at_end",
        "sources_of_an_event",
        "one_event",
        "version_diff",
    ],
)
def test_every_query_answers_within_the_budget(long_flight, name, capsys):
    """כל סוג שאילתה, על הטיסה הארוכה, מתחת לתקציב."""
    service, accepted, _ = long_flight
    middle = accepted[len(accepted) // 2]
    labels = [f"מטרה {number}" for number in range(1, TARGETS + 1)]

    calls: dict[str, Callable[[], object]] = {
        "timeline": lambda: service.timeline(FLIGHT_ID),
        "timeline_by_type": lambda: service.timeline(
            FLIGHT_ID, EventFilter.build(types="contact_with_target")
        ),
        "timeline_by_entity": lambda: service.timeline(
            FLIGHT_ID, EventFilter.build(entity=labels[3])
        ),
        "timeline_in_window": lambda: service.timeline(
            FLIGHT_ID,
            EventFilter.build(
                start=BASE + timedelta(hours=1), end=BASE + timedelta(hours=2)
            ),
        ),
        "timeline_limited": lambda: service.timeline(
            FLIGHT_ID, EventFilter.build(limit=50)
        ),
        "current_state": lambda: service.current_state(FLIGHT_ID),
        "state_at_midflight": lambda: service.state_at(
            FLIGHT_ID, BASE + timedelta(hours=1)
        ),
        "state_at_end": lambda: service.state_at(FLIGHT_ID, BASE + timedelta(days=1)),
        "sources_of_an_event": lambda: service.sources_of(middle.id, FLIGHT_ID),
        "one_event": lambda: service.event(middle.id, FLIGHT_ID),
        "version_diff": lambda: service.version_diff(FLIGHT_ID),
    }

    p95, median = measure(calls[name])
    with capsys.disabled():
        print(f"  {name:22} p95 {p95:7.1f}ms   median {median:7.1f}ms")
    assert p95 < BUDGET_MS, f"{name}: p95 {p95:.1f}ms exceeds {BUDGET_MS:.0f}ms"


def test_a_filtered_timeline_is_not_slower_than_the_whole_one(long_flight):
    """סינון אינו אמור לעלות יותר מהעדרו.

    מסנן שעובד על ידי טעינת הכול ואז סריקה *יכול* להיות איטי יותר -
    וזו בדיוק הדרך שבה סינון בשכבת הקריאה מתדרדר. היחס נבדק רחב
    בכוונה: מה שנשמר כאן הוא שהסינון אינו משנה סדר גודל.
    """
    service, _, _ = long_flight
    whole, _ = measure(lambda: service.timeline(FLIGHT_ID))
    narrowed, _ = measure(
        lambda: service.timeline(
            FLIGHT_ID,
            EventFilter.build(
                types="contact_with_target",
                start=BASE + timedelta(hours=1),
                end=BASE + timedelta(hours=2),
            ),
        )
    )
    assert narrowed < whole * 3


def test_reading_a_past_state_does_not_cost_more_than_the_present_one(long_flight):
    """שחזור לנקודת זמן באמצע הטיסה קורא פחות שינויים, לא יותר."""
    service, _, _ = long_flight
    midflight, _ = measure(lambda: service.state_at(FLIGHT_ID, BASE + timedelta(hours=1)))
    end, _ = measure(lambda: service.state_at(FLIGHT_ID, BASE + timedelta(days=1)))
    assert midflight <= end * 1.5

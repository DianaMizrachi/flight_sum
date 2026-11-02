"""A golden flight loaded into real stores, with no model in the loop.

Stage 9's test instruction is specific about this: *"run against a
database loaded from fixtures only (without an LLM); every query returns
a result matching the manual tagging"*. So the query service under test
must read from the same SQLite stores the live system writes to - not
from objects handed to it - and the only thing standing in for the model
is the fixture's own ``expected`` block.

The path here is the real one, component for component: the tagged events
go through the validator (5), into the state manager (6) one at a time in
spoken order, and out to the event store (7) and the summary store (8).
Nothing is written straight to a table. If any of those components would
have refused or reshaped something, the query tests see what the
components actually produce rather than what the fixture hoped for.

Summaries are written after every event on purpose, which is far more
often than :class:`SummaryPolicy` would allow. The policy's job is tested
in ``test_summarizer.py``; what is needed here is a dense ladder of
versions with distinct coverage markers, so that "the summary as of
06:12" has a precise right answer to be wrong about.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from aura.event_store.store import SQLiteEventStore
from aura.raw_store.sqlite_store import SQLiteRawTranscriptStore
from aura.schemas import (
    ContextBundle,
    Event,
    LLMEvent,
    LLMResult,
    Message,
    MissionState,
    StateChange,
    SummaryTrigger,
    WindowCloseReason,
)
from aura.state_manager import MissionStateManager
from aura.summarizer.service import FlightSummarizer
from aura.summarizer.store import SQLiteSummaryStore
from aura.validator import OutputValidator


@dataclass
class LoadedFlight:
    """טיסת זהב שנטענה למאגרים אמיתיים, ומה שידוע עליה מהתיוג."""

    flight_id: str
    fixture: dict
    raw: SQLiteRawTranscriptStore
    events: SQLiteEventStore
    summaries: SQLiteSummaryStore
    messages: list[Message]
    accepted: list[Event]
    changes: list[StateChange]
    state: MissionState
    times: dict[str, datetime] = field(default_factory=dict)

    @property
    def checkpoints(self) -> list[dict]:
        return self.fixture.get("checkpoints", [])

    def spoken_at(self, event: Event) -> datetime:
        return min(self.times[sid] for sid in event.source_ids)

    def message(self, message_id: str) -> Message:
        return next(m for m in self.messages if m.id == message_id)

    def close(self) -> None:
        self.raw.close()
        self.events.close()
        self.summaries.close()


def tagged_llm_events(fixture: dict) -> list[LLMEvent]:
    """התגיות של הפיקסצ'ר, בצורה שבה המודל היה מפיק אותן."""
    produced = []
    for raw in fixture["messages"]:
        expected = raw["expected"]
        if not expected["has_event"]:
            continue
        produced.append(
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
    return produced


def load_flight(fixture: dict, directory: Path | None = None) -> LoadedFlight:
    """טוען טיסת זהב למאגרים. ``directory=None`` -> מאגרים בזיכרון."""
    flight_id = fixture["flight_id"]
    messages = [
        Message.model_validate({k: v for k, v in raw.items() if k != "expected"})
        for raw in fixture["messages"]
    ]
    times = {message.id: message.timestamp for message in messages}

    raw_store = SQLiteRawTranscriptStore(
        directory / "raw.db" if directory else ":memory:"
    )
    event_store = SQLiteEventStore(
        directory / "events.db" if directory else ":memory:"
    )
    summary_store = SQLiteSummaryStore(
        directory / "summaries.db" if directory else ":memory:"
    )
    raw_store.append_many(messages)

    # רכיב 5: מה שלא עובר אימות אינו קיים בהמשך.
    bundle = ContextBundle(
        window_id=f"{flight_id}-w-all",
        flight_id=flight_id,
        messages=messages,
        window_close_reason=WindowCloseReason.FLIGHT_END,
    )
    validated = OutputValidator().validate(
        LLMResult(
            window_id=bundle.window_id,
            events=tagged_llm_events(fixture),
            llm_model="tagged",
        ),
        bundle,
    )
    assert not validated.rejected, "the tagged events must survive component 5"

    # רכיבים 6-8, אירוע אחרי אירוע בסדר האמירה.
    manager = MissionStateManager()
    summarizer = FlightSummarizer(store=summary_store)
    state = manager.empty_state(flight_id)
    changes: list[StateChange] = []
    ordered = sorted(
        validated.accepted, key=lambda e: min(times[s] for s in e.source_ids)
    )
    for index, event in enumerate(ordered):
        update = manager.apply([event], state, message_times=times)
        state = update.state
        changes.extend(update.changes)

        event_store.append_events([event])
        event_store.append_changes(update.changes)
        event_store.save_state(state)

        summarizer.record([event], messages=1)
        summarizer.summarize(
            flight_id,
            state,
            SummaryTrigger.POST_FLIGHT
            if index == len(ordered) - 1
            else SummaryTrigger.SIGNIFICANT_EVENT,
            covers_up_to_message_id=min(
                event.source_ids, key=lambda sid: (times[sid], sid)
            ),
        )

    return LoadedFlight(
        flight_id=flight_id,
        fixture=fixture,
        raw=raw_store,
        events=event_store,
        summaries=summary_store,
        messages=messages,
        accepted=ordered,
        changes=changes,
        state=state,
        times=times,
    )

"""The summarizer, and the flight's highlights (רכיב 8).

Two outputs from the same material, for two different readers.

The **summary** is context: a bounded, current picture that goes back
into the next window's prompt so the model knows what is already known.
Its job is to let the system forget the transcript without forgetting the
flight, which is why it is capped and why it is rewritten rather than
appended to.

The **highlights** are for a person: the events that actually mattered,
in order, each still pointing at the sentence it came from. Nothing is
summarised away - a highlight is a real event, selected, not rephrased.
They share the code because they answer the same question ("what matters
here") and would drift apart if they did not.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from aura.config import DEFAULT_SETTINGS, Settings
from aura.schemas import (
    Entity,
    EntityStatus,
    Event,
    InfoRelation,
    MissionState,
    Summary,
    SummaryTrigger,
    VersionTag,
)
from aura.summarizer.composer import DeterministicComposer, SummaryComposer
from aura.summarizer.policy import SummaryPolicy
from aura.summarizer.store import SQLiteSummaryStore

# סוגי אירוע שהם תמיד נקודת ציון בטיסה, ולא רק עדכון שוטף.
ALWAYS_HIGHLIGHT = frozenset({"mission_change", "malfunction"})


@dataclass
class Highlight:
    """אירוע שנבחר כנקודת ציון, עם למה הוא נבחר.

    ``spoken_at`` הוא זמן המשפט ולא זמן העיבוד. ``Event.created_at``
    נקבע כשהוולידטור בנה את האירוע - כלומר עכשיו - ולכן כותרות שהוצגו
    לפיו קיבלו כולן את אותה שעה, שאינה אומרת דבר על הטיסה. זה אותו
    עיקרון שמנהל המצב פועל לפיו: הזמן מגיע מהמשפט.
    """

    event: Event
    reason: str
    spoken_at: datetime | None = None

    @property
    def text(self) -> str:
        return self.event.new_info or self.event.description

    @property
    def at(self) -> datetime:
        return self.spoken_at or self.event.created_at

    @property
    def source_ids(self) -> list[str]:
        return list(self.event.source_ids)


def highlights_for(
    events: Sequence[Event],
    state: MissionState | None = None,
    message_times: dict[str, datetime] | None = None,
) -> list[Highlight]:
    """בוחר את האירועים שמספרים את סיפור הטיסה, בסדר כרונולוגי.

    ארבעה קריטריונים, וכל אחד מהם הוא סוג אחר של "חשוב": שינוי משימה
    ותקלה הם נקודות ציון מעצם סוגם; אירוע שסוגר ישות מסיים קו עלילה;
    סתירה היא רגע שבו המערכת שינתה את דעתה; וישות שעדיין פעילה בסוף
    הטיסה - האירוע האחרון עליה הוא מה שפתוח עכשיו.

    מה שאינו נבחר הוא עדכון שוטף על ישות שכבר מוכרת וכבר נסגרה. הוא
    לא נמחק משום מקום - הוא פשוט אינו כותרת.
    """
    def spoken(event: Event) -> datetime | None:
        if not message_times:
            return None
        times = [message_times[s] for s in event.source_ids if s in message_times]
        return min(times) if times else None

    chosen: dict[str, Highlight] = {}

    for event in events:
        if event.type in ALWAYS_HIGHLIGHT:
            chosen[event.id] = Highlight(event, "סוג האירוע הוא נקודת ציון", spoken(event))
        elif event.relation_to_previous is InfoRelation.CLOSES:
            chosen[event.id] = Highlight(event, "סגירת ישות", spoken(event))
        elif event.relation_to_previous is InfoRelation.CONTRADICTS:
            chosen[event.id] = Highlight(event, "סתירה למידע קודם", spoken(event))
        elif event.relation_to_previous is InfoRelation.NEW:
            chosen[event.id] = Highlight(event, "מידע חדש", spoken(event))

    if state is not None:
        active_labels = {
            entity.label
            for entity in state.entities.values()
            if entity.status is EntityStatus.ACTIVE
        }
        latest: dict[str, Event] = {}
        for event in events:
            for ref in event.entities:
                if ref.label in active_labels:
                    latest[ref.label] = event
        for event in latest.values():
            chosen.setdefault(
                event.id, Highlight(event, "המצב הנוכחי של ישות פעילה", spoken(event))
            )

    return sorted(chosen.values(), key=lambda h: (h.at, h.event.id))


class FlightSummarizer:
    """מחליט מתי לתקצר, כותב את התקציר ושומר אותו בגרסה חדשה."""

    def __init__(
        self,
        store: SQLiteSummaryStore | None = None,
        composer: SummaryComposer | None = None,
        settings: Settings | None = None,
        clock=None,
    ) -> None:
        self.settings = settings if settings is not None else DEFAULT_SETTINGS
        self.store = store if store is not None else SQLiteSummaryStore(":memory:")
        self.composer = composer if composer is not None else DeterministicComposer(self.settings)
        self.policy = SummaryPolicy(settings=self.settings)
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._pending_events: list[Event] = []

    def record(self, events: Sequence[Event], *, messages: int = 0, tokens: int = 0) -> None:
        """צובר את מה שקרה מאז התקציר האחרון."""
        self._pending_events.extend(events)
        self.policy.record(events=len(events), messages=messages, tokens=tokens)

    def maybe_summarize(
        self,
        flight_id: str,
        state: MissionState,
        proposed: SummaryTrigger | None,
        *,
        version: VersionTag = VersionTag.REALTIME,
        covers_up_to_message_id: str | None = None,
    ) -> Summary | None:
        """כותב תקציר אם המדיניות אומרת שהגיע הזמן; אחרת ``None``."""
        trigger = self.policy.decide(proposed)
        if trigger is None:
            return None
        return self.summarize(
            flight_id,
            state,
            trigger,
            version=version,
            covers_up_to_message_id=covers_up_to_message_id,
        )

    def summarize(
        self,
        flight_id: str,
        state: MissionState,
        trigger: SummaryTrigger,
        *,
        version: VersionTag = VersionTag.REALTIME,
        covers_up_to_message_id: str | None = None,
    ) -> Summary:
        """כותב תקציר ללא תנאי - למשל בסוף טיסה."""
        previous = self.store.latest(flight_id, version)
        text = self.composer.compose(
            flight_id=flight_id,
            state=state,
            new_events=list(self._pending_events),
            previous=previous,
            trigger=trigger,
        )
        summary = Summary(
            flight_id=flight_id,
            version=version,
            version_number=self.store.next_version_number(flight_id, version),
            text=text,
            trigger_reason=trigger,
            covers_up_to_message_id=covers_up_to_message_id,
            entities_covered=[entity.id for entity in self._active(state)],
            created_at=self._clock(),
        )
        self.store.save(summary)
        self.policy.taken()
        self._pending_events.clear()
        return summary

    def latest(
        self, flight_id: str, version: VersionTag = VersionTag.REALTIME
    ) -> Summary | None:
        """הגרסה האחרונה - מה שנטען באתחול כדי להמשיך מאותו הקשר."""
        return self.store.latest(flight_id, version)

    def resume(self, flight_id: str, version: VersionTag = VersionTag.REALTIME) -> Summary | None:
        """טעינת ההקשר אחרי אתחול השירות באמצע טיסה.

        מאפסת גם את הצבירה: מה שנצבר לפני האתחול כבר מיוצג בתקציר
        השמור, וספירתו שוב הייתה מפעילה תקציר מיותר מיד אחרי העלייה.
        """
        summary = self.store.latest(flight_id, version)
        self._pending_events.clear()
        self.policy.taken()
        return summary

    @staticmethod
    def _active(state: MissionState) -> list[Entity]:
        return [e for e in state.entities.values() if e.status is EntityStatus.ACTIVE]

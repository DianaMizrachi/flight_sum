"""Finding the historical events a window actually needs (שלב 3, משימה 3).

The plan is explicit that this starts as string and id matching, with no
embeddings: a window that mentions "מטרה אלפא" should receive the earlier
alpha events and not the whole transcript. Matching is done against the
labels of entities already known - from mission state and from past events -
so an arbitrary noun in the sentence cannot pull in unrelated history.

The event store is stage 7, so the provider is a Protocol with a fixture
backed implementation for now.
"""

from collections.abc import Iterable, Sequence
from typing import Protocol

from aura.schemas import Event, MissionState


class EventHistoryProvider(Protocol):
    """מקור האירועים ההיסטוריים של טיסה. משלב 7 - מאגר האירועים."""

    def events_for_flight(self, flight_id: str) -> list[Event]: ...


class InMemoryEventHistory:
    """מימוש בזיכרון, לשלבים 3-5 שבהם מאגר האירועים עוד לא קיים."""

    def __init__(self, events: Iterable[Event] = ()) -> None:
        self._events: list[Event] = list(events)

    def add(self, event: Event) -> None:
        self._events.append(event)

    def events_for_flight(self, flight_id: str) -> list[Event]:
        return [event for event in self._events if event.flight_id == flight_id]


def _full_label(label: str) -> str:
    return label.strip()


def _candidate_words(label: str) -> set[str]:
    """המילים שעשויות לשמש כשם קצר של הישות ("אלפא" מתוך "מטרה אלפא")."""
    return {word for word in _full_label(label).split() if len(word) >= 3}


def known_entity_labels(
    events: Sequence[Event], state: MissionState | None = None
) -> dict[str, set[str]]:
    """מיפוי מזהה/תווית ישות -> המחרוזות שמזהות אותה בטקסט.

    התווית המלאה תמיד מזהה. מילה בודדת מתוכה מזהה רק אם היא ייחודית
    לישות אחת: "מטרה" מופיעה בתוויות של כל המטרות, ולכן אינה מזהה
    אף אחת מהן, בעוד "אלפא" מזהה. זו ההגנה על "התאמת מחרוזות" מפני
    גרירת היסטוריה של ישות אחרת לתוך החבילה.
    """
    full: dict[str, set[str]] = {}
    words: dict[str, set[str]] = {}

    def record(key: str, label: str) -> None:
        full.setdefault(key, set()).add(_full_label(label))
        words.setdefault(key, set()).update(_candidate_words(label))

    if state is not None:
        for entity_id, entity in state.entities.items():
            record(entity_id, entity.label)
    for event in events:
        for ref in event.entities:
            record(ref.id or ref.label, ref.label)

    counts: dict[str, int] = {}
    for candidates in words.values():
        for word in candidates:
            counts[word] = counts.get(word, 0) + 1

    return {
        key: {label for label in full[key] if label}
        | {word for word in words[key] if counts[word] == 1}
        for key in full
    }


def mentioned_entities(text: str, labels: dict[str, set[str]]) -> set[str]:
    """אילו ישויות מוזכרות בטקסט החלון, לפי התאמת מחרוזות."""
    return {key for key, keys in labels.items() if any(k in text for k in keys)}


def select_relevant_events(
    window_text: str,
    events: Sequence[Event],
    state: MissionState | None = None,
    max_events: int | None = None,
) -> list[Event]:
    """האירועים ההיסטוריים שרלוונטיים לישויות שבחלון.

    הבחירה היא לפי עדכניות (האירועים האחרונים על אותה ישות), והחזרה היא
    בסדר כרונולוגי - כך שה-prompt קורא את ההיסטוריה כרצף.
    """
    labels = known_entity_labels(events, state)
    mentioned = mentioned_entities(window_text, labels)
    if not mentioned:
        return []

    relevant = [
        event
        for event in events
        if any((ref.id or ref.label) in mentioned for ref in event.entities)
    ]
    relevant.sort(key=lambda event: event.created_at, reverse=True)
    if max_events is not None:
        relevant = relevant[:max_events]
    relevant.sort(key=lambda event: event.created_at)
    return relevant

"""רכיב 7 - מאגר אירועים ומצב משימה (שלב 7).

אירועים ושינויי מצב נשמרים append-only; תמונת המצב הנוכחית היא מטמון
שניתן לבנות מחדש בכל רגע מרצף השינויים - וזו גם היכולת לשחזר את המצב
לכל נקודת זמן בעבר.
"""

from aura.event_store.store import (
    AppendOnlyViolationError,
    DuplicateEventError,
    EventStoreError,
    SQLiteEventStore,
)

__all__ = [
    "AppendOnlyViolationError",
    "DuplicateEventError",
    "EventStoreError",
    "SQLiteEventStore",
]

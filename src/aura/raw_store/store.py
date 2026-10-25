"""The raw transcript store contract (רכיב 2, שלב 2).

The store is append-only: it has no update and no delete method, and the
engine itself refuses both (SQLite triggers here, role grants plus a trigger
on Postgres - see ``migrations/``). Retrieval is by the three keys the plan
names: message id, flight, and time range within a flight.
"""

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol

from aura.schemas import Message


class RawStoreError(RuntimeError):
    """בסיס לשגיאות המאגר הגולמי."""


class DuplicateMessageError(RawStoreError):
    """ניסיון לכתוב מזהה שכבר קיים. המאגר קפדני גם אם רכיב 1 סינן כבר."""


class AppendOnlyViolationError(RawStoreError):
    """ניסיון עדכון או מחיקה - נחסם במנוע, ומתורגם לשגיאה הזו."""


class RawTranscriptStore(Protocol):
    """מאגר append-only של משפטי התמלול כפי שנקלטו."""

    def append(self, message: Message) -> None: ...

    def append_many(self, messages: Iterable[Message]) -> int: ...

    def get(self, message_id: str) -> Message | None: ...

    def exists(self, message_id: str) -> bool: ...

    def by_flight(self, flight_id: str, *, limit: int | None = None) -> list[Message]: ...

    def by_time_range(
        self,
        flight_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Message]: ...

    def count(self, flight_id: str | None = None) -> int: ...

    def flights(self) -> list[str]: ...

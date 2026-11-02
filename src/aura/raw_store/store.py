"""The raw transcript store contract (רכיב 2, שלב 2).

The store is append-only: it has no update and no delete method, and the
engine itself refuses both (SQLite triggers here, role grants plus a trigger
on Postgres - see ``migrations/``). Retrieval is by the three keys the plan
names: message id, flight, and time range within a flight.

A message id is unique *within its flight*, not across the store: the id
comes from the source, and two sources on two flights may both number their
lines ``m01``. So ``get``/``exists`` take an optional ``flight_id``, and an
id given without one is only unambiguous while it happens to belong to a
single flight - otherwise they raise :class:`AmbiguousMessageIdError`
instead of picking a row. Everything that holds an id also holds a flight
(``Event.flight_id`` beside ``Event.source_ids``), so a caller that cares
about one specific message can always pass both.
"""

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol

from aura.schemas import Message


class RawStoreError(RuntimeError):
    """בסיס לשגיאות המאגר הגולמי."""


class DuplicateMessageError(RawStoreError):
    """ניסיון לכתוב מזהה שכבר קיים *באותה טיסה*. המאגר קפדני גם אם רכיב 1
    סינן כבר."""


class AmbiguousMessageIdError(RawStoreError):
    """אותו מזהה הודעה קיים בכמה טיסות, והשליפה לא קיבלה ``flight_id``.

    עדיף להיכשל כאן מלהחזיר שורה אחת מתוך כמה: המזהה שייך למקור, והמקור
    מבטיח ייחודיות בתוך הטיסה שלו בלבד.
    """

    def __init__(self, message_id: str, flights: list[str]) -> None:
        super().__init__(
            f"message id {message_id!r} exists in {len(flights)} flights "
            f"({', '.join(flights)}); pass flight_id to pick one"
        )
        self.message_id = message_id
        self.flights = flights


class AppendOnlyViolationError(RawStoreError):
    """ניסיון עדכון או מחיקה - נחסם במנוע, ומתורגם לשגיאה הזו."""


class RawTranscriptStore(Protocol):
    """מאגר append-only של משפטי התמלול כפי שנקלטו."""

    def append(self, message: Message) -> None: ...

    def append_many(self, messages: Iterable[Message]) -> int: ...

    def get(self, message_id: str, *, flight_id: str | None = None) -> Message | None: ...

    def exists(self, message_id: str, *, flight_id: str | None = None) -> bool: ...

    def by_flight(self, flight_id: str, *, limit: int | None = None) -> list[Message]: ...

    def by_time_range(
        self,
        flight_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Message]: ...

    def count(self, flight_id: str | None = None) -> int: ...

    def flights(self) -> list[str]: ...

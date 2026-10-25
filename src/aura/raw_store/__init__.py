"""רכיב 2 - מאגר התמלול הגולמי (שלב 2).

append-only, עם שליפה לפי מזהה, טיסה וטווח זמן. האכיפה היא במנוע הנתונים
עצמו ולא בקוד: טריגרים ב-SQLite, והרשאות role + טריגר ב-Postgres
(``migrations/001_raw_messages.postgres.sql``).
"""

from aura.raw_store.dedup_index import RawStoreDedupIndex
from aura.raw_store.sqlite_store import SQLiteRawTranscriptStore
from aura.raw_store.store import (
    AppendOnlyViolationError,
    DuplicateMessageError,
    RawStoreError,
    RawTranscriptStore,
)

__all__ = [
    "AppendOnlyViolationError",
    "DuplicateMessageError",
    "RawStoreDedupIndex",
    "RawStoreError",
    "RawTranscriptStore",
    "SQLiteRawTranscriptStore",
]

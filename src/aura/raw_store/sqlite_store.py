"""SQLite implementation of the raw transcript store (שלב 2).

SQLite is the default because it needs no service to run, so the store is
exercised in CI exactly as it will be used - including the append-only
triggers, which are the point of this component. The Postgres schema in
``migrations/`` is the deployment target and carries the same guarantees at
the role-grant level.

Text round-trips byte for byte: it is written as-is and read back as-is, so
``retrieved.text.encode("utf-8") == ingested.text.encode("utf-8")`` for
every stored sentence - the stage 2 pass criterion.
"""

import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from aura.raw_store.store import (
    AppendOnlyViolationError,
    DuplicateMessageError,
)
from aura.schemas import Message

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

_COLUMNS = (
    "id",
    "flight_id",
    "timestamp",
    "ingested_at",
    "source",
    "channel",
    "text",
    "language",
    "dedup_hash",
    "status",
    "dead_letter_reason",
    "metadata",
    "stored_at",
)

_INSERT = (
    f"INSERT INTO raw_messages ({', '.join(_COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(_COLUMNS))})"
)

_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM raw_messages"


class SQLiteRawTranscriptStore:
    """מאגר התמלול הגולמי מעל SQLite.

    ``path=":memory:"`` לבדיקות; נתיב קובץ לשימוש אמיתי. החיבור משותף
    לכמה threads (ה-API מריץ נקודות קצה סינכרוניות ב-threadpool), ולכן כל
    גישה עוברת תחת מנעול אחד.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path not in {":memory:", ""}:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._apply_schema()

    # --- כתיבה ----------------------------------------------------------

    def append(self, message: Message) -> None:
        """הוספת הודעה. מעלה ``DuplicateMessageError`` אם המזהה קיים."""
        with self._lock:
            try:
                self._connection.execute(_INSERT, self._to_row(message))
                self._connection.commit()
            except sqlite3.IntegrityError as exc:
                self._connection.rollback()
                raise DuplicateMessageError(f"message {message.id} already stored") from exc

    def append_many(self, messages: Iterable[Message]) -> int:
        """הוספת רצף הודעות בטרנזקציה אחת - הכול או כלום."""
        rows = [self._to_row(message) for message in messages]
        if not rows:
            return 0
        with self._lock:
            try:
                self._connection.executemany(_INSERT, rows)
                self._connection.commit()
            except sqlite3.IntegrityError as exc:
                self._connection.rollback()
                raise DuplicateMessageError("batch contains an already stored message") from exc
        return len(rows)

    # --- שליפה ----------------------------------------------------------

    def get(self, message_id: str) -> Message | None:
        """שליפה לפי מזהה (מפתח 1 מהתכנית)."""
        row = self._query(f"{_SELECT} WHERE id = ?", (message_id,))
        return self._to_message(row[0]) if row else None

    def exists(self, message_id: str) -> bool:
        return bool(self._query("SELECT 1 FROM raw_messages WHERE id = ?", (message_id,)))

    def by_flight(self, flight_id: str, *, limit: int | None = None) -> list[Message]:
        """כל הודעות הטיסה (מפתח 2), בסדר כרונולוגי לפי החותמת המקורית."""
        sql = f"{_SELECT} WHERE flight_id = ? ORDER BY timestamp, id"
        params: tuple[object, ...] = (flight_id,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (flight_id, limit)
        return [self._to_message(row) for row in self._query(sql, params)]

    def by_time_range(
        self,
        flight_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Message]:
        """שליפה לפי טווח זמן בתוך טיסה (מפתח 3), גבולות כולל.

        הסינון הוא על ``timestamp`` - החותמת המקורית מהמקור - ולכן הודעה
        שהגיעה באיחור נשלפת בטווח שבו היא נאמרה, לא בזה שבו נקלטה.
        """
        sql = f"{_SELECT} WHERE flight_id = ?"
        params: list[object] = [flight_id]
        if start is not None:
            sql += " AND timestamp >= ?"
            params.append(_iso(start))
        if end is not None:
            sql += " AND timestamp <= ?"
            params.append(_iso(end))
        sql += " ORDER BY timestamp, id"
        return [self._to_message(row) for row in self._query(sql, tuple(params))]

    def count(self, flight_id: str | None = None) -> int:
        if flight_id is None:
            rows = self._query("SELECT COUNT(*) AS n FROM raw_messages")
        else:
            rows = self._query(
                "SELECT COUNT(*) AS n FROM raw_messages WHERE flight_id = ?", (flight_id,)
            )
        return int(rows[0]["n"])

    def flights(self) -> list[str]:
        rows = self._query("SELECT DISTINCT flight_id FROM raw_messages ORDER BY flight_id")
        return [row["flight_id"] for row in rows]

    # --- מפתחות הכפילות של רכיב 1 ---------------------------------------

    def lookup_dedup_key(self, key: str) -> str | None:
        rows = self._query("SELECT message_id FROM dedup_keys WHERE key = ?", (key,))
        return rows[0]["message_id"] if rows else None

    def remember_dedup_key(self, key: str, message_id: str) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT OR IGNORE INTO dedup_keys (key, message_id, remembered_at) "
                "VALUES (?, ?, ?)",
                (key, message_id, _iso(datetime.now(UTC))),
            )
            self._connection.commit()

    # --- ניהול ----------------------------------------------------------

    @property
    def connection(self) -> sqlite3.Connection:
        """החיבור עצמו, לתחזוקה ולבדיקות.

        חשיפתו אינה פתח לעדכון: ה-append-only נאכף בטריגרים של המנוע, ולכן
        גם SQL ישר לא יכול לשנות או למחוק שורה.
        """
        return self._connection

    def execute_sql(self, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        """הרצת SQL ישר, לתחזוקה ולבדיקות.

        ניסיון UPDATE או DELETE נחסם בטריגר של המנוע ומתורגם כאן
        ל-``AppendOnlyViolationError``, כדי שגם קוד תחזוקה יקבל את השגיאה
        של הדומיין ולא שגיאת SQLite גנרית.
        """
        with self._lock:
            try:
                rows = self._connection.execute(sql, params).fetchall()
                self._connection.commit()
                return rows
            except sqlite3.IntegrityError as exc:
                self._connection.rollback()
                if "append-only" in str(exc):
                    raise AppendOnlyViolationError(str(exc)) from exc
                raise

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "SQLiteRawTranscriptStore":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    # --- פנימי ----------------------------------------------------------

    def _apply_schema(self) -> None:
        with self._lock:
            self._connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
            self._connection.commit()

    def _query(self, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._connection.execute(sql, params).fetchall()

    @staticmethod
    def _to_row(message: Message) -> tuple[object, ...]:
        return (
            message.id,
            message.flight_id,
            _iso(message.timestamp),
            _iso(message.ingested_at),
            message.source,
            message.channel,
            message.text,
            message.language,
            message.dedup_hash,
            message.status.value,
            message.dead_letter_reason,
            json.dumps(message.metadata, ensure_ascii=False, sort_keys=True),
            _iso(datetime.now(UTC)),
        )

    @staticmethod
    def _to_message(row: sqlite3.Row) -> Message:
        return Message.model_validate(
            {
                "id": row["id"],
                "flight_id": row["flight_id"],
                "timestamp": datetime.fromisoformat(row["timestamp"]),
                "ingested_at": datetime.fromisoformat(row["ingested_at"]),
                "source": row["source"],
                "channel": row["channel"],
                "text": row["text"],
                "language": row["language"],
                "dedup_hash": row["dedup_hash"],
                "status": row["status"],
                "dead_letter_reason": row["dead_letter_reason"],
                "metadata": json.loads(row["metadata"]),
            }
        )


def _iso(value: datetime) -> str:
    """ISO-8601 ב-UTC, כדי שמיון טקסטואלי יהיה מיון כרונולוגי."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()

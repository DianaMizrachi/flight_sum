"""Versioned summary storage (רכיב 8, משימה 3).

Summaries are kept, not replaced. The plan asks for "flight id, update
time, version number; load the latest on startup", and keeping the older
ones costs almost nothing while answering a question that comes up
constantly in review: *what did we believe at the time*, as opposed to
what we concluded later.

``version`` (realtime / final) and ``version_number`` (1, 2, 3…) are
different axes and both matter. The first separates the live run from
stage 11's post-flight pass; the second counts revisions within a run. A
flight therefore has two independent sequences, and asking for "the
latest" without saying which version is ambiguous - so the API makes you
say.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from aura.schemas import Summary, SummaryTrigger, VersionTag

SCHEMA = """
CREATE TABLE IF NOT EXISTS summaries (
    flight_id               TEXT NOT NULL,
    version                 TEXT NOT NULL,
    version_number          INTEGER NOT NULL,
    text                    TEXT NOT NULL,
    trigger_reason          TEXT NOT NULL,
    covers_up_to_message_id TEXT,
    entities_covered        TEXT NOT NULL,  -- JSON
    created_at              TEXT NOT NULL,
    stored_at               TEXT NOT NULL,

    PRIMARY KEY (flight_id, version, version_number)
);

CREATE INDEX IF NOT EXISTS summaries_latest_idx
    ON summaries (flight_id, version, version_number DESC);

-- תקציר שנכתב הוא עובדה על מה שידענו באותו רגע, ולכן אינו משתנה
-- בדיעבד. גרסה חדשה היא שורה חדשה.
CREATE TRIGGER IF NOT EXISTS summaries_block_update
BEFORE UPDATE ON summaries
BEGIN
    SELECT RAISE(ABORT, 'summaries is append-only: UPDATE is blocked');
END;

CREATE TRIGGER IF NOT EXISTS summaries_block_delete
BEFORE DELETE ON summaries
BEGIN
    SELECT RAISE(ABORT, 'summaries is append-only: DELETE is blocked');
END;
"""


class SQLiteSummaryStore:
    """מאגר התקצירים, עם גרסאות."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.executescript(SCHEMA)
        self._connection.commit()

    def next_version_number(
        self, flight_id: str, version: VersionTag = VersionTag.REALTIME
    ) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(version_number), 0) AS n FROM summaries "
            "WHERE flight_id = ? AND version = ?",
            (flight_id, version.value),
        ).fetchone()
        return int(row["n"]) + 1

    def save(self, summary: Summary) -> None:
        import json

        with self._connection:
            self._connection.execute(
                """
                INSERT INTO summaries (
                    flight_id, version, version_number, text, trigger_reason,
                    covers_up_to_message_id, entities_covered, created_at, stored_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    summary.flight_id,
                    summary.version.value,
                    summary.version_number,
                    summary.text,
                    summary.trigger_reason.value,
                    summary.covers_up_to_message_id,
                    json.dumps(summary.entities_covered, ensure_ascii=False),
                    summary.created_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                    datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                ),
            )

    def latest(
        self, flight_id: str, version: VersionTag = VersionTag.REALTIME
    ) -> Summary | None:
        """הגרסה האחרונה - מה שנטען באתחול השירות."""
        row = self._connection.execute(
            "SELECT * FROM summaries WHERE flight_id = ? AND version = ? "
            "ORDER BY version_number DESC LIMIT 1",
            (flight_id, version.value),
        ).fetchone()
        return self._to_summary(row) if row else None

    def history(
        self, flight_id: str, version: VersionTag = VersionTag.REALTIME
    ) -> list[Summary]:
        rows = self._connection.execute(
            "SELECT * FROM summaries WHERE flight_id = ? AND version = ? "
            "ORDER BY version_number",
            (flight_id, version.value),
        )
        return [self._to_summary(row) for row in rows]

    def count(self, flight_id: str, version: VersionTag = VersionTag.REALTIME) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS n FROM summaries WHERE flight_id = ? AND version = ?",
            (flight_id, version.value),
        ).fetchone()
        return int(row["n"])

    def flights(self) -> list[str]:
        rows = self._connection.execute(
            "SELECT DISTINCT flight_id FROM summaries ORDER BY flight_id"
        )
        return [row["flight_id"] for row in rows]

    @staticmethod
    def _to_summary(row: sqlite3.Row) -> Summary:
        import json

        return Summary(
            flight_id=row["flight_id"],
            version=VersionTag(row["version"]),
            version_number=row["version_number"],
            text=row["text"],
            trigger_reason=SummaryTrigger(row["trigger_reason"]),
            covers_up_to_message_id=row["covers_up_to_message_id"],
            entities_covered=json.loads(row["entities_covered"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SQLiteSummaryStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

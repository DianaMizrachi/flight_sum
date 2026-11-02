"""The event and mission-state store (רכיב 7, שלב 7).

Component 6 decides what is true; this is where that survives a restart.
It holds three things and treats them very differently, and the
difference is the whole design:

**Events** and **state changes** are append-only, enforced by triggers in
the engine itself. They are records of things that happened, and a thing
that happened does not stop having happened. Stage 11's final pass does
not correct them - it *adds* rows tagged ``final`` beside the realtime
ones, which is precisely what makes the two versions comparable
afterwards.

**Entity snapshots** are a cache, and are therefore the one table that
may be updated and deleted. Everything in them can be rebuilt by
replaying the change log, and :meth:`rebuild_state` does exactly that.

That replay is the stage's pass criterion - *"state rebuilt at time T
equals the state that was live at time T"* - and it is worth being
explicit about why it can be trusted. The manager never mutates in place:
every value it replaces is written to a change record with both its old
and new value. So the log is not a summary of what happened, it is the
complete sequence, and running it forward is not an approximation of the
live run - it is the same arithmetic in the same order.

Ordering is by ``applied_at`` and then by a stored sequence number. Time
alone is not enough: two changes to one attribute can share a timestamp
to the second, and replaying them in the wrong order yields a different
answer than the live run gave.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from aura.schemas import (
    AttributeSnapshot,
    CertaintyLevel,
    Entity,
    EntityAttribute,
    EntityStatus,
    Event,
    MissionState,
    StateChange,
    StateChangeKind,
    VersionTag,
)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class EventStoreError(RuntimeError):
    """בסיס לשגיאות מאגר האירועים."""


class DuplicateEventError(EventStoreError):
    """ניסיון לכתוב אירוע שכבר קיים באותה טיסה ובאותה גרסה."""


class AppendOnlyViolationError(EventStoreError):
    """ניסיון עדכון או מחיקה על טבלה append-only - נחסם במנוע."""


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class SQLiteEventStore:
    """מאגר האירועים, שינויי המצב ותמונת המצב הנוכחית."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self._connection.commit()

    # --- writing ----------------------------------------------------------

    def append_event(self, event: Event) -> None:
        self.append_events([event])

    def append_events(self, events: Iterable[Event]) -> int:
        """כותב אירועים ואת הקישור שלהם למשפטי המקור, בטרנזקציה אחת.

        הקישור נכתב יחד עם האירוע ולא אחריו: אירוע ששמור בלי מקורותיו
        אינו ניתן לייחוס, וזו התכונה שכל המערכת נשענת עליה.
        """
        rows = list(events)
        if not rows:
            return 0
        stored_at = _iso(datetime.now(UTC))
        try:
            with self._connection:
                for event in rows:
                    self._connection.execute(
                        """
                        INSERT INTO events (
                            id, flight_id, window_id, type, description, entities,
                            new_info, relation_to_previous, related_event_id, confidence,
                            version, created_at, reconciles_event_id, reconciliation_action,
                            reconciliation_reason, stored_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            event.id,
                            event.flight_id,
                            event.window_id,
                            event.type,
                            event.description,
                            json.dumps(
                                [ref.model_dump(mode="json") for ref in event.entities],
                                ensure_ascii=False,
                            ),
                            event.new_info,
                            event.relation_to_previous.value,
                            event.related_event_id,
                            event.confidence,
                            event.version.value,
                            _iso(event.created_at),
                            event.reconciles_event_id,
                            event.reconciliation_action.value
                            if event.reconciliation_action
                            else None,
                            event.reconciliation_reason,
                            stored_at,
                        ),
                    )
                    for position, message_id in enumerate(event.source_ids):
                        self._connection.execute(
                            """
                            INSERT INTO event_sources
                                (flight_id, version, event_id, message_id, position)
                            VALUES (?,?,?,?,?)
                            """,
                            (
                                event.flight_id,
                                event.version.value,
                                event.id,
                                message_id,
                                position,
                            ),
                        )
        except sqlite3.IntegrityError as exc:
            raise DuplicateEventError(str(exc)) from exc
        return len(rows)

    def append_changes(self, changes: Sequence[StateChange]) -> int:
        """כותב רשומות שינוי, עם מספר סידורי שמשמר את סדר הכתיבה.

        הסדר נקבע כאן ולא בזמן הקריאה: שתי רשומות באותה חותמת זמן
        חייבות להישמר עם הסדר שבו הוחלו, אחרת השחזור יכול להחזיר מצב
        אחר מזה שרץ בפועל.
        """
        if not changes:
            return 0
        stored_at = _iso(datetime.now(UTC))
        try:
            with self._connection:
                for change in changes:
                    sequence = self._next_sequence(change.flight_id, change.version)
                    self._connection.execute(
                        """
                        INSERT INTO state_changes (
                            id, flight_id, event_id, entity_id, entity_type, attribute,
                            kind, old_value, new_value, certainty, version, applied_at,
                            sequence, stored_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            change.id,
                            change.flight_id,
                            change.event_id,
                            change.entity_id,
                            change.entity_type.value,
                            change.attribute,
                            change.kind.value,
                            change.old_value,
                            change.new_value,
                            change.certainty.value,
                            change.version.value,
                            _iso(change.applied_at),
                            sequence,
                            stored_at,
                        ),
                    )
        except sqlite3.IntegrityError as exc:
            raise DuplicateEventError(str(exc)) from exc
        return len(changes)

    def _next_sequence(self, flight_id: str, version: VersionTag) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS n FROM state_changes "
            "WHERE flight_id = ? AND version = ?",
            (flight_id, version.value),
        ).fetchone()
        return int(row["n"]) + 1

    def save_state(self, state: MissionState) -> None:
        """שומר את תמונת המצב הנוכחית. מטמון - מותר לדרוס."""
        updated_at = _iso(datetime.now(UTC))
        with self._connection:
            for entity_id, entity in state.entities.items():
                self._connection.execute(
                    """
                    INSERT INTO entity_snapshots
                        (flight_id, version, entity_id, payload, as_of, updated_at)
                    VALUES (?,?,?,?,?,?)
                    ON CONFLICT (flight_id, version, entity_id) DO UPDATE SET
                        payload = excluded.payload,
                        as_of = excluded.as_of,
                        updated_at = excluded.updated_at
                    """,
                    (
                        state.flight_id,
                        state.version.value,
                        entity_id,
                        entity.model_dump_json(),
                        _iso(state.as_of),
                        updated_at,
                    ),
                )

    # --- reading ----------------------------------------------------------

    def get_event(
        self, event_id: str, flight_id: str, version: VersionTag = VersionTag.REALTIME
    ) -> Event | None:
        row = self._connection.execute(
            "SELECT * FROM events WHERE flight_id = ? AND version = ? AND id = ?",
            (flight_id, version.value, event_id),
        ).fetchone()
        return self._to_event(row) if row else None

    def events_for_flight(
        self,
        flight_id: str,
        version: VersionTag = VersionTag.REALTIME,
        *,
        until: datetime | None = None,
        event_type: str | None = None,
    ) -> list[Event]:
        """ציר הזמן של הטיסה, בסדר כרונולוגי."""
        sql = "SELECT * FROM events WHERE flight_id = ? AND version = ?"
        params: list[object] = [flight_id, version.value]
        if until is not None:
            sql += " AND created_at <= ?"
            params.append(_iso(until))
        if event_type is not None:
            sql += " AND type = ?"
            params.append(event_type)
        sql += " ORDER BY created_at, id"
        return [self._to_event(row) for row in self._connection.execute(sql, params)]

    def source_ids_of(
        self, event_id: str, flight_id: str, version: VersionTag = VersionTag.REALTIME
    ) -> list[str]:
        """מאירוע -> משפטי המקור. חייב להצליח תמיד, לכל אירוע שמור."""
        rows = self._connection.execute(
            "SELECT message_id FROM event_sources "
            "WHERE flight_id = ? AND version = ? AND event_id = ? ORDER BY position",
            (flight_id, version.value, event_id),
        )
        return [row["message_id"] for row in rows]

    def events_from_message(
        self, message_id: str, flight_id: str, version: VersionTag = VersionTag.REALTIME
    ) -> list[Event]:
        """הכיוון ההפוך: אילו אירועים נגזרו מהמשפט הזה."""
        rows = self._connection.execute(
            """
            SELECT e.* FROM events e
            JOIN event_sources s
              ON s.flight_id = e.flight_id AND s.version = e.version AND s.event_id = e.id
            WHERE s.flight_id = ? AND s.version = ? AND s.message_id = ?
            ORDER BY e.created_at, e.id
            """,
            (flight_id, version.value, message_id),
        )
        return [self._to_event(row) for row in rows]

    def changes_for_flight(
        self,
        flight_id: str,
        version: VersionTag = VersionTag.REALTIME,
        *,
        until: datetime | None = None,
    ) -> list[StateChange]:
        sql = "SELECT * FROM state_changes WHERE flight_id = ? AND version = ?"
        params: list[object] = [flight_id, version.value]
        if until is not None:
            sql += " AND applied_at <= ?"
            params.append(_iso(until))
        sql += " ORDER BY applied_at, sequence"
        return [self._to_change(row) for row in self._connection.execute(sql, params)]

    def load_state(
        self, flight_id: str, version: VersionTag = VersionTag.REALTIME
    ) -> MissionState | None:
        """תמונת המצב השמורה - הדרך המהירה, בלי שחזור."""
        rows = list(
            self._connection.execute(
                "SELECT * FROM entity_snapshots WHERE flight_id = ? AND version = ?",
                (flight_id, version.value),
            )
        )
        if not rows:
            return None
        return MissionState(
            flight_id=flight_id,
            version=version,
            as_of=datetime.fromisoformat(max(row["as_of"] for row in rows)),
            entities={
                row["entity_id"]: Entity.model_validate_json(row["payload"]) for row in rows
            },
        )

    def flights(self) -> list[str]:
        rows = self._connection.execute(
            "SELECT DISTINCT flight_id FROM events ORDER BY flight_id"
        )
        return [row["flight_id"] for row in rows]

    def count_events(self, flight_id: str | None = None) -> int:
        if flight_id is None:
            row = self._connection.execute("SELECT COUNT(*) AS n FROM events").fetchone()
        else:
            row = self._connection.execute(
                "SELECT COUNT(*) AS n FROM events WHERE flight_id = ?", (flight_id,)
            ).fetchone()
        return int(row["n"])

    # --- the pass criterion: rebuild state at any point in time -----------

    def rebuild_state(
        self,
        flight_id: str,
        version: VersionTag = VersionTag.REALTIME,
        *,
        until: datetime | None = None,
    ) -> MissionState:
        """בונה את המצב מחדש מרצף שינויי המצב, עד נקודת זמן.

        ``until=None`` מחזיר את המצב הסופי, ואז הוא חייב להיות זהה ל-
        ``load_state`` - אותה תוצאה בשתי דרכים, וזו הטענה שמאפשרת
        להתייחס לתמונת המצב כאל מטמון.
        """
        changes = self.changes_for_flight(flight_id, version, until=until)
        entities: dict[str, Entity] = {}

        for change in changes:
            entity = entities.get(change.entity_id)
            if entity is None:
                entity = Entity(
                    id=change.entity_id,
                    type=change.entity_type,
                    # התווית נכתבת ב-new_value של רשומת ההוספה; שינוי
                    # שמגיע לישות שלא נוצרה (שחזור חלקי) נופל למזהה.
                    label=change.new_value or change.entity_id,
                )
                entities[change.entity_id] = entity

            if change.kind is StateChangeKind.CLOSED:
                entity.status = EntityStatus.CLOSED
                entity.closed_at = change.applied_at
                entity.closed_by_event_id = change.event_id
                continue
            if change.attribute is None:
                continue  # רשומת יצירת הישות עצמה - כבר טופלה למעלה

            snapshot = AttributeSnapshot(
                value=change.new_value or "",
                source_event_id=change.event_id,
                certainty=CertaintyLevel(change.certainty),
                updated_at=change.applied_at,
            )
            existing = entity.attributes.get(change.attribute)
            if existing is None:
                entity.attributes[change.attribute] = EntityAttribute(current=snapshot)
            else:
                existing.history.append(existing.current)
                existing.current = snapshot
            if change.kind is StateChangeKind.CONTRADICTED:
                entity.attributes[change.attribute].contradicted = True

        as_of = changes[-1].applied_at if changes else datetime.now(UTC)
        return MissionState(
            flight_id=flight_id, version=version, as_of=as_of, entities=entities
        )

    # --- plumbing ---------------------------------------------------------

    def _to_event(self, row: sqlite3.Row) -> Event:
        return Event(
            id=row["id"],
            flight_id=row["flight_id"],
            window_id=row["window_id"],
            type=row["type"],
            description=row["description"],
            entities=json.loads(row["entities"]),
            new_info=row["new_info"],
            relation_to_previous=row["relation_to_previous"],
            related_event_id=row["related_event_id"],
            confidence=row["confidence"],
            source_ids=self.source_ids_of(
                row["id"], row["flight_id"], VersionTag(row["version"])
            ),
            version=row["version"],
            created_at=datetime.fromisoformat(row["created_at"]),
            reconciles_event_id=row["reconciles_event_id"],
            reconciliation_action=row["reconciliation_action"],
            reconciliation_reason=row["reconciliation_reason"],
        )

    def _to_change(self, row: sqlite3.Row) -> StateChange:
        return StateChange(
            id=row["id"],
            flight_id=row["flight_id"],
            event_id=row["event_id"],
            entity_id=row["entity_id"],
            entity_type=row["entity_type"],
            attribute=row["attribute"],
            kind=row["kind"],
            old_value=row["old_value"],
            new_value=row["new_value"],
            certainty=row["certainty"],
            version=row["version"],
            applied_at=datetime.fromisoformat(row["applied_at"]),
        )

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SQLiteEventStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

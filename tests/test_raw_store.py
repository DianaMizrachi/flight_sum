"""Stage 2 tests - the raw transcript store.

The plan's tests for this stage: retrieval by each of the three keys, a
failing update attempt, and loading a full flight back in chronological
order. The pass criterion - retrieved text identical byte for byte to what
was ingested, for every golden-set message - is the first test here, run
through the real ingestion path rather than by inserting rows by hand.
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from aura.ingestion import IngestionService
from aura.raw_store import (
    AmbiguousMessageIdError,
    AppendOnlyViolationError,
    DuplicateMessageError,
    RawStoreDedupIndex,
    SQLiteRawTranscriptStore,
)
from aura.schemas import Message, MessageStatus

FIXTURES_DIR = Path(__file__).parent / "fixtures"
GOLDEN_FLIGHTS_DIR = FIXTURES_DIR / "golden_flights"
EDGE_CASES_DIR = FIXTURES_DIR / "edge_cases"

FROZEN_NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)

RAW_STORE_PKG = Path(__file__).resolve().parents[1] / "src" / "aura" / "raw_store"


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def raw_inputs(flight: dict) -> list[dict]:
    return [{k: v for k, v in message.items() if k != "expected"} for message in flight["messages"]]


@pytest.fixture
def store() -> SQLiteRawTranscriptStore:
    with SQLiteRawTranscriptStore(":memory:") as opened:
        yield opened


@pytest.fixture
def service(store: SQLiteRawTranscriptStore) -> IngestionService:
    return IngestionService(
        store=store, dedup_index=RawStoreDedupIndex(store), clock=lambda: FROZEN_NOW
    )


def message(**overrides) -> Message:
    base = {
        "id": "f1-m01",
        "flight_id": "f1",
        "timestamp": datetime(2026, 9, 1, 6, 0, tzinfo=UTC),
        "ingested_at": datetime(2026, 9, 1, 6, 0, 1, tzinfo=UTC),
        "source": "operator",
        "channel": "command",
        "text": "בקרה, נץ אחת, הכל תקין.",
    }
    return Message.model_validate(base | overrides)


# --- קריטריון המעבר: טקסט זהה בייט-בייט ---------------------------------


@pytest.mark.parametrize(
    "flight_path", sorted(GOLDEN_FLIGHTS_DIR.glob("*.yaml")), ids=lambda p: p.stem
)
def test_golden_flight_text_round_trips_byte_for_byte(
    flight_path: Path, service: IngestionService, store: SQLiteRawTranscriptStore
):
    flight = load_yaml(flight_path)
    payloads = raw_inputs(flight)

    service.ingest_many(payloads)

    assert store.count(flight["flight_id"]) == len(payloads)
    for raw in payloads:
        stored = store.get(raw["id"])
        assert stored is not None, raw["id"]
        assert stored.text.encode("utf-8") == raw["text"].encode("utf-8")


@pytest.mark.parametrize(
    "flight_path", sorted(GOLDEN_FLIGHTS_DIR.glob("*.yaml")), ids=lambda p: p.stem
)
def test_golden_flight_messages_round_trip_as_the_same_contract(
    flight_path: Path, service: IngestionService, store: SQLiteRawTranscriptStore
):
    """כל שדות ה-Message חוזרים מהמאגר זהים, לא רק הטקסט."""
    flight = load_yaml(flight_path)

    results = service.ingest_many(raw_inputs(flight))

    for result in results:
        assert store.get(result.message.id) == result.message


def test_normalized_message_keeps_the_original_text_through_the_store(
    service: IngestionService, store: SQLiteRawTranscriptStore
):
    raw_text = "  בקרה,‏ נץ אחת,\tהכל   תקין.  "

    result = service.ingest(
        {
            "id": "f9-m01",
            "flight_id": "f9",
            "timestamp": "2026-09-01T06:00:00Z",
            "source": "operator",
            "text": raw_text,
        }
    )

    stored = store.get(result.message.id)
    assert stored.text == "בקרה, נץ אחת, הכל תקין."
    assert stored.metadata["raw_text"].encode("utf-8") == raw_text.encode("utf-8")


# --- שליפה לפי שלושת המפתחות -------------------------------------------


def test_retrieval_by_id(store: SQLiteRawTranscriptStore):
    stored = message()
    store.append(stored)

    assert store.get("f1-m01") == stored
    assert store.exists("f1-m01") is True
    assert store.get("nope") is None
    assert store.exists("nope") is False


def test_retrieval_by_flight_is_scoped_and_counted(store: SQLiteRawTranscriptStore):
    store.append_many(
        [
            message(id="f1-m01", flight_id="f1"),
            message(id="f1-m02", flight_id="f1", timestamp=datetime(2026, 9, 1, 6, 1, tzinfo=UTC)),
            message(id="f2-m01", flight_id="f2"),
        ]
    )

    assert [m.id for m in store.by_flight("f1")] == ["f1-m01", "f1-m02"]
    assert [m.id for m in store.by_flight("f2")] == ["f2-m01"]
    assert store.count("f1") == 2
    assert store.count() == 3
    assert store.flights() == ["f1", "f2"]
    assert [m.id for m in store.by_flight("f1", limit=1)] == ["f1-m01"]


def test_retrieval_by_time_range_uses_the_original_timestamp(store: SQLiteRawTranscriptStore):
    base = datetime(2026, 9, 1, 6, 0, tzinfo=UTC)
    store.append_many(
        [
            message(id="m1", timestamp=base),
            message(id="m2", timestamp=base + timedelta(minutes=5)),
            message(id="m3", timestamp=base + timedelta(minutes=10)),
        ]
    )

    window = store.by_time_range("f1", base + timedelta(minutes=1), base + timedelta(minutes=6))
    assert [m.id for m in window] == ["m2"]
    assert [m.id for m in store.by_time_range("f1", start=base)] == ["m1", "m2", "m3"]
    assert [m.id for m in store.by_time_range("f1", end=base)] == ["m1"]
    assert store.by_time_range("f1", base + timedelta(hours=1)) == []


def test_time_range_bounds_are_inclusive(store: SQLiteRawTranscriptStore):
    base = datetime(2026, 9, 1, 6, 0, tzinfo=UTC)
    store.append_many([message(id="m1", timestamp=base), message(id="m2", timestamp=base)])

    assert len(store.by_time_range("f1", base, base)) == 2


def test_time_range_matches_regardless_of_the_offset_it_is_given(store: SQLiteRawTranscriptStore):
    """חותמות נשמרות ב-UTC, ולכן טווח שנמסר באזור זמן אחר עדיין תואם."""
    store.append(message(timestamp=datetime(2026, 9, 1, 6, 0, tzinfo=UTC)))
    local = datetime.fromisoformat("2026-09-01T09:00:00+03:00")

    assert [m.id for m in store.by_time_range("f1", local, local)] == ["f1-m01"]


# --- סדר כרונולוגי ------------------------------------------------------


@pytest.mark.parametrize(
    "flight_path", sorted(GOLDEN_FLIGHTS_DIR.glob("*.yaml")), ids=lambda p: p.stem
)
def test_full_flight_loads_in_chronological_order(
    flight_path: Path, service: IngestionService, store: SQLiteRawTranscriptStore
):
    flight = load_yaml(flight_path)
    payloads = raw_inputs(flight)
    service.ingest_many(payloads)

    loaded = store.by_flight(flight["flight_id"])

    assert [m.id for m in loaded] == [raw["id"] for raw in payloads]
    assert loaded == sorted(loaded, key=lambda m: m.timestamp)


def test_out_of_order_arrivals_are_restored_chronologically(
    service: IngestionService, store: SQLiteRawTranscriptStore
):
    data = load_yaml(EDGE_CASES_DIR / "late_arrival.yaml")

    service.ingest_many(data["raw_inputs"])

    loaded = store.by_flight("flight-edge-02")
    assert [m.id for m in loaded] == data["expected"]["chronological_order_by_timestamp"]
    # סדר הקליטה נשמר בנפרד, כך שאיחור נותר מזוהה אחרי השליפה.
    late = store.get(data["expected"]["late_message_id"])
    assert (late.ingested_at - late.timestamp).total_seconds() == data["expected"][
        "late_by_seconds"
    ]


# --- append-only --------------------------------------------------------


def test_update_is_blocked_by_the_engine_not_by_the_code(store: SQLiteRawTranscriptStore):
    store.append(message())

    with pytest.raises(AppendOnlyViolationError):
        store.execute_sql("UPDATE raw_messages SET text = 'שונה' WHERE id = 'f1-m01'")

    assert store.get("f1-m01").text == "בקרה, נץ אחת, הכל תקין."


def test_delete_is_blocked_by_the_engine_not_by_the_code(store: SQLiteRawTranscriptStore):
    store.append(message())

    with pytest.raises(AppendOnlyViolationError):
        store.execute_sql("DELETE FROM raw_messages WHERE id = 'f1-m01'")

    assert store.count() == 1


def test_the_store_exposes_no_update_or_delete_api(store: SQLiteRawTranscriptStore):
    public = {name for name in dir(store) if not name.startswith("_")}
    assert not public & {"update", "delete", "remove", "truncate", "upsert"}


def test_appending_the_same_id_twice_is_refused(store: SQLiteRawTranscriptStore):
    store.append(message())

    with pytest.raises(DuplicateMessageError):
        store.append(message(text="נוסח אחר"))

    assert store.count() == 1
    assert store.get("f1-m01").text == "בקרה, נץ אחת, הכל תקין."


# --- המזהה ייחודי בתוך הטיסה, לא במאגר ----------------------------------


def test_the_same_source_id_in_two_flights_is_two_messages(store: SQLiteRawTranscriptStore):
    """מזהה ההודעה בא מהמקור, והוא מבטיח ייחודיות בתוך הטיסה שלו בלבד.

    זו לא פינה תיאורטית: כל קובץ תמלול שממנה את שורותיו m01, m02 - וזה
    הפורמט של ``samples/`` - מגיע עם אותם מזהים בכל טיסה.
    """
    store.append(message(id="m01", flight_id="f1", text="בקרה, נץ אחת, הכל תקין."))
    store.append(message(id="m01", flight_id="f2", text="בקרה, נץ שתיים, ממתינים."))

    assert store.count() == 2
    assert store.get("m01", flight_id="f1").text == "בקרה, נץ אחת, הכל תקין."
    assert store.get("m01", flight_id="f2").text == "בקרה, נץ שתיים, ממתינים."
    assert store.exists("m01", flight_id="f2") is True
    assert store.exists("m01", flight_id="f3") is False


def test_an_id_shared_by_two_flights_is_not_resolved_by_guessing(
    store: SQLiteRawTranscriptStore,
):
    """שליפה בלי טיסה נכשלת במקום להחזיר אחת מהשתיים."""
    store.append(message(id="m01", flight_id="f1"))
    store.append(message(id="m01", flight_id="f2"))

    with pytest.raises(AmbiguousMessageIdError) as raised:
        store.get("m01")

    assert raised.value.flights == ["f1", "f2"]
    assert store.flights_with_message("m01") == ["f1", "f2"]
    # ``exists`` עונה על שאלה אחרת, שאין בה דו-משמעות.
    assert store.exists("m01") is True


def test_ingesting_the_same_source_id_under_another_flight_is_not_a_duplicate(
    service: IngestionService, store: SQLiteRawTranscriptStore
):
    """הרגרסיה עצמה: רכיב 1 משייך את מפתח הכפילות לטיסה, ולכן הודעה כזו
    אושרה - ואז המאגר דחה אותה על התנגשות מזהה גלובלית, והקליטה החזירה
    ``duplicate`` על הודעה שתוכנה חדש לגמרי. כך קובץ תמלול עם מזהים
    קבועים נקלט פעם אחת בחיי המאגר, ובכל טיסה נוספת חזר ככפילות."""
    payload = {
        "id": "m01",
        "timestamp": "2026-09-01T06:00:00Z",
        "source": "operator",
        "text": "בקרה, נץ אחת, הכל תקין.",
    }

    first = service.ingest({**payload, "flight_id": "f1"})
    second = service.ingest({**payload, "flight_id": "f2"})
    again = service.ingest({**payload, "flight_id": "f1"})

    assert first.status == MessageStatus.ACCEPTED
    assert second.status == MessageStatus.ACCEPTED, "another flight is another message"
    assert again.status == MessageStatus.DUPLICATE, "the same flight is still a duplicate"
    assert store.count("f1") == 1
    assert store.count("f2") == 1


def test_a_batch_with_a_duplicate_is_rejected_whole(store: SQLiteRawTranscriptStore):
    store.append(message(id="m1"))

    with pytest.raises(DuplicateMessageError):
        store.append_many([message(id="m2"), message(id="m1")])

    assert store.count() == 1, "the batch is all-or-nothing"


def test_dead_lettered_payloads_are_not_written_to_the_store(
    service: IngestionService, store: SQLiteRawTranscriptStore
):
    data = load_yaml(EDGE_CASES_DIR / "malformed_and_empty.yaml")

    for case in data["cases"]:
        result = service.ingest(case["raw_input"])
        assert result.status == MessageStatus.DEAD_LETTER

    assert store.count() == 0
    assert len(service.dead_letters) == len(data["cases"])


def test_a_duplicate_is_stored_once(service: IngestionService, store: SQLiteRawTranscriptStore):
    data = load_yaml(EDGE_CASES_DIR / "duplicates.yaml")
    case = next(c for c in data["cases"] if c["case_id"] == "duplicate-exact-id")

    service.ingest_many(case["raw_inputs"])

    assert store.count() == 1


# --- עמידות באתחול ------------------------------------------------------


def test_the_store_and_dedup_index_survive_a_restart(tmp_path: Path):
    """אתחול השירות באמצע טיסה: ההודעות נשלפות, והכפילות עוד מזוהה."""
    db_path = tmp_path / "raw.db"
    payload = {
        "id": "f5-m01",
        "flight_id": "f5",
        "timestamp": "2026-09-01T06:00:00Z",
        "source": "operator",
        "text": "בקרה, נץ חמש, הכל תקין.",
    }

    with SQLiteRawTranscriptStore(db_path) as first:
        first_service = IngestionService(store=first, dedup_index=RawStoreDedupIndex(first))
        assert first_service.ingest(payload).status == MessageStatus.ACCEPTED

    with SQLiteRawTranscriptStore(db_path) as second:
        second_service = IngestionService(store=second, dedup_index=RawStoreDedupIndex(second))
        assert second.count("f5") == 1
        assert second.get("f5-m01").text == payload["text"]
        again = second_service.ingest(payload)
        assert again.status == MessageStatus.DUPLICATE
        assert second.count("f5") == 1


def test_schema_creates_the_three_retrieval_indexes(store: SQLiteRawTranscriptStore):
    rows = store.execute_sql(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'raw_messages'"
    )
    names = {row["name"] for row in rows}

    assert {"raw_messages_id_idx", "raw_messages_flight_time_idx"} <= names

    def searches(sql: str) -> bool:
        plan = store.execute_sql(f"EXPLAIN QUERY PLAN {sql}")
        return "SEARCH" in " ".join(str(tuple(row)) for row in plan)

    # ה-PRIMARY KEY (flight_id, id) מכסה שליפה לפי טיסה ולפי מזהה בתוכה;
    # המזהה לבדו אינו העמודה השמאלית, ולכן יש לו אינדקס משלו - אחרת
    # ``get`` בלי טיסה היה סריקה מלאה של המאגר.
    assert searches("SELECT * FROM raw_messages WHERE flight_id = 'f' AND id = 'x'")
    assert searches("SELECT * FROM raw_messages WHERE flight_id = 'f'")
    assert searches("SELECT * FROM raw_messages WHERE id = 'x'")


def test_applying_the_schema_twice_is_harmless(tmp_path: Path):
    db_path = tmp_path / "twice.db"
    with SQLiteRawTranscriptStore(db_path) as first:
        first.append(message())
    with SQLiteRawTranscriptStore(db_path) as second:
        assert second.count() == 1


# --- מיגרציה של מאגר קיים -----------------------------------------------

# הסכמה כפי שהייתה לפני התיקון, מילה במילה: ``id TEXT PRIMARY KEY``. היא
# נשמרת כאן כדי שהבדיקות יפתחו מאגר ישן אמיתי ולא יסתמכו על דמה.
GLOBAL_ID_SCHEMA = """
CREATE TABLE raw_messages (
    id                 TEXT PRIMARY KEY,
    flight_id          TEXT NOT NULL,
    timestamp          TEXT NOT NULL,
    ingested_at        TEXT NOT NULL,
    source             TEXT NOT NULL,
    channel            TEXT,
    text               TEXT NOT NULL,
    language           TEXT NOT NULL,
    dedup_hash         TEXT,
    status             TEXT NOT NULL,
    dead_letter_reason TEXT,
    metadata           TEXT NOT NULL,
    stored_at          TEXT NOT NULL
);
CREATE INDEX raw_messages_flight_idx ON raw_messages (flight_id);
CREATE INDEX raw_messages_flight_time_idx ON raw_messages (flight_id, timestamp);
CREATE INDEX raw_messages_flight_hash_idx ON raw_messages (flight_id, dedup_hash);
CREATE TRIGGER raw_messages_block_update
BEFORE UPDATE ON raw_messages
BEGIN
    SELECT RAISE(ABORT, 'raw_messages is append-only: UPDATE is blocked');
END;
CREATE TRIGGER raw_messages_block_delete
BEFORE DELETE ON raw_messages
BEGIN
    SELECT RAISE(ABORT, 'raw_messages is append-only: DELETE is blocked');
END;
CREATE TABLE dedup_keys (
    key           TEXT PRIMARY KEY,
    message_id    TEXT NOT NULL,
    remembered_at TEXT NOT NULL
);
"""

LEGACY_PK_TABLE = "raw_messages_global_id_pk"


def write_global_id_store(path: Path, rows: list[tuple[str, str, str]]) -> None:
    """מאגר במבנה הישן, עם ``(id, flight_id, text)`` לכל שורה."""
    connection = sqlite3.connect(path)
    connection.executescript(GLOBAL_ID_SCHEMA)
    for message_id, flight_id, text in rows:
        connection.execute(
            "INSERT INTO raw_messages (id, flight_id, timestamp, ingested_at, source, channel, "
            "text, language, dedup_hash, status, dead_letter_reason, metadata, stored_at) "
            "VALUES (?, ?, '2026-09-01T06:00:00+00:00', '2026-09-01T06:00:00+00:00', 'operator', "
            "'command', ?, 'he', 'h', 'accepted', NULL, '{}', '2026-09-01T06:00:00+00:00')",
            (message_id, flight_id, text),
        )
    connection.commit()
    connection.close()


def primary_key_of(store: SQLiteRawTranscriptStore, table: str = "raw_messages") -> list[str]:
    rows = store.execute_sql(f"PRAGMA table_info({table})")
    keyed = sorted((row["pk"], row["name"]) for row in rows if row["pk"])
    return [name for _, name in keyed]


def table_names(store: SQLiteRawTranscriptStore) -> set[str]:
    rows = store.execute_sql("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {row["name"] for row in rows}


def test_a_store_created_with_a_global_id_key_is_migrated_when_opened(tmp_path: Path):
    """``CREATE TABLE IF NOT EXISTS`` לא היה מתקן מאגר קיים לעולם, ולכן
    הפתיחה היא שמעבירה אותו למפתח ``(flight_id, id)`` - בלי לאבד שורה
    ובלי לאבד את אכיפת ה-append-only."""
    db_path = tmp_path / "old.db"
    write_global_id_store(
        db_path, [("m01", "f1", "בקרה, נץ אחת, הכל תקין."), ("f2-m01", "f2", "נץ שתיים, ממתינים.")]
    )

    with SQLiteRawTranscriptStore(db_path) as store:
        assert primary_key_of(store) == ["flight_id", "id"]
        assert LEGACY_PK_TABLE not in table_names(store), "the moved-aside table is cleaned up"

        assert store.count() == 2
        assert store.get("m01", flight_id="f1").text == "בקרה, נץ אחת, הכל תקין."
        assert store.get("f2-m01", flight_id="f2").text == "נץ שתיים, ממתינים."

        # האינדקסים והטריגרים נבנו מחדש על הטבלה החדשה ולא נעלמו עם הישנה.
        indexes = {
            row["name"]
            for row in store.execute_sql(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = 'raw_messages'"
            )
        }
        assert "raw_messages_id_idx" in indexes
        with pytest.raises(AppendOnlyViolationError):
            store.execute_sql("UPDATE raw_messages SET text = 'שונה' WHERE id = 'm01'")

        # ומה שהמיגרציה קיימת בשבילו: אותו מזהה בטיסה אחרת.
        store.append(message(id="m01", flight_id="f3", text="נץ שלוש, בדרך."))
        assert store.count() == 3


def test_a_migration_cut_in_the_middle_is_finished_on_the_next_open(tmp_path: Path):
    """ההעתקה מסתיימת לפני שהטבלה הישנה נמחקת, ולכן הפסקה באמצע משאירה
    אותה במקום - והפתיחה הבאה ממשיכה מאיפה שנעצרה."""
    db_path = tmp_path / "interrupted.db"
    write_global_id_store(db_path, [("m01", "f1", "בקרה, נץ אחת, הכל תקין.")])
    with SQLiteRawTranscriptStore(db_path) as store:
        assert store.count() == 1

    # מצב של מיגרציה שנקטעה: הטבלה שהועברה הצידה עוד קיימת, ושורה אחת
    # שבה טרם הועתקה.
    connection = sqlite3.connect(db_path)
    connection.executescript(
        f"CREATE TABLE {LEGACY_PK_TABLE} AS SELECT * FROM raw_messages WHERE 0;"
        f"INSERT INTO {LEGACY_PK_TABLE} SELECT * FROM raw_messages;"
    )
    connection.execute(
        f"UPDATE {LEGACY_PK_TABLE} SET id = 'm09', flight_id = 'f9', text = 'שורה שלא הועתקה'"
    )
    connection.commit()
    connection.close()

    with SQLiteRawTranscriptStore(db_path) as store:
        assert store.count() == 2
        assert store.get("m09", flight_id="f9").text == "שורה שלא הועתקה"
        assert store.get("m01", flight_id="f1").text == "בקרה, נץ אחת, הכל תקין."
        assert LEGACY_PK_TABLE not in table_names(store)


def test_postgres_migration_covers_the_same_columns_and_blocks_writes():
    """המיגרציה ל-Postgres אינה נבדקת ב-CI (אין שם מסד), ולכן לפחות
    נבדקת התאמתה לסכמת ה-SQLite ולדרישת ההרשאות שבתכנית."""
    sqlite_schema = (RAW_STORE_PKG / "schema.sql").read_text(encoding="utf-8")
    migration = (RAW_STORE_PKG / "migrations" / "001_raw_messages.postgres.sql").read_text(
        encoding="utf-8"
    )

    for column in ("flight_id", "timestamp", "ingested_at", "source", "text", "metadata"):
        assert column in sqlite_schema, column
        assert column in migration, column
    assert "REVOKE ALL ON raw_messages" in migration
    assert "GRANT SELECT, INSERT ON raw_messages" in migration
    assert "BEFORE UPDATE OR DELETE ON raw_messages" in migration

    # המפתח הראשון היה גלובלי; המיגרציה השנייה מחליפה אותו באותו מפתח
    # שהסכמה הנוכחית של SQLite מצהירה עליו, כדי ששני המנועים לא ייפרדו.
    flight_scoped = (
        RAW_STORE_PKG / "migrations" / "002_flight_scoped_message_id.postgres.sql"
    ).read_text(encoding="utf-8")
    assert "PRIMARY KEY (flight_id, id)" in sqlite_schema
    assert "ADD PRIMARY KEY (flight_id, id)" in flight_scoped
    assert "raw_messages_id_idx" in sqlite_schema
    assert "raw_messages_id_idx" in flight_scoped


def test_text_is_stored_without_sqlite_type_coercion(store: SQLiteRawTranscriptStore):
    """משפט שנראה כמספר נשמר כטקסט, לא כמספר."""
    store.append(message(text="123"))

    row = store.execute_sql("SELECT typeof(text) AS t FROM raw_messages")[0]
    assert row["t"] == "text"
    assert store.get("f1-m01").text == "123"


def test_raw_sql_insert_without_metadata_is_refused_by_the_schema(
    store: SQLiteRawTranscriptStore,
):
    """גם כתיבה ישרה ב-SQL חייבת לעמוד באילוצי הטבלה, כך ששורה שלא ניתן
    להמיר בחזרה ל-``Message`` לא נכנסת מההתחלה."""
    with pytest.raises(sqlite3.IntegrityError):
        store.execute_sql(
            "INSERT INTO raw_messages (id, flight_id, timestamp, ingested_at, source, text, "
            "language, status, metadata, stored_at) VALUES "
            "('x', 'f1', '2026-09-01T06:00:00+00:00', '2026-09-01T06:00:00+00:00', 'ops', 't', "
            "'he', 'accepted', NULL, '2026-09-01T06:00:00+00:00')"
        )

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

    assert {"raw_messages_flight_idx", "raw_messages_flight_time_idx"} <= names
    # ה-PRIMARY KEY של id מייצר אינדקס משלו, כך ששליפה לפי מזהה אינה סריקה.
    plan = store.execute_sql("EXPLAIN QUERY PLAN SELECT * FROM raw_messages WHERE id = 'x'")
    assert "SEARCH" in " ".join(str(tuple(row)) for row in plan)


def test_applying_the_schema_twice_is_harmless(tmp_path: Path):
    db_path = tmp_path / "twice.db"
    with SQLiteRawTranscriptStore(db_path) as first:
        first.append(message())
    with SQLiteRawTranscriptStore(db_path) as second:
        assert second.count() == 1


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

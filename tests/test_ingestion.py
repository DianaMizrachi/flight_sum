"""Stage 1 tests - the ingestion component against the stage 0 fixtures.

The pass criterion for stage 1 is phrased in the work plan as: 100% of the
valid golden-set messages are ingested, and every duplicate/error scenario
behaves as labelled. Those two sentences are the first two test groups here;
the rest cover normalization and the late-arrival ordering rule.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from aura.ingestion import (
    DeadLetterReason,
    DedupStrategy,
    IngestionService,
    InMemoryQueue,
    normalize_text,
    normalize_timestamp,
)
from aura.schemas import MessageStatus

FIXTURES_DIR = Path(__file__).parent / "fixtures"
GOLDEN_FLIGHTS_DIR = FIXTURES_DIR / "golden_flights"
EDGE_CASES_DIR = FIXTURES_DIR / "edge_cases"

FROZEN_NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def raw_inputs(flight: dict) -> list[dict]:
    """The fixture messages as the ingest endpoint would receive them -
    without the ``expected`` block, which is manual labelling for later
    stages, not part of the payload."""
    return [{k: v for k, v in message.items() if k != "expected"} for message in flight["messages"]]


@pytest.fixture
def service() -> IngestionService:
    return IngestionService(clock=lambda: FROZEN_NOW)


# --- קריטריון המעבר: סט הזהב נקלט במלואו -------------------------------


@pytest.mark.parametrize(
    "flight_path", sorted(GOLDEN_FLIGHTS_DIR.glob("*.yaml")), ids=lambda p: p.stem
)
def test_every_golden_flight_message_is_accepted(flight_path: Path, service: IngestionService):
    flight = load_yaml(flight_path)
    payloads = raw_inputs(flight)

    results = service.ingest_many(payloads)

    assert [r.status for r in results] == [MessageStatus.ACCEPTED] * len(payloads)
    assert service.dead_letters == []
    assert len(service.queue.published) == len(payloads)


@pytest.mark.parametrize(
    "flight_path", sorted(GOLDEN_FLIGHTS_DIR.glob("*.yaml")), ids=lambda p: p.stem
)
def test_golden_flight_ingest_preserves_ids_text_and_timestamps(
    flight_path: Path, service: IngestionService
):
    flight = load_yaml(flight_path)
    payloads = raw_inputs(flight)

    results = service.ingest_many(payloads)

    for raw, result in zip(payloads, results, strict=True):
        message = result.message
        assert message is not None
        assert message.id == raw["id"]
        assert message.flight_id == flight["flight_id"]
        assert message.text == raw["text"], "golden transcripts need no normalization"
        assert message.timestamp == normalize_timestamp(raw["timestamp"])
        assert message.timestamp.tzinfo == UTC
        assert message.language == "he"
        assert message.dedup_hash is not None


def test_accepted_messages_are_published_to_the_queue_in_arrival_order():
    queue = InMemoryQueue()
    service = IngestionService(queue=queue, clock=lambda: FROZEN_NOW)
    flight = load_yaml(GOLDEN_FLIGHTS_DIR / "flight_01_recon_patrol.yaml")
    payloads = raw_inputs(flight)

    service.ingest_many(payloads)

    assert [m.id for m in queue.drain()] == [raw["id"] for raw in payloads]
    assert len(queue) == 0, "drain empties the queue"


# --- כפילויות ----------------------------------------------------------


def test_duplicate_cases_match_fixture_counts():
    data = load_yaml(EDGE_CASES_DIR / "duplicates.yaml")
    for case in data["cases"]:
        service = IngestionService(clock=lambda: FROZEN_NOW)
        results = service.ingest_many(case["raw_inputs"])
        expected = case["expected"]
        counts = {status: 0 for status in MessageStatus}
        for result in results:
            counts[result.status] += 1

        assert counts[MessageStatus.ACCEPTED] == expected["accepted_count"], case["case_id"]
        assert counts[MessageStatus.DUPLICATE] == expected["duplicate_count"], case["case_id"]
        assert counts[MessageStatus.DEAD_LETTER] == expected["dead_letter_count"], case["case_id"]
        assert len(service.queue.published) == expected["accepted_count"], case["case_id"]

        if "dedup_strategy" in expected:
            strategies = {r.dedup_strategy for r in results}
            assert strategies == {DedupStrategy(expected["dedup_strategy"])}, case["case_id"]


def test_duplicate_is_flagged_rather_than_dropped_silently(service: IngestionService):
    data = load_yaml(EDGE_CASES_DIR / "duplicates.yaml")
    case = next(c for c in data["cases"] if c["case_id"] == "duplicate-exact-id")

    first, second = service.ingest_many(case["raw_inputs"])

    assert first.status == MessageStatus.ACCEPTED
    assert first.queued is True
    assert second.status == MessageStatus.DUPLICATE
    assert second.queued is False, "a duplicate must not reach the analysis pipeline twice"
    assert second.message is not None
    assert second.message.status == MessageStatus.DUPLICATE
    assert second.duplicate_of == first.message.id
    assert second.dedup_strategy == DedupStrategy.BY_ID
    assert len(service.queue.published) == 1


def test_duplicate_without_source_id_is_caught_by_hash(service: IngestionService):
    data = load_yaml(EDGE_CASES_DIR / "duplicates.yaml")
    case = next(c for c in data["cases"] if c["case_id"] == "duplicate-missing-id-same-hash")

    first, second = service.ingest_many(case["raw_inputs"])

    assert first.status == MessageStatus.ACCEPTED
    assert second.status == MessageStatus.DUPLICATE
    assert second.dedup_strategy == DedupStrategy.BY_HASH
    # המזהה הנגזר מה-hash דטרמיניסטי, כך ששני העותקים מצביעים לאותה הודעה.
    assert second.message.id == first.message.id
    assert second.message.dedup_hash == first.message.dedup_hash


def test_dedup_hash_ignores_whitespace_and_bidi_noise(service: IngestionService):
    base = {
        "flight_id": "flight-x",
        "timestamp": "2026-09-10T08:00:00Z",
        "source": "operator",
        "text": "בקרה, נץ אחת, הכל תקין.",
    }
    noisy = {**base, "text": "  בקרה,\u200f נץ אחת,\tהכל   תקין.  "}

    first = service.ingest(base)
    second = service.ingest(noisy)

    assert first.status == MessageStatus.ACCEPTED
    assert second.status == MessageStatus.DUPLICATE, "normalization happens before dedup"


# --- הודעות באיחור ------------------------------------------------------


def test_late_arrival_keeps_original_timestamp_and_chronological_order(service: IngestionService):
    data = load_yaml(EDGE_CASES_DIR / "late_arrival.yaml")
    expected = data["expected"]

    results = service.ingest_many(data["raw_inputs"])
    messages = [r.message for r in results]

    assert all(r.status == MessageStatus.ACCEPTED for r in results)
    # הסדר שבו ההודעות נקלטו אינו הסדר הכרונולוגי...
    assert [m.id for m in messages] != expected["chronological_order_by_timestamp"]
    # ...אבל מיון לפי חותמת הזמן המקורית משחזר אותו במדויק.
    by_timestamp = sorted(messages, key=lambda m: m.timestamp)
    assert [m.id for m in by_timestamp] == expected["chronological_order_by_timestamp"]

    late = next(m for m in messages if m.id == expected["late_message_id"])
    assert (late.ingested_at - late.timestamp) == timedelta(seconds=expected["late_by_seconds"])


def test_ingested_at_falls_back_to_the_clock_when_the_source_omits_it(service: IngestionService):
    result = service.ingest(
        {
            "id": "flight-y-m01",
            "flight_id": "flight-y",
            "timestamp": "2026-09-10T11:59:00Z",
            "source": "operator",
            "text": "בקרה, נץ שתיים, מתחילים סריקה.",
        }
    )

    assert result.message.ingested_at == FROZEN_NOW
    assert result.message.timestamp < result.message.ingested_at


# --- הודעות פגומות -> dead-letter --------------------------------------


def test_malformed_cases_are_dead_lettered_with_the_labelled_reason(service: IngestionService):
    data = load_yaml(EDGE_CASES_DIR / "malformed_and_empty.yaml")

    for case in data["cases"]:
        expected = case["expected"]
        result = service.ingest(case["raw_input"])

        assert result.status == MessageStatus.DEAD_LETTER, case["case_id"]
        assert result.message is None, case["case_id"]
        assert result.dead_letter.reason == DeadLetterReason(expected["reason"]), case["case_id"]
        if "missing_field" in expected:
            assert result.dead_letter.missing_field == expected["missing_field"], case["case_id"]

    assert len(service.dead_letters) == len(data["cases"]), (
        "every rejection is logged, not swallowed"
    )
    assert len(service.queue.published) == 0


def test_dead_letter_record_keeps_the_raw_payload_for_a_later_replay(service: IngestionService):
    data = load_yaml(EDGE_CASES_DIR / "malformed_and_empty.yaml")
    case = next(c for c in data["cases"] if c["case_id"] == "whitespace-only-text")

    result = service.ingest(case["raw_input"])

    record = result.dead_letter
    assert record.raw_payload == case["raw_input"]
    assert record.flight_id == "flight-edge-03"
    assert record.message_id == "flight-edge-03-m03"
    assert record.recorded_at == FROZEN_NOW


def test_unparseable_timestamp_is_dead_lettered_not_raised(service: IngestionService):
    result = service.ingest(
        {
            "id": "flight-z-m01",
            "flight_id": "flight-z",
            "timestamp": "לפני רגע",
            "source": "operator",
            "text": "בקרה, נץ שלוש, הכל תקין.",
        }
    )

    assert result.status == MessageStatus.DEAD_LETTER
    assert result.dead_letter.reason == DeadLetterReason.INVALID_TIMESTAMP
    assert result.dead_letter.detail


def test_blank_flight_id_is_treated_as_a_missing_required_field(service: IngestionService):
    result = service.ingest(
        {
            "id": "flight-z-m02",
            "flight_id": "   ",
            "timestamp": "2026-09-10T08:00:00Z",
            "source": "operator",
            "text": "בקרה, נץ שלוש, הכל תקין.",
        }
    )

    assert result.dead_letter.reason == DeadLetterReason.MISSING_REQUIRED_FIELD
    assert result.dead_letter.missing_field == "flight_id"


# --- נרמול --------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  בקרה, נץ אחת.  ", "בקרה, נץ אחת."),
        ("בקרה,\tנץ\nאחת.", "בקרה, נץ אחת."),
        ("בקרה,   נץ    אחת.", "בקרה, נץ אחת."),
        ("\u200fבקרה, נץ אחת.\u200e", "בקרה, נץ אחת."),
        ("\ufeffבקרה, נץ אחת.", "בקרה, נץ אחת."),
        ("בקרה,\u00a0נץ אחת.", "בקרה, נץ אחת."),
        ("   \t  ", ""),
    ],
)
def test_normalize_text_cases(raw: str, expected: str):
    assert normalize_text(raw) == expected


def test_normalize_text_applies_nfc_so_equivalent_encodings_dedup_together():
    """Two encodings of one character must normalize to the same text -
    otherwise the dedup hash would differ for identical sentences."""
    composed = "\u00e1"  # a with acute, a single code point
    decomposed = "\u0061\u0301"  # a + combining acute
    assert composed != decomposed
    assert normalize_text(decomposed) == normalize_text(composed) == composed


def test_normalized_message_keeps_the_original_text_in_metadata(service: IngestionService):
    raw_text = "  בקרה,\u200f נץ אחת,\tהכל   תקין.  "

    result = service.ingest(
        {
            "id": "flight-w-m01",
            "flight_id": "flight-w",
            "timestamp": "2026-09-10T08:00:00Z",
            "source": " operator ",
            "channel": " command ",
            "text": raw_text,
        }
    )

    message = result.message
    assert message.text == "בקרה, נץ אחת, הכל תקין."
    assert message.metadata["raw_text"] == raw_text, "stage 2 must be able to return the original"
    assert message.source == "operator"
    assert message.channel == "command"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-09-10T08:00:00Z", datetime(2026, 9, 10, 8, 0, tzinfo=UTC)),
        ("2026-09-10T11:00:00+03:00", datetime(2026, 9, 10, 8, 0, tzinfo=UTC)),
        ("2026-09-10T08:00:00", datetime(2026, 9, 10, 8, 0, tzinfo=UTC)),
        (datetime(2026, 9, 10, 8, 0), datetime(2026, 9, 10, 8, 0, tzinfo=UTC)),
    ],
)
def test_normalize_timestamp_converts_everything_to_utc(raw, expected: datetime):
    assert normalize_timestamp(raw) == expected


def test_unknown_source_fields_are_recorded_rather_than_dropped(service: IngestionService):
    result = service.ingest(
        {
            "id": "flight-v-m01",
            "flight_id": "flight-v",
            "timestamp": "2026-09-10T08:00:00Z",
            "source": "operator",
            "text": "בקרה, נץ ארבע, הכל תקין.",
            "confidence_from_asr": 0.82,
        }
    )

    assert result.message.metadata["unmapped_fields"] == "confidence_from_asr"

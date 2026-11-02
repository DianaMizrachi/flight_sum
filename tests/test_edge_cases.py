"""The edge-case fixtures, judged by the components that own the decision.

These fixtures were written in stage 0, before ingestion and the raw store
existed, so the tests beside them re-implemented the behaviour they were
describing: the test computed its own dedup key, did its own sort, and
checked that Pydantic rejected a malformed payload. That proved the
fixtures were self-consistent and nothing more - the suite would have
stayed green while ``IngestionService`` deduplicated on the wrong key, or
``SQLiteRawTranscriptStore`` returned a late sentence out of order, since
neither was ever called.

Components 1 and 2 exist now, so every expectation below is checked
against them: the counts and the dedup strategy come from
:class:`IngestionService`, the chronological order comes from the store,
and each dead-letter reason is the reason ingestion actually recorded.
The one exception is ``same_sentence_contradiction``, whose check belongs
to the validator (stage 5) and is still stated as a contract.
"""

from datetime import datetime
from pathlib import Path

import pytest
import yaml

from aura.ingestion import DeadLetterReason, DedupStrategy, IngestionService
from aura.raw_store import RawStoreDedupIndex, SQLiteRawTranscriptStore
from aura.schemas import LLMResult, Message, MessageStatus

EDGE_CASES_DIR = Path(__file__).parent / "fixtures" / "edge_cases"


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture
def store() -> SQLiteRawTranscriptStore:
    with SQLiteRawTranscriptStore(":memory:") as opened:
        yield opened


@pytest.fixture
def service(store: SQLiteRawTranscriptStore) -> IngestionService:
    """רכיב 1 מעל רכיב 2 - אותו חיבור שהשירות מריץ בפועל.

    ``RawStoreDedupIndex`` מחייב שזיהוי הכפילויות יסתמך על מה שבאמת
    נשמר, ולא על אינדקס בזיכרון שחי רק בתוך הבדיקה.
    """
    return IngestionService(store=store, dedup_index=RawStoreDedupIndex(store))


def counts_of(results: list) -> tuple[int, int, int]:
    statuses = [result.status for result in results]
    return (
        statuses.count(MessageStatus.ACCEPTED),
        statuses.count(MessageStatus.DUPLICATE),
        statuses.count(MessageStatus.DEAD_LETTER),
    )


# --- duplicates ---------------------------------------------------------


DUPLICATE_CASES = load_yaml(EDGE_CASES_DIR / "duplicates.yaml")["cases"]


@pytest.mark.parametrize("case", DUPLICATE_CASES, ids=lambda c: c["case_id"])
def test_ingestion_produces_the_declared_duplicate_counts(case, service):
    """מה שהפיקסצ'ר מצהיר עליו הוא מה שרכיב 1 מחזיר."""
    results = service.ingest_many(case["raw_inputs"])

    expected = case["expected"]
    assert counts_of(results) == (
        expected["accepted_count"],
        expected["duplicate_count"],
        expected["dead_letter_count"],
    ), case["case_id"]


@pytest.mark.parametrize("case", DUPLICATE_CASES, ids=lambda c: c["case_id"])
def test_ingestion_uses_the_declared_dedup_strategy(case, service):
    """הפולבק ל-hash הוא טענה על *איך* הכפילות נתפסה, לא רק על הספירה.

    פיקסצ'ר ``duplicate-missing-id-same-hash`` היה נספר נכון גם אם
    הכפילות הייתה נתפסת לפי מזהה נגזר כלשהו; הטענה היא שהיא נתפסת
    בהיעדר מזהה מהמקור, ולכן האסטרטגיה נבדקת בשמה.
    """
    declared = case["expected"].get("dedup_strategy")
    if declared is None:
        pytest.skip("this case is not about a duplicate")
    results = service.ingest_many(case["raw_inputs"])

    strategies = {result.dedup_strategy for result in results}
    assert strategies == {DedupStrategy(declared)}, case["case_id"]
    duplicate = next(r for r in results if r.status == MessageStatus.DUPLICATE)
    accepted = next(r for r in results if r.status == MessageStatus.ACCEPTED)
    assert duplicate.duplicate_of == accepted.message.id
    assert not duplicate.queued, "a duplicate must not be processed a second time"


@pytest.mark.parametrize("case", DUPLICATE_CASES, ids=lambda c: c["case_id"])
def test_a_duplicate_is_stored_exactly_once(case, service, store):
    """התוצאה הגלויה של הכפילות: במאגר יש שורה אחת, והיא המקורית."""
    results = service.ingest_many(case["raw_inputs"])

    accepted = [r for r in results if r.status == MessageStatus.ACCEPTED]
    assert store.count("flight-edge-01") == len(accepted)
    for result in accepted:
        stored = store.get(result.message.id, flight_id="flight-edge-01")
        assert stored is not None
        assert stored.text == result.message.text


def test_a_resent_sentence_keeps_the_text_of_the_first_copy(service):
    """ההודעה השנייה אינה נכתבת, ולכן המשפט במאגר הוא זה שנקלט ראשון."""
    case = next(c for c in DUPLICATE_CASES if c["case_id"] == "duplicate-exact-id")
    first, second = case["raw_inputs"]

    service.ingest(first)
    result = service.ingest(second)

    assert result.status == MessageStatus.DUPLICATE
    assert result.message.text == first["text"]
    assert result.message.status == MessageStatus.DUPLICATE


# --- late arrival -------------------------------------------------------


LATE_ARRIVAL = load_yaml(EDGE_CASES_DIR / "late_arrival.yaml")


def test_the_raw_store_returns_a_late_sentence_in_its_spoken_order(service, store):
    """הסדר הכרונולוגי מגיע מהמאגר, לא מ-``sorted()`` שבבדיקה.

    שלוש ההודעות נקלטות בסדר שבו *הגיעו* - m01, m03, m02 - והמאגר
    חייב להחזיר אותן לפי חותמת הזמן המקורית.
    """
    results = service.ingest_many(LATE_ARRIVAL["raw_inputs"])
    assert counts_of(results) == (3, 0, 0)

    ordered = [message.id for message in store.by_flight("flight-edge-02")]
    assert ordered == LATE_ARRIVAL["expected"]["chronological_order_by_timestamp"]
    assert ordered != [raw["id"] for raw in LATE_ARRIVAL["raw_inputs"]], (
        "the fixture must actually arrive out of order, or it tests nothing"
    )


def test_a_late_sentence_is_stored_with_both_of_its_times(service, store):
    """``timestamp`` ו-``ingested_at`` נשמרים בנפרד, וההפרש הוא האיחור.

    זה מה שמאפשר לרכיבים 3 ו-6 לדעת שההודעה אחרה, ולא רק היכן היא
    יושבת בסדר.
    """
    service.ingest_many(LATE_ARRIVAL["raw_inputs"])

    late_id = LATE_ARRIVAL["expected"]["late_message_id"]
    stored = store.get(late_id, flight_id="flight-edge-02")
    assert stored is not None
    lateness = (stored.ingested_at - stored.timestamp).total_seconds()
    assert lateness == LATE_ARRIVAL["expected"]["late_by_seconds"]


def test_the_late_sentence_is_returned_by_a_time_range_query(service, store):
    """שליפה לפי טווח זמן מוצאת את ההודעה המאחרת לפי זמן הדיבור שלה."""
    service.ingest_many(LATE_ARRIVAL["raw_inputs"])

    start = datetime.fromisoformat("2026-09-05T10:00:05Z")
    end = datetime.fromisoformat("2026-09-05T10:00:15Z")
    window = store.by_time_range("flight-edge-02", start=start, end=end)
    assert [message.id for message in window] == [LATE_ARRIVAL["expected"]["late_message_id"]]


def test_the_late_sentence_arrives_verbatim(service, store):
    service.ingest_many(LATE_ARRIVAL["raw_inputs"])

    for raw in LATE_ARRIVAL["raw_inputs"]:
        stored = store.get(raw["id"], flight_id="flight-edge-02")
        assert stored.text == raw["text"]


# --- malformed and empty ------------------------------------------------


MALFORMED_CASES = load_yaml(EDGE_CASES_DIR / "malformed_and_empty.yaml")["cases"]


@pytest.mark.parametrize("case", MALFORMED_CASES, ids=lambda c: c["case_id"])
def test_ingestion_dead_letters_each_malformed_case_with_its_reason(case, service, store):
    """הסיבה שנבדקת היא זו שרכיב 1 רשם, לא זו שהבדיקה הניחה.

    קודם לכן נבדק רק ש-``Message.model_validate`` נכשל, מה שלא מבחין
    בין ``missing_required_field`` ל-``empty_text`` - וההבחנה הזו היא
    כל מה שהפיקסצ'ר מצהיר עליו.
    """
    result = service.ingest(case["raw_input"])

    expected = case["expected"]
    assert result.status == MessageStatus.DEAD_LETTER
    assert result.dead_letter.reason == DeadLetterReason(expected["reason"])
    assert result.dead_letter.missing_field == expected.get("missing_field")
    assert result.message is None
    assert not result.queued
    assert store.count() == 0, "a malformed sentence must not reach the raw store"


@pytest.mark.parametrize("case", MALFORMED_CASES, ids=lambda c: c["case_id"])
def test_a_rejected_sentence_keeps_its_raw_payload(case, service):
    """"שמירה לפני עיבוד" חל גם על דחייה: אפשר לקלוט מחדש אחרי תיקון."""
    result = service.ingest(case["raw_input"])

    assert result.dead_letter.raw_payload == case["raw_input"]


@pytest.mark.parametrize("case", MALFORMED_CASES, ids=lambda c: c["case_id"])
def test_every_malformed_case_is_recorded_in_the_dead_letter_log(case, service):
    """הלוג הוא המקום היחיד לחפש בו הודעה שנדחתה - ולכן היא נמצאת בו."""
    service.ingest(case["raw_input"])

    assert len(service.dead_letters) == 1
    record = service.dead_letters[0]
    assert record.reason == DeadLetterReason(case["expected"]["reason"])
    assert record.recorded_at is not None


def test_the_whole_malformed_fixture_leaves_the_store_empty(service, store):
    """חמש השורות יחד: אף אחת אינה נקלטת, ואף אחת אינה נעלמת."""
    results = service.ingest_many(case["raw_input"] for case in MALFORMED_CASES)

    assert counts_of(results) == (0, 0, len(MALFORMED_CASES))
    assert store.count() == 0
    assert len(service.dead_letters) == len(MALFORMED_CASES)


def test_malformed_cases_missing_required_field_fail_message_validation():
    """הטענה החוזית שנשארת: הסכמה עצמה דוחה שדה חובה חסר.

    זו טענה על ``Message`` (שלב 0), ולכן היא נבדקת מול המודל. ההחלטה
    *מה הסיבה* נבדקת למעלה, מול רכיב 1.
    """
    for case in MALFORMED_CASES:
        if case["expected"]["reason"] != "missing_required_field":
            continue
        payload = {**case["raw_input"], "ingested_at": "2026-09-06T12:00:00Z"}
        with pytest.raises(Exception):
            Message.model_validate(payload)


def test_malformed_cases_empty_text_passes_schema_but_is_blank():
    """Schema allows an empty str - blankness is an ingestion business rule,
    not a contract violation, so Message validation must succeed here while
    the dead-letter reason above is ingestion's own decision."""
    for case in MALFORMED_CASES:
        if case["expected"]["reason"] != "empty_text":
            continue
        payload = {**case["raw_input"], "ingested_at": "2026-09-06T12:00:00Z"}
        message = Message.model_validate(payload)
        assert message.text.strip() == ""


# --- same sentence, two contradicting events ----------------------------


def test_same_sentence_contradiction_is_schema_valid_but_semantically_bad():
    """The two contradicting events are structurally fine (this is exactly
    why stage 5 needs an explicit contradiction check, not just schema
    validation)."""
    data = load_yaml(EDGE_CASES_DIR / "same_sentence_contradiction.yaml")
    Message.model_validate(data["source_message"])
    result = LLMResult.model_validate(data["llm_result"])
    assert len(result.events) == 2
    source_id_sets = [set(e.source_ids) for e in result.events]
    assert source_id_sets[0] == source_id_sets[1], "fixture must share the same source sentence"
    assert result.events[0].relation_to_previous != result.events[1].relation_to_previous or (
        result.events[0].type != result.events[1].type
    )


def test_the_contradicting_sentence_itself_is_ingested_normally(service, store):
    """המשפט שעליו נחלקים שני האירועים נקלט כמשפט רגיל ובמלואו.

    הסתירה היא בפלט המודל, לא בקלט; אם הקליטה הייתה דוחה את המשפט,
    לא היה למה שרכיב 5 יתייחס.
    """
    data = load_yaml(EDGE_CASES_DIR / "same_sentence_contradiction.yaml")
    raw = data["source_message"]

    result = service.ingest(raw)

    assert result.status == MessageStatus.ACCEPTED
    stored = store.get(raw["id"], flight_id=raw["flight_id"])
    assert stored.text == raw["text"]
    for event in LLMResult.model_validate(data["llm_result"]).events:
        assert list(event.source_ids) == [stored.id], (
            "both events must cite the sentence as the store knows it"
        )

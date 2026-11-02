"""The tagged fixtures, uploaded the way a user uploads them.

``samples/fixtures/*.txt`` is every fixture under ``tests/fixtures/``
written in the transcript file format, so that the tagged sentences can
be loaded in the client instead of only existing as YAML a test can read.
These files are committed, and the tests here keep them honest in two
different ways:

they must still match the fixtures they were generated from (the exported
OpenAPI spec under ``docs/`` is kept in step the same way), and each one
must actually survive ``POST /messages/upload`` with its sentences intact
- read back through ``GET /flights/{id}/messages``, which is the endpoint
the dashboard reads.

The second part is the one worth having: a file that merely exists proves
nothing. Uploading it through the real app, and comparing what comes back
to the fixture, is what shows the client can load these sentences and
that the microservice receives them as they were tagged.
"""

import re
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from aura.api.app import create_app
from aura.api.dependencies import get_ingestion_service, get_raw_store
from aura.ingestion import IngestionService, find_flight_id_directive, parse_transcript_text
from aura.ingestion.fixture_transcripts import load_fixture_transcript, load_fixture_transcripts
from aura.raw_store import RawStoreDedupIndex, SQLiteRawTranscriptStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = ROOT / "tests" / "fixtures"
SAMPLES_DIR = ROOT / "samples" / "fixtures"

# הסקריפט הוא המקור היחיד לקבצים שבדיקות כאן בודקות, ולכן הוא מיובא
# ישירות ולא דרך ``importorskip`` - ייבוא שנשבר היה הופך את בדיקת ההיסט
# לדילוג שקט, וזו בדיוק הבדיקה שאמורה לתפוס קובץ מיושן.
sys.path.insert(0, str(ROOT / "scripts"))

import export_fixture_transcripts  # noqa: E402

SAMPLE_PATHS = sorted(SAMPLES_DIR.glob("*.txt"))

CLIENT_PAGE = ROOT / "src" / "aura" / "api" / "static" / "index.html"


def client_accepts() -> set[str]:
    """הסיומות שהקליינט מצהיר עליהן ב-``<input type="file" accept=...>``.

    נקרא מהדף ולא מועתק לכאן, כדי שצמצום של ``accept`` ייכשל במקום
    להשאיר בדיקה שמסכימה עם עצמה.
    """
    match = re.search(r'id="pick"[^>]*accept="([^"]+)"', CLIENT_PAGE.read_text(encoding="utf-8"))
    assert match, "the client's file input no longer declares what it accepts"
    return {part.strip() for part in match.group(1).split(",") if part.strip().startswith(".")}


def client_sniff_bytes() -> int:
    """כמה בייטים הקליינט קורא כדי למצוא את הנחיית ``# flight_id:``."""
    match = re.search(
        r"file\.slice\(0,\s*(\d+)\)", CLIENT_PAGE.read_text(encoding="utf-8")
    )
    assert match, "the client no longer sniffs the file for a flight_id directive"
    return int(match.group(1))


@pytest.fixture
def store() -> SQLiteRawTranscriptStore:
    with SQLiteRawTranscriptStore(":memory:") as opened:
        yield opened


@pytest.fixture
def client(store: SQLiteRawTranscriptStore) -> TestClient:
    service = IngestionService(store=store, dedup_index=RawStoreDedupIndex(store))
    app = create_app()
    app.dependency_overrides[get_ingestion_service] = lambda: service
    app.dependency_overrides[get_raw_store] = lambda: store
    with TestClient(app) as test_client:
        yield test_client


def upload(client: TestClient, path: Path):
    """העלאה כמו מהקליינט: multipart, בלי מזהה טיסה בטופס."""
    return client.post(
        "/messages/upload",
        files={"file": (path.name, path.read_bytes(), "text/plain")},
    )


def fixture_for(sample: Path):
    matches = [
        candidate
        for candidate in FIXTURES_DIR.glob("*/*.yaml")
        if candidate.stem == sample.stem
    ]
    assert len(matches) == 1, f"{sample.name} has no single source fixture: {matches}"
    return load_fixture_transcript(matches[0])


# --- the files exist and still match their fixtures ---------------------


def test_every_fixture_has_an_uploadable_sample():
    """כל פיקסצ'ר מתוייג ניתן להעלאה - אין פיקסצ'ר שקיים רק כ-YAML."""
    fixtures = {transcript.name for transcript in load_fixture_transcripts(FIXTURES_DIR)}
    samples = {path.stem for path in SAMPLE_PATHS}

    assert fixtures, "no fixtures were found"
    assert fixtures == samples, (
        "run: python scripts/export_fixture_transcripts.py "
        f"(missing: {sorted(fixtures - samples)}, stale: {sorted(samples - fixtures)})"
    )


def test_the_committed_samples_match_their_fixtures():
    """הקבצים מתועדים ב-git, ולכן הם יכולים להתיישן - וזו הבדיקה.

    נכשלת גם כשפיקסצ'ר שונה בלי להריץ את הסקריפט, וגם כשהתוצאה שהכותרת
    מצהירה עליה אינה מה שהקליטה מחזירה בפועל, כי הסקריפט מודד אותה.
    """
    assert export_fixture_transcripts.export(check=True) == 0, (
        "samples/fixtures is out of date; run: python scripts/export_fixture_transcripts.py"
    )


@pytest.mark.parametrize("sample", SAMPLE_PATHS, ids=lambda p: p.stem)
def test_a_sample_is_a_file_the_client_would_accept(sample: Path):
    """סיומת, קידוד, והנחיית מזהה טיסה - שלושת התנאים להעלאה ללא הקלדה."""
    assert sample.suffix in client_accepts()
    text = sample.read_bytes().decode("utf-8")  # הקליינט דוחה קובץ שאינו UTF-8.
    assert find_flight_id_directive(text), (
        f"{sample.name} has no '# flight_id:' line, so the client would ask for one"
    )


@pytest.mark.parametrize("sample", SAMPLE_PATHS, ids=lambda p: p.stem)
def test_the_client_finds_the_flight_id_without_reading_the_whole_file(sample: Path):
    """ההנחיה נמצאת בחלון שהקליינט באמת קורא, ולא רק "בקובץ".

    הקליינט קורא את ראש הקובץ בלבד ושואל מזהה טיסה אם לא מצא הנחיה.
    הכותרות שהסקריפט כותב גדלות עם הפיקסצ'ר - פיקסצ'ר עם הרבה דחיות
    מצהירות דוחף את ההנחיה למטה - ולכן זה נבדק ולא מונח.
    """
    head = sample.read_bytes()[: client_sniff_bytes()].decode("utf-8", errors="ignore")

    assert find_flight_id_directive(head) == fixture_for(sample).flight_id, (
        f"{sample.name} hides its directive past the first {client_sniff_bytes()} bytes"
    )


@pytest.mark.parametrize("sample", SAMPLE_PATHS, ids=lambda p: p.stem)
def test_a_sample_parses_without_a_single_short_line(sample: Path):
    """כל שורת הודעה מחזיקה חמישה שדות - גם שורה ששדה בה ריק בכוונה."""
    parsed = parse_transcript_text(sample.read_text(encoding="utf-8"), source_name=sample.name)

    assert parsed.short_lines == [], (
        f"{sample.name} has malformed lines at {parsed.short_lines}"
    )
    assert parsed.message_count == len(fixture_for(sample).messages)


@pytest.mark.parametrize("sample", SAMPLE_PATHS, ids=lambda p: p.stem)
def test_a_sample_states_the_outcome_the_upload_really_produces(sample: Path, client):
    """הכותרת מבטיחה "x נקלטות, y כפילויות, z ל-dead-letter" - וזה נבדק.

    ההבטחה הזו היא מה שהקורא רואה לפני שהוא מעלה, ולכן היא לא יכולה
    להישאר הצהרה שאין מי שבודק אותה.
    """
    declared = sample.read_text(encoding="utf-8")
    body = upload(client, sample).json()

    promise = (
        f"{body['accepted_count']} נקלטות, {body['duplicate_count']} כפילויות, "
        f"{body['dead_letter_count']} ל-dead-letter"
    )
    assert promise in declared, f"{sample.name} promises something else than {promise}"


# --- uploading them really works ----------------------------------------


@pytest.mark.parametrize("sample", SAMPLE_PATHS, ids=lambda p: p.stem)
def test_uploading_a_sample_needs_no_flight_id_typed(sample: Path, client):
    """ההנחיה שבקובץ מספיקה: 200, ומזהה הטיסה מגיע מהקובץ."""
    response = upload(client, sample)

    assert response.status_code == 200, response.text
    report = response.json()["report"]
    assert report["flight_id"] == fixture_for(sample).flight_id
    assert report["flight_id_source"] == "directive"
    assert report["filename"] == sample.name
    assert report["message_lines"] == len(fixture_for(sample).messages)


@pytest.mark.parametrize("sample", SAMPLE_PATHS, ids=lambda p: p.stem)
def test_every_tagged_sentence_comes_back_from_the_api_verbatim(sample: Path, client):
    """הטענה המרכזית: מה שהדשבורד מציג הוא מה שהפיקסצ'ר תייג.

    הקריאה היא ל-``GET /flights/{id}/messages``, אותו מקום שממנו
    הקליינט קורא את התמלול, ולא ישירות למאגר.
    """
    transcript = fixture_for(sample)
    upload(client, sample)

    messages = client.get(f"/flights/{transcript.flight_id}/messages?limit=500").json()
    by_id = {message["id"]: message for message in messages}
    for message in transcript.storable():
        returned = by_id.get(str(message["id"]))
        assert returned is not None, (
            f"{message['id']} is missing from the transcript the client reads"
        )
        assert returned["text"] == message["text"], (
            f"{sample.name}/{message['id']} was tagged {message['text']!r} "
            f"but the API returns {returned['text']!r}"
        )


@pytest.mark.parametrize("sample", SAMPLE_PATHS, ids=lambda p: p.stem)
def test_an_uploaded_sample_is_ordered_and_traceable(sample: Path, client):
    """התמלול חוזר בסדר הדיבור, וכל שורה יודעת מאיזה קובץ ושורה היא באה."""
    transcript = fixture_for(sample)
    upload(client, sample)

    messages = client.get(f"/flights/{transcript.flight_id}/messages?limit=500").json()
    stamps = [message["timestamp"] for message in messages]
    assert stamps == sorted(stamps)
    for message in messages:
        assert message["metadata"]["source_file"] == sample.name
        assert message["metadata"]["source_line"].isdigit()


@pytest.mark.parametrize("sample", SAMPLE_PATHS, ids=lambda p: p.stem)
def test_uploading_a_sample_twice_is_a_duplicate_not_a_second_flight(sample: Path, client):
    """אותו קובץ לאותה טיסה אינו מכפיל את התמלול."""
    transcript = fixture_for(sample)
    first = upload(client, sample).json()
    before = client.get(f"/flights/{transcript.flight_id}/messages?limit=500").json()

    second = upload(client, sample).json()
    after = client.get(f"/flights/{transcript.flight_id}/messages?limit=500").json()

    assert len(after) == len(before)
    assert second["accepted_count"] == 0
    assert second["duplicate_count"] == first["accepted_count"] + first["duplicate_count"]


@pytest.mark.parametrize("sample", SAMPLE_PATHS, ids=lambda p: p.stem)
def test_a_sample_can_be_replayed_as_a_new_flight(sample: Path, client):
    """הקלדת מזהה טיסה בקליינט גוברת על ההנחיה, והקובץ נקלט מחדש.

    זה מה שמאפשר להריץ את אותו פיקסצ'ר שוב בלי למחוק כלום - והמאגר
    הוא append-only, ולכן זו הדרך היחידה.
    """
    original = upload(client, sample).json()
    replay = client.post(
        "/messages/upload",
        files={"file": (sample.name, sample.read_bytes(), "text/plain")},
        data={"flight_id": f"{sample.stem}-replay"},
    ).json()

    assert replay["report"]["flight_id"] == f"{sample.stem}-replay"
    assert replay["report"]["flight_id_source"] == "request"
    assert replay["accepted_count"] == original["accepted_count"]
    assert replay["duplicate_count"] == original["duplicate_count"]
    assert replay["dead_letter_count"] == original["dead_letter_count"]


def test_a_rejected_line_is_reported_with_its_reason_for_the_client_to_repair(client):
    """השורות הפגומות חוזרות עם סיבה - ממנה הקליינט בונה את תור התיקון."""
    sample = SAMPLES_DIR / "malformed_and_empty.txt"
    body = upload(client, sample).json()

    rejected = [result for result in body["results"] if result["dead_letter"]]
    assert len(rejected) == body["dead_letter_count"] == 4
    for result in rejected:
        assert result["dead_letter"]["reason"] in {"empty_text", "missing_required_field"}
        assert result["dead_letter"]["raw_payload"], "the client re-sends this payload repaired"


def test_the_golden_flights_land_in_the_flight_list_the_client_offers(client):
    """אחרי העלאה, הטיסות מופיעות ב-``GET /flights`` - ההשלמה בקליינט."""
    golden = sorted(SAMPLES_DIR.glob("flight_0*.txt"))
    assert golden, "no golden-flight samples were exported"
    for sample in golden:
        upload(client, sample)

    flights = client.get("/flights").json()
    for sample in golden:
        assert fixture_for(sample).flight_id in flights


def test_the_samples_are_documented_for_whoever_has_to_find_them():
    """קובץ שאף אחד לא יודע עליו אינו ניתן להעלאה בפועל."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "samples/fixtures" in readme
    assert "export_fixture_transcripts" in readme


def test_the_sample_sentences_are_the_fixture_sentences():
    """בדיקה אחת שלא עוברת דרך השירות: הטקסט בקבצים הוא הטקסט בפיקסצ'רים.

    נכשלת גם אם מישהו תיקן משפט ישירות בקובץ ה-``.txt`` במקום בפיקסצ'ר
    שממנו הוא נוצר.
    """
    for sample in SAMPLE_PATHS:
        transcript = fixture_for(sample)
        parsed = parse_transcript_text(sample.read_text(encoding="utf-8"))
        written = [message["text"] for message in parsed.messages]
        tagged = [str(message.get("text", "")).strip() for message in transcript.messages]
        assert written == tagged, f"{sample.name} does not hold its fixture's sentences"


def test_the_fixture_loader_refuses_a_transcript_that_mixes_flights(tmp_path: Path):
    """שומר על ההנחה שעליה כל הקבצים האלה מבוססים: קובץ = טיסה אחת."""
    path = tmp_path / "mixed.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "messages": [
                    {"id": "a", "flight_id": "flight-a", "text": "x"},
                    {"id": "b", "flight_id": "flight-b", "text": "y"},
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="mixes flights"):
        load_fixture_transcript(path)

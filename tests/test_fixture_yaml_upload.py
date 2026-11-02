"""Uploading a tagged fixture YAML straight into the client.

The fixtures are where the tagged sentences actually live, so a fixture
``.yaml`` is a file a user really picks - and it used to be the wrong
answer twice over: first every one of its 237 lines was dead-lettered one
by one, then it was refused as "not a transcript". Both times the sentences
were right there in the file and the service would not take them.

So the upload endpoint now reads both formats. The pipe-delimited ``.txt``
is unchanged; a fixture ``.yaml`` is recognised by its content and read
through the same fixture reader the export script uses. There is still one
ingest path behind them - same normalisation, same dedup, same
dead-letter - and these tests pin that down: the two formats of one
fixture must deliver the same sentences under the same ids.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aura.api.app import create_app
from aura.api.dependencies import get_ingestion_service, get_raw_store
from aura.ingestion import IngestionService
from aura.ingestion.fixture_transcripts import (
    load_fixture_transcript,
    looks_like_fixture_yaml,
    parse_fixture_upload,
)
from aura.raw_store import RawStoreDedupIndex, SQLiteRawTranscriptStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = ROOT / "tests" / "fixtures"
SAMPLES_DIR = ROOT / "samples" / "fixtures"

FIXTURE_PATHS = sorted(FIXTURES_DIR.glob("*/*.yaml"))


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


def upload(client: TestClient, path: Path, flight_id: str | None = None, name: str | None = None):
    return client.post(
        "/messages/upload",
        files={"file": (name or path.name, path.read_bytes(), "application/x-yaml")},
        data={"flight_id": flight_id} if flight_id else None,
    )


# --- the file the user actually picks ------------------------------------


@pytest.mark.parametrize("fixture", FIXTURE_PATHS, ids=lambda p: p.stem)
def test_a_fixture_yaml_uploads_without_anything_typed(fixture: Path, client):
    """200, והטיסה נלקחת מתוך הפיקסצ'ר עצמו."""
    response = upload(client, fixture)

    assert response.status_code == 200, response.text
    transcript = load_fixture_transcript(fixture)
    report = response.json()["report"]
    assert report["flight_id"] == transcript.flight_id
    assert report["filename"] == fixture.name
    assert report["message_lines"] == len(transcript.messages)
    assert report["short_lines"] == []


def test_the_golden_flight_that_was_rejected_now_loads_every_line(client):
    """המקרה המדויק שנכשל: 237 שורות, 0 נקלטות, ואז 400.

    עכשיו שתי התוצאות האלה שגויות - הקובץ מחזיק 14 משפטים מתוייגים,
    וכולם נקלטים.
    """
    fixture = FIXTURES_DIR / "golden_flights" / "flight_01_recon_patrol.yaml"

    body = upload(client, fixture).json()

    assert (body["accepted_count"], body["duplicate_count"], body["dead_letter_count"]) == (
        14,
        0,
        0,
    )
    assert body["report"]["flight_id"] == "flight-01"
    messages = client.get("/flights/flight-01/messages").json()
    assert len(messages) == 14
    assert messages[0]["text"] == "נץ אחת, בקרה, דווח מצב."


@pytest.mark.parametrize("fixture", FIXTURE_PATHS, ids=lambda p: p.stem)
def test_every_sentence_in_the_yaml_arrives_verbatim(fixture: Path, client):
    """אותה טענה שנבדקת על ה-``.txt``, על הפורמט השני."""
    transcript = load_fixture_transcript(fixture)
    upload(client, fixture)

    returned = client.get(f"/flights/{transcript.flight_id}/messages?limit=500").json()
    by_id = {message["id"]: message["text"] for message in returned}
    for message in transcript.storable():
        assert by_id.get(str(message["id"])) == message["text"], (
            f"{fixture.name}/{message['id']} did not arrive as tagged"
        )


@pytest.mark.parametrize("fixture", FIXTURE_PATHS, ids=lambda p: p.stem)
def test_an_uploaded_fixture_is_traceable_to_the_file(fixture: Path, client):
    """הודעה בפיקסצ'ר פרושה על כמה שורות, ולכן המקור הוא מקום ברצף.

    ``source_index`` ולא ``source_line``: מספר שורה היה צריך להיות
    מומצא כאן, ו-``metadata`` לא אמור להחזיק מספר שלא ניתן לחזור אליו.
    """
    transcript = load_fixture_transcript(fixture)
    upload(client, fixture)

    returned = client.get(f"/flights/{transcript.flight_id}/messages?limit=500").json()
    if not returned:
        # פיקסצ'ר שכל שורותיו מוצהרות כדחויות - נבדק בנפרד למטה.
        assert len(transcript.rejections) == len(transcript.messages)
        pytest.skip("this fixture declares every line rejected")
    for message in returned:
        assert message["metadata"]["source_file"] == fixture.name
        assert message["metadata"]["source_index"].isdigit()
        assert "source_line" not in message["metadata"]


# --- the two formats must agree ------------------------------------------


@pytest.mark.parametrize(
    "fixture", FIXTURE_PATHS, ids=lambda p: p.stem
)
def test_the_yaml_and_the_txt_deliver_the_same_sentences(fixture: Path, client):
    """הפורמט שבו הקובץ נבחר לא משנה את מה שנקלט.

    זו הטענה שמונעת מהתמיכה ב-YAML להיות מסלול קליטה שני: אותו פיקסצ'ר,
    פעם מ-``.yaml`` ופעם מה-``.txt`` שנוצר ממנו, נותן אותם משפטים תחת
    אותם מזהים.
    """
    sample = SAMPLES_DIR / f"{fixture.stem}.txt"
    assert sample.exists(), f"{sample.name} was not exported"

    from_yaml = upload(client, fixture, flight_id="via-yaml").json()
    from_txt = client.post(
        "/messages/upload",
        files={"file": (sample.name, sample.read_bytes(), "text/plain")},
        data={"flight_id": "via-txt"},
    ).json()

    from_yaml_messages = client.get("/flights/via-yaml/messages?limit=500").json()
    from_txt_messages = client.get("/flights/via-txt/messages?limit=500").json()

    # המשפטים עצמם, בסדר הדיבור - זו הטענה.
    assert [m["text"] for m in from_yaml_messages] == [m["text"] for m in from_txt_messages]
    # והמזהים, לשורות שיש להן מזהה מהמקור. שורה בלי מזהה מקבלת מזהה
    # נגזר מ-hash שמחושב *כולל* מזהה הטיסה, ולכן הוא שונה בין שתי
    # הטיסות כאן בהכרח - והשוואתו הייתה בודקת את שם הטיסה, לא את המשפט.
    source_ids = {str(m["id"]) for m in load_fixture_transcript(fixture).messages if m.get("id")}
    assert {m["id"] for m in from_yaml_messages if m["id"] in source_ids} == {
        m["id"] for m in from_txt_messages if m["id"] in source_ids
    }
    assert from_yaml["accepted_count"] == from_txt["accepted_count"]


def test_the_yaml_keeps_the_ingested_at_the_txt_cannot_carry():
    """מה שהפורמטים *לא* חולקים, נאמר במפורש.

    לקובץ הצינורות אין עמודה ל-``ingested_at``, ולכן שורה שנקלטת ממנו
    מקבלת את זמן הקליטה הנוכחי. הפיקסצ'ר מצהיר עליו, והעלאת ה-YAML
    שומרת אותו - וזה מה שמאפשר לבדוק איחור אמיתי מתוך קובץ.
    """
    fixture = FIXTURES_DIR / "edge_cases" / "late_arrival.yaml"
    parsed = parse_fixture_upload(
        fixture.read_text(encoding="utf-8"), source_name=fixture.name
    )

    late = next(m for m in parsed.messages if m["id"] == "flight-edge-02-m02")
    assert late["ingested_at"] == "2026-09-05T10:01:45Z"
    assert late["timestamp"] == "2026-09-05T10:00:10Z"


# --- recognising the format ----------------------------------------------


@pytest.mark.parametrize("fixture", FIXTURE_PATHS, ids=lambda p: p.stem)
def test_a_fixture_is_recognised_even_without_its_extension(fixture: Path):
    """ההכרעה היא לפי התוכן: דפדפן לא תמיד שולח סיומת, ושם אפשר לשנות."""
    text = fixture.read_text(encoding="utf-8")

    assert looks_like_fixture_yaml(text, fixture.name)
    assert looks_like_fixture_yaml(text, "no-extension")
    assert looks_like_fixture_yaml(text, None)


def test_a_transcript_txt_is_never_mistaken_for_a_fixture():
    """הכיוון ההפוך חייב להחזיק: קובץ תמלול נקרא כתמלול.

    אם ההיוריסטיקה הייתה תופסת גם אותו, קובץ הצינורות היה נקרא בקורא
    הלא נכון - ו-29 שורות היו נעלמות.
    """
    for sample in sorted(SAMPLES_DIR.glob("*.txt")):
        assert not looks_like_fixture_yaml(sample.read_text(encoding="utf-8"), sample.name)
    hand_written = ROOT / "samples" / "flight_sim_01.txt"
    assert not looks_like_fixture_yaml(
        hand_written.read_text(encoding="utf-8"), hand_written.name
    )


def test_the_extension_alone_is_enough_to_try_the_fixture_reader(client):
    """קובץ ``.yaml`` שאינו פיקסצ'ר נענה בהסבר על פיקסצ'ר, לא על תמלול."""
    response = client.post(
        "/messages/upload",
        files={"file": ("notes.yaml", b"just: a mapping\nwithout: messages\n", "text/yaml")},
        data={"flight_id": "f"},
    )

    assert response.status_code == 400
    assert "not a usable fixture" in response.json()["detail"]


def test_a_typed_flight_id_overrides_the_one_inside_the_fixture(client):
    """אותה התנהגות שיש להנחיה בקובץ הצינורות: הטופס גובר.

    חשוב במיוחד לפיקסצ'ר, שבו ``flight_id`` כתוב גם על כל הודעה בנפרד -
    בלי לכתוב אותו מחדש על ההודעות, הן היו נקלטות לטיסה שבקובץ.
    """
    fixture = FIXTURES_DIR / "golden_flights" / "flight_01_recon_patrol.yaml"

    body = upload(client, fixture, flight_id="my-own-run").json()

    assert body["report"]["flight_id"] == "my-own-run"
    assert body["report"]["flight_id_source"] == "request"
    assert body["accepted_count"] == 14
    assert client.get("/flights/flight-01/messages").json() == []
    assert len(client.get("/flights/my-own-run/messages").json()) == 14


def test_uploading_the_same_fixture_twice_is_a_duplicate(client):
    fixture = FIXTURES_DIR / "golden_flights" / "flight_02_contact_lost_and_recovered.yaml"
    first = upload(client, fixture).json()
    second = upload(client, fixture).json()

    assert second["accepted_count"] == 0
    assert second["duplicate_count"] == first["accepted_count"]


def test_a_malformed_yaml_is_rejected_with_its_reason(client):
    response = client.post(
        "/messages/upload",
        files={
            "file": (
                "broken.yaml",
                b"messages:\n  - id: m01\n   text: bad indent\n",
                "text/yaml",
            )
        },
        data={"flight_id": "f"},
    )

    assert response.status_code == 400
    assert "not a usable fixture" in response.json()["detail"]


def test_the_client_offers_yaml_in_its_file_picker():
    """אין טעם לקבל פורמט שהקליינט לא מציע לבחור."""
    page = (ROOT / "src" / "aura" / "api" / "static" / "index.html").read_text(encoding="utf-8")

    assert ".yaml" in page
    assert 'accept=".txt,.text,text/plain,.yaml,.yml"' in page


def test_the_client_can_find_a_flight_id_in_either_format():
    """הקליינט חוסם העלאה בלי מזהה טיסה, ולכן הוא חייב לזהות את שני
    המקומות שבהם מזהה טיסה מוצהר - אחרת פיקסצ'ר נעצר לפני השרת."""
    page = (ROOT / "src" / "aura" / "api" / "static" / "index.html").read_text(encoding="utf-8")

    assert r"^#\s*flight_id\s*:\s*([^\s#]+)\s*$" in page
    assert r"""^flight_id\s*:\s*["']?([^\s"'#]+)["']?\s*$""" in page


def test_the_yaml_path_reproduces_the_fixtures_declared_rejections(client):
    """הפיקסצ'ר המשובש נקלט מה-YAML בדיוק כפי שהוא מצהיר: 5 דחיות.

    זה מה שקובץ הצינורות אינו יכול לעשות - בו מזהה הטיסה הוא שדה של
    הקובץ, ולכן השורה שנשלחה בלי ``flight_id`` נקלטת ומתקבלות 4 דחיות.
    הפיקסצ'ר שומר על ההבחנה, ולכן העלאתו היא הדרך המדויקת להריץ אותו.
    """
    fixture = FIXTURES_DIR / "edge_cases" / "malformed_and_empty.yaml"
    transcript = load_fixture_transcript(fixture)

    body = upload(client, fixture).json()

    assert (body["accepted_count"], body["dead_letter_count"]) == (0, 5)
    reasons = {
        result["dead_letter"]["raw_payload"].get("id"): (
            result["dead_letter"]["reason"],
            result["dead_letter"]["missing_field"],
        )
        for result in body["results"]
    }
    for message_id, declared in transcript.rejections.items():
        assert reasons[message_id] == (declared["reason"], declared.get("missing_field"))

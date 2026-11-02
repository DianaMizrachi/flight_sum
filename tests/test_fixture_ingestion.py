"""Every tagged sentence, through the real ingest component, verbatim.

The other fixture tests (``test_golden_flights.py``,
``test_model_eval_scenarios.py``, ``test_edge_cases.py``) check that the
fixtures are *internally* consistent and valid against the stage-0
schemas. That leaves one gap, and it is the one that matters when a
fixture is used to judge a model: nobody asked the service.

So each fixture here is fed to a real :class:`IngestionService` over a
real :class:`SQLiteRawTranscriptStore`, and what comes back out of the
store is compared to the fixture field by field - the sentence above all,
character for character. A normalisation rule that silently rewrote a
Hebrew sentence, or a dedup key that swallowed one, would move the text a
model is scored on away from the text the fixture tags. These tests fail
in that case; schema validation does not.

Each fixture then makes the same trip through the transcript *file*
format - rendered, parsed, ingested - because that is how a user loads
these sentences in the client. Both paths must deliver the same
sentences, and where the file format cannot carry something, the test
says so by name rather than letting it pass unnoticed.
"""

import pytest

from aura.ingestion import (
    DeadLetterReason,
    IngestionService,
    UnrenderableSentenceError,
    parse_transcript_text,
    render_transcript_text,
)
from aura.ingestion.normalize import normalize_text
from aura.raw_store import RawStoreDedupIndex, SQLiteRawTranscriptStore
from aura.schemas import MessageStatus

# שתי השורות בפיקסצ'ר המשובש שפורמט הקובץ אינו יכול לשאת, ולמה. הן
# מפורטות בשמן כדי שהבדל *אחר* בין שני המסלולים ייכשל ולא ייבלע כאן.
FILE_FORMAT_EXCEPTIONS = {
    # שורה בקובץ שנגמרת אחרי הערוץ נותנת טקסט ריק; "שדה חסר" ו"שדה ריק"
    # אינם ניתנים להבחנה בפורמט שורה אחת, והסיבה היא empty_text.
    "flight-edge-03-m01": "missing text reads back as empty text in a line-based file",
    # מזהה הטיסה הוא שדה של הקובץ כולו, ולכן הנחיית `# flight_id:` נותנת
    # אותו לשורה שבפיקסצ'ר נשלחה בלעדיו - והשורה נקלטת.
    None: "a file always supplies flight_id, so a line cannot lack one",
}


@pytest.fixture
def store() -> SQLiteRawTranscriptStore:
    with SQLiteRawTranscriptStore(":memory:") as opened:
        yield opened


@pytest.fixture
def service(store: SQLiteRawTranscriptStore) -> IngestionService:
    return IngestionService(store=store, dedup_index=RawStoreDedupIndex(store))


def ingest(transcript, payloads=None) -> tuple[list, SQLiteRawTranscriptStore]:
    """קולט רצף הודעות דרך רכיב 1 אמיתי מעל מאגר אמיתי."""
    store = SQLiteRawTranscriptStore(":memory:")
    service = IngestionService(store=store, dedup_index=RawStoreDedupIndex(store))
    results = service.ingest_many(
        transcript.payloads() if payloads is None else payloads,
    )
    return results, store


def expected_sentences(transcript) -> dict[str, str]:
    """המשפטים שהפיקסצ'ר מצפה שייקלטו, לפי מזהה ההודעה.

    שורה שהפיקסצ'ר מצהיר עליה כדחויה אינה כאן - היא נבדקת לפי הסיבה
    שהיא צריכה לייצר, ולא לפי המשפט שנשמר. גם שורה בלי מזהה מהמקור
    אינה כאן, כי המזהה שלה נגזר מ-hash ואינו כתוב בפיקסצ'ר.
    """
    return {str(message["id"]): str(message["text"]) for message in transcript.storable()}


# --- the direct path: fixture -> component 1 -> raw store ----------------


def test_every_tagged_sentence_is_stored_character_for_character(fixture_transcript):
    """הבדיקה המרכזית: מה שהמאגר מחזיר הוא מה שהפיקסצ'ר כתב.

    לא "דומה" ולא "אחרי נרמול" - אותה מחרוזת. משפט שהנרמול היה משנה
    פירושו שהתג בפיקסצ'ר מתייחס לטקסט אחד והמודל יקרא טקסט אחר.
    """
    _, store = ingest(fixture_transcript)

    for message_id, sentence in expected_sentences(fixture_transcript).items():
        stored = store.get(message_id, flight_id=fixture_transcript.flight_id)
        assert stored is not None, f"{message_id} never reached the raw store"
        assert stored.text == sentence, (
            f"{fixture_transcript.name}/{message_id} was tagged as {sentence!r} "
            f"but the service stored {stored.text!r}"
        )


def test_tagged_sentences_need_no_normalisation(fixture_transcript):
    """הצד השני של אותו מטבע, בלי המאגר: המשפטים כתובים כבר מנורמלים.

    כשזה נכון, ``metadata["raw_text"]`` אינו נדרש, ואפשר להשוות תג
    לפלט מודל בלי לשאול דרך איזה נרמול כל אחד מהם עבר. היוצא מן הכלל
    הוא שורה שכל תפקידה להיות משובשת.
    """
    for message in fixture_transcript.messages:
        sentence = str(message.get("text", ""))
        if not sentence.strip():
            continue  # שורה ריקה בכוונה - נבדקת ב-dead-letter למטה.
        assert normalize_text(sentence) == sentence, (
            f"{fixture_transcript.name}/{message.get('id')} is tagged with text that "
            f"normalisation rewrites to {normalize_text(sentence)!r}"
        )


def test_stored_messages_keep_the_fixtures_own_fields(fixture_transcript):
    """מקור, ערוץ, זמן וטיסה חוזרים כפי שהפיקסצ'ר מצהיר עליהם."""
    _, store = ingest(fixture_transcript)

    for message in fixture_transcript.storable():
        stored = store.get(str(message["id"]), flight_id=fixture_transcript.flight_id)
        assert stored is not None, f"{message['id']} never reached the raw store"
        assert stored.flight_id == fixture_transcript.flight_id
        assert stored.source == message["source"]
        if message.get("channel"):
            assert stored.channel == message["channel"]
        assert stored.timestamp.isoformat().replace("+00:00", "Z") == message["timestamp"]


def test_the_whole_flight_comes_back_in_spoken_order(fixture_transcript):
    """המאגר מחזיר לפי חותמת הזמן המקורית, לא לפי סדר הקליטה."""
    _, store = ingest(fixture_transcript)

    stored = store.by_flight(fixture_transcript.flight_id)
    stamps = [message.timestamp for message in stored]
    assert stamps == sorted(stamps)


def test_no_tagged_sentence_is_lost_between_the_fixture_and_the_store(fixture_transcript):
    """כל משפט נגמר באחד משלושה מקומות מוצהרים, ואף אחד לא נעלם."""
    results, store = ingest(fixture_transcript)

    assert len(results) == len(fixture_transcript.messages)
    accepted = [r for r in results if r.status == MessageStatus.ACCEPTED]
    duplicates = [r for r in results if r.status == MessageStatus.DUPLICATE]
    dead = [r for r in results if r.status == MessageStatus.DEAD_LETTER]
    assert len(accepted) + len(duplicates) + len(dead) == len(fixture_transcript.messages)
    assert store.count(fixture_transcript.flight_id) == len(accepted)
    for result in dead:
        assert result.dead_letter.raw_payload, "a rejected sentence must keep its raw payload"


def test_a_clean_fixture_has_every_sentence_accepted(fixture_transcript):
    """פיקסצ'ר שאינו מתרחש על כפילות או על שורה פגומה נקלט במלואו.

    הפיקסצ'רים של גבול המערכת מצהירים על הדחיות שלהם, ולהם יש בדיקות
    משלהם ב-``test_edge_cases.py``; כאן נבדק שאף פיקסצ'ר *אחר* לא
    איבד שורה בשקט.
    """
    if fixture_transcript.path.parent.name == "edge_cases":
        pytest.skip("edge-case fixtures declare their own rejections")
    results, _ = ingest(fixture_transcript)

    for result in results:
        assert result.status == MessageStatus.ACCEPTED, (
            f"{fixture_transcript.name}: {result.message.id if result.message else '?'} "
            f"came back {result.status}"
        )
        assert result.queued, "an accepted sentence must reach the internal queue"


def test_every_declared_rejection_is_rejected_for_the_declared_reason(fixture_transcript):
    """דחייה שהפיקסצ'ר מצהיר עליה - עם הסיבה שהוא מצהיר עליה.

    עד כה הפיקסצ'ר אמר "זה צריך להגיע ל-dead-letter עם
    ``missing_field: timestamp``" והבדיקה הסתפקה בכך שהסכמה דוחה את
    ההודעה. סיבת הדחייה היא החלטה של רכיב 1, ולכן היא נבדקת מולו.
    """
    if not fixture_transcript.rejections:
        pytest.skip("this fixture declares no rejection")
    results, store = ingest(fixture_transcript)

    by_id = {
        str(message.get("id")): result
        for message, result in zip(fixture_transcript.messages, results, strict=True)
    }
    for message_id, declared in fixture_transcript.rejections.items():
        result = by_id[message_id]
        assert result.status == MessageStatus.DEAD_LETTER, (
            f"{declared['case_id']}: {message_id} was not rejected"
        )
        assert result.dead_letter.reason == DeadLetterReason(declared["reason"])
        if declared.get("missing_field"):
            assert result.dead_letter.missing_field == declared["missing_field"]
        assert result.dead_letter.raw_payload
        assert not store.exists(message_id, flight_id=fixture_transcript.flight_id), (
            "a rejected sentence must not reach the raw store"
        )


def test_ingesting_a_fixture_twice_adds_nothing(fixture_transcript):
    """הזרמה חוזרת של אותו פיקסצ'ר היא כפילות מלאה, לא טיסה שניה."""
    store = SQLiteRawTranscriptStore(":memory:")
    service = IngestionService(store=store, dedup_index=RawStoreDedupIndex(store))

    service.ingest_many(fixture_transcript.payloads())
    after_first = store.count(fixture_transcript.flight_id)
    replay = service.ingest_many(fixture_transcript.payloads())

    assert store.count(fixture_transcript.flight_id) == after_first
    assert not [r for r in replay if r.status == MessageStatus.ACCEPTED]


# --- the file path: fixture -> .txt -> parser -> component 1 -------------


def test_a_fixture_renders_to_a_file_the_parser_reads_back_identically(fixture_transcript):
    """הסיבוב דרך הקובץ אינו משנה אף שדה שהפורמט יכול לשאת."""
    text = render_transcript_text(
        fixture_transcript.messages, flight_id=fixture_transcript.flight_id
    )
    parsed = parse_transcript_text(text, source_name=f"{fixture_transcript.name}.txt")

    assert parsed.flight_id == fixture_transcript.flight_id
    assert parsed.flight_id_from_directive == fixture_transcript.flight_id
    assert parsed.message_count == len(fixture_transcript.messages)
    assert parsed.short_lines == []
    for original, round_tripped in zip(
        fixture_transcript.messages, parsed.messages, strict=True
    ):
        sentence = str(original.get("text", ""))
        if sentence.strip():
            assert round_tripped["text"] == sentence
        assert round_tripped.get("id", "") == str(original.get("id") or "")
        assert round_tripped["source"] == original["source"]


def test_the_file_path_delivers_the_same_sentences_as_the_direct_path(fixture_transcript):
    """אותו רכיב 1 מקבל את אותם משפטים, בין אם מ-API ובין אם מקובץ.

    זו הטענה שעומדת מאחורי "אפשר להעלות את הפיקסצ'רים בקליינט": הקובץ
    אינו מסלול קליטה שני עם תוצאה אחרת.
    """
    _, direct_store = ingest(fixture_transcript)
    text = render_transcript_text(
        fixture_transcript.messages, flight_id=fixture_transcript.flight_id
    )
    parsed = parse_transcript_text(text, source_name=f"{fixture_transcript.name}.txt")
    _, file_store = ingest(fixture_transcript, payloads=parsed.messages)

    direct = {m.id: m.text for m in direct_store.by_flight(fixture_transcript.flight_id)}
    from_file = {m.id: m.text for m in file_store.by_flight(fixture_transcript.flight_id)}
    for message_id in set(direct) | set(from_file):
        if message_id in direct and message_id in from_file:
            assert direct[message_id] == from_file[message_id], (
                f"{message_id} arrived as a different sentence through the file"
            )
            continue
        # הודעה שמופיעה רק באחד המסלולים מותרת אך ורק אם הפורמט מצהיר
        # עליה כמוגבלת. ``None`` הוא המקרה של שורה בלי מזהה מהמקור.
        only_in_file = message_id not in direct
        key = message_id if message_id in FILE_FORMAT_EXCEPTIONS else None
        assert only_in_file and key in FILE_FORMAT_EXCEPTIONS, (
            f"{message_id} reached only the {'file' if only_in_file else 'direct'} path, "
            "and the file format does not document that"
        )


def test_every_fixture_sentence_survives_the_file_path_verbatim(fixture_transcript):
    """המשפטים עצמם, אחרי העלאה מקובץ, זהים לתגיות שבפיקסצ'ר."""
    text = render_transcript_text(
        fixture_transcript.messages, flight_id=fixture_transcript.flight_id
    )
    parsed = parse_transcript_text(text, source_name=f"{fixture_transcript.name}.txt")
    _, store = ingest(fixture_transcript, payloads=parsed.messages)

    for message_id, sentence in expected_sentences(fixture_transcript).items():
        stored = store.get(message_id, flight_id=fixture_transcript.flight_id)
        assert stored is not None, f"{message_id} did not survive the file path"
        assert stored.text == sentence
        assert stored.metadata["source_file"] == f"{fixture_transcript.name}.txt"
        assert stored.metadata["source_line"].isdigit()


def test_the_documented_file_format_limits_are_still_the_only_ones(fixture_transcript):
    """שתי המגבלות של פורמט הקובץ נבדקות בשמן, ולא כ"בערך אותו דבר".

    ``flight-edge-03-m01`` נשלח בלי שדה טקסט: ישירות זהו
    ``missing_required_field``, ומקובץ - ``empty_text``, כי שורה שנגמרת
    אחרי הערוץ אינה מבדילה בין השניים. השורה בלי ``flight_id`` נקלטת
    מקובץ, כי ההנחיה נותנת לה את מזהה הטיסה.
    """
    if fixture_transcript.name != "malformed_and_empty":
        pytest.skip("only the malformed fixture exercises the format's limits")

    direct, _ = ingest(fixture_transcript)
    text = render_transcript_text(
        fixture_transcript.messages, flight_id=fixture_transcript.flight_id
    )
    parsed = parse_transcript_text(text, source_name="malformed_and_empty.txt")
    from_file, _ = ingest(fixture_transcript, payloads=parsed.messages)

    assert [r.status for r in direct] == [MessageStatus.DEAD_LETTER] * 5
    direct_reasons = [r.dead_letter.reason for r in direct]
    assert direct_reasons[0] == DeadLetterReason.MISSING_REQUIRED_FIELD
    assert direct[0].dead_letter.missing_field == "text"

    statuses = [r.status for r in from_file]
    assert statuses.count(MessageStatus.DEAD_LETTER) == 4
    assert statuses.count(MessageStatus.ACCEPTED) == 1
    file_reasons = [r.dead_letter.reason for r in from_file if r.dead_letter]
    assert file_reasons.count(DeadLetterReason.EMPTY_TEXT) == 3
    assert DeadLetterReason.MISSING_REQUIRED_FIELD in file_reasons
    accepted = next(r for r in from_file if r.status == MessageStatus.ACCEPTED)
    assert accepted.message.flight_id == fixture_transcript.flight_id


# --- the renderer refuses what it cannot carry --------------------------


@pytest.mark.parametrize(
    "field_name, value",
    [
        ("text", "שורה\nשנייה"),
        ("text", "קו\rחוזר"),
        ("id", "m\n01"),
        ("source", "oper|ator"),
        ("channel", "com|mand"),
        ("timestamp", "2026-09-01T06:00:00Z|"),
    ],
)
def test_the_renderer_refuses_content_the_format_would_corrupt(field_name, value):
    """עדיף שגיאה מקובץ שנקרא אחרת ממה שנכתב בו."""
    message = {
        "id": "m01",
        "timestamp": "2026-09-01T06:00:00Z",
        "source": "operator",
        "channel": "command",
        "text": "בקרה, נץ אחת, הכל תקין.",
        field_name: value,
    }
    with pytest.raises(UnrenderableSentenceError):
        render_transcript_text([message], flight_id="flight-01")


@pytest.mark.parametrize(
    "kwargs",
    [
        # מזהה טיסה עם שורה חדשה היה מסיים את ההנחיה ומתחיל שורת הודעה.
        {"flight_id": "f\nm9 | 2026-09-01T06:00:00Z | ops | command | לא נכתב"},
        {"flight_id": "two words"},
        {"flight_id": "   "},
        # הערה עם שורה חדשה היה יוצרת שורה שאינה מתחילה ב-# - כלומר הודעה.
        {
            "flight_id": "f",
            "header_lines": ["ok\nm9 | 2026-09-01T06:00:00Z | ops | command | לא נכתב"],
        },
    ],
)
def test_the_renderer_refuses_a_header_that_would_inject_a_message(kwargs):
    """קובץ אינו יכול לצאת מכאן עם הודעה שאף אחד לא כתב."""
    message = {
        "id": "m01",
        "timestamp": "2026-09-01T06:00:00Z",
        "source": "operator",
        "channel": "command",
        "text": "בקרה, נץ אחת, הכל תקין.",
    }
    with pytest.raises(UnrenderableSentenceError):
        render_transcript_text([message], **kwargs)


def test_a_rendered_header_is_all_comments_and_no_messages():
    """הכותרת שהסקריפט כותב אינה נספרת כהודעה בשום מצב."""
    text = render_transcript_text(
        [
            {
                "id": "m01",
                "timestamp": "2026-09-01T06:00:00Z",
                "source": "ops",
                "channel": "command",
                "text": "שלום.",
            }
        ],
        flight_id="flight-01",
        header_lines=["כותרת", "", "שורה שנייה | עם קו אנכי"],
    )

    parsed = parse_transcript_text(text)
    assert parsed.message_count == 1
    assert parsed.comment_lines == 4  # שלוש הערות + ההנחיה
    assert parsed.flight_id == "flight-01"


def test_the_renderer_keeps_a_pipe_inside_the_sentence():
    """``|`` במשפט מותר: הוא השדה האחרון, והקורא מפצל ב-maxsplit."""
    sentence = "בקרה, נץ אחת, הקוד הוא א|ב."
    text = render_transcript_text(
        [
            {
                "id": "m01",
                "timestamp": "2026-09-01T06:00:00Z",
                "source": "operator",
                "channel": "command",
                "text": sentence,
            }
        ],
        flight_id="flight-01",
    )

    parsed = parse_transcript_text(text)
    assert parsed.messages[0]["text"] == sentence


def test_alignment_padding_does_not_reach_the_service():
    """היישור לעמודות הוא קוסמטי; הקליטה מקבלת את השדות מקוצצים."""
    messages = [
        {
            "id": "m1",
            "timestamp": "2026-09-01T06:00:00Z",
            "source": "ops",
            "channel": "command",
            "text": "קצר.",
        },
        {
            "id": "m-with-a-long-id",
            "timestamp": "2026-09-01T06:00:10Z",
            "source": "operator",
            "channel": "intercom",
            "text": "ארוך.",
        },
    ]
    aligned = parse_transcript_text(render_transcript_text(messages, flight_id="f"))
    tight = parse_transcript_text(render_transcript_text(messages, flight_id="f", align=False))

    assert [dict(m) for m in aligned.messages] == [dict(m) for m in tight.messages]
    assert aligned.messages[0]["id"] == "m1"
    assert aligned.messages[0]["text"] == "קצר."

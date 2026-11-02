"""Turning the tagged fixtures into transcripts the service can be fed.

The fixtures under ``tests/fixtures/`` are the project's tagged truth, but
each family states its sentences in its own shape: golden flights and the
model-comparison scenarios list ``messages``, the late-arrival edge case
lists ``raw_inputs``, and the duplicate/malformed edge cases nest their
inputs inside ``cases``. Nothing downstream should have to know that.

This module reads any of them into one flat list of ingest payloads, so
that the same sentences can go through :class:`IngestionService` directly,
be written as an uploadable ``.txt`` with
:func:`~aura.ingestion.text_format.render_transcript_text`, and be
compared field by field afterwards - without a second definition of
"the sentences in this fixture" anywhere.

It lives in the package rather than in ``tests/`` because
``scripts/export_fixture_transcripts.py`` writes the sample files from it
too, and those files are what a user picks in the client.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from aura.ingestion.text_format import COMMENT_PREFIX, ParsedTranscript

# השדות שהפיקסצ'ר מתאר כקלט. ``expected`` ושאר התגיות שלידם הם התג
# הצפוי, לא חלק מההודעה, ולכן אינם מועברים לקליטה.
INGEST_FIELDS = ("id", "flight_id", "timestamp", "ingested_at", "source", "channel", "text")

# המקומות שבהם פיקסצ'ר מחזיק הודעות גולמיות, לפי משפחת הפיקסצ'רים.
_MESSAGE_KEYS = ("messages", "raw_inputs")
_SINGLE_MESSAGE_KEYS = ("source_message", "raw_input")


@dataclass
class FixtureTranscript:
    """המשפטים של פיקסצ'ר אחד, כרצף קלט אחד לטיסה אחת."""

    path: Path
    flight_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    rejections: dict[str, dict[str, Any]] = field(default_factory=dict)
    title: str = ""

    @property
    def name(self) -> str:
        return self.path.stem

    def payloads(self) -> list[dict[str, Any]]:
        """הקלט כפי שהוא נשלח ל-``IngestionService.ingest``."""
        return [dict(message) for message in self.messages]

    def sentences(self) -> list[str]:
        return [str(message.get("text", "")) for message in self.messages]

    def expects_rejection(self, message_id: object) -> bool:
        """האם הפיקסצ'ר מצהיר שהשורה הזו צריכה להגיע ל-dead-letter."""
        return str(message_id) in self.rejections

    def storable(self) -> list[dict[str, Any]]:
        """ההודעות שהפיקסצ'ר מצפה שייקלטו: יש להן מזהה, יש להן משפט,
        והפיקסצ'ר אינו מצהיר עליהן כדחויות."""
        return [
            dict(message)
            for message in self.messages
            if message.get("id")
            and str(message.get("text", "")).strip()
            and not self.expects_rejection(message["id"])
        ]


def _iter_raw_messages(data: dict) -> Iterator[dict]:
    for key in _MESSAGE_KEYS:
        for raw in data.get(key) or ():
            if isinstance(raw, dict):
                yield raw
    for key in _SINGLE_MESSAGE_KEYS:
        raw = data.get(key)
        if isinstance(raw, dict):
            yield raw
    for case in data.get("cases") or ():
        if isinstance(case, dict):
            yield from _iter_raw_messages(case)


def _iter_declared_rejections(data: dict) -> Iterator[tuple[str, dict[str, Any]]]:
    """``case.expected.dead_letter`` - הדחיות שהפיקסצ'ר מצהיר עליהן בשמן."""
    for case in data.get("cases") or ():
        if not isinstance(case, dict):
            continue
        expected = case.get("expected")
        if not isinstance(expected, dict) or not expected.get("dead_letter"):
            continue
        for raw in _iter_raw_messages(case):
            if raw.get("id"):
                yield str(raw["id"]), {
                    "case_id": case.get("case_id"),
                    "reason": expected.get("reason"),
                    "missing_field": expected.get("missing_field"),
                }


def _as_payload(raw: dict) -> dict[str, Any]:
    payload = {name: raw[name] for name in INGEST_FIELDS if name in raw}
    # ``id: null`` בפיקסצ'ר הוא "אין מזהה מהמקור", וזה בדיוק מה שהקליטה
    # מצפה לקבל כדי ליפול ל-dedup לפי hash. שדה חסר פירושו אותו דבר.
    if payload.get("id") is None:
        payload.pop("id", None)
    return payload


def parse_fixture_transcript_text(
    text: str,
    *,
    flight_id: str | None = None,
    source_name: str | None = None,
) -> FixtureTranscript:
    """קורא פיקסצ'ר מתוך טקסט YAML, בלי לדעת מאיזה קובץ הוא בא.

    הופרד מ:func:`load_fixture_transcript` כדי שההעלאה בשירות תוכל
    לקרוא פיקסצ'ר שהגיע כ-bytes, ולא רק קובץ שיושב על הדיסק - ובלי
    הגדרה שנייה של "המשפטים שבפיקסצ'ר הזה".

    ``flight_id`` של הקורא גובר על מה שכתוב בפיקסצ'ר, בדיוק כמו
    ההתנהגות של :func:`~aura.ingestion.text_format.parse_transcript_text`
    מול הנחיית ``# flight_id:``.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{source_name or '<text>'} does not hold a mapping")

    messages = [_as_payload(raw) for raw in _iter_raw_messages(data)]
    if not messages:
        raise ValueError(
            f"{source_name or '<text>'} holds no messages; expected one of "
            f"{', '.join(_MESSAGE_KEYS + _SINGLE_MESSAGE_KEYS)}"
        )

    flight_ids = {str(m["flight_id"]) for m in messages if m.get("flight_id")}
    declared = data.get("flight_id")
    resolved = (
        str(flight_id)
        if flight_id
        else (str(declared) if declared else (sorted(flight_ids)[0] if flight_ids else ""))
    )
    if not resolved:
        raise ValueError(
            f"{source_name or '<text>'} names no flight_id and no message carries one"
        )
    if not flight_id and flight_ids - {resolved}:
        raise ValueError(
            f"{source_name or '<text>'} mixes flights {sorted(flight_ids | {resolved})}; "
            "a transcript file carries exactly one flight"
        )
    # מזהה טיסה מהקורא גובר, ולכן הוא נכתב על כל ההודעות - אחרת הודעה
    # הייתה נקלטת לטיסה שכתובה בה ולא לזו שהמשתמש ביקש.
    if flight_id:
        for message in messages:
            message["flight_id"] = str(flight_id)

    return FixtureTranscript(
        path=Path(source_name) if source_name else Path("<text>"),
        flight_id=resolved,
        messages=messages,
        rejections=dict(_iter_declared_rejections(data)),
        title=str(data.get("title_he") or data.get("case_id") or data.get("description_he") or ""),
    )


def load_fixture_transcript(path: Path) -> FixtureTranscript:
    """קורא פיקסצ'ר אחד לרצף הודעות, בסדר שבו הוא כותב אותן.

    מזהה הטיסה נלקח מהפיקסצ'ר אם הוא מצהיר עליו, ואחרת ממזהה הטיסה של
    ההודעה הראשונה - שם שתי משפחות הפיקסצ'רים מתכנסות.
    """
    transcript = parse_fixture_transcript_text(
        path.read_text(encoding="utf-8"), source_name=str(path)
    )
    transcript.path = path
    return transcript


def looks_like_fixture_yaml(text: str, filename: str | None = None) -> bool:
    """האם הקובץ הוא פיקסצ'ר YAML ולא תמלול בפורמט הצינורות.

    הסיומת היא רמז ולא הוכחה (דפדפן לא תמיד שולח אותה, ומשתמש יכול
    לשנות שם), ולכן ההכרעה היא לפי התוכן: YAML שנפרש למילון שמחזיק
    מפתח הודעות מוכר. קובץ תמלול אמיתי אינו נפרש כך - שורות הצינורות
    נקראות כמחרוזת אחת, לא כמילון.
    """
    if filename and Path(filename).suffix.lower() in {".yaml", ".yml"}:
        return True
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return False
    return isinstance(data, dict) and any(
        key in data for key in _MESSAGE_KEYS + _SINGLE_MESSAGE_KEYS + ("cases",)
    )


def load_fixture_transcripts(root: Path) -> list[FixtureTranscript]:
    """כל הפיקסצ'רים תחת ``root``, לפי שם הקובץ."""
    return [load_fixture_transcript(path) for path in sorted(root.glob("*/*.yaml"))]


def parse_fixture_upload(
    text: str,
    *,
    flight_id: str | None = None,
    source_name: str | None = None,
) -> ParsedTranscript:
    """פיקסצ'ר YAML -> אותו :class:`ParsedTranscript` שהקורא של הצינורות מחזיר.

    זה מה שמאפשר להעלות פיקסצ'ר ישירות בקליינט: ההעלאה מקבלת את אותו
    מבנה משני הפורמטים, ומכאן והלאה אין שני מסלולי קליטה - אותו רכיב 1,
    אותו נרמול, אותו זיהוי כפילויות ואותו dead-letter.

    שורות ההודעה בפיקסצ'ר פרושות על פני כמה שורות קובץ כל אחת, ולכן
    ``metadata`` מקבל ``source_index`` (המקום ברצף) ולא ``source_line``.
    עדיף מספר שאומר את האמת ממספר שורה שהומצא.
    """
    transcript = parse_fixture_transcript_text(
        text, flight_id=flight_id, source_name=source_name
    )

    lines = text.splitlines()
    parsed = ParsedTranscript(
        flight_id=transcript.flight_id,
        flight_id_from_directive=transcript.flight_id if not flight_id else None,
        total_lines=len(lines),
        comment_lines=sum(1 for line in lines if line.strip().startswith(COMMENT_PREFIX)),
        blank_lines=sum(1 for line in lines if not line.strip()),
    )
    for index, message in enumerate(transcript.messages, start=1):
        payload = dict(message)
        metadata = {"source_index": str(index)}
        if source_name:
            metadata["source_file"] = source_name
        payload["metadata"] = metadata
        parsed.messages.append(payload)
    return parsed

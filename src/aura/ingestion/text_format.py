"""Reading and writing the pipe-delimited transcript file format (רכיב 1, קלט קבצים).

The format is the one ``samples/flight_sim_01.txt`` documents::

    message_id | timestamp | source | channel | text

Comment lines (``#``) and blank lines are not messages and are skipped.
``flight_id`` is a property of the whole file, not of a line: it comes
from an explicit ``# flight_id: <id>`` directive, or from the caller, and
the caller wins.

The parser is deliberately lenient about *content* and strict only about
structure. A line missing its timestamp or its text is still turned into a
message and handed to the ingestion service, which writes it to the
dead-letter log with a reason - per the plan's rule that nothing is
swallowed. If this parser rejected those lines itself, the reason would
be invented here, far from the component that owns the decision, and the
dead-letter log would no longer be the single place to look.

Two structural details worth stating, since both lose data if done the
obvious way:

The text field is split with ``maxsplit=4``, so a sentence containing a
``|`` keeps it instead of being truncated. The sample file says text has
no pipe, but a real transcript is not bound by that promise.

Padding around each field is stripped, while whitespace *inside* the
sentence is preserved - normalisation belongs to component 1, and the
``metadata["raw_text"]`` round-trip there depends on receiving the
sentence as written.

:func:`render_transcript_text` is the inverse, and lives here so that the
format has exactly one definition: a writer that drifted from the parser
would produce files the service reads as something other than what was
written. It is what turns a test fixture, or a flight already in the raw
store, into a file the client can upload.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

COMMENT_PREFIX = "#"
FIELD_SEPARATOR = "|"
FIELD_NAMES = ("id", "timestamp", "source", "channel", "text")

# ‎# flight_id: X - ההנחיה היחידה שהקורא מפרש. שאר ההערות הן טקסט חופשי.
_DIRECTIVE = re.compile(r"^#\s*flight_id\s*:\s*(?P<value>[^\s#]+)\s*$")


@dataclass
class ParsedTranscript:
    """תוצאת הפענוח: ההודעות, מזהה הטיסה, ומה דולג ולמה."""

    messages: list[dict] = field(default_factory=list)
    flight_id: str | None = None
    flight_id_from_directive: str | None = None
    total_lines: int = 0
    comment_lines: int = 0
    blank_lines: int = 0
    short_lines: list[int] = field(default_factory=list)

    @property
    def message_count(self) -> int:
        return len(self.messages)


def find_flight_id_directive(text: str) -> str | None:
    """``# flight_id: X`` אם קיים בקובץ."""
    for line in text.splitlines():
        match = _DIRECTIVE.match(line.strip())
        if match:
            return match.group("value")
    return None


def parse_transcript_text(
    text: str,
    flight_id: str | None = None,
    source_name: str | None = None,
) -> ParsedTranscript:
    """מפענח קובץ תמלול לרשימת מילונים מוכנים ל-``IngestionService``.

    ``flight_id`` של הקורא גובר על ההנחיה בקובץ. כל הודעה מקבלת
    ``metadata`` עם שם הקובץ ומספר השורה, כדי שאירוע שיתגלה בהמשך יוכל
    להיות מיוחס לא רק למשפט אלא לשורה שממנה הוא נקלט.
    """
    directive = find_flight_id_directive(text)
    resolved = flight_id or directive

    parsed = ParsedTranscript(flight_id=resolved, flight_id_from_directive=directive)

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        parsed.total_lines += 1
        line = raw_line.strip()
        if not line:
            parsed.blank_lines += 1
            continue
        if line.startswith(COMMENT_PREFIX):
            parsed.comment_lines += 1
            continue

        # maxsplit=4: משפט שמכיל "|" נשמר במלואו ולא נחתך.
        fields = [part.strip() for part in line.split(FIELD_SEPARATOR, len(FIELD_NAMES) - 1)]
        if len(fields) < len(FIELD_NAMES):
            parsed.short_lines.append(line_number)

        payload: dict = {}
        for name, value in zip(FIELD_NAMES, fields, strict=False):
            # שדה ריק = השדה נעדר, כדי שהקליטה תיתן לו את הסיבה הנכונה.
            # היוצא מן הכלל הוא text: מחרוזת ריקה היא בדיוק מה שמייצר
            # dead-letter עם empty_text, ולכן היא נשמרת כפי שהיא.
            if value or name == "text":
                payload[name] = value
        if resolved:
            payload["flight_id"] = resolved

        metadata = {"source_line": str(line_number)}
        if source_name:
            metadata["source_file"] = source_name
        payload["metadata"] = metadata

        parsed.messages.append(payload)

    return parsed


class UnrenderableSentenceError(ValueError):
    """שורה שאינה ניתנת לייצוג בפורמט הקובץ בלי לשנות את תוכנה.

    הפורמט הוא שורה-אחת-להודעה, ולכן תו שורה חדשה בתוך משפט היה מפצל
    אותו לשתי "הודעות" - והמשפט שהשירות היה מקבל אינו המשפט שנכתב. עדיף
    להיכשל כאן מלכתוב קובץ שנקרא אחרת ממה שנשמר בו.
    """


def render_transcript_text(
    messages: Iterable[Mapping[str, object]],
    *,
    flight_id: str | None = None,
    header_lines: Iterable[str] = (),
    align: bool = True,
) -> str:
    """ההיפוך של :func:`parse_transcript_text`: הודעות -> קובץ תמלול.

    מה שנכתב כאן חוזר מ-``parse_transcript_text`` כאותן הודעות, וזו כל
    מטרת הפונקציה: פיקסצ'ר או טיסה מהמאגר נהפכים לקובץ שאפשר להעלות
    בקליינט, ולדעת שהמשפטים יתקבלו בשירות כפי שהם.

    ``flight_id`` נכתב כהנחיית ``# flight_id:``, כך שהקובץ עומד בזכות
    עצמו ואין צורך להקליד מזהה טיסה בקליינט. ``header_lines`` הן הערות
    חופשיות שנכתבות לפניה. ``align`` מיישר את השדות לעמודות - הקורא מקצץ
    את ריווח היישור, ולכן הוא לא משנה את מה שנקלט.

    מעלה :class:`UnrenderableSentenceError` על תוכן שהפורמט אינו יכול
    לשאת: תו שורה חדשה בכל שדה או בהערה, ``|`` בשדה שאינו המשפט, ומזהה
    טיסה שההנחיה אינה יכולה לשאת.
    """
    rows: list[list[str]] = []
    for message in messages:
        row = [_render_field(message, name) for name in FIELD_NAMES]
        rows.append(row)

    widths = [0] * (len(FIELD_NAMES) - 1)
    if align and rows:
        # המשפט הוא השדה האחרון ולכן אינו מיושר - אין לו מה ליישר מולו.
        widths = [max(len(row[i]) for row in rows) for i in range(len(FIELD_NAMES) - 1)]

    # הערה או מזהה טיסה שמכילים שורה חדשה היו מייצרים שורה שאינה הערה,
    # והקורא היה מפענח אותה כהודעה - קובץ עם הודעה שאף אחד לא כתב.
    lines = []
    for line in header_lines:
        if "\n" in line or "\r" in line:
            raise UnrenderableSentenceError(
                "a header line contains a line break; pass one string per comment line"
            )
        lines.append(f"{COMMENT_PREFIX} {line}".rstrip())
    if flight_id is not None:
        if not str(flight_id).strip() or any(c.isspace() for c in str(flight_id)):
            raise UnrenderableSentenceError(
                f"flight_id {flight_id!r} is empty or holds whitespace; the "
                "'# flight_id:' directive reads a single bare token"
            )
        lines.append(f"{COMMENT_PREFIX} flight_id: {flight_id}")
    if lines:
        lines.append("")
    for row in rows:
        padded = [value.ljust(width) for value, width in zip(row, widths, strict=False)]
        lines.append(f" {FIELD_SEPARATOR} ".join([*padded, row[-1]]).rstrip())
    return "\n".join(lines) + "\n"


def _render_field(message: Mapping[str, object], name: str) -> str:
    value = message.get(name)
    rendered = "" if value is None else str(value)
    if "\n" in rendered or "\r" in rendered:
        raise UnrenderableSentenceError(
            f"{name!r} of {message.get('id') or '<no id>'} contains a line break; "
            "the file format is one message per line"
        )
    if name != "text" and FIELD_SEPARATOR in rendered:
        raise UnrenderableSentenceError(
            f"{name!r} of {message.get('id') or '<no id>'} contains "
            f"{FIELD_SEPARATOR!r}, which separates the fields"
        )
    return rendered

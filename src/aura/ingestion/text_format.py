"""Reading the pipe-delimited transcript file format (רכיב 1, קלט קבצים).

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
"""

from __future__ import annotations

import re
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

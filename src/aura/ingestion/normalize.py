"""Normalization of raw ingest payloads (שלב 1, משימה 2).

Everything here is deterministic and free of side effects: the same raw
payload always normalizes to the same Message fields. That property is what
lets the dedup hash in ``dedup.py`` be computed over the *normalized* text -
a source that resends the same sentence with different whitespace or bidi
markers must not look like a new message.
"""

import unicodedata
from datetime import UTC, datetime

# תווים בלתי-נראים שנפוצים בפלט תמלול ובהעברת טקסט דו-כיווני, ואינם חלק
# מהמשפט עצמו: סימוני כיווניות, רווחים באפס רוחב, BOM ומקף רך. הם נמחקים
# ולא מומרים לרווח, כדי לא לפצל מילה לשתיים.
_INVISIBLE_CHARS = frozenset(
    "\u00ad"  # soft hyphen
    "\u061c"  # arabic letter mark
    "\u200b\u200c\u200d"  # zero-width space / non-joiner / joiner
    "\u200e\u200f"  # LRM / RLM
    "\u202a\u202b\u202c\u202d\u202e"  # bidi embedding / override
    "\u2066\u2067\u2068\u2069"  # bidi isolates
    "\ufeff"  # BOM / zero-width no-break space
)

# קטגוריות Unicode שמומרות לרווח רגיל: תווי בקרה (טאב, שורה חדשה), תווי
# פורמט שנותרו, ורווחים חלופיים כמו NBSP.
_WHITESPACE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Zs"})


def normalize_text(value: str) -> str:
    """נרמול טקסט משפט: קידוד אחיד, הסרת תווים בלתי-נראים וכיווץ רווחים.

    מחזיר מחרוזת ריקה לטקסט שכולו רווחים או תווי בקרה - מצב שהקליטה
    מסמנת כ-``empty_text`` ושולחת ל-dead-letter.
    """
    text = unicodedata.normalize("NFC", value)
    chars = []
    for char in text:
        if char in _INVISIBLE_CHARS:
            continue
        chars.append(" " if unicodedata.category(char) in _WHITESPACE_CATEGORIES else char)
    return " ".join("".join(chars).split())


def normalize_timestamp(value: str | datetime) -> datetime:
    """המרת חותמת זמן לפורמט אחיד: datetime מודע-אזור ב-UTC.

    חותמת ללא אזור זמן מפורשת כ-UTC - הנחה מתועדת, כי המקורות מדווחים
    ב-UTC. מעלה ``ValueError`` או ``TypeError`` על קלט שאינו ניתן לפרסור,
    והקליטה הופכת אותה ל-``invalid_timestamp``.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.strip())
    else:
        raise TypeError(f"unsupported timestamp type: {type(value).__name__}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def normalize_identifier(value: object) -> str:
    """נרמול מזהה/מקור/ערוץ: המרה למחרוזת וקיצוץ רווחים בקצוות."""
    return str(value).strip()

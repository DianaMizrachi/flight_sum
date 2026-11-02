"""What a query refuses, and why (שלב 9).

The one rule worth a module of its own is that a time passed into a query
must say which timezone it is in.

A naive ``datetime`` is not a missing detail here, it is an ambiguous
question, and the three queries that take a time answered it three
different ways before this existed. ``state_at`` sent the value to the
event store, where ``astimezone(UTC)`` on a naive value assumes the
*machine's local time* - so the cutoff silently moved by the server's UTC
offset and the query returned a confident, wrong state. ``timeline`` and
``summary`` compared it against timezone-aware stamps from the raw store
and raised ``TypeError`` from inside the sort.

So: one mistake, one silent wrong answer and two crashes, none of which
told the caller what was wrong.

Guessing is the tempting fix, because every timestamp this system stores
is UTC and assuming UTC would be right nearly always. It is rejected
because of what "nearly" costs here. A caller in UTC+2 asking for the
state at 06:20 local would be shown 08:20 of the flight - a real moment,
with real entities, and nothing anywhere to suggest the answer is to a
different question than the one asked. For a tool whose whole purpose is
reconstructing what was known at a moment, an answer that is confidently
three hours off is worse than a refusal that names the fix.
"""

from __future__ import annotations

from datetime import datetime


class QueryError(RuntimeError):
    """בסיס לשגיאות שירות השאילתות."""


class SummariesUnavailableError(QueryError):
    """שאילתת תקציר כשמאגר התקצירים אינו מחובר.

    נפרד מ"אין תקצירים לטיסה": הראשון הוא תצורה חסרה והשני הוא תשובה
    תקפה. אם שניהם היו מוחזרים כ-``None``, קורא שראה טיסה בלי תקציר לא
    היה יכול לדעת אם הטיסה שקטה או שהרכיב כלל אינו רץ.
    """


class NaiveDatetimeError(QueryError, ValueError):
    """זמן שנמסר לשאילתה בלי אזור זמן.

    יורש גם מ-``ValueError`` כדי ששכבת ה-HTTP של שלב 10 תוכל לתפוס אותו
    כשגיאת קלט (400) ולא כתקלת שרת, בלי להכיר את היררכיית השגיאות של
    הרכיב.
    """

    def __init__(self, field: str, value: datetime) -> None:
        self.field = field
        self.value = value
        super().__init__(
            f"{field}={value.isoformat()} has no timezone; "
            "pass an aware datetime (the stores are UTC, e.g. "
            f"{value.isoformat()}Z)"
        )


def require_aware(value: datetime | None, field: str) -> datetime | None:
    """מחזיר את הזמן כפי שהוא, או נכשל אם אין לו אזור זמן.

    ``None`` עובר - "בלי גבול" הוא בקשה תקפה בכל השאילתות שמקבלות זמן.
    """
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise NaiveDatetimeError(field, value)
    return value

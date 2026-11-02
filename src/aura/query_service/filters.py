"""The filters a timeline query accepts (שלב 9).

The plan asks for five: flight, time range, event type, entity and
certainty level. They are one object rather than five keyword arguments
because they travel together - stage 10 will build them from a query
string, and a single object is what can be validated once and passed
down intact.

Two of the five are less obvious than they look.

**Time range** means the time the sentence was *said*. An event's own
``created_at`` is when the validator built it, so for a flight replayed
from the raw store every event in it shares one timestamp and a range
filter on that field is either everything or nothing. The service
resolves spoken time from the raw store and filters on that;
:meth:`EventFilter.matches` therefore takes the resolved time rather than
reading it off the entry.

**Entity** is matched by label, not by id, even when an id is given. The
reason is that a stored event's ``EntityRef.id`` is usually ``None``: the
model names an entity, and the state manager assigns the id afterwards
without writing it back. Filtering on the id alone would silently return
nothing for exactly the entities that matter most - the ones the flight
introduced. So the service resolves an id to its label through the
mission state first, and matching compares normalised labels.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from aura.query_service.errors import require_aware
from aura.schemas import CertaintyLevel
from aura.state_manager.entities import normalize_label

# מהחמור לקל, לשימוש ב-``at_least``.
CERTAINTY_ORDER = (
    CertaintyLevel.VERIFIED,
    CertaintyLevel.REPORTED,
    CertaintyLevel.UNCERTAIN,
)


def at_least(level: CertaintyLevel) -> frozenset[CertaintyLevel]:
    """כל רמות הוודאות שהן ברמה הזו או גבוהות ממנה.

    "לפחות מדווח" הוא בקשה סבירה יותר מ"מדווח בדיוק", ובלי עזר כזה
    הקורא היה בונה את הקבוצה בעצמו - ואז סדר החומרה היה נכתב מחדש בכל
    מקום שמסנן.
    """
    cutoff = CERTAINTY_ORDER.index(level)
    return frozenset(CERTAINTY_ORDER[: cutoff + 1])


@dataclass(frozen=True)
class EventFilter:
    """סינון ציר הזמן. שדה שלא נקבע אינו מסנן דבר."""

    flights: frozenset[str] | None = None
    start: datetime | None = None
    end: datetime | None = None
    types: frozenset[str] | None = None
    entity: str | None = None
    certainty: frozenset[CertaintyLevel] | None = None
    relations: frozenset[str] | None = None
    limit: int | None = None

    def __post_init__(self) -> None:
        """גבולות הטווח נבדקים כאן, במקום שבו הערך נכנס.

        בדיקה בתוך השאילתה הייתה מצביעה על השורה שמשווה זמנים, ולא על
        השורה שבנתה את המסנן - והשנייה היא זו שצריך לתקן.
        """
        require_aware(self.start, "start")
        require_aware(self.end, "end")

    @classmethod
    def build(
        cls,
        *,
        flights: Iterable[str] | str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        types: Iterable[str] | str | None = None,
        entity: str | None = None,
        certainty: Iterable[CertaintyLevel] | CertaintyLevel | None = None,
        relations: Iterable[str] | str | None = None,
        limit: int | None = None,
    ) -> EventFilter:
        """בנייה סובלנית: מקבלת ערך בודד או אוסף בכל שדה רב-ערכי.

        ``types="malfunction"`` ו-``types=["malfunction"]`` הם אותה
        בקשה, ואין סיבה שהקורא ישלם על ההבדל.
        """
        return cls(
            flights=_as_set(flights),
            start=start,
            end=end,
            types=_as_set(types),
            entity=entity,
            certainty=_as_set(certainty),
            relations=_as_set(relations),
            limit=limit,
        )

    @property
    def empty(self) -> bool:
        return not any(
            (
                self.flights,
                self.start,
                self.end,
                self.types,
                self.entity,
                self.certainty,
                self.relations,
            )
        )

    def in_window(self, spoken_at: datetime | None) -> bool:
        """האם זמן האמירה בטווח. גבולות כולל, כמו במאגר הגולמי.

        אירוע שאין לו זמן אמירה כלל אינו נכנס לטווח מבוקש: החלופה היא
        להחזיר אותו בכל טווח, וטווח שמחזיר גם את מה שאינו ידוע שהוא
        בתוכו אינו טווח.
        """
        if self.start is None and self.end is None:
            return True
        if spoken_at is None:
            return False
        if self.start is not None and spoken_at < self.start:
            return False
        return not (self.end is not None and spoken_at > self.end)

    def matches(
        self,
        *,
        flight_id: str,
        event_type: str,
        relation: str,
        certainty: CertaintyLevel,
        entity_labels: Iterable[str],
        spoken_at: datetime | None,
    ) -> bool:
        """האם אירוע עובר את כל הסינונים שנקבעו."""
        if self.flights is not None and flight_id not in self.flights:
            return False
        if self.types is not None and event_type not in self.types:
            return False
        if self.relations is not None and relation not in self.relations:
            return False
        if self.certainty is not None and certainty not in self.certainty:
            return False
        if not self.in_window(spoken_at):
            return False
        if self.entity is not None:
            wanted = normalize_label(self.entity)
            if wanted not in {normalize_label(label) for label in entity_labels}:
                return False
        return True


def _as_set(value: object) -> frozenset | None:
    if value is None:
        return None
    if isinstance(value, str):
        return frozenset({value})
    if isinstance(value, CertaintyLevel):
        return frozenset({value})
    items = frozenset(value)  # type: ignore[arg-type]
    return items or None

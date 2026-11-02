"""Writing the summary text (רכיב 8, משימה 2).

The stage has two pass criteria that pull against each other: the summary
must not exceed its size limit, and it must contain **100% of the active
entities** at every checkpoint. With enough targets those two cannot both
be satisfied by writing less, so the composer has to decide what gives -
and it decides in favour of coverage.

The reasoning is that the two failures are not symmetrical. A summary
that runs slightly long costs tokens. A summary that silently drops an
active target loses the crew's knowledge of something that is still out
there, and the system has no way to notice it did so. So sections are
trimmed in a fixed order, entity *descriptions* shorten before entity
*names* disappear, and the entity list is the last thing standing.

The default composer is deterministic rather than model-written, and that
is a deliberate choice for a component whose criteria are mechanical. A
model asked to "summarise in under 1200 characters covering every active
entity" will usually comply and occasionally not, with no way to tell
which run you got. Composition that guarantees both by construction can
be *tested* to guarantee both. :class:`LLMSummaryComposer` is there for
when prose matters more than the guarantee, and it is honest about the
trade: it validates its own output and falls back rather than return
something that breaks the criteria.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from aura.config import DEFAULT_SETTINGS, Settings
from aura.schemas import Entity, EntityStatus, Event, MissionState, Summary, SummaryTrigger

ELLIPSIS = "…"

TRIGGER_LABELS = {
    SummaryTrigger.MISSION_CHANGE: "שינוי משימה",
    SummaryTrigger.SIGNIFICANT_EVENT: "אירוע משמעותי",
    SummaryTrigger.MAJOR_STATE_CHANGE: "שינוי מצב מהותי",
    SummaryTrigger.THRESHOLD_EXCEEDED: "מעבר סף",
    SummaryTrigger.POST_FLIGHT: "סוף טיסה",
}

ENTITY_TYPE_LABELS = {"target": "מטרה", "area": "אזור", "force": "כוח"}


class SummaryComposer(Protocol):
    """כותב את טקסט התקציר."""

    def compose(
        self,
        *,
        flight_id: str,
        state: MissionState,
        new_events: Sequence[Event],
        previous: Summary | None,
        trigger: SummaryTrigger,
    ) -> str: ...


def _active(state: MissionState) -> list[Entity]:
    return [e for e in state.entities.values() if e.status is EntityStatus.ACTIVE]


def _closed(state: MissionState) -> list[Entity]:
    return [e for e in state.entities.values() if e.status is EntityStatus.CLOSED]


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if limit <= 1 or len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + ELLIPSIS


class DeterministicComposer:
    """מרכיב תקציר מהמצב ומהאירועים, בלי מודל.

    הסדר קבוע ומכוון: מה שפעיל עכשיו, מה שנסגר, ומה חדש מאז התקציר
    הקודם. קורא שרואה רק את השורות הראשונות עדיין יודע את הדבר החשוב
    ביותר - מה פתוח.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings if settings is not None else DEFAULT_SETTINGS

    def compose(
        self,
        *,
        flight_id: str,
        state: MissionState,
        new_events: Sequence[Event],
        previous: Summary | None,
        trigger: SummaryTrigger,
    ) -> str:
        budget = self.settings.summary.max_chars
        active = _active(state)
        closed = _closed(state)

        header = f"טיסה {flight_id} · {TRIGGER_LABELS.get(trigger, trigger.value)}"

        # תקציב התיאורים נגזר ממה שנשאר אחרי השמות, ולא להפך: השמות הם
        # מה שחייב להיכנס, והתיאור הוא מה שמתקצר כדי לפנות להם מקום.
        names_cost = sum(len(e.label) + 12 for e in active)
        room_for_detail = max(budget - len(header) - names_cost - 80, 0)
        per_entity = max(room_for_detail // max(len(active), 1), 0)

        lines = [header]
        if active:
            lines.append("")
            lines.append("פעיל עכשיו:")
            for entity in active:
                lines.append(self._entity_line(entity, per_entity))
        if closed:
            lines.append("")
            lines.append("נסגר: " + ", ".join(e.label for e in closed))

        body = "\n".join(lines)
        remaining = budget - len(body)
        if remaining > 40 and new_events:
            addition = self._events_block(new_events, remaining - 2)
            if addition:
                body = f"{body}\n\n{addition}"

        if len(body) > budget:
            body = self._enforce_budget(body, header, active, closed, budget)
        return body

    def _entity_line(self, entity: Entity, detail_budget: int) -> str:
        type_label = ENTITY_TYPE_LABELS.get(entity.type.value, entity.type.value)
        line = f"  • {entity.label} ({type_label})"
        if not entity.attributes or detail_budget <= 0:
            return line
        parts = []
        for name, attribute in entity.attributes.items():
            flag = " ⚠" if attribute.contradicted else ""
            parts.append(f"{name}: {attribute.current.value}{flag}")
        return f"{line} — {_shorten('; '.join(parts), detail_budget)}"

    def _events_block(self, events: Sequence[Event], budget: int) -> str:
        lines = ["מאז התקציר הקודם:"]
        used = len(lines[0])
        for event in events:
            text = f"  · {_shorten(event.new_info or event.description, 110)}"
            if used + len(text) + 1 > budget:
                skipped = len(events) - (len(lines) - 1)
                if skipped > 0:
                    tail = f"  · (ועוד {skipped})"
                    if used + len(tail) + 1 <= budget:
                        lines.append(tail)
                break
            lines.append(text)
            used += len(text) + 1
        return "\n".join(lines) if len(lines) > 1 else ""

    def _enforce_budget(
        self,
        body: str,
        header: str,
        active: list[Entity],
        closed: list[Entity],
        budget: int,
    ) -> str:
        """הנפילה האחרונה: שמות הישויות הפעילות שורדים, השאר נופל.

        כאן נמצאת ההכרעה בין שני הקריטריונים, ברגע שבו הם באמת
        מתנגשים: די ישויות פעילות, ושמותיהן לבדם ארוכים מהתקציב. אז
        **הכיסוי מנצח והתקציר חורג**, עם שורה שאומרת זאת במפורש.

        ההיגיון הוא שהתקלות אינן סימטריות. תקציר שחורג עולה טוקנים,
        וזה נראה מיד. תקציר שמשמיט מטרה פעילה מאבד ידע על משהו שעדיין
        שם, ואין במערכת דבר שיבחין בכך. חריגה מוצהרת עדיפה על שלמות
        מדומה - ולכן גם נכתב כמה ישויות יש, כדי שהחריגה תהיה מדידה.
        """
        minimal = [header, "", "פעיל עכשיו:"]
        minimal += [f"  • {entity.label}" for entity in active]
        if closed:
            minimal.append(f"נסגר: {len(closed)}")
        trimmed = "\n".join(minimal)
        if len(trimmed) <= budget:
            return trimmed
        return "\n".join(
            [
                header,
                f"(חריגה מהתקציב: {len(active)} ישויות פעילות אינן נכנסות ב-{budget} תווים)",
                "פעיל עכשיו:",
                *[f"  • {entity.label}" for entity in active],
            ]
        )


class LLMSummaryComposer:
    """תקציר שנכתב על ידי מודל, עם בדיקה עצמית ונפילה לאחור.

    המודל כותב עברית טובה יותר, אבל אינו יכול *להבטיח* את שני
    הקריטריונים. לכן הפלט שלו נבדק מול אותם שני תנאים, וכשהוא נכשל
    מוחזר התקציר הדטרמיניסטי. תקציר קריא פחות עדיף על תקציר שמשמיט
    מטרה פעילה בלי שאיש ידע.
    """

    def __init__(self, client, settings: Settings | None = None) -> None:
        self.client = client
        self.settings = settings if settings is not None else DEFAULT_SETTINGS
        self.fallback = DeterministicComposer(self.settings)
        self.fallback_count = 0

    def compose(self, **kwargs) -> str:
        safe = self.fallback.compose(**kwargs)
        state: MissionState = kwargs["state"]
        try:
            text = self.client.complete(
                self._system_prompt(), self._user_prompt(safe, kwargs["trigger"]), {}
            ).text.strip()
        except Exception:
            self.fallback_count += 1
            return safe
        if self._acceptable(text, state):
            return text
        self.fallback_count += 1
        return safe

    def _acceptable(self, text: str, state: MissionState) -> bool:
        if not text or len(text) > self.settings.summary.max_chars:
            return False
        return all(entity.label in text for entity in _active(state))

    def _system_prompt(self) -> str:
        return (
            "אתה כותב תקציר מצב קצר לטיסה, בעברית. "
            f"עד {self.settings.summary.max_chars} תווים. "
            "חובה להזכיר בשמה כל ישות פעילה שמופיעה בקלט. "
            "בלי להמציא מידע שאינו בקלט."
        )

    def _user_prompt(self, structured: str, trigger: SummaryTrigger) -> str:
        return f"סיבת העדכון: {TRIGGER_LABELS.get(trigger, trigger.value)}\n\n{structured}"

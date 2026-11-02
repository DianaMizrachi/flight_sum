"""When a summary is worth writing (רכיב 8, משימה 1).

The plan is specific that summarising is *trigger-driven* - "fires only
on triggers" - and its test is a count: how many times did it run over a
whole flight, and is that fewer than once per sentence.

That bar is easy to fail by accident, because component 6 already
proposes a trigger and proposes one generously: any window that changed
anything comes back as ``significant_event``. Taking that at face value
means a summary after nearly every window, which is both expensive and
useless - a summary that changes constantly is not a summary.

So the manager answers "did something notable happen" and this answers
"is it worth writing a new summary", and the two are deliberately not the
same question. Mission changes and major state changes pass immediately,
because those are the moments the context genuinely turned. Ordinary
significant events have to accumulate first.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aura.config import DEFAULT_SETTINGS, Settings
from aura.schemas import SummaryTrigger

# הטריגרים שעוקפים כל סף: הם מגדירים מחדש את ההקשר, ולכן תקציר שלא
# משקף אותם הוא תקציר שגוי, לא רק ישן.
IMMEDIATE_TRIGGERS = frozenset(
    {SummaryTrigger.MISSION_CHANGE, SummaryTrigger.MAJOR_STATE_CHANGE, SummaryTrigger.POST_FLIGHT}
)


@dataclass
class PendingWork:
    """מה הצטבר מאז התקציר האחרון."""

    events: int = 0
    messages: int = 0
    tokens: int = 0

    def add(self, *, events: int = 0, messages: int = 0, tokens: int = 0) -> None:
        self.events += events
        self.messages += messages
        self.tokens += tokens

    def clear(self) -> None:
        self.events = self.messages = self.tokens = 0

    @property
    def empty(self) -> bool:
        return not (self.events or self.messages or self.tokens)


@dataclass
class SummaryPolicy:
    """מחליטה אם הטריגר שהוצע מצדיק תקציר חדש עכשיו."""

    settings: Settings = field(default_factory=lambda: DEFAULT_SETTINGS)
    pending: PendingWork = field(default_factory=PendingWork)

    def record(self, *, events: int = 0, messages: int = 0, tokens: int = 0) -> None:
        self.pending.add(events=events, messages=messages, tokens=tokens)

    def decide(self, proposed: SummaryTrigger | None) -> SummaryTrigger | None:
        """הטריגר שבאמת יופעל, או ``None`` אם אין מה לתקצר עכשיו.

        הסף של מספר המשפטים והטוקנים נבדק *גם כשלא הוצע טריגר כלל*: טיסה
        ארוכה ושקטה, בלי אף אירוע מהותי, עדיין צוברת הקשר שצריך לתקצר -
        וזה בדיוק מה שהתכנית מונה כטריגר רביעי.
        """
        config = self.settings.summary

        if proposed in IMMEDIATE_TRIGGERS:
            return proposed

        crossed_threshold = (
            self.pending.messages >= config.threshold_messages
            or self.pending.tokens >= config.threshold_tokens
        )
        if crossed_threshold:
            return SummaryTrigger.THRESHOLD_EXCEEDED

        if proposed is SummaryTrigger.SIGNIFICANT_EVENT:
            if self.pending.events >= config.min_events_between:
                return proposed
            return None

        return None

    def taken(self) -> None:
        """מסמנת שתקציר נכתב - הצבירה מתאפסת."""
        self.pending.clear()

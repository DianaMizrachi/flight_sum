"""The retry policy (רכיב 5, משימה 5).

The plan allows the model exactly one second chance: *"one repair attempt
with the error message, then flagged for manual review."* Both halves of
that matter. Without a retry, a model that merely mislabelled one event
costs the window its whole output. Without a *bound* on the retry, a
model that cannot satisfy the rule retries forever on live traffic, and
the component that is supposed to protect the pipeline becomes the thing
that stalls it.

So the loop here is deliberately not a loop: one call, and if it still
fails, the result is marked ``manual_review`` and handed on. Nothing is
silently dropped - the rejected events keep their raw payload and their
reason, which is the whole point of having somewhere to look afterwards.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from aura.schemas import (
    ContextBundle,
    LLMResult,
    RejectedEvent,
    ValidatedResult,
    ValidationStatus,
    VersionTag,
)
from aura.validator.validator import OutputValidator

# הפונקציה שמריצה את המודל. בניסיון הראשון היא מקבלת ``None``, ובניסיון
# התיקון - הודעת השגיאה, כדי שהמודל יראה מה בדיוק נדחה ולמה.
DetectFn = Callable[[str | None], LLMResult]


def format_feedback(rejected: Iterable[RejectedEvent]) -> str:
    """הודעת התיקון שנשלחת חזרה למודל.

    נכתבת מהדחיות עצמן ולא מנוסחת ביד בכל מקום קריאה, כדי שמה שהמודל
    מתבקש לתקן יהיה בדיוק מה שהוולידטור דחה - ולא ניסוח מקביל שנשאר
    מאחור כשכלל משתנה.
    """
    lines = ["הפלט הקודם נדחה. תקן את הנקודות הבאות והחזר JSON תקין בלבד:"]
    for index, item in enumerate(rejected, start=1):
        event_type = item.raw.get("type", "?")
        detail = item.detail or item.reason
        lines.append(f"{index}. אירוע מסוג {event_type!r} - {item.reason}: {detail}")
    return "\n".join(lines)


@dataclass
class RepairOutcome:
    """תוצאת מדיניות התיקון: מה התקבל בסוף, וכמה ניסיונות זה לקח."""

    result: ValidatedResult
    attempts: int = 1
    feedback_sent: list[str] = field(default_factory=list)

    @property
    def needs_manual_review(self) -> bool:
        return self.result.status == ValidationStatus.MANUAL_REVIEW


def validate_with_repair(
    validator: OutputValidator,
    bundle: ContextBundle,
    detect: DetectFn,
    *,
    max_repair_attempts: int = 1,
    known_entity_ids: Iterable[str] | None = None,
    version: VersionTag = VersionTag.REALTIME,
) -> RepairOutcome:
    """מריץ את המודל, מאמת, ובמקרה של דחייה נותן לו הזדמנות אחת לתקן.

    התוצאה המוחזרת היא של הניסיון האחרון, גם כשהוא גרוע מקודמו. זו
    הסמנטיקה של "תיקון": המודל קיבל את השגיאות וענה שוב, והתשובה
    השנייה היא תשובתו. לשמור במקום זאת את הניסיון ה"טוב ביותר" היה
    אומר להרכיב פלט שהמודל מעולם לא הפיק.
    """
    known = list(known_entity_ids) if known_entity_ids is not None else None
    feedback_sent: list[str] = []
    attempts = 0
    result = None

    while attempts <= max_repair_attempts:
        feedback = feedback_sent[-1] if feedback_sent else None
        llm_result = detect(feedback)
        attempts += 1
        result = validator.validate(
            llm_result,
            bundle,
            known_entity_ids=known,
            version=version,
            retry_count=attempts - 1,
        )
        if not result.rejected:
            # ``retried`` פירושו "נדרש תיקון והוא הצליח" - מידע שאובד
            # אם מסמנים את התוצאה ``ok`` רק מפני שהיא נקייה עכשיו.
            result.status = ValidationStatus.OK if attempts == 1 else ValidationStatus.RETRIED
            return RepairOutcome(result=result, attempts=attempts, feedback_sent=feedback_sent)
        if attempts > max_repair_attempts:
            break
        feedback_sent.append(format_feedback(result.rejected))

    assert result is not None  # הלולאה רצה לפחות פעם אחת
    result.status = ValidationStatus.MANUAL_REVIEW
    return RepairOutcome(result=result, attempts=attempts, feedback_sent=feedback_sent)

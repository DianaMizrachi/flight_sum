"""רכיב 5 - אימות ופענוח פלט המודל (שלב 5).

``LLMResult`` נכנס, ``ValidatedResult`` יוצא: רק אירועים שעומדים בחוזה
ממשיכים למנהל המצב, והשאר נשמרים עם סיבה ועם הפלט הגולמי. מדיניות
התיקון נותנת למודל הזדמנות אחת לתקן את עצמו לפני סימון לבדיקה ידנית.
"""

from aura.validator.reasons import RejectionReason
from aura.validator.repair import (
    DetectFn,
    RepairOutcome,
    format_feedback,
    validate_with_repair,
)
from aura.validator.rules import (
    PER_EVENT_RULES,
    Rejection,
    ValidationContext,
    context_from_bundle,
    find_contradictions,
)
from aura.validator.validator import OutputValidator, default_event_id

__all__ = [
    "PER_EVENT_RULES",
    "DetectFn",
    "OutputValidator",
    "Rejection",
    "RejectionReason",
    "RepairOutcome",
    "ValidationContext",
    "context_from_bundle",
    "default_event_id",
    "find_contradictions",
    "format_feedback",
    "validate_with_repair",
]

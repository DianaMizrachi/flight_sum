from pydantic import BaseModel, ConfigDict, Field

from aura.schemas.enums import ValidationStatus
from aura.schemas.event import Event


class RejectedEvent(BaseModel):
    """אירוע שנדחה באימות (רכיב 5), עם הסיבה והפלט הגולמי לבדיקה ידנית."""

    model_config = ConfigDict(extra="forbid")

    raw: dict = Field(description="הפלט הגולמי מהמודל שנדחה, כמילון")
    reason: str = Field(
        min_length=1,
        description=(
            "קוד הדחייה היציב מתוך ``RejectionReason`` - מה שנספר, מסונן ומושווה "
            "מול הפיקסצ'רים. ההסבר החופשי יושב ב-``detail`` ולא כאן, כדי שהקוד "
            "יישאר בר-השוואה מדויקת."
        ),
    )
    detail: str | None = Field(
        default=None, description="הסבר קריא לאדם שבא לבדוק את הדחייה ידנית"
    )
    retry_count: int = Field(default=0, ge=0)


class ValidatedResult(BaseModel):
    """פלט שלב האימות (רכיב 5): אירועים מאומתים מוכנים למנהל המצב, ונדחים."""

    model_config = ConfigDict(extra="forbid")

    window_id: str = Field(min_length=1)
    accepted: list[Event] = Field(default_factory=list)
    rejected: list[RejectedEvent] = Field(default_factory=list)
    status: ValidationStatus = ValidationStatus.OK

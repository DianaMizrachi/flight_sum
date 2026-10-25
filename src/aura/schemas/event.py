from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from aura.schemas.entity import EntityRef
from aura.schemas.enums import InfoRelation, ReconciliationAction, VersionTag


class Event(BaseModel):
    """אירוע משמעותי שזוהה בחלון משפטים, לאחר אימות (רכיבים 4-6).

    ``source_ids`` הם מזהי ה-``Message`` שמהם נגזר האירוע - השדה שמאפשר
    לחזור מכל אירוע לתמלול המקורי. ``type`` הוא מזהה מתוך הטקסונומיה
    (``aura.taxonomy``), לא Enum קבוע, כדי שאפשר להרחיב סוגי אירועים
    בלי לשנות את הסכמה.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    flight_id: str = Field(min_length=1)
    window_id: str = Field(min_length=1)
    type: str = Field(min_length=1)
    description: str = Field(min_length=1)
    entities: list[EntityRef] = Field(default_factory=list)
    new_info: str = Field(description="תקציר המידע החדש שהאירוע מוסיף")
    relation_to_previous: InfoRelation
    related_event_id: str | None = Field(
        default=None, description="האירוע הקודם שמתעדכן/נסתר/נסגר, אם רלוונטי"
    )
    confidence: float = Field(ge=0.0, le=1.0)
    source_ids: list[str] = Field(min_length=1)
    version: VersionTag
    created_at: datetime

    # מעבר התיקון (שלב 11) מוסיף את השדות הבאים לאירועי הגרסה הסופית בלבד.
    reconciles_event_id: str | None = Field(
        default=None, description="מזהה אירוע גרסת זמן האמת שהאירוע הזה משקף"
    )
    reconciliation_action: ReconciliationAction | None = None
    reconciliation_reason: str | None = None

    @field_validator("source_ids")
    @classmethod
    def source_ids_unique(cls, v: list[str]) -> list[str]:
        if len(v) != len(set(v)):
            raise ValueError("source_ids must not contain duplicates")
        return v

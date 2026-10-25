from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from aura.schemas.enums import CertaintyLevel, EntityType, VersionTag


class StateChangeKind(StrEnum):
    """סוג השינוי במצב, לפי יחס המידע שהפיק אותו (ראו InfoRelation)."""

    ADDED = "added"
    REPLACED = "replaced"
    CONTRADICTED = "contradicted"
    CLOSED = "closed"


class StateChange(BaseModel):
    """רשומת שינוי מצב בודדת (append-only), תוצר מנהל המצב (רכיב 6).

    מאפשרת שחזור מצב לכל נקודת זמן (רכיב 7) ע"י הרצה מחדש של הרשומות
    עד אותה נקודת זמן.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    flight_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1, description="האירוע שגרם לשינוי")
    entity_id: str = Field(min_length=1)
    entity_type: EntityType
    attribute: str | None = Field(default=None, description="None כאשר השינוי הוא ברמת הישות")
    kind: StateChangeKind
    old_value: str | None = None
    new_value: str | None = None
    certainty: CertaintyLevel
    version: VersionTag
    applied_at: datetime

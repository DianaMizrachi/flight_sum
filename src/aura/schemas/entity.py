from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from aura.schemas.enums import CertaintyLevel, EntityStatus, EntityType


class EntityRef(BaseModel):
    """הפניה קלה לישות, כפי שמודל ה-LLM או אימות הפלט מייצרים אותה.

    ``id`` חסר/None כאשר המודל מציג ישות חדשה שלא הופיעה קודם בטיסה -
    מנהל המצב (רכיב 6) אחראי להקצות לה מזהה.
    """

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    type: EntityType
    label: str = Field(min_length=1)


class AttributeSnapshot(BaseModel):
    """ערך בודד של מאפיין ישות, עם המקור ורמת הוודאות שלו."""

    model_config = ConfigDict(extra="forbid")

    value: str
    source_event_id: str
    certainty: CertaintyLevel
    updated_at: datetime


class EntityAttribute(BaseModel):
    """מאפיין ישות עם הערך הנוכחי וההיסטוריה - עדכון לא מוחק את הקודם."""

    model_config = ConfigDict(extra="forbid")

    current: AttributeSnapshot
    history: list[AttributeSnapshot] = Field(default_factory=list)
    contradicted: bool = False


class Entity(BaseModel):
    """ישות במצב המשימה: מטרה, אזור או כוח, עם מאפייניה."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    type: EntityType
    label: str = Field(min_length=1)
    status: EntityStatus = EntityStatus.ACTIVE
    attributes: dict[str, EntityAttribute] = Field(default_factory=dict)
    closed_at: datetime | None = None
    closed_by_event_id: str | None = None

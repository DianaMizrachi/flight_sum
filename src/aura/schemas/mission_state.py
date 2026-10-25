from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from aura.schemas.entity import Entity
from aura.schemas.enums import VersionTag


class MissionState(BaseModel):
    """תמונת מצב המשימה בנקודת זמן נתונה (רכיב 6/7).

    ``as_of`` הוא נקודת הזמן שהמצב משקף - מאפשר שחזור מצב לכל נקודת
    זמן מתוך רצף ה-``StateChange`` (שלב 7).
    """

    model_config = ConfigDict(extra="forbid")

    flight_id: str = Field(min_length=1)
    version: VersionTag
    as_of: datetime
    entities: dict[str, Entity] = Field(default_factory=dict)

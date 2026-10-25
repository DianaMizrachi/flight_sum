from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from aura.schemas.enums import SummaryTrigger, VersionTag


class Summary(BaseModel):
    """תקציר טיסה בגרסה נתונה (רכיבים 7-8). הטקסט בעברית."""

    model_config = ConfigDict(extra="forbid")

    flight_id: str = Field(min_length=1)
    version: VersionTag
    version_number: int = Field(ge=1, description="מספר גרסה עולה לאותה טיסה/תגית גרסה")
    text: str = Field(min_length=1, description="טקסט התקציר, בעברית")
    trigger_reason: SummaryTrigger
    covers_up_to_message_id: str | None = None
    entities_covered: list[str] = Field(default_factory=list)
    created_at: datetime

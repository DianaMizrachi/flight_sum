from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from aura.schemas.enums import MessageStatus


class Message(BaseModel):
    """משפט תמלול בודד, כפי שהוא נקלט ונשמר במאגר הגולמי (רכיבים 1-2).

    ``timestamp`` הוא חותמת הזמן המקורית מהמקור (UTC) ואינה משתנה גם אם
    ההודעה מגיעה למערכת באיחור - ``ingested_at`` משמש לזיהוי איחור.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    flight_id: str = Field(min_length=1)
    timestamp: datetime
    ingested_at: datetime
    source: str = Field(min_length=1, description="דובר/מקור ההודעה, למשל operator, pilot, ops")
    channel: str | None = Field(default=None, description="ערוץ תקשורת, אם רלוונטי")
    text: str
    language: str = Field(default="he")
    dedup_hash: str | None = Field(
        default=None, description="hash של מקור+זמן+טקסט, לשימוש כשאין מזהה ייחודי מהמקור"
    )
    status: MessageStatus = MessageStatus.ACCEPTED
    dead_letter_reason: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

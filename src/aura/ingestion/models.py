"""Ingestion's own output types (שלב 1).

``Message`` is the cross-component contract (stage 0); the models here are
local to component 1: the reasons a payload can be rejected, what gets
written to the dead-letter log, and what one ingest attempt returned.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aura.ingestion.dedup import DedupStrategy
from aura.schemas import Message, MessageStatus


class DeadLetterReason(StrEnum):
    """סיבות דחייה לקליטה. ארבע הראשונות הן אוצר המילים של פיקסצ'ר
    ``malformed_and_empty.yaml``; השאר נחוצות לתעבורה אמיתית."""

    MISSING_REQUIRED_FIELD = "missing_required_field"
    EMPTY_TEXT = "empty_text"
    INVALID_TIMESTAMP = "invalid_timestamp"
    SCHEMA_INVALID = "schema_invalid"


class DeadLetterRecord(BaseModel):
    """רשומת dead-letter: הודעה שלא ניתן היה לקלוט, עם הסיבה והמקור הגולמי.

    ``raw_payload`` נשמר כפי שהגיע, לפי העיקרון "שמירה לפני עיבוד" - גם
    הודעה פגומה לא נעלמת, וניתן לנסות לקלוט אותה מחדש אחרי תיקון המקור.
    """

    model_config = ConfigDict(extra="forbid")

    reason: DeadLetterReason
    detail: str | None = Field(default=None, description="פירוט חופשי, למשל שגיאת הסכמה")
    missing_field: str | None = Field(default=None, description="שם שדה החובה החסר, אם רלוונטי")
    flight_id: str | None = None
    message_id: str | None = None
    raw_payload: dict[str, Any]
    recorded_at: datetime


class IngestResult(BaseModel):
    """תוצאת ניסיון קליטה אחד: נקלט / כפילות / dead-letter."""

    model_config = ConfigDict(extra="forbid")

    status: MessageStatus
    message: Message | None = Field(
        default=None, description="ה-Message שנבנה; קיים גם לכפילות, מסומן כ-duplicate"
    )
    dead_letter: DeadLetterRecord | None = None
    dedup_strategy: DedupStrategy | None = Field(
        default=None, description="המפתח שלפיו נבדקה הכפילות - גם כשההודעה נקלטה"
    )
    duplicate_of: str | None = Field(default=None, description="מזהה ההודעה שנקלטה ראשונה")
    queued: bool = Field(default=False, description="האם ההודעה הועברה לתור הפנימי")

    @property
    def accepted(self) -> bool:
        return self.status == MessageStatus.ACCEPTED

    @model_validator(mode="after")
    def _status_matches_payload(self) -> "IngestResult":
        if self.status == MessageStatus.DEAD_LETTER:
            if self.dead_letter is None:
                raise ValueError("dead_letter result must carry a DeadLetterRecord")
        elif self.message is None:
            raise ValueError(f"{self.status} result must carry a Message")
        if self.status == MessageStatus.DUPLICATE and self.duplicate_of is None:
            raise ValueError("duplicate result must name the message it duplicates")
        if self.queued and self.status != MessageStatus.ACCEPTED:
            raise ValueError("only accepted messages are published to the queue")
        return self

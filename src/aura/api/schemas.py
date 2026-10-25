"""HTTP-level request/response models for the aura API.

Deliberately permissive: every ingest field is optional at the transport
layer, because component 1 - not FastAPI - decides what is invalid and
writes it to the dead-letter log with a reason. If the HTTP model rejected a
payload first, that payload would be lost with a bare 422, which is exactly
the "nothing is swallowed" rule the plan sets for stage 1.

Responses reuse the component's own types (``IngestResult``,
``DeadLetterRecord``), so the published contract is the component contract.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aura.ingestion import IngestResult


class IngestRequest(BaseModel):
    """משפט תמלול בודד כפי שהמקור שולח אותו.

    ``flight_id``, ``timestamp``, ``source`` ו-``text`` נדרשים בפועל; הם
    מוגדרים כאופציונליים כדי שהודעה חסרה תגיע ל-dead-letter עם סיבה
    במקום להידחות בשכבת ה-HTTP. שדות שאינם מוכרים נשמרים ונרשמים
    ב-``metadata["unmapped_fields"]``.
    """

    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={
            "examples": [
                {
                    "id": "flight-01-m04",
                    "flight_id": "flight-01",
                    "timestamp": "2026-09-01T06:12:00Z",
                    "source": "operator",
                    "channel": "command",
                    "text": "בקרה, נץ אחת, קשר עין עם רכב בודד בציר המזרחי.",
                },
                {
                    "flight_id": "flight-01",
                    "timestamp": "2026-09-01T06:12:30Z",
                    "source": "operator",
                    "text": "בקרה, נץ אחת, ממשיכים מעקב.",
                },
            ]
        },
    )

    id: str | None = Field(default=None, description="מזהה ייחודי מהמקור; בהיעדרו נגזר מ-hash")
    flight_id: str | None = Field(default=None, description="מזהה הטיסה (נדרש)")
    timestamp: str | None = Field(
        default=None, description="חותמת הזמן המקורית, ISO-8601 (נדרש). חותמת ללא אזור זמן = UTC"
    )
    ingested_at: str | None = Field(
        default=None,
        description="זמן הקליטה - לשימוש בהזרמה חוזרת/השלמה בדיעבד. בתעבורה רגילה נקבע בשרת",
    )
    source: str | None = Field(default=None, description="דובר/מקור ההודעה, למשל operator (נדרש)")
    channel: str | None = Field(default=None, description="ערוץ תקשורת, אם רלוונטי")
    text: str | None = Field(default=None, description="טקסט המשפט (נדרש)")
    language: str | None = Field(default=None, description="קוד שפה; ברירת מחדל he")
    metadata: dict[str, str] | None = Field(default=None, description="מטא-דאטה חופשית מהמקור")


class BatchIngestRequest(BaseModel):
    """רצף הודעות, נקלטות בסדר שבו נשלחו - לא בסדר חותמות הזמן שלהן."""

    model_config = ConfigDict(extra="forbid")

    messages: list[IngestRequest] = Field(min_length=1, max_length=1000)


class BatchIngestResponse(BaseModel):
    """סיכום קליטה מרוכז: מנייה לפי סטטוס, ותוצאה מלאה לכל הודעה."""

    model_config = ConfigDict(extra="forbid")

    accepted_count: int
    duplicate_count: int
    dead_letter_count: int
    results: list[IngestResult]


class QueueStatus(BaseModel):
    """מצב התור הפנימי. התור הוא בזיכרון עד שנכנס צורכן אמיתי (שלב 3)."""

    model_config = ConfigDict(extra="forbid")

    depth: int = Field(description="מספר ההודעות שממתינות לעיבוד אסינכרוני")


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    version: str
    implemented_stages: list[int] = Field(description="השלבים בתכנית העבודה שממומשים בבנייה הזו")


class ErrorResponse(BaseModel):
    """שגיאת שכבת ה-HTTP (גוף לא תקין). דחיית הודעה *תקינה מבחינת תחבורה*
    אך פגומה בתוכנה מוחזרת כ-``IngestResult`` עם ``dead_letter``, לא כאן."""

    model_config = ConfigDict(extra="forbid")

    detail: Any

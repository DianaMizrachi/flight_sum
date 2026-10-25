from pydantic import BaseModel, ConfigDict, Field

from aura.schemas.entity import EntityRef
from aura.schemas.enums import InfoRelation


class LLMEvent(BaseModel):
    """אירוע כפי שמודל ה-LLM מחזיר אותו, לפני אימות (רכיב 4).

    בכוונה פחות מחמיר מ-``Event``: אין עדיין ``id`` או ``version``, ו-``type``
    הוא המחרוזת החופשית שהמודל הפיק - האימות (רכיב 5) הוא זה שבודק אותה
    מול הטקסונומיה.
    """

    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1)
    description: str = Field(min_length=1)
    entities: list[EntityRef] = Field(default_factory=list)
    new_info: str = ""
    relation_to_previous: InfoRelation
    confidence: float = Field(ge=0.0, le=1.0)
    source_ids: list[str] = Field(min_length=1)


class LLMResult(BaseModel):
    """פלט מנוע ה-LLM לחלון משפטים בודד (רכיב 4) - רשימת אירועים, גם ריקה."""

    model_config = ConfigDict(extra="forbid")

    window_id: str = Field(min_length=1)
    events: list[LLMEvent] = Field(default_factory=list)
    llm_model: str = Field(min_length=1, description="מזהה המודל שהפיק את התשובה")
    raw_response: str | None = Field(
        default=None, description="התשובה הגולמית מהמודל, לשמירה/debug"
    )

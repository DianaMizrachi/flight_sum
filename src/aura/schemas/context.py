from pydantic import BaseModel, ConfigDict, Field

from aura.schemas.enums import WindowCloseReason
from aura.schemas.event import Event
from aura.schemas.message import Message
from aura.schemas.mission_state import MissionState
from aura.schemas.summary import Summary
from aura.schemas.taxonomy import EventTypeDefinition


class TokenBudgetInfo(BaseModel):
    """מידע אבחוני על תקציב הטוקנים של החבילה (שלב 3, משימה 4)."""

    model_config = ConfigDict(extra="forbid")

    budget: int = Field(gt=0)
    used_estimate: int = Field(ge=0)
    history_items_dropped: int = Field(default=0, ge=0)


class ContextBundle(BaseModel):
    """חבילת ההקשר שבונה רכיב 3 ומוזנת למנוע ה-LLM (רכיב 4).

    ``messages`` הוא חלון המשפטים (כולל חפיפה), ``relevant_history`` הם
    אירועים היסטוריים רלוונטיים לישויות שבחלון. בשלבים 3-5, לפני שמנהל
    המצב (רכיב 6) קיים, ``current_state`` ו-``last_summary`` הם fixtures.
    """

    model_config = ConfigDict(extra="forbid")

    window_id: str = Field(min_length=1)
    flight_id: str = Field(min_length=1)
    messages: list[Message] = Field(min_length=1)
    window_close_reason: WindowCloseReason
    current_state: MissionState | None = None
    last_summary: Summary | None = None
    mission_settings: dict[str, str] = Field(default_factory=dict)
    event_type_catalog: list[EventTypeDefinition] = Field(default_factory=list)
    relevant_history: list[Event] = Field(default_factory=list)
    token_budget: TokenBudgetInfo | None = None

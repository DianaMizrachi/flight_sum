"""The contract with the model itself (רכיב 4, משימה 1).

``LLMResult`` is the contract between component 4 and component 5, and it
carries two fields the model must never invent: ``llm_model`` and
``raw_response`` are harness metadata, and ``window_id`` is already known
from the bundle. Handing the model the full ``LLMResult`` schema would
invite it to fill those in - and under constrained decoding it would be
*forced* to, producing a confident guess at its own name.

So the model is constrained to this narrower schema instead, and the
engine wraps the result into ``LLMResult`` with the fields it owns.
"""

from pydantic import BaseModel, ConfigDict, Field

from aura.schemas import LLMEvent


class ModelEventsResponse(BaseModel):
    """מה שהמודל מחזיר: רשימת אירועים בלבד, גם ריקה."""

    model_config = ConfigDict(extra="forbid")

    events: list[LLMEvent] = Field(default_factory=list)


def response_json_schema() -> dict:
    """הסכמה שמועברת ל-``format`` של ollama כדי לכפות פלט תקין."""
    return ModelEventsResponse.model_json_schema()

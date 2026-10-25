"""Pydantic contracts between every component, per 'חוזים לפני קוד' (stage 0).

Each module here maps to one named contract in flight-transcript-work-plan.md:
Message, ContextBundle, LLMResult, ValidatedResult, MissionState, Event,
StateChange, Summary - plus the shared enums and the EventTypeDefinition
used by the taxonomy.
"""

from aura.schemas.context import ContextBundle, TokenBudgetInfo
from aura.schemas.entity import AttributeSnapshot, Entity, EntityAttribute, EntityRef
from aura.schemas.enums import (
    CertaintyLevel,
    EntityStatus,
    EntityType,
    InfoRelation,
    MessageStatus,
    ReconciliationAction,
    SummaryTrigger,
    ValidationStatus,
    VersionTag,
    WindowCloseReason,
)
from aura.schemas.event import Event
from aura.schemas.llm_result import LLMEvent, LLMResult
from aura.schemas.message import Message
from aura.schemas.mission_state import MissionState
from aura.schemas.state_change import StateChange, StateChangeKind
from aura.schemas.summary import Summary
from aura.schemas.taxonomy import EventTypeDefinition
from aura.schemas.validated_result import RejectedEvent, ValidatedResult

__all__ = [
    "AttributeSnapshot",
    "CertaintyLevel",
    "ContextBundle",
    "Entity",
    "EntityAttribute",
    "EntityRef",
    "EntityStatus",
    "EntityType",
    "Event",
    "EventTypeDefinition",
    "InfoRelation",
    "LLMEvent",
    "LLMResult",
    "Message",
    "MessageStatus",
    "MissionState",
    "ReconciliationAction",
    "RejectedEvent",
    "StateChange",
    "StateChangeKind",
    "Summary",
    "SummaryTrigger",
    "TokenBudgetInfo",
    "ValidatedResult",
    "ValidationStatus",
    "VersionTag",
    "WindowCloseReason",
]

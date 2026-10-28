"""רכיב 4 - מנוע LLM לזיהוי אירועים (שלב 4).

prompt מהטקסונומיה, שכבת לקוח מופשטת (ollama / מדומה / מקליט), פענוח
מוגבל-סכמה לפי ``LLMResult``, וניקוד מול תרחישים מתויגים.
"""

from aura.llm_engine.client import (
    LLMClient,
    LLMCompletion,
    LLMError,
    LLMProviderError,
    LLMTimeoutError,
    MockLLMClient,
    OllamaClient,
    RecordingLLMClient,
)
from aura.llm_engine.engine import EventDetectionEngine, LLMResponseFormatError
from aura.llm_engine.prompt import (
    PromptProfile,
    build_system_prompt,
    build_user_prompt,
    format_event_catalog,
)
from aura.llm_engine.response import ModelEventsResponse, response_json_schema
from aura.llm_engine.scoring import (
    AggregateScore,
    EventOutcome,
    ExpectedEvent,
    ScenarioScore,
    TrapMessage,
    TrapOutcome,
    expected_from_golden_flight,
    expected_from_model_eval,
    score_scenario,
)

__all__ = [
    "AggregateScore",
    "EventDetectionEngine",
    "EventOutcome",
    "ExpectedEvent",
    "LLMClient",
    "LLMCompletion",
    "LLMError",
    "LLMProviderError",
    "LLMResponseFormatError",
    "LLMTimeoutError",
    "MockLLMClient",
    "ModelEventsResponse",
    "OllamaClient",
    "PromptProfile",
    "RecordingLLMClient",
    "ScenarioScore",
    "TrapMessage",
    "TrapOutcome",
    "build_system_prompt",
    "build_user_prompt",
    "expected_from_golden_flight",
    "expected_from_model_eval",
    "format_event_catalog",
    "response_json_schema",
    "score_scenario",
]

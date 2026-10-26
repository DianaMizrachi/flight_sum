"""רכיב 3 - בניית הקשר (שלב 3).

קיבוץ משפטים לחלונות לפי כללי הסגירה, שליפת ההיסטוריה הרלוונטית לישויות
שבחלון, והרכבת ``ContextBundle`` בתוך תקציב טוקנים קבוע.
"""

from aura.context_builder.builder import ContextBuilder
from aura.context_builder.consumer import QueueWindowPump
from aura.context_builder.history import (
    EventHistoryProvider,
    InMemoryEventHistory,
    mentioned_entities,
    select_relevant_events,
)
from aura.context_builder.tokens import HeuristicTokenCounter, TokenCounter
from aura.context_builder.windowing import SentenceWindow, WindowAssembler, assemble_windows

__all__ = [
    "ContextBuilder",
    "EventHistoryProvider",
    "HeuristicTokenCounter",
    "InMemoryEventHistory",
    "QueueWindowPump",
    "SentenceWindow",
    "TokenCounter",
    "WindowAssembler",
    "assemble_windows",
    "mentioned_entities",
    "select_relevant_events",
]

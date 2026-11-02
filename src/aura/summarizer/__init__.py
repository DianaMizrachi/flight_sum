"""רכיב 8 - מנגנון ומאגר התקצירים (שלב 8).

תקציר מוגבל בגודל שנכתב רק בטריגרים, נשמר בגרסאות וניתן לטעינה מחדש
אחרי אתחול - ולצדו בחירת נקודות הציון של הטיסה (highlights).
"""

from aura.summarizer.composer import (
    DeterministicComposer,
    LLMSummaryComposer,
    SummaryComposer,
)
from aura.summarizer.policy import IMMEDIATE_TRIGGERS, PendingWork, SummaryPolicy
from aura.summarizer.service import (
    ALWAYS_HIGHLIGHT,
    FlightSummarizer,
    Highlight,
    highlights_for,
)
from aura.summarizer.store import SQLiteSummaryStore

__all__ = [
    "ALWAYS_HIGHLIGHT",
    "IMMEDIATE_TRIGGERS",
    "DeterministicComposer",
    "FlightSummarizer",
    "Highlight",
    "LLMSummaryComposer",
    "PendingWork",
    "SQLiteSummaryStore",
    "SummaryComposer",
    "SummaryPolicy",
    "highlights_for",
]

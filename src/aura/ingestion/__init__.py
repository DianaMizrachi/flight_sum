"""רכיב 1 - קליטת משפטי תמלול (שלב 1).

נקודת הכניסה היחידה של המערכת לנתונים חיים: נרמול, זיהוי כפילויות,
dead-letter להודעות פגומות, והעברה לתור הפנימי.
"""

from aura.ingestion.dedup import (
    DedupIndex,
    DedupStrategy,
    InMemoryDedupIndex,
    dedup_key,
    payload_hash,
)
from aura.ingestion.models import DeadLetterReason, DeadLetterRecord, IngestResult
from aura.ingestion.normalize import normalize_identifier, normalize_text, normalize_timestamp
from aura.ingestion.queue import InMemoryQueue, MessageQueue
from aura.ingestion.service import IngestionService
from aura.ingestion.text_format import (
    ParsedTranscript,
    find_flight_id_directive,
    parse_transcript_text,
)

__all__ = [
    "DeadLetterReason",
    "DeadLetterRecord",
    "DedupIndex",
    "DedupStrategy",
    "InMemoryDedupIndex",
    "InMemoryQueue",
    "IngestResult",
    "IngestionService",
    "MessageQueue",
    "ParsedTranscript",
    "dedup_key",
    "find_flight_id_directive",
    "normalize_identifier",
    "normalize_text",
    "normalize_timestamp",
    "parse_transcript_text",
    "payload_hash",
]

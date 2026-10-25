"""Wiring for the API layer.

One process-wide raw store and one :class:`IngestionService` on top of it.
The dedup index is the store-backed one, so duplicate detection survives a
restart mid-flight; the queue is still in-memory until stage 3 gives it a
consumer.

``AURA_RAW_STORE_PATH`` picks the SQLite file (default ``var/aura-raw.db``);
set it to ``:memory:`` for a throwaway run.
"""

import os
from functools import lru_cache

from aura.ingestion import IngestionService
from aura.raw_store import RawStoreDedupIndex, SQLiteRawTranscriptStore

DEFAULT_RAW_STORE_PATH = "var/aura-raw.db"


@lru_cache(maxsize=1)
def get_raw_store() -> SQLiteRawTranscriptStore:
    return SQLiteRawTranscriptStore(os.environ.get("AURA_RAW_STORE_PATH", DEFAULT_RAW_STORE_PATH))


@lru_cache(maxsize=1)
def get_ingestion_service() -> IngestionService:
    store = get_raw_store()
    return IngestionService(dedup_index=RawStoreDedupIndex(store), store=store)


def reset_ingestion_service() -> None:
    """איפוס השירות והמאגר - לבדיקות ולהרצה מקומית בין טיסות."""
    get_ingestion_service.cache_clear()
    get_raw_store.cache_clear()

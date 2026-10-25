"""A persistent dedup index backed by the raw store (closes the stage 1 gap).

Stage 1 shipped ``InMemoryDedupIndex``, which forgets everything on restart.
This adapter satisfies the same ``DedupIndex`` Protocol on top of the raw
store's append-only ``dedup_keys`` table, so a service restarted mid-flight
still recognises a message it has already ingested.
"""

from aura.raw_store.sqlite_store import SQLiteRawTranscriptStore


class RawStoreDedupIndex:
    """אינדקס כפילויות שנשמר במאגר הגולמי, ושורד אתחול באמצע טיסה."""

    def __init__(self, store: SQLiteRawTranscriptStore) -> None:
        self._store = store

    def lookup(self, key: str) -> str | None:
        return self._store.lookup_dedup_key(key)

    def remember(self, key: str, message_id: str) -> None:
        self._store.remember_dedup_key(key, message_id)

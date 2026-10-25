"""Duplicate detection for ingestion (שלב 1, משימה 3).

Two strategies, in the order the plan prescribes: by the source's own unique
id when it sends one, and otherwise by a hash of source + time + text. The
index itself is behind a Protocol so stage 2 can replace the in-memory
implementation with a unique index in the raw store without touching the
ingestion service.
"""

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class DedupStrategy(StrEnum):
    """איזה מפתח שימש לזיהוי הכפילות (מקביל ל-``dedup_strategy`` בפיקסצ'רים)."""

    BY_ID = "by_id"
    BY_HASH = "by_hash"


def payload_hash(flight_id: str, source: str, timestamp: datetime, text: str) -> str:
    """hash של טיסה+מקור+זמן+טקסט, לזיהוי כפילות כשאין מזהה מהמקור.

    הטקסט נכנס אחרי נרמול, כך שהפרש ברווחים או בתווי כיווניות לא ייצור
    מזהה חדש. המפריד ``\\x1f`` מונע התנגשות בין שדות שתוכנם מתחלף.
    """
    joined = "\x1f".join([flight_id, source, timestamp.isoformat(), text])
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def dedup_key(strategy: DedupStrategy, flight_id: str, value: str) -> str:
    """מפתח הכפילות, משויך לטיסה כדי שמזהים לא יתנגשו בין טיסות."""
    return f"{strategy.value}:{flight_id}:{value}"


class DedupIndex(Protocol):
    """אינדקס הכפילויות: מפתח -> מזהה ההודעה שנקלטה ראשונה."""

    def lookup(self, key: str) -> str | None: ...

    def remember(self, key: str, message_id: str) -> None: ...


class InMemoryDedupIndex:
    """מימוש בזיכרון, לשלב 1 ולבדיקות.

    משלב 2 האינדקס נשען על מאגר התמלול הגולמי, כדי שהכפילות תזוהה גם אחרי
    אתחול השירות באמצע טיסה.
    """

    def __init__(self) -> None:
        self._keys: dict[str, str] = {}

    def lookup(self, key: str) -> str | None:
        return self._keys.get(key)

    def remember(self, key: str, message_id: str) -> None:
        self._keys.setdefault(key, message_id)

    def __len__(self) -> int:
        return len(self._keys)

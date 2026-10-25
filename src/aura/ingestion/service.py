"""The ingest entry point (רכיב 1 - קליטת משפטים, שלב 1).

One sentence in, one :class:`IngestResult` out. The order of checks is the
order of the stage-1 task list: required fields, normalization, duplicate
detection, then handing the message to the internal queue. Nothing is ever
swallowed - a payload that can't become a ``Message`` is written to the
dead-letter log with a reason and the raw payload attached.
"""

from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

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
from aura.raw_store.store import DuplicateMessageError, RawTranscriptStore
from aura.schemas import Message, MessageStatus

# שדות שהקליטה יודעת למפות ל-Message. כל שדה אחר בהודעה הגולמית נרשם
# בשמו ב-metadata, כדי שתוספת לא מתועדת מהמקור תהיה גלויה ולא תיבלע.
_KNOWN_FIELDS = frozenset(
    {
        "id",
        "flight_id",
        "timestamp",
        "ingested_at",
        "source",
        "channel",
        "text",
        "language",
        "metadata",
    }
)


class IngestionService:
    """נקודת הקליטה: מקבלת הודעה גולמית, מנרמלת, מזהה כפילויות ומעבירה לתור.

    ``store`` הוא מאגר התמלול הגולמי (רכיב 2). כשהוא מחובר, ההודעה נכתבת
    אליו *לפני* שהיא מועברת לתור - לפי העיקרון "שמירה לפני עיבוד".

    ``dead_letters`` הוא לוג בזיכרון; הודעה פגומה אינה נכתבת למאגר הגולמי,
    כי היא אינה ``Message`` תקין, אך המקור הגולמי שלה נשמר ברשומה.
    """

    def __init__(
        self,
        queue: MessageQueue | None = None,
        dedup_index: DedupIndex | None = None,
        clock: Callable[[], datetime] | None = None,
        store: RawTranscriptStore | None = None,
    ) -> None:
        self.queue: MessageQueue = queue if queue is not None else InMemoryQueue()
        self.dedup_index: DedupIndex = (
            dedup_index if dedup_index is not None else InMemoryDedupIndex()
        )
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self.store = store
        self.dead_letters: list[DeadLetterRecord] = []

    def ingest(self, raw: Mapping[str, Any]) -> IngestResult:
        payload = dict(raw)

        # 1. שדות חובה. טקסט ריק אינו "שדה חסר" אלא כלל עסקי נפרד (ראו 3).
        for field in ("flight_id", "source"):
            value = payload.get(field)
            if value is None or not normalize_identifier(value):
                return self._dead_letter(payload, missing_field=field)
        for field in ("timestamp", "text"):
            if payload.get(field) is None:
                return self._dead_letter(payload, missing_field=field)

        flight_id = normalize_identifier(payload["flight_id"])
        source = normalize_identifier(payload["source"])

        # 2. נרמול: זמן ל-UTC, טקסט לקידוד ורווחים אחידים.
        try:
            timestamp = normalize_timestamp(payload["timestamp"])
            ingested_raw = payload.get("ingested_at")
            ingested_at = (
                normalize_timestamp(ingested_raw) if ingested_raw is not None else self._clock()
            )
        except (TypeError, ValueError) as exc:
            return self._dead_letter(
                payload, reason=DeadLetterReason.INVALID_TIMESTAMP, detail=str(exc)
            )

        raw_text = str(payload["text"])
        text = normalize_text(raw_text)

        # 3. משפט ריק או משובש (רק רווחים/תווי בקרה) - אין מה לנתח.
        if not text:
            return self._dead_letter(payload, reason=DeadLetterReason.EMPTY_TEXT)

        # 4. מזהה וכפילות: לפי המזהה מהמקור, ובהיעדרו לפי hash. המזהה
        # הנגזר מה-hash דטרמיניסטי, כך שעותק שני מקבל אותו מזהה.
        dedup_hash = payload_hash(flight_id, source, timestamp, text)
        source_id = payload.get("id")
        source_id = normalize_identifier(source_id) if source_id is not None else ""
        if source_id:
            strategy = DedupStrategy.BY_ID
            message_id = source_id
            key = dedup_key(strategy, flight_id, message_id)
        else:
            strategy = DedupStrategy.BY_HASH
            message_id = f"{flight_id}-h-{dedup_hash[:16]}"
            key = dedup_key(strategy, flight_id, dedup_hash)

        try:
            message = self._build_message(
                payload,
                message_id=message_id,
                flight_id=flight_id,
                source=source,
                timestamp=timestamp,
                ingested_at=ingested_at,
                text=text,
                raw_text=raw_text,
                dedup_hash=dedup_hash,
            )
        except ValidationError as exc:
            return self._dead_letter(
                payload,
                reason=DeadLetterReason.SCHEMA_INVALID,
                detail=str(exc),
                message_id=message_id,
            )

        existing_id = self.dedup_index.lookup(key)
        if existing_id is not None:
            # ההודעה נשמרת פעם אחת בלבד; העותק מסומן ולא מועבר לתור.
            return IngestResult(
                status=MessageStatus.DUPLICATE,
                message=message.model_copy(update={"status": MessageStatus.DUPLICATE}),
                dedup_strategy=strategy,
                duplicate_of=existing_id,
            )

        # 5. שמירה לפני עיבוד: קודם למאגר הגולמי (append-only), ורק אחר כך
        # לתור. אם המאגר תופס כפילות שהאינדקס פספס - ההודעה לא נכנסת שוב.
        if self.store is not None:
            try:
                self.store.append(message)
            except DuplicateMessageError:
                return IngestResult(
                    status=MessageStatus.DUPLICATE,
                    message=message.model_copy(update={"status": MessageStatus.DUPLICATE}),
                    dedup_strategy=strategy,
                    duplicate_of=message.id,
                )

        # 6. העברה לתור הפנימי לעיבוד אסינכרוני.
        self.dedup_index.remember(key, message.id)
        self.queue.publish(message)
        return IngestResult(
            status=MessageStatus.ACCEPTED,
            message=message,
            dedup_strategy=strategy,
            queued=True,
        )

    def ingest_many(self, raws: Iterable[Mapping[str, Any]]) -> list[IngestResult]:
        """קליטת רצף הודעות בסדר שבו הגיעו - לא בסדר חותמות הזמן שלהן."""
        return [self.ingest(raw) for raw in raws]

    def _build_message(
        self,
        payload: Mapping[str, Any],
        *,
        message_id: str,
        flight_id: str,
        source: str,
        timestamp: datetime,
        ingested_at: datetime,
        text: str,
        raw_text: str,
        dedup_hash: str,
    ) -> Message:
        metadata = {str(k): str(v) for k, v in (payload.get("metadata") or {}).items()}
        if text != raw_text:
            # הטקסט המקורי נשמר כפי שהגיע, כדי שמאגר התמלול הגולמי (שלב 2)
            # יוכל להחזיר אותו בייט-בייט גם אחרי הנרמול.
            metadata.setdefault("raw_text", raw_text)
        unmapped = sorted(set(payload) - _KNOWN_FIELDS)
        if unmapped:
            metadata.setdefault("unmapped_fields", ",".join(unmapped))

        channel = payload.get("channel")
        fields: dict[str, Any] = {
            "id": message_id,
            "flight_id": flight_id,
            "timestamp": timestamp,
            "ingested_at": ingested_at,
            "source": source,
            "channel": normalize_identifier(channel) or None if channel is not None else None,
            "text": text,
            "dedup_hash": dedup_hash,
            "status": MessageStatus.ACCEPTED,
            "metadata": metadata,
        }
        language = payload.get("language")
        if language is not None:
            fields["language"] = normalize_identifier(language)
        return Message.model_validate(fields)

    def _dead_letter(
        self,
        payload: Mapping[str, Any],
        *,
        reason: DeadLetterReason = DeadLetterReason.MISSING_REQUIRED_FIELD,
        missing_field: str | None = None,
        detail: str | None = None,
        message_id: str | None = None,
    ) -> IngestResult:
        raw_id = payload.get("id")
        raw_flight_id = payload.get("flight_id")
        record = DeadLetterRecord(
            reason=reason,
            detail=detail,
            missing_field=missing_field,
            flight_id=normalize_identifier(raw_flight_id) or None if raw_flight_id else None,
            message_id=message_id or (normalize_identifier(raw_id) or None if raw_id else None),
            raw_payload=dict(payload),
            recorded_at=self._clock(),
        )
        self.dead_letters.append(record)
        return IngestResult(status=MessageStatus.DEAD_LETTER, dead_letter=record)

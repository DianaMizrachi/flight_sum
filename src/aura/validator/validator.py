"""Output validation (רכיב 5, שלב 5).

``LLMResult`` in, ``ValidatedResult`` out: the events that passed every
rule, already built into real ``Event`` objects, and the ones that did
not, each with its reason and the raw payload kept for manual review.

The stage's pass criterion is one sentence - *no malformed output reaches
the state update* - and it drives the whole shape of this module. The
validator never repairs an event, never fills in a missing field and
never picks between two contradicting claims. An event either satisfies
the contract or it is handed back; anything else would mean component 6
receives something the model did not actually say.

Two things the validator does own, because they cannot be left to the
model: the event's identity and its provenance fields. ``id``,
``flight_id``, ``window_id``, ``version`` and ``created_at`` are assigned
here from the bundle and the clock. Asking the model for them would be
asking it to guess facts the harness already knows for certain.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from pydantic import ValidationError

from aura.schemas import (
    ContextBundle,
    Event,
    EventTypeDefinition,
    LLMEvent,
    LLMResult,
    RejectedEvent,
    ValidatedResult,
    ValidationStatus,
    VersionTag,
)
from aura.taxonomy import load_event_taxonomy
from aura.validator.reasons import RejectionReason
from aura.validator.rules import (
    PER_EVENT_RULES,
    Rejection,
    ValidationContext,
    context_from_bundle,
    find_contradictions,
)

EventIdFactory = Callable[[str, int], str]


def default_event_id(window_id: str, ordinal: int) -> str:
    """``<window_id>-evt-<n>`` - דטרמיניסטי, ייחודי וניתן לקריאה.

    מזהה החלון כבר ייחודי בתוך הטיסה, ולכן אין צורך ב-UUID כדי להבטיח
    ייחודיות; מזהה שאפשר לקרוא שווה הרבה כשמסתכלים על אירוע בלוג ורוצים
    לדעת מאיזה חלון הוא בא. מי שצריך אחרת מזריק ``id_factory``.
    """
    return f"{window_id}-evt-{ordinal}"


class OutputValidator:
    """רכיב 5: בודק את פלט המודל ומעביר הלאה רק את מה שעומד בחוזה."""

    def __init__(
        self,
        event_types: Iterable[EventTypeDefinition] | None = None,
        clock: Callable[[], datetime] | None = None,
        id_factory: EventIdFactory | None = None,
    ) -> None:
        self.event_types = list(event_types) if event_types is not None else load_event_taxonomy()
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._id_factory = id_factory if id_factory is not None else default_event_id

    # --- the main entry point -------------------------------------------

    def validate(
        self,
        result: LLMResult,
        bundle: ContextBundle,
        *,
        known_entity_ids: Iterable[str] | None = None,
        version: VersionTag = VersionTag.REALTIME,
        retry_count: int = 0,
    ) -> ValidatedResult:
        """מאמת פלט מודל אחד מול החלון שממנו הוא נגזר."""
        if result.window_id != bundle.window_id:
            # לא כלל של אירוע בודד אלא של הפלט כולו: אם הפלט אינו של
            # החלון הזה, אין משמעות לבדוק את מזהי המקור שלו מולו. נדחה
            # כל אירוע שבו, ואם לא היו אירועים - הפלט עצמו, כדי שתמיד
            # תישאר רשומה אחת לפחות להסתכל עליה.
            detail = f"result is for window {result.window_id!r}, not {bundle.window_id!r}"
            raws = [event.model_dump(mode="json") for event in result.events] or [
                {"window_id": result.window_id}
            ]
            return ValidatedResult(
                window_id=bundle.window_id,
                rejected=[
                    RejectedEvent(
                        raw=raw,
                        reason=RejectionReason.SCHEMA_INVALID,
                        detail=detail,
                        retry_count=retry_count,
                    )
                    for raw in raws
                ],
                status=ValidationStatus.MANUAL_REVIEW,
            )

        ctx = context_from_bundle(bundle, self.event_types, known_entity_ids=known_entity_ids)
        rejections = self._collect_rejections(result.events, ctx)

        accepted: list[Event] = []
        rejected: list[RejectedEvent] = []
        created_at = self._clock()
        for index, event in enumerate(result.events):
            rejection = rejections.get(index)
            if rejection is not None:
                rejected.append(
                    RejectedEvent(
                        raw=event.model_dump(mode="json"),
                        reason=rejection.reason,
                        detail=rejection.detail,
                        retry_count=retry_count,
                    )
                )
                continue
            # המספור רץ על המתקבלים בלבד, כדי שמזהי האירועים יהיו רצופים
            # ולא יחשפו כמה אירועים נדחו בדרך.
            accepted.append(
                self._build_event(
                    event,
                    ctx,
                    ordinal=len(accepted) + 1,
                    version=version,
                    created_at=created_at,
                )
            )

        return ValidatedResult(
            window_id=bundle.window_id,
            accepted=accepted,
            rejected=rejected,
            status=ValidationStatus.MANUAL_REVIEW if rejected else ValidationStatus.OK,
        )

    def validate_payload(
        self,
        payload: str | bytes | dict,
        bundle: ContextBundle,
        *,
        llm_model: str = "unknown",
        **kwargs,
    ) -> ValidatedResult:
        """אותו אימות, אבל מפלט גולמי שעדיין לא עבר פענוח.

        המנוע (רכיב 4) כבר מחזיר ``LLMResult`` מאומת-סכמה, ולכן המסלול
        הזה אינו הדרך הרגילה. הוא קיים כי שלב 5 נמדד גם מול JSON שבור
        ומול פלט שחסר בו שדה - ודחייה עם סיבה עדיפה על חריגה שמטפסת
        מעלה ומפילה את העיבוד של החלון כולו.
        """
        try:
            data = self._as_mapping(payload)
        except ValueError as exc:
            return self._whole_output_rejected(
                bundle, RejectionReason.NOT_JSON, str(exc), payload, **kwargs
            )

        data.setdefault("window_id", bundle.window_id)
        data.setdefault("llm_model", llm_model)
        try:
            result = LLMResult.model_validate(data)
        except ValidationError as exc:
            return self._whole_output_rejected(
                bundle,
                RejectionReason.SCHEMA_INVALID,
                f"{exc.error_count()} schema error(s): {exc.errors()[0].get('msg', '')}",
                payload,
                **kwargs,
            )
        return self.validate(result, bundle, **kwargs)

    # --- internals -------------------------------------------------------

    def _collect_rejections(
        self, events: list[LLMEvent], ctx: ValidationContext
    ) -> dict[int, Rejection]:
        """הדחייה הראשונה לכל אירוע, לפי סדר הכללים.

        אירוע נדחה פעם אחת ובסיבה אחת: רשימת כל מה שפגום בו נראית
        יסודית, אבל היא מקשה על מי שקורא אותה ועל הודעת התיקון שנשלחת
        חזרה למודל. הסדר ב-``PER_EVENT_RULES`` הוא סדר המשימות בשלב 5,
        ולכן הסיבה שמדווחת היא הבסיסית ביותר שנמצאה.
        """
        rejections: dict[int, Rejection] = {}
        for index, event in enumerate(events):
            for rule in PER_EVENT_RULES:
                rejection = rule(event, ctx)
                if rejection is not None:
                    rejections[index] = rejection
                    break

        # סתירה היא תכונה של זוג, ולכן היא נבדקת רק אחרי שכל אירוע נבדק
        # לבדו - ורק בין אירועים ששרדו, כדי לא לדווח על סתירה עם אירוע
        # שכבר נדחה מסיבה אחרת.
        survivors = [(i, e) for i, e in enumerate(events) if i not in rejections]
        contradictions = find_contradictions([e for _, e in survivors], ctx)
        for position, rejection in contradictions.items():
            rejections[survivors[position][0]] = rejection
        return rejections

    def _build_event(
        self,
        event: LLMEvent,
        ctx: ValidationContext,
        *,
        ordinal: int,
        version: VersionTag,
        created_at: datetime,
    ) -> Event:
        return Event(
            id=self._id_factory(ctx.window_id, ordinal),
            flight_id=ctx.flight_id,
            window_id=ctx.window_id,
            type=event.type,
            description=event.description,
            entities=list(event.entities),
            new_info=event.new_info,
            relation_to_previous=event.relation_to_previous,
            confidence=event.confidence,
            source_ids=list(event.source_ids),
            version=version,
            created_at=created_at,
        )

    @staticmethod
    def _as_mapping(payload: str | bytes | dict) -> dict:
        if isinstance(payload, dict):
            return dict(payload)
        text = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"JSON is a {type(data).__name__}, not an object")
        return data

    def _whole_output_rejected(
        self,
        bundle: ContextBundle,
        reason: RejectionReason,
        detail: str,
        payload: str | bytes | dict,
        *,
        retry_count: int = 0,
        **_ignored,
    ) -> ValidatedResult:
        """פלט שלא ניתן לפענח בכלל - נדחה כולו, עם מה שהגיע שמור ברשומה."""
        if isinstance(payload, dict):
            raw = dict(payload)
        else:
            text = (
                payload.decode("utf-8", errors="replace")
                if isinstance(payload, bytes)
                else payload
            )
            raw = {"raw_response": text}
        return ValidatedResult(
            window_id=bundle.window_id,
            accepted=[],
            rejected=[
                RejectedEvent(raw=raw, reason=reason, detail=detail, retry_count=retry_count)
            ],
            status=ValidationStatus.MANUAL_REVIEW,
        )

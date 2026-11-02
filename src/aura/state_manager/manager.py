"""The mission state manager (רכיב 6, שלב 6).

Validated events in, updated mission state out - plus an append-only
``StateChange`` for every single thing that moved, which is what lets
stage 7 rebuild the state at any point in time by replaying them.

The plan calls stage 6 the project's main checkpoint, and the reason is
that this is the first component whose output is an *opinion*. Everything
before it moves information around without judging it: ingestion
normalises, the store keeps, the context builder selects, the engine
proposes, the validator refuses. Here the system finally says "this is
what is true about the mission right now" - and every rule below exists
to make sure it can say how it knows.

Four decisions shape the whole module:

``relation_to_previous`` drives the update, not the event type. The model
already told us whether this is new information, a replacement, a
contradiction or a closure; re-deriving that from the type would be a
second, worse answer to a question already answered.

Nothing is ever overwritten. A replaced value moves into ``history``
with the event that put it there, so "how did we come to believe this"
is always answerable. That is also why ``contradicted`` is a flag on the
attribute rather than a deletion.

Confidence decides what a fact *is*, and separately whether it may
displace what is already there. A guess does not get to overwrite
something verified just by arriving later.

Time comes from the sentence, not from the clock. A late message carries
an old timestamp, and an old timestamp must not displace a newer belief -
the message is new to us, but what it describes is not.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from aura.config import DEFAULT_SETTINGS, Settings
from aura.schemas import (
    AttributeSnapshot,
    CertaintyLevel,
    Entity,
    EntityAttribute,
    EntityStatus,
    Event,
    EventTypeDefinition,
    InfoRelation,
    MissionState,
    StateChange,
    StateChangeKind,
    SummaryTrigger,
    VersionTag,
)
from aura.state_manager.entities import EntityResolver
from aura.taxonomy import load_event_taxonomy

# ברירת המחדל כשהטקסונומיה אינה מצהירה על מאפיין לסוג האירוע.
FALLBACK_ATTRIBUTE = "status"

_RELATION_TO_KIND = {
    InfoRelation.NEW: StateChangeKind.ADDED,
    InfoRelation.UPDATES: StateChangeKind.REPLACED,
    InfoRelation.CONTRADICTS: StateChangeKind.CONTRADICTED,
    InfoRelation.CLOSES: StateChangeKind.CLOSED,
}


@dataclass
class StateUpdate:
    """תוצאת עיבוד אצווה אחת של אירועים (רכיב 6, פלט).

    ``skipped`` הם אירועים שנרשמו אך לא נגעו במצב - ביטחון נמוך מדי, או
    מידע ישן יותר ממה שכבר ידוע. הם אינם שגיאה ואינם נעלמים: האירוע
    עצמו תקף, והוא פשוט לא שינה את מה שהמערכת מאמינה בו.
    """

    state: MissionState
    changes: list[StateChange] = field(default_factory=list)
    applied_event_ids: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    summary_trigger: SummaryTrigger | None = None

    @property
    def changed(self) -> bool:
        return bool(self.changes)


class MissionStateManager:
    """מנהל מצב המשימה: אירועים מאומתים -> מצב מעודכן + רשומות שינוי."""

    def __init__(
        self,
        settings: Settings | None = None,
        event_types: Iterable[EventTypeDefinition] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings if settings is not None else DEFAULT_SETTINGS
        definitions = list(event_types) if event_types is not None else load_event_taxonomy()
        self.event_types = {definition.id: definition for definition in definitions}
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._change_seq = 0

    # --- public API -------------------------------------------------------

    def empty_state(
        self, flight_id: str, version: VersionTag = VersionTag.REALTIME
    ) -> MissionState:
        return MissionState(
            flight_id=flight_id, version=version, as_of=self._clock(), entities={}
        )

    def apply(
        self,
        events: Sequence[Event],
        state: MissionState | None = None,
        *,
        message_times: dict[str, datetime] | None = None,
    ) -> StateUpdate:
        """מעבד אירועים לפי סדר משפטי המקור שלהם.

        ``message_times`` ממפה מזהה משפט לזמן הדיבור שלו. בלעדיו הסדר
        נקבע לפי ``created_at`` של האירוע - שהוא זמן העיבוד, לא זמן
        האמירה, ולכן הודעה שאחרה תיראה כאילו נאמרה עכשיו. זה בדיוק
        המקרה שהתכנית מסמנת כסיכון, ולכן הזמנים מועברים במפורש.
        """
        if not events:
            return StateUpdate(state=state or self.empty_state("unknown"))

        flight_id = events[0].flight_id
        state = state if state is not None else self.empty_state(flight_id, events[0].version)
        update = StateUpdate(state=state)
        resolver = EntityResolver(state)

        for event in self._in_spoken_order(events, message_times):
            self._apply_one(event, state, resolver, update, message_times)

        if update.changes:
            state.as_of = max(change.applied_at for change in update.changes)
        update.summary_trigger = self._summary_trigger(events, update)
        return update

    # --- ordering ---------------------------------------------------------

    def _in_spoken_order(
        self, events: Sequence[Event], message_times: dict[str, datetime] | None
    ) -> list[Event]:
        """סדר האמירה, לא סדר ההגעה.

        שני אירועים מאותו חלון - "הקשר אבד" ו"הקשר חודש" - חייבים
        להיקרא בסדר שבו נאמרו, אחרת המצב הסופי הוא ההפוך מהאמת.
        """
        return sorted(events, key=lambda e: (self._event_time(e, message_times), e.id))

    def _event_time(self, event: Event, message_times: dict[str, datetime] | None) -> datetime:
        if message_times:
            times = [message_times[sid] for sid in event.source_ids if sid in message_times]
            if times:
                return min(times)
        return event.created_at

    # --- one event --------------------------------------------------------

    def _apply_one(
        self,
        event: Event,
        state: MissionState,
        resolver: EntityResolver,
        update: StateUpdate,
        message_times: dict[str, datetime] | None,
    ) -> None:
        thresholds = self.settings.state
        if event.confidence < thresholds.min_confidence_to_apply:
            update.skipped.append((event.id, "confidence_below_threshold"))
            return
        if not event.entities:
            # אירוע בלי ישות אינו טענה על שום דבר שהמצב מחזיק. הוא תקף
            # ונשמר כאירוע (רכיב 7), אבל אין לו לאן ללכת כאן.
            update.skipped.append((event.id, "no_entity_to_update"))
            return

        applied_at = self._event_time(event, message_times)
        certainty = self._certainty_for(event.confidence)
        attribute_name = self._attribute_for(event)
        relation = event.relation_to_previous

        touched = False
        for ref in event.entities:
            entity, is_new = resolver.ensure(ref)
            if is_new:
                update.changes.append(
                    self._change(
                        event,
                        entity,
                        attribute=None,
                        kind=StateChangeKind.ADDED,
                        old_value=None,
                        new_value=entity.label,
                        certainty=certainty,
                        applied_at=applied_at,
                    )
                )
                touched = True

            if relation is InfoRelation.CLOSES:
                if self._close(entity, event, applied_at):
                    update.changes.append(
                        self._change(
                            event,
                            entity,
                            attribute=None,
                            kind=StateChangeKind.CLOSED,
                            old_value=EntityStatus.ACTIVE.value,
                            new_value=EntityStatus.CLOSED.value,
                            certainty=certainty,
                            applied_at=applied_at,
                        )
                    )
                    touched = True
                continue

            change = self._write_attribute(
                entity,
                attribute_name,
                event=event,
                relation=relation,
                certainty=certainty,
                applied_at=applied_at,
            )
            if change is not None:
                update.changes.append(change)
                touched = True

        if touched:
            update.applied_event_ids.append(event.id)
        else:
            update.skipped.append((event.id, "older_or_weaker_than_what_is_known"))

    # --- attributes -------------------------------------------------------

    def _write_attribute(
        self,
        entity: Entity,
        name: str,
        *,
        event: Event,
        relation: InfoRelation,
        certainty: CertaintyLevel,
        applied_at: datetime,
    ) -> StateChange | None:
        """כותב ערך מאפיין, ומחזיר את רשומת השינוי - או ``None`` אם ויתר.

        ``new_info`` הוא מה שהאירוע תורם, ולכן הוא הערך. האירוע אינו
        נושא שדה מאפיין מובנה, ולכן *שם* המאפיין מגיע מהטקסונומיה -
        וכך שני סוגים שמדברים על אותו היבט כותבים לאותו מקום.
        """
        snapshot = AttributeSnapshot(
            value=event.new_info or event.description,
            source_event_id=event.id,
            certainty=certainty,
            updated_at=applied_at,
        )
        existing = entity.attributes.get(name)

        if existing is None:
            entity.attributes[name] = EntityAttribute(current=snapshot)
            return self._change(
                event,
                entity,
                attribute=name,
                kind=StateChangeKind.ADDED,
                old_value=None,
                new_value=snapshot.value,
                certainty=certainty,
                applied_at=applied_at,
            )

        if not self._may_displace(existing.current, snapshot):
            # המידע נרשם בהיסטוריה אך אינו הופך לערך הנוכחי: הוא ישן
            # יותר, או חלש יותר ממידע מאומת. לאבד אותו לגמרי היה למחוק
            # דיווח אמיתי רק מפני שהגיע באיחור.
            existing.history.append(snapshot)
            if relation is InfoRelation.CONTRADICTS:
                existing.contradicted = True
            return None

        previous = existing.current
        existing.history.append(previous)
        existing.current = snapshot
        if relation is InfoRelation.CONTRADICTS:
            existing.contradicted = True

        return self._change(
            event,
            entity,
            attribute=name,
            kind=_RELATION_TO_KIND.get(relation, StateChangeKind.REPLACED),
            old_value=previous.value,
            new_value=snapshot.value,
            certainty=certainty,
            applied_at=applied_at,
        )

    def _may_displace(self, current: AttributeSnapshot, incoming: AttributeSnapshot) -> bool:
        """האם המידע החדש רשאי להחליף את הערך הנוכחי.

        שני מחסומים, ושניהם מהתכנית: מידע ישן יותר אינו דורס מידע חדש
        יותר (הודעה שאחרה), ומידע שאינו מאומת אינו דורס מידע מאומת
        (ביטחון נמוך). שווה-בזמן נחשב חדש יותר, כי אירוע מאוחר יותר
        באותו חלון כן אמור להחליף.
        """
        if incoming.updated_at < current.updated_at:
            return False
        if current.certainty is CertaintyLevel.VERIFIED:
            return incoming.certainty is CertaintyLevel.VERIFIED
        return True

    def _close(self, entity: Entity, event: Event, applied_at: datetime) -> bool:
        if entity.status is EntityStatus.CLOSED:
            return False
        entity.status = EntityStatus.CLOSED
        entity.closed_at = applied_at
        entity.closed_by_event_id = event.id
        return True

    # --- helpers ----------------------------------------------------------

    def _attribute_for(self, event: Event) -> str:
        definition = self.event_types.get(event.type)
        if definition is None or not definition.state_attribute:
            return FALLBACK_ATTRIBUTE
        return definition.state_attribute

    def _certainty_for(self, confidence: float) -> CertaintyLevel:
        thresholds = self.settings.state
        if confidence >= thresholds.verified_confidence:
            return CertaintyLevel.VERIFIED
        if confidence >= thresholds.reported_confidence:
            return CertaintyLevel.REPORTED
        return CertaintyLevel.UNCERTAIN

    def _change(
        self,
        event: Event,
        entity: Entity,
        *,
        attribute: str | None,
        kind: StateChangeKind,
        old_value: str | None,
        new_value: str | None,
        certainty: CertaintyLevel,
        applied_at: datetime,
    ) -> StateChange:
        self._change_seq += 1
        return StateChange(
            id=f"{event.id}-chg-{self._change_seq}",
            flight_id=event.flight_id,
            event_id=event.id,
            entity_id=entity.id,
            entity_type=entity.type,
            attribute=attribute,
            kind=kind,
            old_value=old_value,
            new_value=new_value,
            certainty=certainty,
            version=event.version,
            applied_at=applied_at,
        )

    def _summary_trigger(
        self, events: Sequence[Event], update: StateUpdate
    ) -> SummaryTrigger | None:
        """ההחלטה אם להפעיל תקציר (רכיב 8 יקרא לה בשלב 8).

        מוחזרת ולא מופעלת: מנהל המצב יודע *מתי* הקשר השתנה מהותית, אבל
        מי שמחליט מה לעשות עם זה הוא מי שמחזיק את מנגנון התקצירים.
        """
        if not update.changes:
            return None
        if any(event.type == "mission_change" for event in events):
            return SummaryTrigger.MISSION_CHANGE
        if any(change.kind is StateChangeKind.CLOSED for change in update.changes):
            return SummaryTrigger.MAJOR_STATE_CHANGE
        if any(change.kind is StateChangeKind.CONTRADICTED for change in update.changes):
            return SummaryTrigger.MAJOR_STATE_CHANGE
        return SummaryTrigger.SIGNIFICANT_EVENT

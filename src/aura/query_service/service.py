"""The query service (רכיב 9, שלב 9).

This is the read side of the system, and it is the first component that
exists purely to answer questions rather than to move the flight forward.
Everything it reads is already in the stores, so it runs with no model,
no network and no ingest - which is why the stage can be tested against a
database loaded from fixtures alone.

Three decisions shape it.

**It reads only stores, never components.** The state it returns comes
from component 7's tables, not from a live :class:`MissionStateManager`.
That is what makes "the state at 06:20" answerable at all: a live manager
holds one state, the present one, and cannot be asked about a moment that
has passed. It also means a query never competes with ingest for the
manager, and a restarted service answers exactly as the one before it.

**Time means spoken time.** Every timestamp a caller passes in or gets
back is when a sentence was said, not when the system got to it. The
stores are split on this point - ``StateChange.applied_at`` is already
spoken time, ``Event.created_at`` is not - so the service resolves event
times from the raw store and filters on those. Without that, a time range
over a replayed flight matches all events or none, because they were all
built within the same second.

**Default to the final version.** The plan asks for this directly, and
the reason is the risk it removes: an investigation run against the
real-time draft reaches conclusions from information that was later
corrected. Until stage 11 writes a final version there is only the
real-time one, so today the default resolves to it - but callers that say
nothing get the better answer automatically the moment a better answer
exists, instead of needing to be changed then.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime

from aura.config import DEFAULT_SETTINGS, Settings
from aura.event_store.store import SQLiteEventStore
from aura.query_service.filters import EventFilter
from aura.query_service.models import (
    AttributeValue,
    AttributeView,
    EntityMention,
    EntityView,
    EventPair,
    SourceSentence,
    StateView,
    SummaryView,
    TimelineEntry,
    VersionDiff,
)
from aura.raw_store.store import RawTranscriptStore
from aura.schemas import (
    Entity,
    Event,
    MissionState,
    ReconciliationAction,
    Summary,
    VersionTag,
)
from aura.state_manager.manager import certainty_for
from aura.summarizer.store import SQLiteSummaryStore


class QueryError(RuntimeError):
    """בסיס לשגיאות שירות השאילתות."""


class SummariesUnavailableError(QueryError):
    """שאילתת תקציר כשמאגר התקצירים אינו מחובר.

    נפרד מ"אין תקצירים לטיסה": הראשון הוא תצורה חסרה והשני הוא תשובה
    תקפה. אם שניהם היו מוחזרים כ-``None``, קורא שראה טיסה בלי תקציר לא
    היה יכול לדעת אם הטיסה שקטה או שהרכיב כלל אינו רץ.
    """


class QueryService:
    """שכבת הקריאה מעל המאגרים: מצב, ציר זמן, תקצירים ומקורות.

    המאגרים מוזרקים ואינם נבנים כאן, מאותה סיבה שבצינור: השירות אינו
    מחליט היכן הנתונים יושבים.
    """

    def __init__(
        self,
        events: SQLiteEventStore,
        raw: RawTranscriptStore,
        summaries: SQLiteSummaryStore | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.events = events
        self.raw = raw
        self.summaries = summaries
        self.settings = settings if settings is not None else DEFAULT_SETTINGS

    # --- flights and versions --------------------------------------------

    def flights(self) -> list[str]:
        """כל הטיסות שיש עליהן אירועים."""
        return self.events.flights()

    def versions_of(self, flight_id: str) -> list[VersionTag]:
        return self.events.versions_of(flight_id)

    def resolve_version(
        self, flight_id: str, requested: VersionTag | None = None
    ) -> VersionTag:
        """איזו גרסה תיענה: המבוקשת, או הסופית אם קיימת, אחרת זמן אמת.

        גרסה שנתבקשה במפורש מוחזרת גם אם אין בה אירועים: "הגרסה הסופית
        של הטיסה הזו ריקה" היא תשובה, והחלפתה בשקט בגרסת זמן האמת
        הייתה מציגה טיוטה כתוצאה סופית.
        """
        if requested is not None:
            return requested
        if VersionTag.FINAL in self.events.versions_of(flight_id):
            return VersionTag.FINAL
        return VersionTag.REALTIME

    # --- query 1: the state now ------------------------------------------

    def current_state(
        self, flight_id: str, version: VersionTag | None = None
    ) -> StateView:
        """מצב המשימה העדכני.

        קודם תמונת המצב השמורה, שהיא מטמון; אם אין - שחזור מרצף
        השינויים. שלב 7 מוכיח שהשתיים זהות, ולכן זו אופטימיזציה ולא
        שתי תשובות אפשריות.
        """
        resolved = self.resolve_version(flight_id, version)
        state = self.events.load_state(flight_id, resolved)
        if state is not None:
            return self._state_view(state, rebuilt=False)
        return self._state_view(
            self.events.rebuild_state(flight_id, resolved), rebuilt=True
        )

    # --- query 2: the state at a past moment -----------------------------

    def state_at(
        self, flight_id: str, at: datetime, version: VersionTag | None = None
    ) -> StateView:
        """מצב המשימה כפי שהיה בנקודת זמן בעבר.

        ``at`` הוא זמן אמירה, ורצף שינויי המצב כבר מתוייג בזמן אמירה
        (``applied_at``), ולכן השחזור כאן הוא השוואה ישרה.
        """
        resolved = self.resolve_version(flight_id, version)
        state = self.events.rebuild_state(flight_id, resolved, until=at)
        return self._state_view(state, rebuilt=True, as_of=at)

    # --- query 3: the event timeline -------------------------------------

    def timeline(
        self,
        flight_id: str | None = None,
        filter: EventFilter | None = None,  # noqa: A002 - "filter" is the domain word
        version: VersionTag | None = None,
    ) -> list[TimelineEntry]:
        """ציר הזמן של האירועים, מסונן, בסדר זמן האמירה.

        ``flight_id=None`` פורש על כל הטיסות, ואז הגרסה נבחרת *לכל טיסה
        בנפרד*: מעבר התיקון רץ בסוף כל טיסה בנפרד, ולכן באותו רגע יכולה
        להיות טיסה אחת עם גרסה סופית ואחרת בלי - וגרסה אחת לכולן הייתה
        מחזירה טיוטה לטיסה שכבר תוקנה.
        """
        criteria = filter if filter is not None else EventFilter()
        targets = self._target_flights(flight_id, criteria)

        entries: list[TimelineEntry] = []
        for flight in targets:
            resolved = self.resolve_version(flight, version)
            times = self.raw.timestamps_for_flight(flight)
            # הסינון לפי ישות נפתר פעם אחת לכל טיסה, לא לכל אירוע: מזהה
            # ישות הופך לתווית דרך מצב המשימה, וההשוואה עצמה נשארת
            # בידי המסנן.
            effective = self._with_resolved_entity(criteria, flight, resolved)
            for event in self.events.events_for_flight(flight, resolved):
                spoken = self._spoken_at(event, times)
                if not effective.matches(
                    flight_id=flight,
                    event_type=event.type,
                    relation=event.relation_to_previous.value,
                    certainty=certainty_for(event.confidence, self.settings),
                    entity_labels=[ref.label for ref in event.entities]
                    + [ref.id for ref in event.entities if ref.id],
                    spoken_at=spoken,
                ):
                    continue
                entries.append(self._entry(event, spoken))

        entries.sort(key=lambda e: (e.at, e.event_id))
        if criteria.limit is not None:
            return entries[: criteria.limit]
        return entries

    def event(
        self, event_id: str, flight_id: str, version: VersionTag | None = None
    ) -> TimelineEntry | None:
        """אירוע בודד."""
        resolved = self.resolve_version(flight_id, version)
        found = self.events.get_event(event_id, flight_id, resolved)
        if found is None:
            return None
        times = self.raw.timestamps_for_flight(flight_id)
        return self._entry(found, self._spoken_at(found, times))

    # --- query 4: from an event back to its sentences --------------------

    def sources_of(
        self, event_id: str, flight_id: str, version: VersionTag | None = None
    ) -> list[SourceSentence]:
        """מאירוע -> משפטי התמלול שממנו נגזר, בסדר האמירה.

        זו השאילתה שהתכנית מציבה כחובה ("חייבת להצליח תמיד"), והיא
        הבסיס ל"לחיצה על אירוע פותחת את התמלול" בשלב 10. מזהה מקור
        שאינו נמצא במאגר הגולמי מוחזר מסומן ולא מושמט.
        """
        resolved = self.resolve_version(flight_id, version)
        return self._sentences(
            self.events.source_ids_of(event_id, flight_id, resolved), flight_id
        )

    def events_from_sentence(
        self, message_id: str, flight_id: str, version: VersionTag | None = None
    ) -> list[TimelineEntry]:
        """הכיוון ההפוך: אילו אירועים נגזרו מהמשפט הזה."""
        resolved = self.resolve_version(flight_id, version)
        times = self.raw.timestamps_for_flight(flight_id)
        return [
            self._entry(event, self._spoken_at(event, times))
            for event in self.events.events_from_message(message_id, flight_id, resolved)
        ]

    def sentences_of_flight(
        self, flight_id: str, start: datetime | None = None, end: datetime | None = None
    ) -> list[SourceSentence]:
        """התמלול עצמו, לטווח זמן. מה שהקורא רואה כשהוא פותח אירוע בהקשר."""
        messages = self.raw.by_time_range(flight_id, start, end)
        return [
            SourceSentence(
                message_id=message.id,
                flight_id=message.flight_id,
                spoken_at=message.timestamp,
                source=message.source,
                channel=message.channel,
                text=message.text,
            )
            for message in messages
        ]

    # --- query 5: the summary, now or up to a moment ---------------------

    def summary(
        self,
        flight_id: str,
        version: VersionTag | None = None,
        at: datetime | None = None,
    ) -> SummaryView | None:
        """התקציר של הטיסה - האחרון, או זה שכיסה עד נקודת זמן.

        ``at`` בוחר את התקציר העדכני ביותר שאינו *מאוחר* מאותו רגע, כדי
        שתחקור של נקודת זמן יראה את ההקשר שהיה זמין אז ולא את מה שנודע
        אחר כך. תקציר שאינו מצהיר עד היכן הוא מכסה אינו נבחר בשאילתה
        כזו: אי אפשר למקם אותו בזמן אמירה, והנחה לטובתו הייתה מציגה
        ידע מאוחר כאילו היה קיים.
        """
        store = self._summary_store()
        resolved = self.resolve_version(flight_id, version)
        times = self.raw.timestamps_for_flight(flight_id)
        if at is None:
            latest = store.latest(flight_id, resolved)
            return self._summary_view(latest, times) if latest else None

        best: Summary | None = None
        for summary in store.history(flight_id, resolved):
            covered = times.get(summary.covers_up_to_message_id or "")
            if covered is None or covered > at:
                continue
            if best is None or summary.version_number > best.version_number:
                best = summary
        return self._summary_view(best, times) if best else None

    def summary_history(
        self, flight_id: str, version: VersionTag | None = None
    ) -> list[SummaryView]:
        """כל גרסאות התקציר - "מה האמנו באותו רגע" לאורך הטיסה."""
        store = self._summary_store()
        resolved = self.resolve_version(flight_id, version)
        # זמני האמירה נטענים פעם אחת ולא פעם לכל תקציר: טיסה ארוכה
        # מצטברת עשרות גרסאות, וטעינה חוזרת לכל אחת מהן הופכת שאילתה
        # זולה לסריקה חוזרת של כל הטיסה.
        times = self.raw.timestamps_for_flight(flight_id)
        return [
            self._summary_view(summary, times)
            for summary in store.history(flight_id, resolved)
        ]

    # --- the version difference ------------------------------------------

    def version_diff(self, flight_id: str) -> VersionDiff:
        """מה מעבר התיקון שינה: אישר, תיקן, הוסיף או ביטל.

        מונחה על ידי הקישור שהגרסה הסופית נושאת בעצמה
        (``reconciles_event_id`` ו-``reconciliation_action``) ולא על ידי
        השוואת טקסטים: המעבר יודע מה הוא עשה לכל אירוע, וניחוש מחדש
        מתוך הפלט היה יכול לסתור אותו.
        """
        realtime = self.events.events_for_flight(flight_id, VersionTag.REALTIME)
        final = self.events.events_for_flight(flight_id, VersionTag.FINAL)
        if not final:
            return VersionDiff(
                flight_id=flight_id,
                final_exists=False,
                realtime_count=len(realtime),
            )

        times = self.raw.timestamps_for_flight(flight_id)
        by_id = {event.id: event for event in realtime}
        buckets: dict[str, list[EventPair]] = {
            ReconciliationAction.CONFIRMED.value: [],
            ReconciliationAction.CORRECTED.value: [],
            ReconciliationAction.ADDED.value: [],
            ReconciliationAction.CANCELLED.value: [],
        }
        claimed: set[str] = set()

        for event in final:
            origin = by_id.get(event.reconciles_event_id or "")
            if origin is not None:
                claimed.add(origin.id)
            action = (
                event.reconciliation_action.value
                if event.reconciliation_action is not None
                # הגרסה הסופית נושאת אירוע בלי סיווג: אם הוא מקושר
                # לאירוע זמן אמת זה תיקון, ואם לא - תוספת.
                else (
                    ReconciliationAction.CORRECTED.value
                    if origin is not None
                    else ReconciliationAction.ADDED.value
                )
            )
            buckets.setdefault(action, []).append(
                EventPair(
                    action=action,
                    realtime=self._entry(origin, self._spoken_at(origin, times))
                    if origin
                    else None,
                    final=self._entry(event, self._spoken_at(event, times)),
                    reason=event.reconciliation_reason,
                )
            )

        unmatched = tuple(
            EventPair(
                action="unmatched",
                realtime=self._entry(event, self._spoken_at(event, times)),
            )
            for event in realtime
            if event.id not in claimed
        )

        return VersionDiff(
            flight_id=flight_id,
            final_exists=True,
            realtime_count=len(realtime),
            final_count=len(final),
            confirmed=tuple(buckets[ReconciliationAction.CONFIRMED.value]),
            corrected=tuple(buckets[ReconciliationAction.CORRECTED.value]),
            added=tuple(buckets[ReconciliationAction.ADDED.value]),
            cancelled=tuple(buckets[ReconciliationAction.CANCELLED.value]),
            unmatched=unmatched,
        )

    # --- building the read models ----------------------------------------

    def _summary_store(self) -> SQLiteSummaryStore:
        if self.summaries is None:
            raise SummariesUnavailableError(
                "no summary store is connected to this query service"
            )
        return self.summaries

    def _target_flights(
        self, flight_id: str | None, criteria: EventFilter
    ) -> list[str]:
        if flight_id is not None:
            return [flight_id]
        if criteria.flights:
            return sorted(criteria.flights)
        return self.events.flights()

    @staticmethod
    def _spoken_at(
        event: Event | None, times: dict[str, datetime]
    ) -> datetime | None:
        """זמן האמירה של האירוע: המשפט המוקדם מבין מקורותיו."""
        if event is None:
            return None
        found = [times[sid] for sid in event.source_ids if sid in times]
        return min(found) if found else None

    def _with_resolved_entity(
        self, criteria: EventFilter, flight_id: str, version: VersionTag
    ) -> EventFilter:
        """מחליף מזהה ישות בתווית שלה, כדי שסינון לפי ישות יעבוד בכלל.

        אירוע שמור נושא ``EntityRef`` שה-``id`` שלו לרוב ``None``: מנהל
        המצב מקצה את המזהה ואינו כותב אותו בחזרה לאירוע. סינון על
        המזהה לבדו היה מחזיר ריק דווקא לישויות שהטיסה חידשה - ולכן
        המזהה נפתר כאן לתווית דרך מצב המשימה. מה שאינו מזהה מוכר נשאר
        כפי שהוא, ואז הוא נחשב תווית מלכתחילה.
        """
        if criteria.entity is None:
            return criteria
        state = self.events.load_state(flight_id, version)
        if state is None:
            state = self.events.rebuild_state(flight_id, version)
        found = state.entities.get(criteria.entity)
        if found is None:
            return criteria
        return replace(criteria, entity=found.label)

    def _entry(self, event: Event, spoken_at: datetime | None) -> TimelineEntry:
        return TimelineEntry(
            event_id=event.id,
            flight_id=event.flight_id,
            type=event.type,
            description=event.description,
            new_info=event.new_info,
            relation=event.relation_to_previous.value,
            certainty=certainty_for(event.confidence, self.settings).value,
            source_ids=tuple(event.source_ids),
            version=event.version.value,
            recorded_at=event.created_at,
            spoken_at=spoken_at,
            entities=tuple(
                EntityMention(type=ref.type.value, label=ref.label, id=ref.id)
                for ref in event.entities
            ),
            related_event_id=event.related_event_id,
            reconciliation=(
                event.reconciliation_action.value
                if event.reconciliation_action is not None
                else None
            ),
            reconciliation_reason=event.reconciliation_reason,
            reconciles_event_id=event.reconciles_event_id,
        )

    def _sentences(
        self, message_ids: Sequence[str], flight_id: str
    ) -> list[SourceSentence]:
        found: list[SourceSentence] = []
        for message_id in message_ids:
            message = self.raw.get(message_id, flight_id=flight_id)
            if message is None:
                found.append(
                    SourceSentence(
                        message_id=message_id, flight_id=flight_id, missing=True
                    )
                )
                continue
            found.append(
                SourceSentence(
                    message_id=message.id,
                    flight_id=message.flight_id,
                    spoken_at=message.timestamp,
                    source=message.source,
                    channel=message.channel,
                    text=message.text,
                )
            )
        # סדר האמירה, ומה שאין לו זמן נשאר בסוף בסדר שבו נשמר. מיון
        # יחיד על מפתח מעורב היה משווה תאריך למספר, ולכן הפרדה.
        known = [sentence for sentence in found if sentence.spoken_at is not None]
        unknown = [sentence for sentence in found if sentence.spoken_at is None]
        known.sort(key=lambda sentence: sentence.spoken_at)  # type: ignore[arg-type,return-value]
        return known + unknown

    def _state_view(
        self, state: MissionState, *, rebuilt: bool, as_of: datetime | None = None
    ) -> StateView:
        return StateView(
            flight_id=state.flight_id,
            version=state.version.value,
            as_of=as_of if as_of is not None else state.as_of,
            entities=tuple(
                self._entity_view(entity)
                for entity in sorted(state.entities.values(), key=lambda e: e.id)
            ),
            rebuilt=rebuilt,
        )

    @staticmethod
    def _entity_view(entity: Entity) -> EntityView:
        return EntityView(
            id=entity.id,
            type=entity.type.value,
            label=entity.label,
            status=entity.status.value,
            attributes=tuple(
                AttributeView(
                    name=name,
                    current=AttributeValue(
                        value=attribute.current.value,
                        certainty=attribute.current.certainty.value,
                        updated_at=attribute.current.updated_at,
                        source_event_id=attribute.current.source_event_id,
                    ),
                    contradicted=attribute.contradicted,
                    history=tuple(
                        AttributeValue(
                            value=past.value,
                            certainty=past.certainty.value,
                            updated_at=past.updated_at,
                            source_event_id=past.source_event_id,
                        )
                        for past in attribute.history
                    ),
                )
                for name, attribute in sorted(entity.attributes.items())
            ),
            closed_at=entity.closed_at,
            closed_by_event_id=entity.closed_by_event_id,
        )

    @staticmethod
    def _summary_view(summary: Summary, times: dict[str, datetime]) -> SummaryView:
        return SummaryView(
            flight_id=summary.flight_id,
            version=summary.version.value,
            version_number=summary.version_number,
            text=summary.text,
            trigger=summary.trigger_reason.value,
            created_at=summary.created_at,
            covers_up_to_message_id=summary.covers_up_to_message_id,
            covers_up_to=times.get(summary.covers_up_to_message_id or ""),
            entities_covered=tuple(summary.entities_covered),
        )

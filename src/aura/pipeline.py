"""The full 1→6 pipeline, connected (שלב 6, משימה אחרונה).

Until now every component was reachable only on its own. Ingestion put
accepted sentences on the queue and nothing read them continuously; the
context builder could make bundles but nobody fed them to a model; the
engine could detect events that nothing validated; the validator produced
events that nothing applied. This module is the wire between them, and
the plan marks the moment it exists as the project's main checkpoint:
*sentence → event → updated state*, for the first time.

It is deliberately thin. Every decision still belongs to the component
that owns it - windows close by the context builder's rules, events are
refused by the validator's rules, state moves by the state manager's
rules - and nothing here second-guesses any of them. What this module
owns is only the order of the steps and what happens when one of them
throws.

That last part is the reason it is a class and not four lines in a
script. A model call can fail or time out, and a flight whose window
cannot be analysed must not take the ingest path down with it: the
sentences are already saved (component 2 runs before the queue), so the
correct behaviour is to record the failure against that window and keep
going. A pipeline that stopped at the first bad window would lose every
later sentence of every other flight.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from aura.context_builder.builder import ContextBuilder
from aura.context_builder.consumer import QueueWindowPump
from aura.event_store.store import SQLiteEventStore
from aura.ingestion.models import IngestResult
from aura.ingestion.service import IngestionService
from aura.llm_engine.client import LLMError
from aura.llm_engine.engine import EventDetectionEngine
from aura.schemas import (
    ContextBundle,
    Event,
    Message,
    MissionState,
    RejectedEvent,
    StateChange,
    Summary,
    SummaryTrigger,
)
from aura.state_manager.manager import MissionStateManager, StateUpdate
from aura.summarizer.service import FlightSummarizer, Highlight, highlights_for
from aura.validator.validator import OutputValidator


@dataclass
class WindowOutcome:
    """מה קרה לחלון אחד לאורך כל הצינור."""

    bundle: ContextBundle
    accepted: list[Event] = field(default_factory=list)
    rejected: list[RejectedEvent] = field(default_factory=list)
    changes: list[StateChange] = field(default_factory=list)
    summary: Summary | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)


@dataclass
class PipelineRun:
    """תוצאת מחזור עיבוד אחד: כל החלונות שנסגרו, ומה יצא מהם."""

    windows: list[WindowOutcome] = field(default_factory=list)
    states: dict[str, MissionState] = field(default_factory=dict)

    @property
    def events(self) -> list[Event]:
        return [event for window in self.windows for event in window.accepted]

    @property
    def changes(self) -> list[StateChange]:
        return [change for window in self.windows for change in window.changes]

    @property
    def rejected(self) -> list[RejectedEvent]:
        return [item for window in self.windows for item in window.rejected]

    @property
    def summaries(self) -> list[Summary]:
        return [w.summary for w in self.windows if w.summary is not None]

    @property
    def failures(self) -> list[WindowOutcome]:
        return [window for window in self.windows if not window.ok]


class FlightPipeline:
    """משפט נכנס בקצה אחד, מצב משימה מעודכן יוצא בשני.

    הרכיבים מוזרקים ולא נבנים כאן: הצינור אינו מחליט באיזה מודל להשתמש,
    באיזה מאגר או באילו ספים - הוא רק מריץ אותם בסדר הנכון.
    """

    def __init__(
        self,
        ingestion: IngestionService,
        engine: EventDetectionEngine,
        validator: OutputValidator | None = None,
        state_manager: MissionStateManager | None = None,
        context_builder: ContextBuilder | None = None,
        event_store: SQLiteEventStore | None = None,
        summarizer: FlightSummarizer | None = None,
    ) -> None:
        self.ingestion = ingestion
        self.engine = engine
        self.validator = validator if validator is not None else OutputValidator()
        self.state_manager = (
            state_manager if state_manager is not None else MissionStateManager()
        )
        self.pump = QueueWindowPump(
            ingestion.queue,
            builder=context_builder if context_builder is not None else ContextBuilder(),
        )
        # רכיב 7 אופציונלי: בלעדיו הצינור עובד במלואו אבל המצב חי
        # בזיכרון בלבד ונעלם באתחול. איתו - האירועים, שינויי המצב
        # ותמונת המצב נשמרים, ואפשר לשחזר כל נקודת זמן.
        self.event_store = event_store
        # רכיב 8 אופציונלי כמו רכיב 7: בלעדיו הצינור רץ במלואו, פשוט
        # בלי תקצירים.
        self.summarizer = summarizer
        self.states: dict[str, MissionState] = {}
        # זמני אמירה *לפי טיסה*. מזהה משפט ייחודי בתוך הטיסה ולא במאגר
        # כולו - זו הסיבה שהמפתח הראשי של המאגר הגולמי הוא (טיסה, מזהה)
        # - ולכן מילון שטוח אחד היה מאפשר ל-"m01" של טיסה אחת לדרוס את
        # זה של השנייה, ולסדר אירועים לפי זמן של טיסה אחרת.
        self._message_times: dict[str, dict[str, Any]] = {}

    # --- component 1 ------------------------------------------------------

    def ingest(self, raw: Mapping[str, Any]) -> IngestResult:
        """קליטת משפט בודד. זהה לחלוטין ל-``POST /messages``."""
        result = self.ingestion.ingest(raw)
        if result.message is not None:
            self._remember_time(result.message)
        return result

    def ingest_many(self, raws: Iterable[Mapping[str, Any]]) -> list[IngestResult]:
        return [self.ingest(raw) for raw in raws]

    def _remember_time(self, message: Message) -> None:
        """זמן האמירה של כל משפט, לשימוש מנהל המצב.

        בלעדיו הוא נופל לזמן העיבוד, והודעה שאחרה נראית כאילו נאמרה
        עכשיו - בדיוק מה שהיא לא.
        """
        self._message_times.setdefault(message.flight_id, {})[message.id] = message.timestamp

    # --- components 3→6 ---------------------------------------------------

    def drain(self) -> PipelineRun:
        """מעבד כל חלון שנסגר מההודעות שבתור."""
        return self._process(self.pump.drain())

    def close_flight(self, flight_id: str) -> PipelineRun:
        """סוגר את החלון הפתוח של טיסה ומעבד אותו - סוף טיסה.

        מרוקן קודם את התור. בלי זה, משפט שנקלט ועדיין ממתין בתור לא היה
        מגיע לאף assembler, וסגירת הטיסה הייתה "סוגרת" חלון שמעולם לא
        ראה אותו - כלומר מאבדת בשקט את סוף הטיסה. סגירה חייבת לחול על
        כל מה שנקלט, לא רק על מה שכבר נשאב.
        """
        pending = self.pump.drain()
        window = self.pump.close_flight(flight_id)
        if window is not None:
            pending.append(window)
        return self._process(pending)

    def state_of(self, flight_id: str) -> MissionState | None:
        return self.states.get(flight_id)

    def summary_of(self, flight_id: str) -> Summary | None:
        """התקציר האחרון של הטיסה, אם רכיב 8 מחובר."""
        return self.summarizer.latest(flight_id) if self.summarizer else None

    def highlights_of(self, flight_id: str) -> list[Highlight]:
        """נקודות הציון של הטיסה. דורש את מאגר האירועים (רכיב 7)."""
        if self.event_store is None:
            return []
        return highlights_for(
            self.event_store.events_for_flight(flight_id),
            self.states.get(flight_id),
            self.times_of(flight_id),
        )

    def times_of(self, flight_id: str) -> dict[str, Any]:
        """זמני האמירה של משפטי טיסה אחת."""
        return self._message_times.get(flight_id, {})

    def finish_flight(self, flight_id: str) -> Summary | None:
        """סוף טיסה: תקציר אחרון ללא תנאי, גם אם הסף לא נחצה.

        מסומן במשפט האחרון שנקלט בטיסה. בלי זה התקציר היחיד שמכסה את
        הטיסה כולה הוא גם היחיד שאינו מצהיר עד היכן הוא מגיע, ושאילתת
        "התקציר עד נקודת זמן" (שלב 9) אינה יכולה למקם אותו.
        """
        state = self.states.get(flight_id)
        if self.summarizer is None or state is None:
            return None
        times = self.times_of(flight_id)
        last = max(times, key=lambda mid: (times[mid], mid)) if times else None
        return self.summarizer.summarize(
            flight_id, state, SummaryTrigger.POST_FLIGHT, covers_up_to_message_id=last
        )

    def _process(self, windows: list) -> PipelineRun:
        run = PipelineRun()
        for window in windows:
            bundle = self.pump.builder.build(window)
            run.windows.append(self._process_bundle(bundle))
        run.states = dict(self.states)
        return run

    def _process_bundle(self, bundle: ContextBundle) -> WindowOutcome:
        outcome = WindowOutcome(bundle=bundle)

        # רכיב 4. כישלון כאן הוא כישלון של חלון אחד, לא של הצינור: המשפטים
        # כבר שמורים במאגר הגולמי, ואפשר לנתח אותם שוב בשלב 11.
        try:
            result = self.engine.detect(bundle)
        except LLMError as exc:
            outcome.error = f"{type(exc).__name__}: {exc}"
            return outcome

        # רכיב 5.
        state = self.states.get(bundle.flight_id)
        known_ids = list(state.entities) if state is not None else None
        validated = self.validator.validate(
            result, bundle, known_entity_ids=known_ids
        )
        outcome.accepted = list(validated.accepted)
        # הדחיות נישאות ולא נספרות בלבד: כל הטעם ב-"לבדיקה ידנית" הוא
        # שיישאר מה לבדוק, וצינור ששומר רק מונה מאבד בדיוק את זה.
        outcome.rejected = list(validated.rejected)
        if not validated.accepted:
            return outcome

        # רכיב 6.
        update = self._apply(bundle.flight_id, validated.accepted)
        outcome.changes = list(update.changes)

        # רכיב 7. נכתב אחרי שהמצב כבר עודכן, ובסדר הזה בכוונה: האירועים
        # והשינויים הם מה שקרה, ותמונת המצב היא התוצאה שלהם.
        if self.event_store is not None:
            self.event_store.append_events(validated.accepted)
            self.event_store.append_changes(update.changes)
            self.event_store.save_state(update.state)

        # רכיב 8. מנהל המצב *מציע* טריגר; המדיניות של המתקצר מחליטה אם
        # הוא מצדיק תקציר חדש עכשיו.
        if self.summarizer is not None:
            self.summarizer.record(
                validated.accepted, messages=len(bundle.messages)
            )
            outcome.summary = self.summarizer.maybe_summarize(
                bundle.flight_id,
                update.state,
                update.summary_trigger,
                covers_up_to_message_id=bundle.messages[-1].id if bundle.messages else None,
            )
        return outcome

    def _apply(self, flight_id: str, events: list[Event]) -> StateUpdate:
        state = self.states.get(flight_id)
        if state is None:
            state = self.state_manager.empty_state(flight_id)
            self.states[flight_id] = state
        update = self.state_manager.apply(
            events, state, message_times=self.times_of(flight_id)
        )
        self.states[flight_id] = update.state
        return update

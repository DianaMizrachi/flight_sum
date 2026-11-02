"""Summaries and flight highlights (רכיב 8, שלב 8).

The stage has three pass criteria and all three are mechanical, which is
why they can be tested rather than judged:

*the summary does not exceed its size*, *it contains 100% of the active
entities at every checkpoint*, and *restarting mid-flight resumes with
the same context*.

The first two can conflict - enough active targets and there is no way to
say everything briefly - so the tests pin down which one gives. Coverage
wins, and the reasoning is that the failures are not symmetrical: running
long costs tokens, while silently dropping an active target loses
something still out there, and nothing downstream can detect that it
happened.

The fourth property has no criterion but is the one the plan's own test
describes: *count the invocations over a whole flight*, and confirm it is
far fewer than one per sentence. A summary rewritten constantly is not a
summary.
"""

from datetime import UTC, datetime, timedelta

import pytest

from aura.config import Settings, SummaryConfig
from aura.schemas import (
    EntityStatus,
    Event,
    SummaryTrigger,
    VersionTag,
)
from aura.state_manager import MissionStateManager
from aura.summarizer import (
    DeterministicComposer,
    FlightSummarizer,
    LLMSummaryComposer,
    SQLiteSummaryStore,
    SummaryPolicy,
    highlights_for,
)

FLIGHT = "flight-s8"
START = datetime(2026, 9, 1, 6, 0, tzinfo=UTC)


def at(minutes: int) -> datetime:
    return START + timedelta(minutes=minutes)


def event(
    event_id: str,
    *,
    type: str = "contact_with_target",
    relation: str = "new",
    info: str = "קשר עין עם מטרה אלפא.",
    label: str = "מטרה אלפא",
    entity_type: str = "target",
    minutes: int = 0,
    source: str | None = None,
) -> Event:
    return Event(
        id=event_id,
        flight_id=FLIGHT,
        window_id="w01",
        type=type,
        description=info,
        entities=[{"id": None, "type": entity_type, "label": label}],
        new_info=info,
        relation_to_previous=relation,
        confidence=0.9,
        source_ids=[source or f"m-{event_id}"],
        version=VersionTag.REALTIME,
        created_at=at(minutes),
    )


def state_from(events, manager=None):
    manager = manager or MissionStateManager()
    times = {e.source_ids[0]: e.created_at for e in events}
    return manager.apply(events, message_times=times)


@pytest.fixture
def summarizer() -> FlightSummarizer:
    return FlightSummarizer(clock=lambda: at(30))


# --- criterion 1: the summary stays within its size ----------------------


def test_a_summary_fits_the_budget(summarizer):
    events = [event(f"e{i}", minutes=i, label=f"מטרה {i}") for i in range(5)]
    update = state_from(events)
    summarizer.record(events)

    summary = summarizer.summarize(FLIGHT, update.state, SummaryTrigger.POST_FLIGHT)

    assert len(summary.text) <= 1200


@pytest.mark.parametrize("entity_count", [1, 5, 20])
def test_the_budget_holds_while_coverage_allows_it(entity_count):
    """התקציב נשמר בכל מצב שבו הוא יכול להישמר."""
    settings = Settings(summary=SummaryConfig(max_chars=400))
    events = [
        event(f"e{i}", minutes=i, label=f"מטרה מספר {i}", source=f"m{i}")
        for i in range(entity_count)
    ]
    update = state_from(events)
    service = FlightSummarizer(
        composer=DeterministicComposer(settings), settings=settings, clock=lambda: at(90)
    )
    service.record(events)

    summary = service.summarize(FLIGHT, update.state, SummaryTrigger.POST_FLIGHT)

    assert len(summary.text) <= 400


def test_when_the_two_criteria_truly_conflict_the_overflow_is_declared():
    """הרגע שבו שני הקריטריונים אינם יכולים להתקיים יחד.

    60 ישויות פעילות ששמותיהן לבדם ארוכים מהתקציב: הכיסוי מנצח,
    התקציר חורג, והחריגה כתובה בו במפורש כדי שתהיה מדידה ולא שקטה.
    """
    settings = Settings(summary=SummaryConfig(max_chars=400))
    events = [
        event(f"e{i}", minutes=i, label=f"מטרה מספר {i}", source=f"m{i}") for i in range(60)
    ]
    update = state_from(events)
    service = FlightSummarizer(
        composer=DeterministicComposer(settings), settings=settings, clock=lambda: at(90)
    )
    service.record(events)

    text = service.summarize(FLIGHT, update.state, SummaryTrigger.POST_FLIGHT).text

    assert len(text) > 400, "the names cannot fit; the summary is expected to overflow"
    assert "חריגה מהתקציב" in text, "an overflow must say so, never be silent"
    for entity in update.state.entities.values():
        assert entity.label in text


def test_a_long_sentence_is_shortened_not_dropped(summarizer):
    long_info = "קשר עין עם מטרה אלפא, " + "פירוט ארוך מאוד " * 40
    update = state_from([event("e1", info=long_info)])
    summarizer.record([event("e1", info=long_info)])

    summary = summarizer.summarize(FLIGHT, update.state, SummaryTrigger.POST_FLIGHT)

    assert len(summary.text) <= 1200
    assert "מטרה אלפא" in summary.text


# --- criterion 2: 100% of active entities --------------------------------


def test_every_active_entity_is_named(summarizer):
    events = [
        event("e1", label="מטרה אלפא", minutes=0),
        event("e2", label="מטרה ברבו", minutes=2, source="m2"),
        event("e3", label="מטרה צ'רלי", minutes=4, source="m3"),
    ]
    update = state_from(events)
    summarizer.record(events)

    summary = summarizer.summarize(FLIGHT, update.state, SummaryTrigger.POST_FLIGHT)

    for entity in update.state.entities.values():
        assert entity.label in summary.text


@pytest.mark.parametrize("entity_count", [5, 20, 60])
def test_coverage_wins_over_brevity_when_they_conflict(entity_count):
    """ההכרעה המכוונת: שם של ישות פעילה לא נופל כדי לעמוד בתקציב.

    שתי התקלות אינן סימטריות - תקציר שחורג עולה טוקנים, ותקציר שמשמיט
    מטרה פעילה מאבד ידע שאיש אינו יודע שאבד.
    """
    settings = Settings(summary=SummaryConfig(max_chars=500))
    events = [
        event(f"e{i}", minutes=i, label=f"מטרה {i}", source=f"m{i}")
        for i in range(entity_count)
    ]
    update = state_from(events)
    service = FlightSummarizer(
        composer=DeterministicComposer(settings), settings=settings, clock=lambda: at(90)
    )
    service.record(events)

    summary = service.summarize(FLIGHT, update.state, SummaryTrigger.POST_FLIGHT)

    missing = [e.label for e in update.state.entities.values() if e.label not in summary.text]
    assert not missing, f"active entities dropped from the summary: {missing}"


def test_entities_covered_lists_the_active_ones(summarizer):
    events = [
        event("e1", label="מטרה אלפא"),
        event("e2", relation="closes", label="מטרה אלפא", minutes=5, source="m2"),
        event("e3", label="מטרה ברבו", minutes=7, source="m3"),
    ]
    update = state_from(events)
    summarizer.record(events)

    summary = summarizer.summarize(FLIGHT, update.state, SummaryTrigger.POST_FLIGHT)

    active = {
        e.id for e in update.state.entities.values() if e.status is EntityStatus.ACTIVE
    }
    assert set(summary.entities_covered) == active


def test_a_closed_entity_is_mentioned_but_not_as_active(summarizer):
    events = [
        event("e1", label="מטרה אלפא"),
        event("e2", relation="closes", label="מטרה אלפא", minutes=5, source="m2"),
    ]
    update = state_from(events)
    summarizer.record(events)

    summary = summarizer.summarize(FLIGHT, update.state, SummaryTrigger.POST_FLIGHT)

    assert "נסגר" in summary.text
    assert summary.entities_covered == []


# --- criterion 3: restart mid-flight -------------------------------------


def test_the_latest_summary_survives_a_restart(tmp_path):
    """אתחול באמצע טיסה והמשך עם אותו הקשר."""
    path = tmp_path / "summaries.db"
    events = [event("e1"), event("e2", label="מטרה ברבו", minutes=3, source="m2")]
    update = state_from(events)

    with SQLiteSummaryStore(path) as store:
        service = FlightSummarizer(store=store, clock=lambda: at(10))
        service.record(events)
        written = service.summarize(FLIGHT, update.state, SummaryTrigger.MISSION_CHANGE)

    with SQLiteSummaryStore(path) as reopened:
        resumed = FlightSummarizer(store=reopened).resume(FLIGHT)

    assert resumed is not None
    assert resumed.text == written.text
    assert resumed.version_number == written.version_number
    assert resumed.entities_covered == written.entities_covered


def test_resuming_does_not_immediately_trigger_another_summary(tmp_path):
    """מה שנצבר לפני האתחול כבר מיוצג בתקציר השמור."""
    path = tmp_path / "s.db"
    events = [event(f"e{i}", minutes=i, source=f"m{i}") for i in range(5)]
    update = state_from(events)

    with SQLiteSummaryStore(path) as store:
        first = FlightSummarizer(store=store, clock=lambda: at(10))
        first.record(events, messages=50)
        first.summarize(FLIGHT, update.state, SummaryTrigger.POST_FLIGHT)

    with SQLiteSummaryStore(path) as reopened:
        service = FlightSummarizer(store=reopened)
        service.resume(FLIGHT)
        assert service.maybe_summarize(FLIGHT, update.state, None) is None


def test_versions_count_up(summarizer):
    update = state_from([event("e1")])

    for expected in (1, 2, 3):
        summary = summarizer.summarize(FLIGHT, update.state, SummaryTrigger.MISSION_CHANGE)
        assert summary.version_number == expected
    assert len(summarizer.store.history(FLIGHT)) == 3


def test_older_summaries_are_kept(summarizer):
    """"מה האמנו אז" היא שאלה שנשאלת בתחקור."""
    update = state_from([event("e1")])
    summarizer.summarize(FLIGHT, update.state, SummaryTrigger.MISSION_CHANGE)
    summarizer.summarize(FLIGHT, update.state, SummaryTrigger.POST_FLIGHT)

    history = summarizer.store.history(FLIGHT)
    assert [s.trigger_reason for s in history] == [
        SummaryTrigger.MISSION_CHANGE,
        SummaryTrigger.POST_FLIGHT,
    ]


def test_realtime_and_final_summaries_are_separate_sequences(summarizer):
    update = state_from([event("e1")])

    summarizer.summarize(FLIGHT, update.state, SummaryTrigger.POST_FLIGHT)
    final = summarizer.summarize(
        FLIGHT, update.state, SummaryTrigger.POST_FLIGHT, version=VersionTag.FINAL
    )

    assert final.version_number == 1
    assert summarizer.latest(FLIGHT).version is VersionTag.REALTIME
    assert summarizer.latest(FLIGHT, VersionTag.FINAL).version is VersionTag.FINAL


# --- the policy: not after every sentence --------------------------------


def test_a_mission_change_summarizes_immediately():
    policy = SummaryPolicy()

    assert policy.decide(SummaryTrigger.MISSION_CHANGE) is SummaryTrigger.MISSION_CHANGE
    assert policy.decide(SummaryTrigger.MAJOR_STATE_CHANGE) is SummaryTrigger.MAJOR_STATE_CHANGE


def test_an_ordinary_event_waits_for_others():
    """בלי הסף הזה מתקבל תקציר אחרי כמעט כל חלון."""
    policy = SummaryPolicy()

    threshold = policy.settings.summary.min_events_between

    policy.record(events=threshold - 1)
    assert policy.decide(SummaryTrigger.SIGNIFICANT_EVENT) is None
    policy.record(events=1)
    assert policy.decide(SummaryTrigger.SIGNIFICANT_EVENT) is SummaryTrigger.SIGNIFICANT_EVENT


def test_a_long_quiet_stretch_still_summarizes():
    """הטריגר הרביעי: צבירה בלי אף אירוע מהותי."""
    policy = SummaryPolicy()

    policy.record(messages=40)

    assert policy.decide(None) is SummaryTrigger.THRESHOLD_EXCEEDED


def test_tokens_cross_the_threshold_too():
    policy = SummaryPolicy()

    policy.record(tokens=1500)

    assert policy.decide(None) is SummaryTrigger.THRESHOLD_EXCEEDED


def test_nothing_pending_means_no_summary():
    assert SummaryPolicy().decide(None) is None


def test_taking_a_summary_resets_the_count():
    policy = SummaryPolicy()
    policy.record(events=5, messages=50)

    policy.taken()

    assert policy.decide(None) is None


def test_a_whole_flight_summarizes_a_handful_of_times(summarizer):
    """המדידה שהתכנית מבקשת: ספירת הפעלות על טיסה מלאה."""
    events = [
        event(
            f"e{i}",
            minutes=i,
            source=f"m{i}",
            type="mission_change" if i in (12, 24) else "contact_with_target",
            entity_type="area" if i in (12, 24) else "target",
            label="הגזרה המזרחית" if i in (12, 24) else f"מטרה {i % 3}",
        )
        for i in range(30)
    ]
    manager = MissionStateManager()
    state = manager.empty_state(FLIGHT)
    runs = 0

    for single in events:
        update = manager.apply(
            [single], state, message_times={single.source_ids[0]: single.created_at}
        )
        state = update.state
        summarizer.record([single], messages=1)
        if summarizer.maybe_summarize(FLIGHT, state, update.summary_trigger):
            runs += 1

    assert runs <= 6, f"{runs} summaries for {len(events)} events is too many"
    assert runs >= 2, "a 30-event flight with two mission changes must summarise at least twice"


# --- the composer --------------------------------------------------------


def test_the_summary_leads_with_what_is_still_open(summarizer):
    events = [
        event("e1", label="מטרה אלפא"),
        event("e2", relation="closes", label="מטרה אלפא", minutes=4, source="m2"),
        event("e3", label="מטרה ברבו", minutes=6, source="m3"),
    ]
    update = state_from(events)
    summarizer.record(events)

    text = summarizer.summarize(FLIGHT, update.state, SummaryTrigger.POST_FLIGHT).text

    assert text.index("פעיל עכשיו") < text.index("נסגר")


def test_a_contradicted_attribute_is_marked(summarizer):
    events = [
        event("e1", info="המטרה בצומת."),
        event("e2", relation="contradicts", info="לא, היא לא שם.", minutes=2, source="m2"),
    ]
    update = state_from(events)
    summarizer.record(events)

    text = summarizer.summarize(FLIGHT, update.state, SummaryTrigger.POST_FLIGHT).text

    assert "⚠" in text


def test_an_empty_flight_still_produces_a_summary(summarizer):
    state = MissionStateManager().empty_state(FLIGHT)

    summary = summarizer.summarize(FLIGHT, state, SummaryTrigger.POST_FLIGHT)

    assert FLIGHT in summary.text
    assert summary.entities_covered == []


# --- the LLM composer falls back rather than break a criterion -----------


class FakeCompletion:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeClient:
    def __init__(self, text: str | Exception) -> None:
        self.text = text

    def complete(self, system, user, schema):
        if isinstance(self.text, Exception):
            raise self.text
        return FakeCompletion(self.text)


def test_the_model_written_summary_is_used_when_it_is_valid():
    update = state_from([event("e1", label="מטרה אלפא")])
    composer = LLMSummaryComposer(FakeClient("סיכום: מטרה אלפא במעקב."))

    text = composer.compose(
        flight_id=FLIGHT,
        state=update.state,
        new_events=[],
        previous=None,
        trigger=SummaryTrigger.POST_FLIGHT,
    )

    assert text == "סיכום: מטרה אלפא במעקב."
    assert composer.fallback_count == 0


@pytest.mark.parametrize(
    "answer",
    [
        "סיכום שאינו מזכיר אף ישות פעילה בשמה.",  # חסרה ישות פעילה
        "א" * 5000,  # חורג מהתקרה
        "",  # ריק
    ],
)
def test_a_model_answer_that_breaks_a_criterion_is_discarded(answer):
    """תקציר קריא פחות עדיף על תקציר שמשמיט מטרה פעילה בלי שאיש ידע."""
    update = state_from([event("e1", label="מטרה אלפא")])
    composer = LLMSummaryComposer(FakeClient(answer))

    text = composer.compose(
        flight_id=FLIGHT,
        state=update.state,
        new_events=[],
        previous=None,
        trigger=SummaryTrigger.POST_FLIGHT,
    )

    assert "מטרה אלפא" in text
    assert len(text) <= 1200
    assert composer.fallback_count == 1


def test_a_model_failure_does_not_break_summarising():
    update = state_from([event("e1")])
    composer = LLMSummaryComposer(FakeClient(RuntimeError("model down")))

    text = composer.compose(
        flight_id=FLIGHT,
        state=update.state,
        new_events=[],
        previous=None,
        trigger=SummaryTrigger.POST_FLIGHT,
    )

    assert "מטרה אלפא" in text


# --- highlights ----------------------------------------------------------


def test_a_mission_change_is_always_a_highlight():
    events = [
        event("e1", minutes=0),
        event(
            "e2",
            type="mission_change",
            label="הגזרה המזרחית",
            entity_type="area",
            info="עוברים מזרחה.",
            minutes=3,
            source="m2",
        ),
    ]

    picked = highlights_for(events)

    assert "e2" in {h.event.id for h in picked}


def test_closing_and_contradicting_are_highlights():
    events = [
        event("e1"),
        event("e2", relation="closes", minutes=2, source="m2"),
        event("e3", relation="contradicts", minutes=4, source="m3"),
    ]

    reasons = {h.event.id: h.reason for h in highlights_for(events)}

    assert reasons["e2"] == "סגירת ישות"
    assert reasons["e3"] == "סתירה למידע קודם"


def test_a_routine_update_on_a_closed_entity_is_not_a_highlight():
    """מה שאינו כותרת - אינו נמחק, הוא פשוט לא נבחר."""
    events = [
        event("e1", relation="new", minutes=0),
        event("e2", relation="updates", info="עדכון שוטף.", minutes=2, source="m2"),
        event("e3", relation="closes", minutes=4, source="m3"),
    ]
    update = state_from(events)

    picked = {h.event.id for h in highlights_for(events, update.state)}

    assert "e2" not in picked


def test_an_active_entity_always_contributes_its_latest_state():
    events = [
        event("e1", relation="new", minutes=0),
        event("e2", relation="updates", info="המטרה נעה מזרחה.", minutes=5, source="m2"),
    ]
    update = state_from(events)

    picked = {h.event.id: h.reason for h in highlights_for(events, update.state)}

    assert "e2" in picked
    assert picked["e2"] == "המצב הנוכחי של ישות פעילה"


def test_highlights_are_chronological_and_keep_their_sources():
    events = [
        event("e3", minutes=9, source="m9"),
        event("e1", minutes=0, source="m1"),
        event(
            "e2",
            type="malfunction",
            entity_type="force",
            label="נץ אחת",
            minutes=4,
            source="m4",
        ),
    ]

    picked = highlights_for(events)

    assert [h.at for h in picked] == sorted(h.at for h in picked)
    for highlight in picked:
        assert highlight.source_ids, "a highlight must stay traceable to its sentence"


def test_highlights_are_real_events_not_rewritten_text():
    """כותרת היא אירוע שנבחר, לא ניסוח חדש שלו."""
    events = [event("e1", info="קשר עין עם מטרה אלפא בצומת הצפוני.")]

    highlight = highlights_for(events)[0]

    assert highlight.text == "קשר עין עם מטרה אלפא בצומת הצפוני."
    assert highlight.event is events[0]


def test_no_events_means_no_highlights():
    assert highlights_for([]) == []


def test_a_highlight_is_timed_by_when_it_was_said(summarizer):
    """``created_at`` הוא זמן העיבוד, ולכן כותרות שהוצגו לפיו קיבלו
    כולן את אותה שעה - שעה שאינה אומרת דבר על הטיסה."""
    events = [
        event("e1", minutes=0, source="m1"),
        event("e2", type="malfunction", entity_type="force", label="נץ אחת",
              minutes=0, source="m2"),
    ]
    # שני האירועים נבנו באותו רגע עיבוד, אך נאמרו בהפרש של 20 דקות.
    spoken = {"m1": at(0), "m2": at(20)}

    picked = highlights_for(events, None, spoken)

    assert [h.at for h in picked] == [at(0), at(20)]


def test_without_message_times_a_highlight_falls_back_to_the_event(summarizer):
    events = [event("e1", minutes=7, source="m1")]

    assert highlights_for(events)[0].at == at(7)

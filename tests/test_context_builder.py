"""Stage 3 tests - windowing, relevant history and the token budget.

The plan's tests for this stage: a window closes correctly under each of
the four conditions; a late sentence is placed by its original timestamp;
a window that mentions a previously reported target receives that target's
details rather than the whole transcript; and a huge input stays inside the
budget. The pass criterion adds: the closing rules behave as expected over
the golden set, with zero budget overruns.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from aura.config import Settings, TokenBudgetConfig, WindowConfig
from aura.context_builder import (
    ContextBuilder,
    HeuristicTokenCounter,
    InMemoryEventHistory,
    QueueWindowPump,
    WindowAssembler,
    assemble_windows,
    select_relevant_events,
)
from aura.ingestion import InMemoryQueue
from aura.schemas import (
    AttributeSnapshot,
    CertaintyLevel,
    ContextBundle,
    Entity,
    EntityAttribute,
    EntityRef,
    EntityType,
    Event,
    InfoRelation,
    Message,
    MissionState,
    Summary,
    SummaryTrigger,
    VersionTag,
    WindowCloseReason,
)

GOLDEN_FLIGHTS_DIR = Path(__file__).parent / "fixtures" / "golden_flights"
EDGE_CASES_DIR = Path(__file__).parent / "fixtures" / "edge_cases"

BASE = datetime(2026, 9, 1, 6, 0, tzinfo=UTC)


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def golden_messages(path: Path) -> list[Message]:
    flight = load_yaml(path)
    return [
        Message.model_validate({k: v for k, v in message.items() if k != "expected"})
        for message in flight["messages"]
    ]


def msg(
    index: int,
    *,
    offset: int | None = None,
    source: str = "operator",
    channel: str | None = "command",
    text: str | None = None,
    flight_id: str = "f1",
) -> Message:
    seconds = index * 5 if offset is None else offset
    return Message(
        id=f"{flight_id}-m{index:02d}",
        flight_id=flight_id,
        timestamp=BASE + timedelta(seconds=seconds),
        ingested_at=BASE + timedelta(seconds=seconds),
        source=source,
        channel=channel,
        text=text or f"בקרה, נץ אחת, משפט מספר {index}.",
    )


def event(
    event_id: str,
    label: str,
    *,
    entity_id: str | None = None,
    minutes: int = 0,
    event_type: str = "contact_with_target",
    flight_id: str = "f1",
) -> Event:
    return Event(
        id=event_id,
        flight_id=flight_id,
        window_id=f"{flight_id}-w001",
        type=event_type,
        description=f"אירוע על {label}",
        entities=[EntityRef(id=entity_id, type=EntityType.TARGET, label=label)],
        new_info=f"מידע חדש על {label}",
        relation_to_previous=InfoRelation.NEW,
        confidence=0.9,
        source_ids=[f"{flight_id}-m01"],
        version=VersionTag.REALTIME,
        created_at=BASE + timedelta(minutes=minutes),
    )


def mission_state(label: str = "מטרה אלפא", entity_id: str = "ent-alpha") -> MissionState:
    snapshot = AttributeSnapshot(
        value="במעקב",
        source_event_id="evt-1",
        certainty=CertaintyLevel.REPORTED,
        updated_at=BASE,
    )
    return MissionState(
        flight_id="f1",
        version=VersionTag.REALTIME,
        as_of=BASE,
        entities={
            entity_id: Entity(
                id=entity_id,
                type=EntityType.TARGET,
                label=label,
                attributes={"status": EntityAttribute(current=snapshot)},
            )
        },
    )


# --- ארבעת תנאי הסגירה --------------------------------------------------


def test_silence_above_the_threshold_closes_the_window():
    config = WindowConfig(silence_threshold_seconds=20, close_on_speaker_change=False)
    windows = assemble_windows([msg(1, offset=0), msg(2, offset=21)], config)

    assert [w.close_reason for w in windows] == [
        WindowCloseReason.SILENCE_TIMEOUT,
        WindowCloseReason.FLIGHT_END,
    ]
    assert [m.id for m in windows[0].messages] == ["f1-m01"]


def test_silence_exactly_at_the_threshold_does_not_close():
    """התכנית אומרת "שתיקה מעל סף" - שוויון אינו חריגה."""
    config = WindowConfig(silence_threshold_seconds=20, close_on_speaker_change=False)
    windows = assemble_windows([msg(1, offset=0), msg(2, offset=20)], config)

    assert len(windows) == 1
    assert [m.id for m in windows[0].messages] == ["f1-m01", "f1-m02"]


def test_speaker_change_closes_the_window_when_enabled():
    config = WindowConfig(close_on_speaker_change=True)
    windows = assemble_windows([msg(1), msg(2, source="ops")], config)

    assert windows[0].close_reason == WindowCloseReason.SPEAKER_CHANGE


def test_speaker_change_is_ignored_when_disabled():
    config = WindowConfig(close_on_speaker_change=False)
    windows = assemble_windows([msg(1), msg(2, source="ops")], config)

    assert len(windows) == 1
    assert windows[0].close_reason == WindowCloseReason.FLIGHT_END


def test_channel_change_closes_the_window():
    config = WindowConfig(close_on_speaker_change=False)
    windows = assemble_windows([msg(1), msg(2, channel="intercom")], config)

    assert windows[0].close_reason == WindowCloseReason.CHANNEL_CHANGE


def test_max_sentences_closes_the_window_and_counts_the_overlap():
    config = WindowConfig(max_sentences=4, overlap_sentences=2, close_on_speaker_change=False)
    windows = assemble_windows([msg(i) for i in range(1, 8)], config)

    assert windows[0].close_reason == WindowCloseReason.MAX_SENTENCES
    assert len(windows[0].messages) == 4
    # החלון הבא מתחיל בשני משפטי החפיפה, ולכן מכיל שניים חדשים בלבד.
    assert len(windows[1].messages) == 4
    assert windows[1].overlap_count == 2
    assert len(windows[1].own_messages) == 2


def test_max_duration_closes_the_window():
    config = WindowConfig(
        max_duration_seconds=30, silence_threshold_seconds=60, close_on_speaker_change=False
    )
    windows = assemble_windows(
        [msg(1, offset=0), msg(2, offset=20), msg(3, offset=40)], config
    )

    assert windows[0].close_reason == WindowCloseReason.MAX_DURATION
    assert [m.id for m in windows[0].messages] == ["f1-m01", "f1-m02"]


def test_max_duration_is_measured_over_the_windows_own_sentences():
    """שתיקה ארוכה שנגררה בחפיפה מהחלון הקודם לא סוגרת מיד את החדש."""
    config = WindowConfig(
        max_duration_seconds=60,
        silence_threshold_seconds=20,
        overlap_sentences=2,
        close_on_speaker_change=False,
    )
    messages = [msg(1, offset=0), msg(2, offset=5), msg(3, offset=100), msg(4, offset=105)]

    windows = assemble_windows(messages, config)

    assert windows[0].close_reason == WindowCloseReason.SILENCE_TIMEOUT
    second = windows[1]
    assert [m.id for m in second.messages] == ["f1-m01", "f1-m02", "f1-m03", "f1-m04"]
    assert second.duration_seconds == 5.0, "the 95s carried-in gap is not the window's duration"


# --- חפיפה --------------------------------------------------------------


def test_overlap_carries_the_last_sentences_into_the_next_window():
    config = WindowConfig(overlap_sentences=2, silence_threshold_seconds=10)
    messages = [msg(1, offset=0), msg(2, offset=5), msg(3, offset=9), msg(4, offset=30)]

    windows = assemble_windows(messages, WindowConfig(**config.model_dump()))

    assert [m.id for m in windows[0].messages] == ["f1-m01", "f1-m02", "f1-m03"]
    assert [m.id for m in windows[1].messages] == ["f1-m02", "f1-m03", "f1-m04"]
    assert windows[1].overlap_count == 2
    assert [m.id for m in windows[1].own_messages] == ["f1-m04"]


def test_overlap_never_produces_a_window_without_new_content():
    config = WindowConfig(overlap_sentences=2, silence_threshold_seconds=1)
    # כל הודעה סוגרת חלון בגלל שתיקה; החלונות לא יכולים להיות חפיפה בלבד.
    windows = assemble_windows([msg(i, offset=i * 10) for i in range(1, 6)], config)

    assert all(window.own_messages for window in windows)


def test_window_ids_are_sequential_per_flight():
    windows = assemble_windows([msg(i, offset=i * 30) for i in range(1, 4)], WindowConfig())

    assert [w.window_id for w in windows] == ["f1-w001", "f1-w002", "f1-w003"]


def test_flush_returns_nothing_when_only_overlap_remains():
    assembler = WindowAssembler("f1", WindowConfig(max_sentences=2, overlap_sentences=1))

    assembler.push(msg(1))
    closed = assembler.push(msg(2))

    assert len(closed) == 1
    assert assembler.flush() is None, "an overlap-only tail is not a window"


def test_pushing_a_message_from_another_flight_is_refused():
    assembler = WindowAssembler("f1")

    with pytest.raises(ValueError, match="belongs to flight"):
        assembler.push(msg(1, flight_id="f2"))


def test_mixed_flights_are_refused_in_batch_mode():
    with pytest.raises(ValueError, match="several flights"):
        assemble_windows([msg(1), msg(2, flight_id="f2")])


# --- הודעות באיחור ------------------------------------------------------


def test_a_late_sentence_is_placed_by_its_original_timestamp_in_batch_mode():
    """הזרימה של סוף הטיסה (ושלב 11): מיון לפי זמן לפני החלוקה."""
    data = load_yaml(EDGE_CASES_DIR / "late_arrival.yaml")
    messages = [Message.model_validate(raw) for raw in data["raw_inputs"]]
    config = WindowConfig(silence_threshold_seconds=20, close_on_speaker_change=False)

    windows = assemble_windows(messages, config)

    ordered = [m.id for window in windows for m in window.own_messages]
    assert ordered == data["expected"]["chronological_order_by_timestamp"]


def test_a_late_sentence_is_reported_as_an_orphan_in_the_realtime_stream():
    config = WindowConfig(silence_threshold_seconds=10, close_on_speaker_change=False)
    assembler = WindowAssembler("f1", config)

    assembler.push(msg(1, offset=0))
    assembler.push(msg(2, offset=30))  # closes window 1
    late = msg(3, offset=5)  # belongs inside the closed window
    closed = assembler.push(late)

    assert closed == []
    assert [m.id for m in assembler.orphans] == ["f1-m03"]
    assert late not in assembler.open_messages


def test_a_late_sentence_joins_the_open_window_when_configured():
    config = WindowConfig(
        silence_threshold_seconds=10, close_on_speaker_change=False, include_late_messages=True
    )
    assembler = WindowAssembler("f1", config)

    assembler.push(msg(1, offset=0))
    assembler.push(msg(2, offset=30))
    assembler.push(msg(3, offset=5))

    assert [m.id for m in assembler.orphans] == ["f1-m03"]
    assert "f1-m03" in [m.id for m in assembler.open_messages]


# --- סט הזהב ------------------------------------------------------------


@pytest.mark.parametrize(
    "flight_path", sorted(GOLDEN_FLIGHTS_DIR.glob("*.yaml")), ids=lambda p: p.stem
)
def test_every_golden_sentence_appears_exactly_once_across_windows(flight_path: Path):
    messages = golden_messages(flight_path)

    windows = assemble_windows(messages)

    covered = [m.id for window in windows for m in window.own_messages]
    assert covered == [m.id for m in messages], "no sentence lost, none analysed twice"


@pytest.mark.parametrize(
    "flight_path", sorted(GOLDEN_FLIGHTS_DIR.glob("*.yaml")), ids=lambda p: p.stem
)
def test_golden_windows_respect_every_configured_limit(flight_path: Path):
    config = WindowConfig()
    windows = assemble_windows(golden_messages(flight_path), config)

    for window in windows:
        assert len(window.messages) <= config.max_sentences
        assert window.duration_seconds <= config.max_duration_seconds
        assert window.overlap_count <= config.overlap_sentences
        own = window.own_messages
        gaps = [
            (b.timestamp - a.timestamp).total_seconds() for a, b in zip(own, own[1:], strict=False)
        ]
        assert all(gap <= config.silence_threshold_seconds for gap in gaps)


@pytest.mark.parametrize(
    "flight_path", sorted(GOLDEN_FLIGHTS_DIR.glob("*.yaml")), ids=lambda p: p.stem
)
def test_no_golden_window_exceeds_the_token_budget(flight_path: Path):
    builder = ContextBuilder()

    for window in assemble_windows(golden_messages(flight_path)):
        bundle = builder.build(window)
        assert bundle.token_budget.used_estimate <= bundle.token_budget.budget


# --- היסטוריה רלוונטית --------------------------------------------------


def test_a_window_mentioning_a_target_gets_that_targets_events_only():
    history = InMemoryEventHistory(
        [
            event("evt-alpha-1", "מטרה אלפא", entity_id="ent-alpha", minutes=0),
            event("evt-beta-1", "מטרה בטא", entity_id="ent-beta", minutes=1),
            event("evt-alpha-2", "מטרה אלפא", entity_id="ent-alpha", minutes=2,
                  event_type="loss_of_contact"),
        ]
    )
    builder = ContextBuilder(history=history)
    window = assemble_windows(
        [msg(1, text="בקרה, נץ אחת, חידשנו קשר עם מטרה אלפא.")], WindowConfig()
    )[0]

    bundle = builder.build(window)

    assert [e.id for e in bundle.relevant_history] == ["evt-alpha-1", "evt-alpha-2"]


def test_history_is_empty_when_the_window_mentions_no_known_entity():
    history = InMemoryEventHistory([event("evt-alpha-1", "מטרה אלפא", entity_id="ent-alpha")])
    builder = ContextBuilder(history=history)
    window = assemble_windows([msg(1, text="בקרה, נץ אחת, הכל תקין.")], WindowConfig())[0]

    assert builder.build(window).relevant_history == []


def test_an_entity_is_matched_by_its_short_name_too():
    events = [event("evt-1", "מטרה אלפא", entity_id="ent-alpha")]

    selected = select_relevant_events("בקרה, נץ אחת, אלפא נעה מערבה.", events)

    assert [e.id for e in selected] == ["evt-1"]


def test_history_returns_chronological_order_and_respects_the_cap():
    events = [
        event(f"evt-{i}", "מטרה אלפא", entity_id="ent-alpha", minutes=i) for i in range(10)
    ]

    selected = select_relevant_events("מטרה אלפא", events, max_events=3)

    assert [e.id for e in selected] == ["evt-7", "evt-8", "evt-9"], "newest three, oldest first"


def test_state_entities_are_matched_even_without_prior_events():
    events = [event("evt-1", "מטרה אלפא", entity_id="ent-alpha")]

    selected = select_relevant_events("מטרה אלפא במעקב", events, mission_state())

    assert [e.id for e in selected] == ["evt-1"]


# --- תקציב הטוקנים ------------------------------------------------------


def test_history_is_trimmed_oldest_first_until_the_bundle_fits():
    history = InMemoryEventHistory(
        [
            event(f"evt-{i:02d}", "מטרה אלפא", entity_id="ent-alpha", minutes=i)
            for i in range(20)
        ]
    )
    settings = Settings(token_budget=TokenBudgetConfig(max_tokens_per_window=400))
    builder = ContextBuilder(settings=settings, history=history)
    window = assemble_windows([msg(1, text="מטרה אלפא במעקב")], WindowConfig())[0]

    bundle = builder.build(window)

    assert bundle.token_budget.history_items_dropped > 0
    assert bundle.token_budget.used_estimate <= 400
    kept = [e.id for e in bundle.relevant_history]
    assert kept == sorted(kept), "what survives is still chronological"
    assert "evt-19" in kept, "the newest event is the last to be dropped"


def test_a_huge_window_keeps_the_sentences_and_makes_the_overrun_visible():
    """החלון, המצב והתקציר נשמרים תמיד - ולכן חריגה אפשרית, אך גלויה."""
    long_text = "בקרה, נץ אחת, " + ("דיווח ארוך מאוד על המצב בגזרה. " * 80)
    settings = Settings(token_budget=TokenBudgetConfig(max_tokens_per_window=200))
    builder = ContextBuilder(settings=settings)
    window = assemble_windows(
        [msg(i, text=long_text) for i in range(1, 4)],
        WindowConfig(close_on_speaker_change=False),
    )[0]

    bundle = builder.build(window)

    assert len(bundle.messages) == 3, "sentences under analysis are never dropped"
    assert bundle.relevant_history == []
    assert bundle.token_budget.used_estimate > bundle.token_budget.budget


def test_state_and_summary_survive_a_tight_budget():
    settings = Settings(token_budget=TokenBudgetConfig(max_tokens_per_window=50))
    builder = ContextBuilder(settings=settings)
    window = assemble_windows([msg(1)], WindowConfig())[0]
    summary = Summary(
        flight_id="f1",
        version=VersionTag.REALTIME,
        version_number=1,
        text="תקציר: מטרה אלפא במעקב.",
        trigger_reason=SummaryTrigger.SIGNIFICANT_EVENT,
        created_at=BASE,
    )

    bundle = builder.build(window, current_state=mission_state(), last_summary=summary)

    assert bundle.current_state is not None
    assert bundle.last_summary is not None


def test_the_token_estimate_is_conservative_and_itemised():
    counter = HeuristicTokenCounter(chars_per_token=2.5, per_item_overhead=4)

    assert counter.count("") == 0
    assert counter.count("abcde") == 2
    assert counter.count_items(["abcde", "abcde"]) == 12
    assert counter.count("א" * 100) > counter.count("א" * 50)


def test_the_bundle_matches_the_stage_0_contract():
    builder = ContextBuilder()
    window = assemble_windows([msg(1), msg(2)], WindowConfig(close_on_speaker_change=False))[0]

    bundle = builder.build(window, mission_settings={"sector": "north"})

    ContextBundle.model_validate(bundle.model_dump())
    assert bundle.window_id == window.window_id
    assert bundle.window_close_reason == window.close_reason
    assert bundle.mission_settings == {"sector": "north"}
    assert [d.id for d in bundle.event_type_catalog] == [
        "contact_with_target",
        "loss_of_contact",
        "mission_change",
        "malfunction",
    ]


# --- צורכן התור ---------------------------------------------------------


def test_the_queue_pump_turns_queued_messages_into_windows():
    queue = InMemoryQueue()
    for message in [msg(1, offset=0), msg(2, offset=5), msg(3, offset=60)]:
        queue.publish(message)
    pump = QueueWindowPump(queue, ContextBuilder())

    windows = pump.drain()

    assert len(queue) == 0, "the queue is drained"
    assert [w.close_reason for w in windows] == [WindowCloseReason.SILENCE_TIMEOUT]
    tail = pump.close_flight("f1")
    assert tail is not None and tail.close_reason == WindowCloseReason.FLIGHT_END
    assert [b.window_id for b in pump.bundles(windows)] == [w.window_id for w in windows]


def test_the_queue_pump_keeps_flights_apart():
    queue = InMemoryQueue()
    queue.publish(msg(1, flight_id="f1"))
    queue.publish(msg(1, flight_id="f2"))
    pump = QueueWindowPump(queue)

    pump.drain()

    assert pump.close_flight("f1").flight_id == "f1"
    assert pump.close_flight("f2").flight_id == "f2"
    assert pump.close_flight("nope") is None


def test_a_word_shared_by_several_labels_does_not_match_either_entity():
    """"מטרה" מופיעה בכל תווית של מטרה, ולכן אינה מזהה אף אחת מהן."""
    events = [
        event("evt-alpha", "מטרה אלפא", entity_id="ent-alpha"),
        event("evt-beta", "מטרה בטא", entity_id="ent-beta"),
    ]

    generic = select_relevant_events("בקרה, נץ אחת, מטרה במעקב.", events)
    specific = select_relevant_events("בקרה, נץ אחת, בטא במעקב.", events)

    assert generic == []
    assert [e.id for e in specific] == ["evt-beta"]


def test_sentences_inside_a_window_are_ordered_by_their_original_timestamp():
    config = WindowConfig(silence_threshold_seconds=60, close_on_speaker_change=False)
    assembler = WindowAssembler("f1", config)

    assembler.push(msg(1, offset=0))
    assembler.push(msg(3, offset=40))
    assembler.push(msg(2, offset=20))  # arrives late, still inside the open window
    window = assembler.flush()

    assert [m.id for m in window.messages] == ["f1-m01", "f1-m02", "f1-m03"]
    assert assembler.orphans == []

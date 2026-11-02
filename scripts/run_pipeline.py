#!/usr/bin/env python3
"""Run a flight through the pipeline and print what came out.

Stages 1-9 are built, but nothing exposes them over HTTP yet - that is
stage 10 - so until then there is no way to *watch* the system work. This
script is that window. It takes a flight, runs it through the real
components, and prints the events and the mission state that came out the
far end, the summary and highlights it wrote, and the answers the query
service gives when asked about what was stored.

It is a diagnostic, not part of the service. Everything it calls is the
same code the service will call.

Three modes, and the difference between them is exactly how much of the
system is really being exercised, so the report says which one ran:

    --tagged FIXTURE   the hand-tagged events from a golden flight, run
                       through validation, state, summaries and queries
                       (components 5-9).
                       No model, no GPU - this is the honest way to see
                       what the system does with known-correct input.

    --mock FILE        a transcript file through all of 1-9, with a
                       canned model answer. Proves the wiring; proves
                       nothing about event detection.

    --model NAME FILE  the real thing: a transcript file through 1-9 with
                       a local ollama model. Needs ollama running.

Examples::

    python scripts/run_pipeline.py --tagged flight_01_recon_patrol
    python scripts/run_pipeline.py --model qwen3:14b samples/fixtures/eval_01_clean_baseline.txt
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aura.config import DEFAULT_SETTINGS  # noqa: E402
from aura.event_store import SQLiteEventStore  # noqa: E402
from aura.ingestion import IngestionService, parse_transcript_text  # noqa: E402
from aura.llm_engine import (  # noqa: E402
    EventDetectionEngine,
    MockLLMClient,
    OllamaClient,
)
from aura.pipeline import FlightPipeline  # noqa: E402
from aura.query_service import EventFilter, QueryService  # noqa: E402
from aura.raw_store import RawStoreDedupIndex, SQLiteRawTranscriptStore  # noqa: E402
from aura.schemas import (  # noqa: E402
    ContextBundle,
    LLMEvent,
    LLMResult,
    Message,
    SummaryTrigger,
    WindowCloseReason,
)
from aura.state_manager import MissionStateManager  # noqa: E402
from aura.summarizer import (  # noqa: E402
    FlightSummarizer,
    SQLiteSummaryStore,
    highlights_for,
)
from aura.validator import OutputValidator  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


def rule(title: str) -> None:
    print(f"\n{'─' * 72}\n{title}\n{'─' * 72}")


def show_state(state, changes) -> None:
    """מצב המשימה כפי שהוא, כולל ההיסטוריה שלא נמחקה."""
    rule("מצב המשימה")
    if state is None or not state.entities:
        print("  (ריק - אף אירוע לא שינה מצב)")
        return
    print(f"  טיסה {state.flight_id} · גרסה {state.version.value} · נכון ל-{state.as_of}")
    for entity in state.entities.values():
        mark = "✕" if entity.status.value == "closed" else "•"
        print(f"\n  {mark} {entity.label}  [{entity.type.value}]  id={entity.id}")
        if entity.closed_by_event_id:
            print(f"      נסגרה ב-{entity.closed_at} על ידי {entity.closed_by_event_id}")
        for name, attribute in entity.attributes.items():
            flag = "  ⚠ מסומן כסתירה" if attribute.contradicted else ""
            print(f"      {name}: {attribute.current.value}{flag}")
            print(
                f"        ({attribute.current.certainty.value}, "
                f"מתוך {attribute.current.source_event_id})"
            )
            for old in attribute.history:
                print(f"        ← קודם: {old.value}  ({old.source_event_id})")
    if changes:
        print(f"\n  {len(changes)} רשומות שינוי (StateChange) - הבסיס לשחזור בשלב 7")


def show_summary(summary) -> None:
    rule("סיכום הטיסה")
    if summary is None:
        print("  (לא הופק)")
        return
    print(f"  גרסה {summary.version_number} · {summary.trigger_reason.value} · "
          f"{len(summary.text)} תווים\n")
    for line in summary.text.splitlines():
        print(f"  {line}")


def show_highlights(picked) -> None:
    rule(f"נקודות ציון ({len(picked)})")
    if not picked:
        print("  (אין)")
        return
    for item in picked:
        print(f"  {item.at.strftime('%H:%M:%S')}  {item.text}")
        print(f"      [{item.reason}]  ← {', '.join(item.source_ids)}")


def show_queries(service: QueryService, flight_id: str) -> None:
    """שירות השאילתות (רכיב 9) על מה שנשמר הרגע.

    הנקודה כאן אינה שהנתונים נכונים - זה נבדק למעלה - אלא שאותם
    נתונים נגישים עכשיו *כשאלות*: מה המצב, מה קרה, מה היה המצב אז,
    ומאיזה משפט זה בא. זו השכבה שעליה שלב 10 יבנה את הממשק.
    """
    rule("שירות השאילתות (רכיב 9)")
    version = service.resolve_version(flight_id)
    print(f"  גרסה שנבחרה כברירת מחדל: {version.value}")

    timeline = service.timeline(flight_id)
    print(f"\n  ציר זמן ({len(timeline)} אירועים, בסדר האמירה):")
    for entry in timeline:
        when = f"{entry.at:%H:%M:%S}" if entry.spoken_at else "זמן לא ידוע"
        labels = ", ".join(ref.label for ref in entry.entities) or "—"
        print(f"    {when}  {entry.type:22} {entry.certainty:9} {labels}")

    if timeline:
        first = timeline[0]
        print(f"\n  מאירוע למקור ({first.event_id}):")
        for sentence in service.sources_of(first.event_id, flight_id):
            if sentence.missing:
                print(f"    {sentence.message_id} — חסר במאגר הגולמי")
            else:
                print(f"    [{sentence.message_id}] {sentence.source}: {sentence.text}")

        # מצב בנקודת זמן בעבר: מה היה ידוע כשהאירוע הראשון נאמר.
        if first.spoken_at is not None:
            past = service.state_at(flight_id, first.spoken_at)
            now = service.current_state(flight_id)
            print(f"\n  מצב ב-{first.spoken_at:%H:%M:%S} מול המצב עכשיו:")
            print(f"    אז:   {len(past.entities)} ישויות " + ", ".join(
                f"{e.label} ({e.status})" for e in past.entities
            ))
            print(f"    עכשיו: {len(now.entities)} ישויות " + ", ".join(
                f"{e.label} ({e.status})" for e in now.entities
            ))

        types = sorted({entry.type for entry in timeline})
        print("\n  סינון לפי סוג אירוע:")
        for event_type in types:
            found = service.timeline(flight_id, EventFilter.build(types=event_type))
            print(f"    {event_type:22} {len(found)}")

    diff = service.version_diff(flight_id)
    if diff.final_exists:
        print(
            f"\n  הבדלי גרסאות: אושרו {len(diff.confirmed)}, תוקנו "
            f"{len(diff.corrected)}, נוספו {len(diff.added)}, בוטלו "
            f"{len(diff.cancelled)}"
        )
    else:
        print("\n  הבדלי גרסאות: אין גרסה סופית (מעבר התיקון הוא שלב 11)")


def show_events(accepted, rejected) -> None:
    rule(f"אירועים: {len(accepted)} התקבלו, {len(rejected)} נדחו")
    for event in accepted:
        print(
            f"\n  ✓ {event.type}  "
            f"(ביטחון {event.confidence}, {event.relation_to_previous.value})"
        )
        print(f"      {event.new_info or event.description}")
        print(f"      ישויות: {', '.join(r.label for r in event.entities) or '—'}")
        print(f"      מקור: {', '.join(event.source_ids)}")
    for item in rejected:
        print(f"\n  ✗ {item.raw.get('type', '?')}  →  {item.reason}")
        print(f"      {item.detail}")


# --- mode 1: tagged golden flight (no model) -----------------------------


def run_tagged(name: str) -> int:
    path = next((p for p in FIXTURES.glob("*/*.yaml") if p.stem == name), None)
    if path is None:
        print(f"no fixture named {name!r}. available:", file=sys.stderr)
        for p in sorted(FIXTURES.glob("*/*.yaml")):
            print(f"  {p.stem}", file=sys.stderr)
        return 1

    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if "messages" not in data or not any(
        m.get("expected", {}).get("has_event") for m in data["messages"]
    ):
        print(f"{name} has no per-sentence event tags; try a golden flight", file=sys.stderr)
        return 1

    rule(f"מצב: תגיות ידניות מתוך {path.relative_to(ROOT)}")
    print("  רכיבים 5-6 בלבד. אין מודל ואין GPU - הקלט הוא התיוג הידני,")
    print("  ולכן זה מראה מה המערכת עושה עם קלט נכון בוודאות.")

    flight_id = data["flight_id"]
    messages = [
        Message.model_validate({k: v for k, v in raw.items() if k != "expected"})
        for raw in data["messages"]
    ]
    times = {m.id: m.timestamp for m in messages}
    tagged = [
        LLMEvent.model_validate(
            {
                "type": raw["expected"]["event_type"],
                "description": raw["expected"].get("new_info") or raw["text"],
                "entities": raw["expected"].get("entities", []),
                "new_info": raw["expected"].get("new_info", ""),
                "relation_to_previous": raw["expected"]["relation_to_previous"],
                "confidence": raw["expected"].get("confidence_min", 0.8),
                "source_ids": [raw["id"]],
            }
        )
        for raw in data["messages"]
        if raw["expected"]["has_event"]
    ]
    print(f"\n  {len(messages)} משפטים, {len(tagged)} מהם מתויגים כאירוע")

    bundle = ContextBundle(
        window_id=f"{flight_id}-w-all",
        flight_id=flight_id,
        messages=messages,
        window_close_reason=WindowCloseReason.FLIGHT_END,
    )
    validated = OutputValidator().validate(
        LLMResult(window_id=bundle.window_id, events=tagged, llm_model="tagged"), bundle
    )
    show_events(validated.accepted, validated.rejected)

    update = MissionStateManager().apply(validated.accepted, message_times=times)
    show_state(update.state, update.changes)
    if update.skipped:
        print(f"\n  אירועים שנרשמו ולא שינו מצב: {update.skipped}")

    summarizer = FlightSummarizer(store=SQLiteSummaryStore(":memory:"))
    summarizer.record(validated.accepted)
    last = max(times, key=lambda mid: (times[mid], mid)) if times else None
    show_summary(
        summarizer.summarize(
            flight_id,
            update.state,
            SummaryTrigger.POST_FLIGHT,
            covers_up_to_message_id=last,
        )
    )
    show_highlights(highlights_for(validated.accepted, update.state, times))

    # רכיבים 7 ו-9: אותם נתונים, הפעם כשאלות. המאגרים נבנים כאן ולא
    # למעלה כי המסלול המתויג אינו עובר דרך הקליטה.
    raw_store = SQLiteRawTranscriptStore(":memory:")
    raw_store.append_many(messages)
    event_store = SQLiteEventStore(":memory:")
    event_store.append_events(validated.accepted)
    event_store.append_changes(update.changes)
    event_store.save_state(update.state)
    show_queries(
        QueryService(event_store, raw_store, summarizer.store), flight_id
    )
    return 0


# --- modes 2 and 3: a transcript file through all of 1-6 -----------------


CANNED = json.dumps(
    {
        "events": [
            {
                "type": "contact_with_target",
                "description": "תשובת מודל מדומה",
                "entities": [{"id": None, "type": "target", "label": "מטרה אלפא"}],
                "new_info": "תשובה קבועה מראש - אינה ניתוח אמיתי.",
                "relation_to_previous": "new",
                "confidence": 0.9,
                "source_ids": ["REPLACED_AT_RUNTIME"],
            }
        ]
    },
    ensure_ascii=False,
)


def run_file(path: Path, model: str | None, flight_id: str | None) -> int:
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 1
    parsed = parse_transcript_text(path.read_text(encoding="utf-8"), flight_id=flight_id)
    if not parsed.flight_id:
        print("the file has no '# flight_id:' line; pass --flight-id", file=sys.stderr)
        return 1

    if model:
        rule(f"מצב: הצינור המלא 1→6, מודל אמיתי ({model})")
        client = OllamaClient(model=model, settings=DEFAULT_SETTINGS.llm)
    else:
        rule("מצב: הצינור המלא 1→6, מודל מדומה")
        print("  התשובה קבועה מראש. זה מוכיח את ההשחלה בין הרכיבים,")
        print("  ואינו מוכיח דבר על איכות זיהוי האירועים.")
        first = next((m.get("id") for m in parsed.messages if m.get("id")), None)
        client = MockLLMClient(CANNED.replace("REPLACED_AT_RUNTIME", first or "unknown"))

    store = SQLiteRawTranscriptStore(":memory:")
    service = IngestionService(store=store, dedup_index=RawStoreDedupIndex(store))
    events = SQLiteEventStore(":memory:")
    summarizer = FlightSummarizer(store=SQLiteSummaryStore(":memory:"))
    pipeline = FlightPipeline(
        service,
        EventDetectionEngine(client),
        event_store=events,
        summarizer=summarizer,
    )

    results = pipeline.ingest_many(parsed.messages)
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status.value] = counts.get(result.status.value, 0) + 1
    print(f"\n  קליטה (רכיבים 1-2): {counts}")
    print(f"  במאגר הגולמי: {store.count(parsed.flight_id)} משפטים")

    started = datetime.now()
    run = pipeline.close_flight(parsed.flight_id)
    elapsed = (datetime.now() - started).total_seconds()

    print(f"  חלונות שנסגרו (רכיב 3): {len(run.windows)}  ·  {elapsed:.1f}s")
    for window in run.failures:
        print(f"  ✗ חלון {window.bundle.window_id} נכשל: {window.error}")

    show_events(run.events, run.rejected)

    show_state(pipeline.state_of(parsed.flight_id), run.changes)

    # רכיב 7: מה שנשמר, ומה שאפשר לשחזר ממנו.
    rule("מאגר האירועים (רכיב 7)")
    print(f"  {events.count_events(parsed.flight_id)} אירועים נשמרו")
    rebuilt = events.rebuild_state(parsed.flight_id)
    live = pipeline.state_of(parsed.flight_id)
    same = live is not None and set(rebuilt.entities) == set(live.entities)
    print(f"  שחזור מרצף השינויים זהה למצב החי: {'כן' if same else 'לא'}")
    timeline = events.events_for_flight(parsed.flight_id)
    if timeline:
        first = timeline[0]
        print(
            f"  ייחוס לדוגמה: {first.id} ← "
            f"{', '.join(events.source_ids_of(first.id, parsed.flight_id))}"
        )

    show_summary(pipeline.finish_flight(parsed.flight_id))
    show_highlights(pipeline.highlights_of(parsed.flight_id))

    show_queries(QueryService(events, store, summarizer.store), parsed.flight_id)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("file", nargs="?", type=Path, help="transcript .txt to run")
    parser.add_argument("--tagged", metavar="FIXTURE", help="a golden flight's own tags")
    parser.add_argument("--model", help="ollama model name; omit for a canned answer")
    parser.add_argument("--flight-id", help="override the file's flight id")
    args = parser.parse_args()

    if args.tagged:
        return run_tagged(args.tagged)
    if args.file:
        return run_file(args.file, args.model, args.flight_id)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run tagged scenarios through one or more local models and score them.

This is stage 4's evaluation script (רכיב 4, משימה 4). It serves two jobs
that share all their machinery:

  model comparison - two or more models over the three-minute scenarios in
  tests/fixtures/model_eval, to decide which model stage 4 ships with;

  the stage-4 gate - the golden flights, for the documented
  precision/recall baseline and the ≥90% source-attribution criterion.

Examples
--------
Compare the two candidates on the short scenarios::

    python scripts/evaluate_llm.py \\
        --model hf.co/dicta-il/DictaLM-3.0-Nemotron-12B-Instruct-GGUF:Q4_K_M \\
        --model qwen3.6:27b

Separate "cannot read the Hebrew" from "will not follow the rule", by
running both prompt profiles::

    python scripts/evaluate_llm.py --model <m> --profile minimal --profile guarded

Score the golden flights and record the answers as test fixtures::

    python scripts/evaluate_llm.py --suite golden --model <m> \\
        --record tests/fixtures/llm_responses

Needs ollama serving on the host (`ollama serve`) and the model pulled
(`ollama pull <model>`). Nothing here talks to a network service other
than that local endpoint.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from aura.config import DEFAULT_SETTINGS, LLMConfig, Settings, WindowConfig  # noqa: E402
from aura.context_builder import ContextBuilder, assemble_windows  # noqa: E402
from aura.llm_engine import (  # noqa: E402
    AggregateScore,
    EventDetectionEngine,
    LLMError,
    LLMResponseFormatError,
    OllamaClient,
    PromptProfile,
    RecordingLLMClient,
    ScenarioScore,
    expected_from_golden_flight,
    expected_from_model_eval,
    score_scenario,
)
from aura.schemas import LLMResult, Message  # noqa: E402

SUITES = {
    "model_eval": REPO_ROOT / "tests" / "fixtures" / "model_eval",
    "golden": REPO_ROOT / "tests" / "fixtures" / "golden_flights",
}

# ברירת המחדל היא התצורה המומלצת ולא ברירת המחדל של התכנית: בתצורת
# התכנית כל משפט הוא חלון נפרד, ואז המודל רואה אישור בלי הדיווח שהוא
# מאשר - כך מלכודת הספירה הכפולה הופכת לארטיפקט של החלונות במקום למדידה
# של המודל. ‎--plan-defaults מאפשר למדוד בדיוק את ההפרש הזה.
RECOMMENDED_WINDOW = WindowConfig(
    silence_threshold_seconds=60, max_duration_seconds=180, close_on_speaker_change=False
)


def load_scenarios(suite: str, only: list[str] | None) -> list[tuple[str, dict]]:
    paths = sorted(SUITES[suite].glob("*.yaml"))
    scenarios = []
    for path in paths:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        name = data.get("scenario_id") or data["flight_id"]
        if only and name not in only and path.stem not in only:
            continue
        scenarios.append((name, data))
    return scenarios


def run_scenario(
    name: str,
    scenario: dict,
    suite: str,
    engine: EventDetectionEngine,
    settings: Settings,
    recorder_dir: Path | None,
) -> ScenarioScore:
    """מריץ תרחיש אחד: חלונות -> חבילות הקשר -> מודל -> ניקוד."""
    if suite == "golden":
        expected, traps = expected_from_golden_flight(scenario)
        raw_messages = [
            {key: value for key, value in raw.items() if key != "expected"}
            for raw in scenario["messages"]
        ]
    else:
        expected, traps = expected_from_model_eval(scenario)
        raw_messages = scenario["messages"]

    messages = [Message.model_validate(raw) for raw in raw_messages]
    windows = assemble_windows(messages, settings.window)
    builder = ContextBuilder(settings=settings, event_types=engine.event_types)

    all_events = []
    raw_chunks = []
    started = time.monotonic()
    for window in windows:
        bundle = builder.build(window)
        client = engine.client
        if recorder_dir is not None:
            engine.client = RecordingLLMClient(client, recorder_dir, window.window_id)
        try:
            result = engine.detect(bundle)
        except LLMResponseFormatError as exc:
            return score_scenario(
                name,
                expected,
                traps,
                None,
                profile=engine.profile.value,
                duration_seconds=time.monotonic() - started,
                error=f"{exc} | raw={exc.raw_response[:200]}",
            )
        except LLMError as exc:
            return score_scenario(
                name,
                expected,
                traps,
                None,
                profile=engine.profile.value,
                duration_seconds=time.monotonic() - started,
                error=str(exc),
            )
        finally:
            engine.client = client
        all_events.extend(result.events)
        raw_chunks.append(result.raw_response or "")

    # כל החלונות של התרחיש מנוקדים יחד: הציפיות מוגדרות על שלוש הדקות
    # כולן, לא על חלון בודד.
    merged = LLMResult(
        window_id=f"{name}-all",
        events=all_events,
        llm_model=engine.client.model,
        raw_response="\n".join(raw_chunks) or None,
    )
    return score_scenario(
        name,
        expected,
        traps,
        merged,
        profile=engine.profile.value,
        duration_seconds=time.monotonic() - started,
    )


def print_scenario_detail(score: ScenarioScore) -> None:
    status = "schema-ok" if score.schema_ok else "SCHEMA FAIL"
    print(f"\n  {score.scenario_id} [{status}] {score.duration_seconds:.1f}s")
    if score.error:
        print(f"    error: {score.error}")
        return
    for outcome in score.events:
        tag = "optional" if outcome.optional else "required"
        if not outcome.detected:
            mark = "MISS"
        elif outcome.attribution_exact:
            mark = "ok"
        else:
            mark = "attribution"
        print(
            f"    [{mark:>11}] {outcome.key} ({tag}) {outcome.type}"
            f" expected={outcome.expected_source_ids} cited={outcome.cited_source_ids}"
            + ("" if outcome.matched_count <= 1 else f" SPLIT x{outcome.matched_count}")
            # צורת הישויות נבדקת רק על אירוע שזוהה - על MISS היא חסרת מידע.
            + ("" if outcome.entity_shape_ok or not outcome.detected else " entity-shape-mismatch")
        )
    for trap in score.traps:
        if trap.triggered:
            print(f"    [       TRAP] {trap.message_id} produced {trap.produced_types}")
            print(f"                 {trap.reason_he}")
    for item in score.spurious:
        print(f"    [   spurious] {item}")


def print_summary(aggregates: list[AggregateScore]) -> None:
    print("\n" + "=" * 100)
    header = (
        f"{'model':<46} {'prof':<8} {'schema':>7} {'recall':>7} "
        f"{'attrib':>7} {'prec':>6} {'trap':>5} {'split':>6} {'spur':>5} {'sec':>7} gate"
    )
    print(header)
    print("-" * 100)
    for agg in aggregates:
        model = agg.model if len(agg.model) <= 45 else "..." + agg.model[-42:]
        print(
            f"{model:<46} {agg.profile:<8} "
            f"{agg.schema_conformance:>6.0%} "
            f"{agg.recall:>6.0%} "
            f"{agg.attribution_rate:>6.0%} "
            f"{agg.precision:>5.0%} "
            f"{agg.trap_hits:>5} {agg.split_errors:>6} {agg.spurious:>5} "
            f"{agg.total_seconds:>6.1f}s " + ("PASS" if agg.meets_stage4_gate else "fail")
        )
    print("-" * 100)
    print(
        "schema >=98% and attrib >=90% is the stage-4 gate. recall/precision are the\n"
        "documented baseline; the final targets are set in stage 12. trap = events on\n"
        "sentences that carry none, split = one event returned as several, spur = right\n"
        "sentence wrong type."
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--model",
        action="append",
        dest="models",
        metavar="NAME",
        help="ollama model name; repeat to compare several (default: config value)",
    )
    parser.add_argument("--suite", choices=sorted(SUITES), default="model_eval")
    parser.add_argument(
        "--profile",
        action="append",
        dest="profiles",
        choices=[profile.value for profile in PromptProfile],
        help="prompt profile; repeat to run both (default: guarded)",
    )
    parser.add_argument("--scenario", action="append", dest="only", metavar="ID")
    parser.add_argument("--base-url", default=DEFAULT_SETTINGS.llm.base_url)
    parser.add_argument("--timeout", type=float, default=DEFAULT_SETTINGS.llm.timeout_seconds)
    parser.add_argument("--temperature", type=float, default=DEFAULT_SETTINGS.llm.temperature)
    parser.add_argument("--num-ctx", type=int, default=DEFAULT_SETTINGS.llm.num_ctx)
    parser.add_argument(
        "--think",
        choices=["on", "off"],
        help="force thinking mode on/off for models that support it (default: model's own)",
    )
    parser.add_argument(
        "--plan-defaults",
        action="store_true",
        help="window with the plan's values instead of the recommended ones",
    )
    parser.add_argument(
        "--record",
        type=Path,
        metavar="DIR",
        help="save every real response as a test fixture in DIR",
    )
    parser.add_argument("--json", type=Path, metavar="FILE", help="also write results as JSON")
    args = parser.parse_args()

    models = args.models or [DEFAULT_SETTINGS.llm.model]
    profiles = [PromptProfile(name) for name in (args.profiles or [PromptProfile.GUARDED.value])]
    scenarios = load_scenarios(args.suite, args.only)
    if not scenarios:
        parser.error(f"no scenarios matched in {SUITES[args.suite]}")

    window_config = DEFAULT_SETTINGS.window if args.plan_defaults else RECOMMENDED_WINDOW
    print(
        f"suite={args.suite} scenarios={len(scenarios)} "
        f"windowing={'plan defaults' if args.plan_defaults else 'recommended'} "
        f"(silence={window_config.silence_threshold_seconds}s, "
        f"max_duration={window_config.max_duration_seconds}s, "
        f"speaker_change={window_config.close_on_speaker_change})"
    )

    aggregates: list[AggregateScore] = []
    for model in models:
        config = LLMConfig(
            model=model,
            base_url=args.base_url,
            timeout_seconds=args.timeout,
            temperature=args.temperature,
            num_ctx=args.num_ctx,
            think=None if args.think is None else args.think == "on",
        )
        settings = Settings(window=window_config, llm=config)
        for profile in profiles:
            print(f"\n### {model}  [{profile.value}]")
            with OllamaClient(config=config) as client:
                engine = EventDetectionEngine(client, settings=settings, profile=profile)
                scores = []
                for name, scenario in scenarios:
                    score = run_scenario(name, scenario, args.suite, engine, settings, args.record)
                    print_scenario_detail(score)
                    scores.append(score)
            aggregates.append(AggregateScore(model=model, profile=profile.value, scenarios=scores))

    print_summary(aggregates)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                [
                    {
                        "model": agg.model,
                        "profile": agg.profile,
                        "schema_conformance": agg.schema_conformance,
                        "recall": agg.recall,
                        "attribution_rate": agg.attribution_rate,
                        "precision": agg.precision,
                        "trap_hits": agg.trap_hits,
                        "split_errors": agg.split_errors,
                        "spurious": agg.spurious,
                        "seconds": agg.total_seconds,
                        "meets_stage4_gate": agg.meets_stage4_gate,
                        "scenarios": [
                            {
                                "scenario_id": score.scenario_id,
                                "schema_ok": score.schema_ok,
                                "error": score.error,
                                "recall": score.recall,
                                "attribution_rate": score.attribution_rate,
                                "trap_hits": score.trap_hits,
                                "events": [
                                    {
                                        "key": outcome.key,
                                        "detected": outcome.detected,
                                        "attribution_exact": outcome.attribution_exact,
                                        "expected_source_ids": outcome.expected_source_ids,
                                        "cited_source_ids": outcome.cited_source_ids,
                                        "optional": outcome.optional,
                                    }
                                    for outcome in score.events
                                ],
                            }
                            for score in agg.scenarios
                        ],
                    }
                    for agg in aggregates
                ],
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"\nwrote {args.json}")

    return 0 if all(agg.schema_conformance > 0 for agg in aggregates) else 1


if __name__ == "__main__":
    raise SystemExit(main())

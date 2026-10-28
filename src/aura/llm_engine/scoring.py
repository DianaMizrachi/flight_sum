"""Scoring a model's output against tagged expectations (רכיב 4, משימה 4).

The stage-4 pass criterion has two numbers in it - ≥98% of responses
conform to the schema, and ≥90% of events point at the correct source
sentences - plus a documented precision/recall baseline. This module
computes all of them, and keeps five failures apart that a single accuracy
number would blur together:

``detected``            the event was found, with the right type
``attribution_exact``   ...and it cites exactly the right sentences
``split_errors``        one real event returned as two or more
``trap_hits``           an event produced from a sentence that carries none
``spurious``            an event on a real sentence but of the wrong type

The distinction that matters most in practice is between a trap hit and a
spurious event. Both are false positives, but a trap hit means the model
read a question, an acknowledgement or a condition as a report - a
domain-convention failure - while a spurious event means it read the
sentence and typed it wrong. The fixes are not the same.

Entity comparison is lenient on ``label`` text and strict on ``type`` and
count, because the label is free Hebrew copied from the transcript and
there is no single right wording for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aura.schemas import LLMEvent, LLMResult


@dataclass
class ExpectedEvent:
    """אירוע צפוי, בצורה שאינה תלויה בפורמט הקובץ שממנו נקרא."""

    key: str
    type: str
    source_ids: list[str]
    entity_types: list[str] = field(default_factory=list)
    optional: bool = False


@dataclass
class TrapMessage:
    """משפט שאסור שיפיק אירוע, עם הנימוק מהתרחיש."""

    message_id: str
    reason_he: str


@dataclass
class EventOutcome:
    key: str
    type: str
    detected: bool
    attribution_exact: bool
    matched_count: int
    expected_source_ids: list[str]
    cited_source_ids: list[str]
    entity_shape_ok: bool
    optional: bool


@dataclass
class TrapOutcome:
    message_id: str
    reason_he: str
    produced_types: list[str]

    @property
    def triggered(self) -> bool:
        return bool(self.produced_types)


@dataclass
class ScenarioScore:
    """ניקוד של מודל אחד על תרחיש אחד."""

    scenario_id: str
    model: str
    profile: str
    schema_ok: bool
    events: list[EventOutcome]
    traps: list[TrapOutcome]
    spurious: list[str]
    duration_seconds: float = 0.0
    error: str | None = None

    @property
    def required(self) -> list[EventOutcome]:
        return [outcome for outcome in self.events if not outcome.optional]

    @property
    def detected_count(self) -> int:
        return sum(1 for outcome in self.required if outcome.detected)

    @property
    def attribution_count(self) -> int:
        return sum(1 for outcome in self.required if outcome.detected and outcome.attribution_exact)

    @property
    def split_errors(self) -> int:
        return sum(1 for outcome in self.required if outcome.matched_count > 1)

    @property
    def trap_hits(self) -> int:
        return sum(1 for trap in self.traps if trap.triggered)

    @property
    def recall(self) -> float:
        """מהאירועים הצפויים, איזה חלק זוהה."""
        return _ratio(self.detected_count, len(self.required))

    @property
    def attribution_rate(self) -> float:
        """מהאירועים שזוהו, איזה חלק מצביע בדיוק על משפטי המקור הנכונים.

        זה המדד של קריטריון ה-90% בשלב 4. הוא מחושב מתוך האירועים שזוהו
        בלבד, כי לאירוע שלא זוהה אין ייחוס להעריך.
        """
        return _ratio(self.attribution_count, self.detected_count)

    @property
    def precision(self) -> float:
        """מהאירועים שהמודל הפיק, איזה חלק נכון."""
        produced = self.detected_count + self.trap_hits + len(self.spurious)
        return _ratio(self.detected_count, produced)


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def score_scenario(
    scenario_id: str,
    expected: list[ExpectedEvent],
    traps: list[TrapMessage],
    result: LLMResult | None,
    profile: str = "guarded",
    duration_seconds: float = 0.0,
    error: str | None = None,
) -> ScenarioScore:
    """משווה פלט של מודל לציפיות של תרחיש אחד.

    ``result=None`` עם ``error`` מסומן כאי-תאימות סכמה: המודל לא החזיר
    פלט שאפשר לנקד, וזה נספר בקריטריון ה-98% ולא כאפס אירועים.
    """
    if result is None:
        return ScenarioScore(
            scenario_id=scenario_id,
            model="unknown",
            profile=profile,
            schema_ok=False,
            events=[
                EventOutcome(
                    key=item.key,
                    type=item.type,
                    detected=False,
                    attribution_exact=False,
                    matched_count=0,
                    expected_source_ids=list(item.source_ids),
                    cited_source_ids=[],
                    entity_shape_ok=False,
                    optional=item.optional,
                )
                for item in expected
            ],
            traps=[TrapOutcome(trap.message_id, trap.reason_he, []) for trap in traps],
            spurious=[],
            duration_seconds=duration_seconds,
            error=error,
        )

    trap_ids = {trap.message_id for trap in traps}
    matched_event_indexes: set[int] = set()
    outcomes: list[EventOutcome] = []

    for item in expected:
        expected_ids = set(item.source_ids)
        matches = [
            (index, event)
            for index, event in enumerate(result.events)
            if event.type == item.type and expected_ids & set(event.source_ids)
        ]
        matched_event_indexes.update(index for index, _ in matches)

        if matches:
            # הייחוס נבחן על ההתאמה הטובה ביותר: אם אחד מהאירועים שהמודל
            # הפיק מצטט בדיוק את הקבוצה הנכונה, הייחוס נכון.
            best = max(matches, key=lambda pair: len(expected_ids & set(pair[1].source_ids)))[1]
            cited = list(best.source_ids)
            exact = any(set(event.source_ids) == expected_ids for _, event in matches)
            entity_ok = _entity_shape_matches(item, best)
        else:
            cited = []
            exact = False
            entity_ok = False

        outcomes.append(
            EventOutcome(
                key=item.key,
                type=item.type,
                detected=bool(matches),
                attribution_exact=exact,
                matched_count=len(matches),
                expected_source_ids=list(item.source_ids),
                cited_source_ids=cited,
                entity_shape_ok=entity_ok,
                optional=item.optional,
            )
        )

    trap_outcomes = [
        TrapOutcome(
            message_id=trap.message_id,
            reason_he=trap.reason_he,
            produced_types=[
                event.type for event in result.events if trap.message_id in event.source_ids
            ],
        )
        for trap in traps
    ]

    # אירוע שלא התאים לשום ציפייה ולא נגע במלכודת: המודל קרא משפט אמיתי
    # ונתן לו סוג שגוי.
    spurious = [
        f"{event.type}<-{','.join(event.source_ids)}"
        for index, event in enumerate(result.events)
        if index not in matched_event_indexes and not trap_ids & set(event.source_ids)
    ]

    return ScenarioScore(
        scenario_id=scenario_id,
        model=result.llm_model,
        profile=profile,
        schema_ok=True,
        events=outcomes,
        traps=trap_outcomes,
        spurious=spurious,
        duration_seconds=duration_seconds,
        error=error,
    )


def _entity_shape_matches(item: ExpectedEvent, event: LLMEvent) -> bool:
    if not item.entity_types:
        return True
    return sorted(item.entity_types) == sorted(entity.type.value for entity in event.entities)


@dataclass
class AggregateScore:
    """סיכום של מודל אחד על כמה תרחישים - השורה בטבלת ההשוואה."""

    model: str
    profile: str
    scenarios: list[ScenarioScore]

    @property
    def schema_conformance(self) -> float:
        ok = sum(1 for score in self.scenarios if score.schema_ok)
        return _ratio(ok, len(self.scenarios))

    @property
    def expected_total(self) -> int:
        return sum(len(score.required) for score in self.scenarios)

    @property
    def detected_total(self) -> int:
        return sum(score.detected_count for score in self.scenarios)

    @property
    def attribution_total(self) -> int:
        return sum(score.attribution_count for score in self.scenarios)

    @property
    def trap_hits(self) -> int:
        return sum(score.trap_hits for score in self.scenarios)

    @property
    def split_errors(self) -> int:
        return sum(score.split_errors for score in self.scenarios)

    @property
    def spurious(self) -> int:
        return sum(len(score.spurious) for score in self.scenarios)

    @property
    def recall(self) -> float:
        return _ratio(self.detected_total, self.expected_total)

    @property
    def attribution_rate(self) -> float:
        return _ratio(self.attribution_total, self.detected_total)

    @property
    def precision(self) -> float:
        produced = self.detected_total + self.trap_hits + self.spurious
        return _ratio(self.detected_total, produced)

    @property
    def total_seconds(self) -> float:
        return sum(score.duration_seconds for score in self.scenarios)

    @property
    def meets_stage4_gate(self) -> bool:
        """קריטריון המעבר של שלב 4: ≥98% סכמה ו-≥90% ייחוס מקורות."""
        return self.schema_conformance >= 0.98 and self.attribution_rate >= 0.90


def expected_from_model_eval(scenario: dict) -> tuple[list[ExpectedEvent], list[TrapMessage]]:
    """קורא תרחיש מ-``tests/fixtures/model_eval``."""
    expected = [
        ExpectedEvent(
            key=item["key"],
            type=item["type"],
            source_ids=list(item["source_ids"]),
            entity_types=[entity["type"] for entity in item.get("entities", [])],
            optional=optional,
        )
        for optional, items in (
            (False, scenario.get("expected_events", [])),
            (True, scenario.get("optional_events", [])),
        )
        for item in items
    ]
    traps = [
        TrapMessage(message_id=case["message_id"], reason_he=case["reason_he"].strip())
        for case in scenario.get("must_not_produce_events", [])
    ]
    return expected, traps


def expected_from_golden_flight(flight: dict) -> tuple[list[ExpectedEvent], list[TrapMessage]]:
    """קורא טיסת זהב, שבה הציפיות מתויגות לכל משפט בנפרד.

    טיסת זהב מתייגת אירוע אחד לכל משפט, ולכן ``source_ids`` הוא המשפט
    עצמו. משפט שתויג ``has_event: false`` הוא מלכודת - בלי נימוק מפורש,
    כי בפורמט הזה אין שדה כזה.
    """
    expected: list[ExpectedEvent] = []
    traps: list[TrapMessage] = []
    for raw in flight["messages"]:
        tag = raw["expected"]
        if tag["has_event"]:
            expected.append(
                ExpectedEvent(
                    key=raw["id"],
                    type=tag["event_type"],
                    source_ids=[raw["id"]],
                    entity_types=[entity["type"] for entity in tag.get("entities", [])],
                )
            )
        else:
            traps.append(TrapMessage(message_id=raw["id"], reason_he="תויג ללא אירוע בטיסת הזהב"))
    return expected, traps

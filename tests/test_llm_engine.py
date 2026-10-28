"""Stage 4 unit tests: prompt, client layer, engine, scoring.

Every test here runs against MockLLMClient, per the plan's "LLM מדומה
בבדיקות" principle - no model, no network, no GPU, so CI stays green on a
machine that has none of them. The real model is exercised by
scripts/evaluate_llm.py, which is what produces the pass-criterion
numbers.
"""

import json

import httpx
import pytest

from aura.config import LLMConfig, Settings, WindowConfig
from aura.context_builder import ContextBuilder, assemble_windows
from aura.llm_engine import (
    AggregateScore,
    EventDetectionEngine,
    ExpectedEvent,
    LLMProviderError,
    LLMResponseFormatError,
    LLMTimeoutError,
    MockLLMClient,
    ModelEventsResponse,
    OllamaClient,
    PromptProfile,
    RecordingLLMClient,
    TrapMessage,
    build_system_prompt,
    build_user_prompt,
    expected_from_golden_flight,
    expected_from_model_eval,
    response_json_schema,
    score_scenario,
)
from aura.schemas import LLMResult, Message
from aura.taxonomy import load_event_taxonomy

TAXONOMY = load_event_taxonomy()

WINDOW_CONFIG = WindowConfig(
    silence_threshold_seconds=60, max_duration_seconds=180, close_on_speaker_change=False
)


def make_messages() -> list[Message]:
    return [
        Message.model_validate(
            {
                "id": "f-m01",
                "flight_id": "f",
                "timestamp": "2026-10-28T08:00:00Z",
                "ingested_at": "2026-10-28T08:00:00Z",
                "source": "operator",
                "channel": "command",
                "text": "בקרה, יש לנו קשר עם מטרה אלפא, טווח שני קילומטר.",
            }
        ),
        Message.model_validate(
            {
                "id": "f-m02",
                "flight_id": "f",
                "timestamp": "2026-10-28T08:00:20Z",
                "ingested_at": "2026-10-28T08:00:20Z",
                "source": "ops",
                "channel": "command",
                "text": "מאשר, המשך מעקב.",
            }
        ),
    ]


def make_bundle():
    windows = assemble_windows(make_messages(), WINDOW_CONFIG)
    assert len(windows) == 1
    return ContextBuilder(event_types=TAXONOMY).build(windows[0])


def events_json(*events: dict) -> str:
    return json.dumps({"events": list(events)}, ensure_ascii=False)


CONTACT_EVENT = {
    "type": "contact_with_target",
    "description": "קשר עם מטרה אלפא",
    "entities": [{"id": None, "type": "target", "label": "מטרה אלפא"}],
    "new_info": "קשר עם מטרה אלפא, טווח שני קילומטר.",
    "relation_to_previous": "new",
    "confidence": 0.9,
    "source_ids": ["f-m01"],
}


# --- prompt ---------------------------------------------------------------


def test_system_prompt_lists_every_taxonomy_type():
    prompt = build_system_prompt(TAXONOMY)
    for definition in TAXONOMY:
        assert definition.id in prompt
        assert definition.label_he in prompt


def test_system_prompt_includes_taxonomy_examples():
    """The plan asks for the type definitions *and* examples in the prompt."""
    prompt = build_system_prompt(TAXONOMY)
    for definition in TAXONOMY:
        for example in definition.examples_he:
            assert example in prompt


def test_guarded_profile_adds_rules_and_minimal_does_not():
    guarded = build_system_prompt(TAXONOMY, PromptProfile.GUARDED)
    minimal = build_system_prompt(TAXONOMY, PromptProfile.MINIMAL)
    assert "שלילי" in guarded, "the negation rule must be stated"
    assert "אישורים וחזרות" in guarded
    assert "שלילי" not in minimal
    assert len(minimal) < len(guarded)
    # שתי הגרסאות מתארות את אותה משימה ואותו קטלוג - ההפרש הוא הכללים בלבד.
    assert "contact_with_target" in minimal


def test_user_prompt_labels_each_sentence_with_its_id():
    bundle = make_bundle()
    prompt = build_user_prompt(bundle)
    for message in bundle.messages:
        assert f"[{message.id}]" in prompt
        assert message.text in prompt


def test_user_prompt_omits_absent_optional_sections():
    prompt = build_user_prompt(make_bundle())
    assert "מצב המשימה הנוכחי" not in prompt
    assert "תקציר הטיסה" not in prompt
    assert "אירועים קודמים" not in prompt


# --- the model-facing schema ---------------------------------------------


def test_response_schema_excludes_harness_owned_fields():
    """A constrained model must not be forced to invent its own name."""
    schema = response_json_schema()
    assert set(schema["properties"]) == {"events"}
    assert "llm_model" not in json.dumps(schema)
    assert "raw_response" not in json.dumps(schema)


def test_response_schema_constrains_relation_and_entity_type():
    schema = json.dumps(response_json_schema())
    for value in ("new", "updates", "contradicts", "closes"):
        assert f'"{value}"' in schema
    for value in ("target", "area", "force"):
        assert f'"{value}"' in schema


def test_empty_event_list_is_a_valid_response():
    assert ModelEventsResponse.model_validate_json('{"events": []}').events == []


# --- client layer ---------------------------------------------------------


def test_mock_client_records_what_was_sent():
    client = MockLLMClient(events_json(CONTACT_EVENT))
    client.complete("sys", "usr", {"type": "object"})
    assert client.calls == [{"system": "sys", "user": "usr", "schema": {"type": "object"}}]


def test_mock_client_returns_queued_responses_in_order():
    client = MockLLMClient(["a", "b"])
    assert client.complete("s", "u", {}).text == "a"
    assert client.complete("s", "u", {}).text == "b"
    with pytest.raises(AssertionError, match="ran out of responses"):
        client.complete("s", "u", {})


def test_ollama_client_sends_schema_and_deterministic_options():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "message": {"content": events_json(CONTACT_EVENT)},
                "prompt_eval_count": 410,
                "eval_count": 77,
            },
        )

    transport = httpx.MockTransport(handler)
    config = LLMConfig(model="test-model", temperature=0.0, num_ctx=4096)
    http = httpx.Client(transport=transport, base_url=config.base_url)
    completion = OllamaClient(config=config, client=http).complete("sys", "usr", {"k": "v"})

    assert captured["model"] == "test-model"
    assert captured["format"] == {"k": "v"}, "format must carry the JSON schema"
    assert captured["stream"] is False
    assert captured["options"]["temperature"] == 0.0
    assert captured["options"]["num_ctx"] == 4096
    assert [m["role"] for m in captured["messages"]] == ["system", "user"]
    assert completion.prompt_tokens == 410
    assert completion.completion_tokens == 77


def test_ollama_client_raises_timeout_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://x")
    with pytest.raises(LLMTimeoutError):
        OllamaClient(config=LLMConfig(), client=http).complete("s", "u", {})


def test_ollama_client_raises_provider_error_on_http_status():
    http = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(500, text="model not found")),
        base_url="http://x",
    )
    with pytest.raises(LLMProviderError, match="500"):
        OllamaClient(config=LLMConfig(), client=http).complete("s", "u", {})


def test_ollama_client_raises_provider_error_on_unexpected_shape():
    http = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"nope": 1})),
        base_url="http://x",
    )
    with pytest.raises(LLMProviderError, match="unexpected"):
        OllamaClient(config=LLMConfig(), client=http).complete("s", "u", {})


def test_recording_client_writes_a_reusable_fixture(tmp_path):
    inner = MockLLMClient(events_json(CONTACT_EVENT), model="m1")
    recorder = RecordingLLMClient(inner, tmp_path, window_id="f-w001")
    recorder.complete("sys", "usr", {"type": "object"})

    assert len(recorder.written) == 1
    saved = json.loads(recorder.written[0].read_text(encoding="utf-8"))
    assert saved["model"] == "m1"
    assert saved["window_id"] == "f-w001"
    assert saved["system_prompt"] == "sys"
    assert ModelEventsResponse.model_validate_json(saved["response"]).events


def test_recording_client_overwrites_the_same_window_on_a_rerun(tmp_path):
    inner = MockLLMClient(["first", "second"], model="m1")
    recorder = RecordingLLMClient(inner, tmp_path, window_id="f-w001")
    recorder.complete("s", "u", {})
    recorder.complete("s", "u", {})
    assert len(list(tmp_path.glob("*.json"))) == 1


# --- engine ---------------------------------------------------------------


def test_engine_returns_schema_valid_result_with_owned_fields():
    client = MockLLMClient(events_json(CONTACT_EVENT), model="dictalm-test")
    result = EventDetectionEngine(client, event_types=TAXONOMY).detect(make_bundle())

    assert isinstance(result, LLMResult)
    assert result.window_id == "f-w001", "window_id comes from the bundle, not the model"
    assert result.llm_model == "dictalm-test"
    assert result.raw_response is not None
    assert len(result.events) == 1
    assert result.events[0].source_ids == ["f-m01"]


def test_engine_rejects_a_response_that_invents_a_window_id():
    """Nothing the model returns may decide which window a result belongs to.

    The schema sent to the model carries ``additionalProperties: false``, so
    a constrained model cannot produce this at all - reaching it means the
    schema was not applied, which is worth failing loudly rather than
    quietly dropping the field.
    """
    payload = json.dumps({"events": [], "window_id": "wrong"}, ensure_ascii=False)
    client = MockLLMClient(payload)
    with pytest.raises(LLMResponseFormatError):
        EventDetectionEngine(client, event_types=TAXONOMY).detect(make_bundle())


def test_engine_accepts_an_empty_event_list():
    client = MockLLMClient('{"events": []}')
    result = EventDetectionEngine(client, event_types=TAXONOMY).detect(make_bundle())
    assert result.events == []


def test_engine_raises_format_error_on_non_json():
    client = MockLLMClient("אין אירועים בחלון הזה.")
    with pytest.raises(LLMResponseFormatError) as excinfo:
        EventDetectionEngine(client, event_types=TAXONOMY).detect(make_bundle())
    assert excinfo.value.raw_response == "אין אירועים בחלון הזה."


def test_engine_raises_format_error_on_schema_mismatch():
    client = MockLLMClient('{"events": [{"type": "contact_with_target"}]}')
    with pytest.raises(LLMResponseFormatError, match="output schema"):
        EventDetectionEngine(client, event_types=TAXONOMY).detect(make_bundle())


def test_engine_does_not_validate_against_the_taxonomy():
    """Taxonomy checking is stage 5's job - duplicating it here would mean
    two places to change when the taxonomy changes."""
    invented = {**CONTACT_EVENT, "type": "no_such_event_type"}
    client = MockLLMClient(events_json(invented))
    result = EventDetectionEngine(client, event_types=TAXONOMY).detect(make_bundle())
    assert result.events[0].type == "no_such_event_type"


def test_engine_passes_the_bundle_catalog_to_the_prompt():
    client = MockLLMClient('{"events": []}')
    engine = EventDetectionEngine(client, event_types=TAXONOMY)
    engine.detect(make_bundle())
    assert "contact_with_target" in client.calls[0]["system"]
    assert "[f-m01]" in client.calls[0]["user"]


def test_engine_propagates_provider_errors_unchanged():
    client = MockLLMClient(LLMTimeoutError("timed out"))
    with pytest.raises(LLMTimeoutError):
        EventDetectionEngine(client, event_types=TAXONOMY).detect(make_bundle())


def test_last_prompts_matches_what_detect_sends():
    client = MockLLMClient('{"events": []}')
    engine = EventDetectionEngine(client, event_types=TAXONOMY)
    bundle = make_bundle()
    system, user = engine.last_prompts(bundle)
    engine.detect(bundle)
    assert client.calls[0]["system"] == system
    assert client.calls[0]["user"] == user


# --- scoring --------------------------------------------------------------


EXPECTED_ONE = [ExpectedEvent(key="contact", type="contact_with_target", source_ids=["m01"])]
TRAP_ONE = [TrapMessage(message_id="m02", reason_he="אישור קליטה")]


def result_with(*events: dict) -> LLMResult:
    return LLMResult.model_validate_json(
        json.dumps({"events": list(events), "window_id": "w", "llm_model": "m"})
    )


def event(type_: str, source_ids: list[str], entity_type: str = "target") -> dict:
    return {
        "type": type_,
        "description": "d",
        "entities": [{"type": entity_type, "label": "מטרה אלפא"}],
        "new_info": "n",
        "relation_to_previous": "new",
        "confidence": 0.9,
        "source_ids": source_ids,
    }


def test_perfect_answer_scores_full_marks():
    score = score_scenario(
        "s", EXPECTED_ONE, TRAP_ONE, result_with(event("contact_with_target", ["m01"]))
    )
    assert score.detected_count == 1
    assert score.attribution_count == 1
    assert score.attribution_rate == 1.0
    assert score.trap_hits == 0
    assert score.precision == 1.0


def test_right_type_wrong_sentence_is_a_miss_not_an_attribution_error():
    score = score_scenario(
        "s", EXPECTED_ONE, TRAP_ONE, result_with(event("contact_with_target", ["m09"]))
    )
    assert score.detected_count == 0
    assert score.recall == 0.0


def test_partial_citation_counts_as_detected_but_not_attributed():
    expected = [ExpectedEvent(key="k", type="contact_with_target", source_ids=["m01", "m02"])]
    score = score_scenario("s", expected, [], result_with(event("contact_with_target", ["m01"])))
    assert score.detected_count == 1
    assert score.attribution_count == 0
    assert score.attribution_rate == 0.0


def test_extra_citation_counts_as_detected_but_not_attributed():
    score = score_scenario(
        "s", EXPECTED_ONE, [], result_with(event("contact_with_target", ["m01", "m07"]))
    )
    assert score.detected_count == 1
    assert score.attribution_count == 0


def test_one_event_returned_twice_is_a_split_error():
    score = score_scenario(
        "s",
        [ExpectedEvent(key="k", type="contact_with_target", source_ids=["m01", "m02"])],
        [],
        result_with(event("contact_with_target", ["m01"]), event("contact_with_target", ["m02"])),
    )
    assert score.detected_count == 1
    assert score.split_errors == 1


def test_event_on_a_trap_sentence_is_counted_against_that_trap():
    score = score_scenario(
        "s",
        EXPECTED_ONE,
        TRAP_ONE,
        result_with(event("contact_with_target", ["m01"]), event("contact_with_target", ["m02"])),
    )
    assert score.trap_hits == 1
    assert score.traps[0].message_id == "m02"
    assert score.traps[0].produced_types == ["contact_with_target"]
    assert score.precision == 0.5
    assert not score.spurious, "a trap hit must not also be counted as spurious"


def test_wrong_type_on_a_real_sentence_is_spurious_not_a_trap():
    score = score_scenario("s", EXPECTED_ONE, TRAP_ONE, result_with(event("malfunction", ["m01"])))
    assert score.trap_hits == 0
    assert score.spurious == ["malfunction<-m01"]
    assert score.detected_count == 0


def test_optional_events_are_excluded_from_the_score():
    expected = EXPECTED_ONE + [
        ExpectedEvent(key="maybe", type="mission_change", source_ids=["m05"], optional=True)
    ]
    score = score_scenario("s", expected, [], result_with(event("contact_with_target", ["m01"])))
    assert len(score.required) == 1
    assert score.recall == 1.0, "a missing optional event is not a miss"


def test_optional_event_when_produced_is_not_spurious():
    expected = EXPECTED_ONE + [
        ExpectedEvent(key="maybe", type="mission_change", source_ids=["m05"], optional=True)
    ]
    score = score_scenario(
        "s",
        expected,
        [],
        result_with(
            event("contact_with_target", ["m01"]), event("mission_change", ["m05"], "area")
        ),
    )
    assert score.spurious == []
    assert score.precision == 1.0


def test_entity_shape_mismatch_is_reported_without_failing_detection():
    score = score_scenario(
        "s",
        [
            ExpectedEvent(
                key="k", type="contact_with_target", source_ids=["m01"], entity_types=["target"]
            )
        ],
        [],
        result_with(event("contact_with_target", ["m01"], entity_type="force")),
    )
    assert score.detected_count == 1
    assert score.events[0].entity_shape_ok is False


def test_unparseable_response_is_a_schema_failure_not_zero_events():
    score = score_scenario("s", EXPECTED_ONE, TRAP_ONE, None, error="not JSON")
    assert score.schema_ok is False
    assert score.error == "not JSON"
    assert score.detected_count == 0
    assert AggregateScore("m", "guarded", [score]).schema_conformance == 0.0


def test_aggregate_gate_needs_both_thresholds():
    good = score_scenario("s", EXPECTED_ONE, [], result_with(event("contact_with_target", ["m01"])))
    assert AggregateScore("m", "guarded", [good]).meets_stage4_gate

    attributed_wrong = score_scenario(
        "s", EXPECTED_ONE, [], result_with(event("contact_with_target", ["m01", "m09"]))
    )
    assert not AggregateScore("m", "guarded", [attributed_wrong]).meets_stage4_gate

    broken = score_scenario("s", EXPECTED_ONE, [], None, error="x")
    assert not AggregateScore("m", "guarded", [good, broken]).meets_stage4_gate


# --- reading the tagged fixtures -----------------------------------------


def test_expected_from_model_eval_marks_optional_events(model_eval_scenario):
    expected, traps = expected_from_model_eval(model_eval_scenario)
    required = [item for item in expected if not item.optional]
    assert len(required) == model_eval_scenario["expected_event_count"]
    assert len(expected) - len(required) == len(model_eval_scenario.get("optional_events", []))
    assert len(traps) == len(model_eval_scenario["must_not_produce_events"])
    assert all(trap.reason_he for trap in traps)


def test_expected_from_model_eval_keeps_multi_sentence_attribution():
    """eval-03 is the scenario whose point is a two-sentence event."""
    import yaml

    from tests.conftest import MODEL_EVAL_DIR

    scenario = yaml.safe_load(
        (MODEL_EVAL_DIR / "eval_03_attribution_traps.yaml").read_text(encoding="utf-8")
    )
    expected, _ = expected_from_model_eval(scenario)
    multi = [item for item in expected if len(item.source_ids) > 1]
    assert multi, "the multi-sentence expectation must survive the conversion"
    assert multi[0].source_ids == ["eval-03-m01", "eval-03-m02"]


def test_expected_from_golden_flight_splits_events_and_traps(golden_flight):
    expected, traps = expected_from_golden_flight(golden_flight)
    tagged = [raw for raw in golden_flight["messages"] if raw["expected"]["has_event"]]
    assert len(expected) == len(tagged)
    assert len(traps) == len(golden_flight["messages"]) - len(tagged)
    for item in expected:
        assert len(item.source_ids) == 1, "golden flights tag one event per sentence"


def test_full_pipeline_scores_a_scenario_end_to_end(model_eval_scenario):
    """Windowing -> bundle -> mocked model -> score, with no network.

    The mock answers every window with the scenario's own expected events,
    which is the only answer that should score 100% - so this test fails if
    the scorer, the fixture format or the engine's field ownership drift
    apart.
    """
    expected, traps = expected_from_model_eval(model_eval_scenario)
    required = [item for item in expected if not item.optional]
    payload = events_json(
        *[event(item.type, list(item.source_ids), item.entity_types[0]) for item in required]
    )

    messages = [Message.model_validate(raw) for raw in model_eval_scenario["messages"]]
    windows = assemble_windows(messages, WINDOW_CONFIG)
    assert len(windows) == 1, "each scenario is one window under the recommended config"

    settings = Settings(window=WINDOW_CONFIG)
    bundle = ContextBuilder(settings=settings, event_types=TAXONOMY).build(windows[0])
    client = MockLLMClient(payload, model="mock")
    result = EventDetectionEngine(client, settings=settings, event_types=TAXONOMY).detect(bundle)

    score = score_scenario(model_eval_scenario["scenario_id"], expected, traps, result)
    assert score.schema_ok
    assert score.recall == 1.0
    assert score.attribution_rate == 1.0
    assert score.trap_hits == 0
    assert score.split_errors == 0
    assert score.spurious == []
    assert AggregateScore("mock", "guarded", [score]).meets_stage4_gate

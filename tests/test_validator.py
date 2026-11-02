"""Output validation (רכיב 5, שלב 5).

The stage's pass criterion is a negative one - *no malformed output
reaches the state update* - so most of what follows is a deliberate set
of broken model outputs, each rejected for a named reason: broken JSON, a
type that is not in the taxonomy, an invented entity id, a source id that
points outside the window, a required field left empty, and the same
sentence used to assert two things that cannot both be true.

The positive half matters just as much, and is easy to forget: a clean
result must pass through *unchanged*. A validator that is merely strict
is trivially correct against the criterion above and useless - so every
golden flight and every model-comparison scenario is also run through it
here, and every tagged event must survive.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from aura.schemas import (
    ContextBundle,
    Entity,
    EntityRef,
    LLMEvent,
    LLMResult,
    Message,
    MissionState,
    ValidationStatus,
    VersionTag,
    WindowCloseReason,
)
from aura.taxonomy import load_event_taxonomy
from aura.validator import (
    OutputValidator,
    RejectionReason,
    context_from_bundle,
    default_event_id,
    find_contradictions,
)

FIXTURES = Path(__file__).parent / "fixtures"
FROZEN_NOW = datetime(2026, 9, 7, 14, 30, tzinfo=UTC)

FLIGHT = "flight-v1"
WINDOW = "flight-v1-w01"


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def message(message_id: str, text: str = "בקרה, נץ אחת, מטרה אלפא בתנועה.") -> Message:
    return Message(
        id=message_id,
        flight_id=FLIGHT,
        timestamp=datetime(2026, 9, 7, 14, 0, tzinfo=UTC),
        ingested_at=datetime(2026, 9, 7, 14, 0, tzinfo=UTC),
        source="operator",
        channel="command",
        text=text,
    )


def bundle(
    message_ids: tuple[str, ...] = ("m01", "m02"),
    *,
    state_entities: dict[str, Entity] | None = None,
) -> ContextBundle:
    current_state = None
    if state_entities is not None:
        current_state = MissionState(
            flight_id=FLIGHT,
            version=VersionTag.REALTIME,
            as_of=FROZEN_NOW,
            entities=state_entities,
        )
    return ContextBundle(
        window_id=WINDOW,
        flight_id=FLIGHT,
        messages=[message(mid) for mid in message_ids],
        window_close_reason=WindowCloseReason.SILENCE_TIMEOUT,
        current_state=current_state,
    )


def llm_event(**overrides) -> LLMEvent:
    payload = {
        "type": "contact_with_target",
        "description": "קשר עין עם מטרה אלפא",
        "entities": [{"id": None, "type": "target", "label": "מטרה אלפא"}],
        "new_info": "קשר עין עם מטרה אלפא בציר המזרחי.",
        "relation_to_previous": "new",
        "confidence": 0.9,
        "source_ids": ["m01"],
    }
    payload.update(overrides)
    return LLMEvent.model_validate(payload)


def result(*events: LLMEvent, window_id: str = WINDOW) -> LLMResult:
    return LLMResult(window_id=window_id, events=list(events), llm_model="test-model")


@pytest.fixture
def validator() -> OutputValidator:
    return OutputValidator(clock=lambda: FROZEN_NOW)


# --- a clean result passes through untouched -----------------------------


def test_a_valid_event_is_accepted(validator):
    validated = validator.validate(result(llm_event()), bundle())

    assert validated.status == ValidationStatus.OK
    assert not validated.rejected
    assert len(validated.accepted) == 1


def test_an_accepted_event_keeps_everything_the_model_said(validator):
    """האימות אינו כותב מחדש את האירוע - רק בודק אותו ועוטף אותו."""
    source = llm_event()

    event = validator.validate(result(source), bundle()).accepted[0]

    assert event.type == source.type
    assert event.description == source.description
    assert event.new_info == source.new_info
    assert event.relation_to_previous == source.relation_to_previous
    assert event.confidence == source.confidence
    assert event.source_ids == source.source_ids
    assert [e.model_dump() for e in event.entities] == [
        e.model_dump() for e in source.entities
    ]


def test_the_validator_assigns_the_fields_the_model_must_not_invent(validator):
    """זהות וייחוס נקבעים מהחבילה ומהשעון, לא מהמודל."""
    event = validator.validate(result(llm_event()), bundle()).accepted[0]

    assert event.id == default_event_id(WINDOW, 1)
    assert event.flight_id == FLIGHT
    assert event.window_id == WINDOW
    assert event.version == VersionTag.REALTIME
    assert event.created_at == FROZEN_NOW


def test_event_ids_are_unique_within_the_window(validator):
    validated = validator.validate(
        result(llm_event(), llm_event(source_ids=["m02"])), bundle()
    )

    ids = [event.id for event in validated.accepted]
    assert len(ids) == len(set(ids)) == 2


def test_an_empty_result_is_valid(validator):
    """חלון בלי אירועים הוא תשובה לגיטימית, לא כשל."""
    validated = validator.validate(result(), bundle())

    assert validated.status == ValidationStatus.OK
    assert validated.accepted == []
    assert validated.rejected == []


def test_a_final_version_can_be_requested(validator):
    """מעבר התיקון (שלב 11) מריץ את אותו אימות ומסמן גרסה סופית."""
    validated = validator.validate(
        result(llm_event()), bundle(), version=VersionTag.FINAL
    )

    assert validated.accepted[0].version == VersionTag.FINAL


# --- allowed values ------------------------------------------------------


def test_an_event_type_outside_the_taxonomy_is_rejected(validator):
    validated = validator.validate(result(llm_event(type="target_destroyed")), bundle())

    assert validated.accepted == []
    assert validated.rejected[0].reason == RejectionReason.UNKNOWN_EVENT_TYPE
    assert "target_destroyed" in validated.rejected[0].detail


def test_a_required_field_left_empty_is_rejected(validator):
    """``malfunction`` דורש ``new_info``; מחרוזת ריקה עוברת את הסכמה."""
    validated = validator.validate(
        result(
            llm_event(
                type="malfunction",
                new_info="   ",
                entities=[{"id": None, "type": "force", "label": "נץ אחת"}],
            )
        ),
        bundle(),
    )

    assert validated.rejected[0].reason == RejectionReason.MISSING_REQUIRED_FIELD
    assert "new_info" in validated.rejected[0].detail


def test_a_required_entity_list_left_empty_is_rejected(validator):
    validated = validator.validate(result(llm_event(entities=[])), bundle())

    assert validated.rejected[0].reason == RejectionReason.MISSING_REQUIRED_FIELD
    assert "entities" in validated.rejected[0].detail


def test_an_entity_type_the_event_does_not_apply_to_is_rejected(validator):
    """``contact_with_target`` חל על מטרות בלבד."""
    validated = validator.validate(
        result(llm_event(entities=[{"id": None, "type": "area", "label": "הגזרה הצפונית"}])),
        bundle(),
    )

    assert validated.rejected[0].reason == RejectionReason.ENTITY_TYPE_NOT_APPLICABLE
    assert "area" in validated.rejected[0].detail


def test_mission_change_accepts_an_area_because_the_taxonomy_says_so(validator):
    """אותו כלל בדיוק, בכיוון ההפוך - הטקסונומיה היא המקור, לא הוולידטור."""
    validated = validator.validate(
        result(
            llm_event(
                type="mission_change",
                entities=[{"id": None, "type": "area", "label": "הגזרה המזרחית"}],
                new_info="מעבר לסיור בגזרה המזרחית.",
            )
        ),
        bundle(),
    )

    assert validated.accepted and not validated.rejected


# --- entity ids ----------------------------------------------------------


def known_entity(entity_id: str = "target-alpha") -> dict[str, Entity]:
    return {
        entity_id: Entity.model_validate(
            {"id": entity_id, "type": "target", "label": "מטרה אלפא"}
        )
    }


def test_an_invented_entity_id_is_rejected(validator):
    """מזהה שהמודל המציא ייראה במורד הזרם כהפניה לישות אמיתית."""
    validated = validator.validate(
        result(llm_event(entities=[{"id": "target-zulu", "type": "target", "label": "מטרה"}])),
        bundle(state_entities=known_entity()),
    )

    assert validated.rejected[0].reason == RejectionReason.UNKNOWN_ENTITY_ID
    assert "target-zulu" in validated.rejected[0].detail


def test_a_known_entity_id_is_accepted(validator):
    validated = validator.validate(
        result(
            llm_event(entities=[{"id": "target-alpha", "type": "target", "label": "מטרה אלפא"}])
        ),
        bundle(state_entities=known_entity()),
    )

    assert validated.accepted and not validated.rejected


def test_a_new_entity_without_an_id_is_accepted(validator):
    """ישות חדשה נשלחת בלי מזהה; ההקצאה היא תפקידו של מנהל המצב."""
    validated = validator.validate(
        result(llm_event(entities=[{"id": None, "type": "target", "label": "מטרה חדשה"}])),
        bundle(state_entities=known_entity()),
    )

    assert validated.accepted and not validated.rejected


def test_entity_ids_are_not_checked_when_there_is_no_state_to_check_against(validator):
    """בלי מצב משימה אין מול מה לבדוק, והכלל מדלג במפורש.

    זו ההחלטה החשובה כאן: לדחות כל ישות מזוהה רק מפני שרכיב 6 עדיין לא
    קיים היה הופך את הוולידטור לחוסם, ולהעמיד פנים שהמזהה נבדק היה
    גרוע מכך.
    """
    validated = validator.validate(
        result(llm_event(entities=[{"id": "target-zulu", "type": "target", "label": "מטרה"}])),
        bundle(),
    )

    assert validated.accepted and not validated.rejected


def test_an_entity_from_the_relevant_history_counts_as_known(validator):
    """המודל יכול היה לפגוש את המזהה גם באירועי ההיסטוריה שבחבילה."""
    base = bundle()
    history_event = validator.validate(
        result(llm_event(entities=[{"id": None, "type": "target", "label": "מטרה אלפא"}])),
        base,
    ).accepted[0]
    history_event = history_event.model_copy(
        update={
            "entities": [
                EntityRef(id="target-alpha", type="target", label="מטרה אלפא")
            ]
        }
    )
    with_history = base.model_copy(update={"relevant_history": [history_event]})

    ctx = context_from_bundle(with_history, load_event_taxonomy())

    assert "target-alpha" in ctx.known_entity_ids


# --- source ids ----------------------------------------------------------


def test_a_source_id_outside_the_window_is_rejected(validator):
    """המודל לא ראה את המשפט ההוא, ולכן לא יכול היה לגזור ממנו דבר."""
    validated = validator.validate(result(llm_event(source_ids=["m99"])), bundle())

    assert validated.rejected[0].reason == RejectionReason.SOURCE_ID_NOT_IN_WINDOW
    assert "m99" in validated.rejected[0].detail


def test_one_bad_source_id_among_good_ones_is_still_rejected(validator):
    validated = validator.validate(result(llm_event(source_ids=["m01", "m99"])), bundle())

    assert validated.rejected[0].reason == RejectionReason.SOURCE_ID_NOT_IN_WINDOW


def test_an_event_with_no_source_cannot_even_be_built():
    """"אירוע בלי מקור נדחה" נאכף כבר בסכמה, ולכן אין דרך לייצר אותו."""
    with pytest.raises(Exception):
        llm_event(source_ids=[])


def test_a_result_for_another_window_is_rejected_whole(validator):
    validated = validator.validate(result(llm_event(), window_id="some-other-window"), bundle())

    assert validated.accepted == []
    assert validated.status == ValidationStatus.MANUAL_REVIEW
    assert validated.rejected[0].reason == RejectionReason.SCHEMA_INVALID


# --- contradictions ------------------------------------------------------


def contradicting_pair(source_ids: list[str] | None = None) -> tuple[LLMEvent, LLMEvent]:
    ids = source_ids or ["m01"]
    return (
        llm_event(type="contact_with_target", source_ids=list(ids)),
        llm_event(
            type="loss_of_contact",
            description="אובדן קשר עם מטרה אלפא",
            new_info="אין קשר עם מטרה אלפא.",
            source_ids=list(ids),
        ),
    )


def test_the_same_sentence_cannot_assert_two_contradicting_events(validator):
    validated = validator.validate(result(*contradicting_pair()), bundle())

    assert validated.accepted == []
    assert len(validated.rejected) == 2
    for rejection in validated.rejected:
        assert rejection.reason == RejectionReason.CONTRADICTING_EVENTS_SAME_SOURCE_SENTENCE


def test_both_sides_of_a_contradiction_are_rejected_not_one(validator):
    """שום דבר בפלט לא אומר איזה מהשניים נכון."""
    validated = validator.validate(result(*contradicting_pair()), bundle())

    types = {rejection.raw["type"] for rejection in validated.rejected}
    assert types == {"contact_with_target", "loss_of_contact"}


def test_contradicting_types_about_different_entities_are_both_kept(validator):
    """הסתירה היא על אותה ישות; שתי מטרות שונות אינן סותרות זו את זו."""
    first, second = contradicting_pair()
    second = second.model_copy(
        update={"entities": [EntityRef(id=None, type="target", label="מטרה ברבו")]}
    )

    validated = validator.validate(result(first, second), bundle())

    assert len(validated.accepted) == 2
    assert not validated.rejected


def test_contradicting_types_from_different_sentences_are_both_kept(validator):
    """דיווחים סותרים על פני זמן הם בדיוק מה ש-``contradicts`` נועד לתאר."""
    first, second = contradicting_pair()
    second = second.model_copy(
        update={"source_ids": ["m02"], "relation_to_previous": "contradicts"}
    )

    validated = validator.validate(result(first, second), bundle())

    assert len(validated.accepted) == 2
    assert not validated.rejected


def test_two_non_contradicting_events_from_one_sentence_are_both_kept(validator):
    """משפט אחד שמוליד שני אירועים שונים הוא דבר רגיל לגמרי."""
    first = llm_event()
    second = llm_event(
        type="mission_change",
        description="שינוי משימה",
        new_info="עוברים לגזרה המזרחית.",
        entities=[{"id": None, "type": "area", "label": "הגזרה המזרחית"}],
    )

    validated = validator.validate(result(first, second), bundle())

    assert len(validated.accepted) == 2


def test_a_contradiction_is_not_reported_against_an_already_rejected_event(validator):
    """אירוע שנפל על כלל בסיסי אינו משמש צד בסתירה."""
    first, second = contradicting_pair()
    first = first.model_copy(update={"source_ids": ["m99"]})

    validated = validator.validate(result(first, second), bundle())

    reasons = {rejection.reason for rejection in validated.rejected}
    assert reasons == {RejectionReason.SOURCE_ID_NOT_IN_WINDOW}
    assert len(validated.accepted) == 1


def test_contradiction_detection_is_driven_by_the_taxonomy(validator):
    """הידע "מה סותר מה" מוצהר בטקסונומיה ולא מקודד בוולידטור."""
    taxonomy = {d.id: d for d in load_event_taxonomy()}
    assert "loss_of_contact" in taxonomy["contact_with_target"].contradicts
    assert "contact_with_target" in taxonomy["loss_of_contact"].contradicts

    neutral = [d.model_copy(update={"contradicts": []}) for d in load_event_taxonomy()]
    ctx = context_from_bundle(bundle(), neutral)
    assert find_contradictions(list(contradicting_pair()), ctx) == {}


# --- the stage-0 edge-case fixture ---------------------------------------


def test_the_contradiction_fixture_is_rejected_exactly_as_it_declares(validator):
    """הפיקסצ'ר מתייג את התוצאה הצפויה מאז שלב 0, והרכיב קיים עכשיו.

    עד כה הבדיקה שלידו יכלה רק לומר "זה תקין מבחינת הסכמה, ולכן שלב 5
    נחוץ". כאן נבדק שהרכיב באמת עושה את מה שהפיקסצ'ר הבטיח.
    """
    data = load_yaml(FIXTURES / "edge_cases" / "same_sentence_contradiction.yaml")
    source = Message.model_validate(data["source_message"])
    llm_result = LLMResult.model_validate(data["llm_result"])
    window = ContextBundle(
        window_id=llm_result.window_id,
        flight_id=source.flight_id,
        messages=[source],
        window_close_reason=WindowCloseReason.SILENCE_TIMEOUT,
    )

    validated = validator.validate(llm_result, window)

    expected = data["expected"]
    assert expected["validator_outcome"] == "reject"
    assert validated.accepted == []
    assert len(validated.rejected) == expected["rejected_event_count"]
    for rejection in validated.rejected:
        assert rejection.reason == expected["reason"]


# --- malformed payloads --------------------------------------------------


@pytest.mark.parametrize(
    "payload, reason",
    [
        ("{not json at all", RejectionReason.NOT_JSON),
        ("", RejectionReason.NOT_JSON),
        ('["a list, not an object"]', RejectionReason.NOT_JSON),
        ('{"events": "not a list"}', RejectionReason.SCHEMA_INVALID),
        ('{"events": [{"type": "contact_with_target"}]}', RejectionReason.SCHEMA_INVALID),
        ('{"events": [], "unexpected_field": 1}', RejectionReason.SCHEMA_INVALID),
    ],
)
def test_a_payload_that_cannot_be_parsed_is_rejected_with_a_reason(payload, reason, validator):
    """פלט פגום נדחה עם סיבה, ולא מפיל את עיבוד החלון בחריגה."""
    validated = validator.validate_payload(payload, bundle())

    assert validated.accepted == []
    assert validated.status == ValidationStatus.MANUAL_REVIEW
    assert validated.rejected[0].reason == reason


def test_a_rejected_payload_keeps_the_raw_response_for_review(validator):
    validated = validator.validate_payload("{broken", bundle())

    assert validated.rejected[0].raw["raw_response"] == "{broken"


def test_a_well_formed_payload_goes_through_the_same_rules(validator):
    payload = {"events": [llm_event().model_dump(mode="json")]}

    validated = validator.validate_payload(payload, bundle())

    assert len(validated.accepted) == 1
    assert validated.accepted[0].window_id == WINDOW


# --- what reaches the state update ---------------------------------------


def test_a_mixed_result_passes_the_good_and_holds_the_bad(validator):
    """הקריטריון של השלב: שום פלט פגום לא ממשיך הלאה."""
    good = llm_event()
    bad = llm_event(type="not_a_real_type", source_ids=["m02"])

    validated = validator.validate(result(good, bad), bundle())

    assert len(validated.accepted) == 1
    assert validated.accepted[0].type == "contact_with_target"
    assert len(validated.rejected) == 1
    assert validated.status == ValidationStatus.MANUAL_REVIEW


def test_every_rejection_keeps_the_payload_it_was_given(validator):
    """"לבדיקה ידנית" מחייב שיהיה מה לבדוק."""
    bad = llm_event(type="not_a_real_type")

    rejection = validator.validate(result(bad), bundle()).rejected[0]

    assert rejection.raw == bad.model_dump(mode="json")
    assert rejection.detail


def test_an_event_is_rejected_once_with_the_most_basic_reason(validator):
    """אירוע עם כמה בעיות מדווח בסיבה אחת - הבסיסית שבהן."""
    bad = llm_event(type="not_a_real_type", source_ids=["m99"], entities=[])

    rejected = validator.validate(result(bad), bundle()).rejected

    assert len(rejected) == 1
    assert rejected[0].reason == RejectionReason.UNKNOWN_EVENT_TYPE


# --- the retry policy ----------------------------------------------------


def scripted(*results: LLMResult):
    """מודל מדומה שמחזיר תשובה אחרת בכל קריאה, ורושם מה נאמר לו."""
    calls: list[str | None] = []

    def detect(feedback: str | None) -> LLMResult:
        calls.append(feedback)
        return results[min(len(calls) - 1, len(results) - 1)]

    detect.calls = calls  # type: ignore[attr-defined]
    return detect


def test_a_clean_first_answer_is_not_retried(validator):
    from aura.validator import validate_with_repair

    detect = scripted(result(llm_event()))
    outcome = validate_with_repair(validator, bundle(), detect)

    assert outcome.attempts == 1
    assert detect.calls == [None], "the model must not be told about errors that did not happen"
    assert outcome.result.status == ValidationStatus.OK
    assert not outcome.needs_manual_review


def test_a_rejected_answer_gets_exactly_one_more_chance(validator):
    from aura.validator import validate_with_repair

    detect = scripted(result(llm_event(type="not_a_real_type")), result(llm_event()))
    outcome = validate_with_repair(validator, bundle(), detect)

    assert outcome.attempts == 2
    assert outcome.result.status == ValidationStatus.RETRIED
    assert len(outcome.result.accepted) == 1
    assert not outcome.result.rejected


def test_the_repair_attempt_is_told_what_was_wrong(validator):
    """"ניסיון תיקון עם הודעת השגיאה" - ההודעה נבנית מהדחיות עצמן."""
    from aura.validator import validate_with_repair

    detect = scripted(result(llm_event(type="not_a_real_type")), result(llm_event()))
    outcome = validate_with_repair(validator, bundle(), detect)

    feedback = detect.calls[1]
    assert feedback is not None
    assert RejectionReason.UNKNOWN_EVENT_TYPE in feedback
    assert "not_a_real_type" in feedback
    assert outcome.feedback_sent == [feedback]


def test_a_model_that_cannot_fix_itself_is_flagged_not_retried_forever(validator):
    """החסם הוא העיקר: רכיב שנועד להגן על הצינור לא יכול לתקוע אותו."""
    from aura.validator import validate_with_repair

    detect = scripted(result(llm_event(type="not_a_real_type")))
    outcome = validate_with_repair(validator, bundle(), detect)

    assert outcome.attempts == 2
    assert len(detect.calls) == 2
    assert outcome.result.status == ValidationStatus.MANUAL_REVIEW
    assert outcome.needs_manual_review
    assert outcome.result.accepted == []


def test_a_rejection_records_which_attempt_it_came_from(validator):
    from aura.validator import validate_with_repair

    detect = scripted(result(llm_event(type="not_a_real_type")))
    outcome = validate_with_repair(validator, bundle(), detect)

    assert outcome.result.rejected[0].retry_count == 1


def test_retries_can_be_turned_off_entirely(validator):
    from aura.validator import validate_with_repair

    detect = scripted(result(llm_event(type="not_a_real_type")))
    outcome = validate_with_repair(validator, bundle(), detect, max_repair_attempts=0)

    assert outcome.attempts == 1
    assert outcome.result.status == ValidationStatus.MANUAL_REVIEW


def test_the_last_answer_is_the_one_kept_even_if_it_is_worse(validator):
    """סמנטיקה של תיקון: המודל קיבל את השגיאות וענה שוב, וזו תשובתו.

    לשמור במקום זאת את "הניסיון הטוב ביותר" היה מרכיב פלט שהמודל
    מעולם לא הפיק, ומנהל המצב היה מקבל אירועים משתי תשובות שונות.
    """
    from aura.validator import validate_with_repair

    first = result(llm_event(), llm_event(type="not_a_real_type", source_ids=["m02"]))
    worse = result(llm_event(type="not_a_real_type"))
    outcome = validate_with_repair(validator, bundle(), scripted(first, worse))

    assert outcome.result.accepted == []
    assert outcome.result.status == ValidationStatus.MANUAL_REVIEW

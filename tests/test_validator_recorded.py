"""Real recorded model answers, through the real validator (רכיבים 4→5).

``tests/fixtures/llm_responses/`` holds answers captured from models that
actually ran on a GPU. ``test_llm_fixtures.py`` already checks they still
parse into the stage-4 schema; this file asks the next question, which is
the one stage 5 exists for: *what does the validator do with them?*

That matters because every other validator test feeds it input that a
person wrote - either a deliberate break or a hand-tagged correct answer.
Real model output is neither. It is where a rule meets confidences of
exactly ``1``, entity labels phrased slightly differently from the tags,
and source attribution the model chose on its own.

These tests assert the validator reaches a *decision* on real output and
that the decision is consistent with the rules - never that the model was
right. Some of these recordings scored badly on purpose (see the README's
model comparison), so a rejection here can be the correct outcome.
"""

import json
from pathlib import Path

import pytest
import yaml

from aura.llm_engine import ModelEventsResponse
from aura.schemas import (
    ContextBundle,
    LLMResult,
    Message,
    ValidationStatus,
    WindowCloseReason,
)
from aura.validator import OutputValidator, RejectionReason

RESPONSES_DIR = Path(__file__).parent / "fixtures" / "llm_responses"
MODEL_EVAL_DIR = Path(__file__).parent / "fixtures" / "model_eval"
RECORDINGS = sorted(RESPONSES_DIR.glob("*.json"))


def scenario_for(window_id: str) -> dict:
    """``eval-01-w001`` -> תרחיש ``eval-01``."""
    scenario_id = window_id.rsplit("-w", 1)[0]
    for path in sorted(MODEL_EVAL_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if data.get("scenario_id") == scenario_id:
            return data
    raise AssertionError(f"no scenario fixture for {window_id!r}")


@pytest.fixture(params=RECORDINGS, ids=lambda p: p.stem)
def recording(request) -> dict:
    return json.loads(request.param.read_text(encoding="utf-8"))


@pytest.fixture
def validator() -> OutputValidator:
    return OutputValidator()


def bundle_for(recording: dict) -> ContextBundle:
    """החלון שהמודל באמת ראה, לפי התרחיש שההקלטה נעשתה עליו."""
    scenario = scenario_for(recording["window_id"])
    return ContextBundle(
        window_id=recording["window_id"],
        flight_id=scenario["flight_id"],
        messages=[Message.model_validate(raw) for raw in scenario["messages"]],
        window_close_reason=WindowCloseReason.MAX_SENTENCES,
    )


def test_a_recorded_answer_reaches_a_decision(recording, validator):
    """כל אירוע מוכרע: התקבל או נדחה עם סיבה. אין שלישי."""
    bundle = bundle_for(recording)
    parsed = ModelEventsResponse.model_validate_json(recording["response"])
    result = LLMResult(
        window_id=bundle.window_id,
        events=parsed.events,
        llm_model=recording["model"],
        raw_response=recording["response"],
    )

    validated = validator.validate(result, bundle)

    assert len(validated.accepted) + len(validated.rejected) == len(parsed.events)
    for rejection in validated.rejected:
        assert rejection.reason in set(RejectionReason)
        assert rejection.detail
        assert rejection.raw


def test_the_raw_response_can_go_straight_in(recording, validator):
    """המסלול שבו הפלט הגולמי מגיע בלי פענוח מוקדם מגיע לאותה תוצאה."""
    bundle = bundle_for(recording)
    parsed = ModelEventsResponse.model_validate_json(recording["response"])

    validated = validator.validate_payload(
        recording["response"], bundle, llm_model=recording["model"]
    )

    assert len(validated.accepted) + len(validated.rejected) == len(parsed.events)


def test_nothing_malformed_survives_a_real_answer(recording, validator):
    """קריטריון המעבר של השלב, מול פלט אמיתי.

    כל אירוע שהתקבל הוא ``Event`` מלא - יש לו מזהה, טיסה, חלון, גרסה
    וזמן יצירה, סוגו בטקסונומיה, וכל מזהה מקור שלו הוא משפט בחלון.
    """
    bundle = bundle_for(recording)
    known_sources = {message.id for message in bundle.messages}
    taxonomy_ids = {definition.id for definition in validator.event_types}

    validated = validator.validate_payload(
        recording["response"], bundle, llm_model=recording["model"]
    )

    for event in validated.accepted:
        assert event.id and event.flight_id == bundle.flight_id
        assert event.window_id == bundle.window_id
        assert event.type in taxonomy_ids
        assert set(event.source_ids) <= known_sources
        assert event.created_at is not None


def test_accepted_events_have_unique_ids(recording, validator):
    bundle = bundle_for(recording)

    validated = validator.validate_payload(recording["response"], bundle)

    ids = [event.id for event in validated.accepted]
    assert len(ids) == len(set(ids))


def test_the_status_matches_whether_anything_was_rejected(recording, validator):
    bundle = bundle_for(recording)

    validated = validator.validate_payload(recording["response"], bundle)

    expected = ValidationStatus.MANUAL_REVIEW if validated.rejected else ValidationStatus.OK
    assert validated.status == expected


def test_at_least_one_recorded_answer_is_fully_accepted(validator):
    """אם *כל* התשובות האמיתיות נדחות, הכללים חונקים ולא מגנים.

    זו הבדיקה שתתריע אם כלל עתידי ייעשה מחמיר מדי: היא נכשלת כשאף פלט
    אמיתי שנשמר אינו עובר, בלי להתחייב לאיזה מהם עובר.
    """
    fully_accepted = []
    for path in RECORDINGS:
        data = json.loads(path.read_text(encoding="utf-8"))
        validated = validator.validate_payload(data["response"], bundle_for(data))
        if validated.accepted and not validated.rejected:
            fully_accepted.append(path.stem)

    assert fully_accepted, "every recorded real answer was rejected; the rules are too strict"


def test_how_each_recording_fares_is_visible(validator, capsys):
    """לא טענה - דוח. מראה לאן הולך כל פלט אמיתי, לקריאה ב-``-s``."""
    lines = []
    for path in RECORDINGS:
        data = json.loads(path.read_text(encoding="utf-8"))
        validated = validator.validate_payload(data["response"], bundle_for(data))
        reasons = sorted({r.reason for r in validated.rejected})
        lines.append(
            f"{path.stem:70s} accepted={len(validated.accepted):2d} "
            f"rejected={len(validated.rejected):2d} {','.join(reasons) or '-'}"
        )
    print("\n" + "\n".join(lines))

    assert lines


# --- what the rules actually catch in real output ------------------------


def test_the_real_rejections_are_the_two_known_disagreements(validator):
    """מה שנדחה בפועל מפלט אמיתי, מפורש בשמו.

    שתי הדחיות האלה אינן תקלה בוולידטור אלא מחלוקת אמיתית בין מה
    שהמודל הפיק לבין מה שהטקסונומיה מצהירה, והן ההחזר הראשון של שלב 5:

    ``mission_change`` עם ``new_info`` ריק - המודל מילא רק
    ``description``. הטקסונומיה דורשת ``new_info`` במפורש, והאירוע
    באמת חסר את המידע החדש שהוא אמור לתרום.

    ``contact_with_target`` שמצרף גם ישות ``area`` - המודל צירף את
    *המקום* שבו נוצר הקשר כישות של האירוע. הטקסונומיה אומרת שהסוג הזה
    חל על ``target`` בלבד.

    השנייה היא החלטה תחומית ולא תוכנתית: אם קשר עם מטרה *אמור* להיות
    מסוגל להצביע גם על האזור, התיקון הוא
    ``applicable_entity_types: [target, area]`` בטקסונומיה - ואז הבדיקה
    הזו נכשלת ומבקשת לעדכן אותה במודע, במקום שהכלל ייחלש בשקט.
    """
    seen: dict[str, list[str]] = {}
    for path in RECORDINGS:
        data = json.loads(path.read_text(encoding="utf-8"))
        validated = validator.validate_payload(data["response"], bundle_for(data))
        if validated.rejected:
            seen[path.stem] = sorted(r.reason for r in validated.rejected)

    assert seen == {
        "qwen3-14b__eval-02-w001": [
            RejectionReason.ENTITY_TYPE_NOT_APPLICABLE,
            RejectionReason.ENTITY_TYPE_NOT_APPLICABLE,
        ],
        "qwen3-14b__eval-03-w001": [
            RejectionReason.MISSING_REQUIRED_FIELD,
            RejectionReason.MISSING_REQUIRED_FIELD,
        ],
    }


def test_a_rejected_real_event_is_not_silently_lost(validator):
    """הערך של "לבדיקה ידנית": הפלט שנדחה נשמר שלם."""
    data = json.loads((RESPONSES_DIR / "qwen3-14b__eval-03-w001.json").read_text("utf-8"))

    validated = validator.validate_payload(data["response"], bundle_for(data))

    for rejection in validated.rejected:
        assert rejection.raw["type"] == "mission_change"
        assert rejection.raw["description"], "the model's own words survive the rejection"
        assert rejection.raw["source_ids"], "and so does its attribution"

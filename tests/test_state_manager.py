"""The mission state manager (רכיב 6, שלב 6).

The plan names three scenarios this component is judged on, and they are
the three that are genuinely hard:

*target A loses contact and regains it* - the end state must say regained
and both reports must survive in history, **including when the two
events arrive in the same window**, where the only thing that orders them
is which sentence they came from;

*low-confidence information must not overwrite verified information* - a
guess does not get to displace a fact by arriving later;

*a late message must not overwrite newer state* - the message is new to
us, but what it describes is not, and the state has to tell the
difference.

Everything else here exists to keep those three honest: ordering, the
append-only change log, and the fact that nothing is ever deleted.
"""

from datetime import UTC, datetime, timedelta

import pytest

from aura.config import Settings, StateConfig
from aura.schemas import (
    CertaintyLevel,
    EntityStatus,
    Event,
    InfoRelation,
    StateChangeKind,
    SummaryTrigger,
    VersionTag,
)
from aura.state_manager import (
    MissionStateManager,
    default_entity_id,
    entity_key,
    normalize_label,
)

FLIGHT = "flight-s6"
START = datetime(2026, 9, 1, 6, 0, tzinfo=UTC)
ALPHA = {"id": None, "type": "target", "label": "מטרה אלפא"}


def at(minutes: int) -> datetime:
    return START + timedelta(minutes=minutes)


def event(
    event_id: str,
    *,
    type: str = "contact_with_target",
    relation: str | InfoRelation = InfoRelation.NEW,
    entities: list[dict] | None = None,
    new_info: str = "קשר עין עם מטרה אלפא.",
    confidence: float = 0.9,
    source_ids: list[str] | None = None,
    version: VersionTag = VersionTag.REALTIME,
    created_at: datetime | None = None,
) -> Event:
    return Event(
        id=event_id,
        flight_id=FLIGHT,
        window_id="w01",
        type=type,
        description=new_info,
        entities=entities if entities is not None else [dict(ALPHA)],
        new_info=new_info,
        relation_to_previous=relation,
        confidence=confidence,
        source_ids=source_ids or ["m01"],
        version=version,
        created_at=created_at or START,
    )


@pytest.fixture
def manager() -> MissionStateManager:
    return MissionStateManager(clock=lambda: START)


def only_entity(update):
    assert len(update.state.entities) == 1
    return next(iter(update.state.entities.values()))


# --- scenario 1: contact lost, then regained -----------------------------


LOST_THEN_REGAINED = [
    event(
        "e1",
        type="contact_with_target",
        relation=InfoRelation.NEW,
        new_info="קשר עין עם מטרה אלפא.",
        source_ids=["m01"],
    ),
    event(
        "e2",
        type="loss_of_contact",
        relation=InfoRelation.UPDATES,
        new_info="איבדנו קשר עם מטרה אלפא.",
        source_ids=["m02"],
    ),
    event(
        "e3",
        type="contact_with_target",
        relation=InfoRelation.UPDATES,
        new_info="חידשנו קשר עם מטרה אלפא.",
        source_ids=["m03"],
    ),
]
TIMES = {"m01": at(0), "m02": at(2), "m03": at(4)}


def test_the_end_state_is_the_last_thing_said(manager):
    update = manager.apply(LOST_THEN_REGAINED, message_times=TIMES)

    attribute = only_entity(update).attributes["contact_status"]
    assert attribute.current.value == "חידשנו קשר עם מטרה אלפא."
    assert attribute.current.source_event_id == "e3"


def test_both_earlier_reports_survive_in_history(manager):
    """שום דבר לא נמחק: "איך הגענו להאמין בזה" תמיד ניתן לענייה."""
    update = manager.apply(LOST_THEN_REGAINED, message_times=TIMES)

    history = only_entity(update).attributes["contact_status"].history
    assert [snapshot.value for snapshot in history] == [
        "קשר עין עם מטרה אלפא.",
        "איבדנו קשר עם מטרה אלפא.",
    ]
    assert [snapshot.source_event_id for snapshot in history] == ["e1", "e2"]


def test_the_two_event_types_share_one_attribute(manager):
    """"קשר" ו"אובדן קשר" מדברים על אותו היבט, ולכן הם מחליפים זה את זה.

    אילו כל סוג היה כותב למאפיין משלו, המצב היה מחזיק בו-זמנית "יש
    קשר" ו"אין קשר" - שניהם "נוכחיים", ושניהם נכונים לפי עצמם.
    """
    update = manager.apply(LOST_THEN_REGAINED, message_times=TIMES)

    assert list(only_entity(update).attributes) == ["contact_status"]


def test_the_same_window_is_ordered_by_its_sentences_not_by_arrival(manager):
    """התרחיש שהתכנית מדגישה: שני האירועים באותו חלון.

    הם מגיעים כאן בסדר ההפוך לסדר האמירה. בלי מיון לפי משפט המקור,
    המצב הסופי היה "איבדנו קשר" - ההפך מהאמת.
    """
    reversed_arrival = list(reversed(LOST_THEN_REGAINED))

    update = manager.apply(reversed_arrival, message_times=TIMES)

    assert only_entity(update).attributes["contact_status"].current.value == (
        "חידשנו קשר עם מטרה אלפא."
    )


def test_every_step_leaves_a_change_record(manager):
    update = manager.apply(LOST_THEN_REGAINED, message_times=TIMES)

    kinds = [(change.kind, change.attribute) for change in update.changes]
    assert kinds == [
        (StateChangeKind.ADDED, None),  # הישות עצמה
        (StateChangeKind.ADDED, "contact_status"),
        (StateChangeKind.REPLACED, "contact_status"),
        (StateChangeKind.REPLACED, "contact_status"),
    ]
    assert [c.event_id for c in update.changes] == ["e1", "e1", "e2", "e3"]
    assert all(change.flight_id == FLIGHT for change in update.changes)


def test_change_records_carry_the_value_they_replaced(manager):
    """שלב 7 משחזר מצב בהרצה חוזרת שלהן, ולכן הן חייבות להיות מלאות."""
    update = manager.apply(LOST_THEN_REGAINED, message_times=TIMES)

    last = update.changes[-1]
    assert last.old_value == "איבדנו קשר עם מטרה אלפא."
    assert last.new_value == "חידשנו קשר עם מטרה אלפא."
    assert last.entity_id == only_entity(update).id
    assert last.applied_at == at(4)


# --- scenario 2: confidence ----------------------------------------------


def test_a_guess_does_not_overwrite_verified_information(manager):
    """ביטחון נמוך נרשם, אך אינו דורס מידע מאומת."""
    verified = event("e1", confidence=0.95, new_info="קשר עין ודאי עם מטרה אלפא.")
    guess = event(
        "e2",
        relation=InfoRelation.UPDATES,
        confidence=0.55,
        new_info="נראה שהמטרה עזבה.",
        source_ids=["m02"],
    )

    update = manager.apply([verified, guess], message_times={"m01": at(0), "m02": at(2)})

    attribute = only_entity(update).attributes["contact_status"]
    assert attribute.current.value == "קשר עין ודאי עם מטרה אלפא."
    assert attribute.current.certainty is CertaintyLevel.VERIFIED
    assert "נראה שהמטרה עזבה." in [s.value for s in attribute.history], (
        "the weaker report is kept, just not believed"
    )
    assert ("e2", "older_or_weaker_than_what_is_known") in update.skipped


def test_verified_information_does_replace_verified_information(manager):
    """החסם הוא על חולשה, לא על עדכון."""
    first = event("e1", confidence=0.95)
    second = event(
        "e2",
        relation=InfoRelation.UPDATES,
        confidence=0.95,
        new_info="המטרה בתנועה מזרחה.",
        source_ids=["m02"],
    )

    update = manager.apply([first, second], message_times={"m01": at(0), "m02": at(2)})

    assert only_entity(update).attributes["contact_status"].current.value == (
        "המטרה בתנועה מזרחה."
    )


def test_an_event_below_the_floor_does_not_touch_the_state_at_all(manager):
    """מתחת לסף התחתון האירוע נרשם אך אינו משנה מצב כלל."""
    update = manager.apply([event("e1", confidence=0.1)])

    assert update.state.entities == {}
    assert update.changes == []
    assert update.skipped == [("e1", "confidence_below_threshold")]


@pytest.mark.parametrize(
    "confidence, expected",
    [
        (0.95, CertaintyLevel.VERIFIED),
        (0.85, CertaintyLevel.VERIFIED),
        (0.7, CertaintyLevel.REPORTED),
        (0.5, CertaintyLevel.REPORTED),
        (0.4, CertaintyLevel.UNCERTAIN),
    ],
)
def test_confidence_becomes_a_named_certainty(confidence, expected, manager):
    update = manager.apply([event("e1", confidence=confidence)])

    assert only_entity(update).attributes["contact_status"].current.certainty is expected


def test_the_thresholds_are_configuration_not_constants():
    strict = MissionStateManager(
        settings=Settings(state=StateConfig(verified_confidence=0.99)),
        clock=lambda: START,
    )

    update = strict.apply([event("e1", confidence=0.95)])

    assert only_entity(update).attributes["contact_status"].current.certainty is (
        CertaintyLevel.REPORTED
    )


# --- scenario 3: a late message ------------------------------------------


def test_a_late_message_does_not_overwrite_newer_state(manager):
    """ההודעה חדשה לנו; מה שהיא מתארת אינו חדש.

    האירוע המאחר מגיע אחרון לעיבוד אבל נאמר ראשון, ולכן הוא אינו רשאי
    להיות הערך הנוכחי - גם אם מבחינת סדר ההגעה הוא האחרון שנראה.
    """
    recent = event("e2", new_info="המטרה עצרה.", source_ids=["m02"])
    late = event(
        "e1", relation=InfoRelation.UPDATES, new_info="המטרה בתנועה.", source_ids=["m01"]
    )
    times = {"m01": at(0), "m02": at(5)}

    update = manager.apply([recent, late], message_times=times)

    attribute = only_entity(update).attributes["contact_status"]
    assert attribute.current.value == "המטרה עצרה."
    assert "המטרה בתנועה." in [snapshot.value for snapshot in attribute.history]


def test_without_message_times_the_event_timestamp_is_used(manager):
    """בלי זמני משפטים נופלים לזמן האירוע - מתועד, ופחות מדויק."""
    first = event("e1", new_info="ראשון.", created_at=at(0))
    second = event("e2", relation=InfoRelation.UPDATES, new_info="שני.", created_at=at(3))

    update = manager.apply([second, first])

    assert only_entity(update).attributes["contact_status"].current.value == "שני."


# --- closing and contradiction -------------------------------------------


def test_closes_marks_the_entity_closed_and_says_which_event_did_it(manager):
    opened = event("e1")
    closed = event(
        "e2",
        relation=InfoRelation.CLOSES,
        new_info="סיימנו מעקב אחרי מטרה אלפא.",
        source_ids=["m02"],
    )

    update = manager.apply([opened, closed], message_times={"m01": at(0), "m02": at(9)})

    entity = only_entity(update)
    assert entity.status is EntityStatus.CLOSED
    assert entity.closed_by_event_id == "e2"
    assert entity.closed_at == at(9)
    assert update.changes[-1].kind is StateChangeKind.CLOSED


def test_closing_an_already_closed_entity_changes_nothing(manager):
    events = [
        event("e1"),
        event("e2", relation=InfoRelation.CLOSES, source_ids=["m02"]),
        event("e3", relation=InfoRelation.CLOSES, source_ids=["m03"]),
    ]

    update = manager.apply(events, message_times={"m01": at(0), "m02": at(5), "m03": at(7)})

    assert only_entity(update).closed_by_event_id == "e2"
    assert len([c for c in update.changes if c.kind is StateChangeKind.CLOSED]) == 1


def test_a_contradiction_updates_the_value_and_flags_it(manager):
    """סותר אינו מוחק - הוא מעדכן ומסמן, כדי שהסתירה תישאר נראית."""
    first = event("e1", new_info="המטרה בצומת.")
    against = event(
        "e2",
        relation=InfoRelation.CONTRADICTS,
        new_info="המטרה מעולם לא הייתה בצומת.",
        source_ids=["m02"],
    )

    update = manager.apply([first, against], message_times={"m01": at(0), "m02": at(2)})

    attribute = only_entity(update).attributes["contact_status"]
    assert attribute.contradicted is True
    assert attribute.current.value == "המטרה מעולם לא הייתה בצומת."
    assert update.changes[-1].kind is StateChangeKind.CONTRADICTED


def test_a_contradiction_too_weak_to_apply_is_still_flagged(manager):
    """הסתירה נרשמת גם כשהיא אינה מנצחת - אחרת היא נעלמת בשקט."""
    verified = event("e1", confidence=0.95, new_info="המטרה בצומת.")
    weak = event(
        "e2",
        relation=InfoRelation.CONTRADICTS,
        confidence=0.55,
        new_info="לא, היא לא שם.",
        source_ids=["m02"],
    )

    update = manager.apply([verified, weak], message_times={"m01": at(0), "m02": at(2)})

    attribute = only_entity(update).attributes["contact_status"]
    assert attribute.contradicted is True
    assert attribute.current.value == "המטרה בצומת."


# --- entities ------------------------------------------------------------


def test_the_same_label_is_the_same_entity(manager):
    """שני משפטים על "מטרה אלפא" הם אותה מטרה, לא שתיים."""
    update = manager.apply(
        [event("e1"), event("e2", relation=InfoRelation.UPDATES, source_ids=["m02"])],
        message_times={"m01": at(0), "m02": at(2)},
    )

    assert len(update.state.entities) == 1


def test_whitespace_in_a_label_does_not_split_an_entity(manager):
    spaced = event("e2", entities=[{"id": None, "type": "target", "label": "מטרה  אלפא"}])

    update = manager.apply([event("e1"), spaced])

    assert len(update.state.entities) == 1


def test_the_same_label_on_a_different_type_is_a_different_entity(manager):
    """אזור בשם "צפון" וכוח בשם "צפון" אינם אותו דבר."""
    area = event(
        "e1", type="mission_change", entities=[{"id": None, "type": "area", "label": "צפון"}]
    )
    force = event(
        "e2",
        type="malfunction",
        entities=[{"id": None, "type": "force", "label": "צפון"}],
        source_ids=["m02"],
    )

    update = manager.apply([area, force], message_times={"m01": at(0), "m02": at(1)})

    assert len(update.state.entities) == 2
    assert entity_key("area", "צפון") != entity_key("force", "צפון")


def test_an_entity_id_is_derived_from_its_label_and_is_stable():
    """אותה תווית -> אותו מזהה, בכל הרצה. שחזור והשוואת גרסאות תלויים בזה."""
    first = default_entity_id("target", "מטרה אלפא", set())
    again = default_entity_id("target", "מטרה אלפא", set())

    assert first == again
    assert first.startswith("target-")
    assert normalize_label(" מטרה   אלפא ") == "מטרה אלפא"


def test_a_label_collision_gets_a_distinct_id():
    taken = {"target-mtrh-alpa"}

    assert default_entity_id("target", "מטרה אלפא", taken) == "target-mtrh-alpa-2"


def test_an_event_referring_to_a_known_entity_id_uses_it(manager):
    opened = manager.apply([event("e1")])
    known_id = only_entity(opened).id

    update = manager.apply(
        [
            event(
                "e2",
                relation=InfoRelation.UPDATES,
                entities=[{"id": known_id, "type": "target", "label": "משהו אחר לגמרי"}],
                source_ids=["m02"],
            )
        ],
        opened.state,
        message_times={"m02": at(5)},
    )

    assert len(update.state.entities) == 1


def test_an_event_with_no_entities_is_recorded_but_changes_nothing(manager):
    update = manager.apply([event("e1", type="mission_change", entities=[])])

    assert update.state.entities == {}
    assert update.skipped == [("e1", "no_entity_to_update")]


# --- state across batches and versions -----------------------------------


def test_state_carries_across_calls(manager):
    first = manager.apply([event("e1")], message_times={"m01": at(0)})

    second = manager.apply(
        [event("e2", relation=InfoRelation.UPDATES, new_info="עדכון.", source_ids=["m02"])],
        first.state,
        message_times={"m02": at(4)},
    )

    assert second.state is first.state
    assert only_entity(second).attributes["contact_status"].current.value == "עדכון."


def test_as_of_follows_the_last_thing_applied(manager):
    update = manager.apply(LOST_THEN_REGAINED, message_times=TIMES)

    assert update.state.as_of == at(4)


def test_the_change_log_carries_the_version_of_its_event(manager):
    """הגרסה הסופית מחליפה בלי למחוק: שתיהן מתויגות ברשומות."""
    update = manager.apply([event("e1", version=VersionTag.FINAL)])

    assert all(change.version is VersionTag.FINAL for change in update.changes)


def test_an_empty_batch_is_not_an_error(manager):
    update = manager.apply([])

    assert update.changes == []
    assert update.summary_trigger is None


# --- the summary trigger -------------------------------------------------


def test_a_mission_change_asks_for_a_summary(manager):
    update = manager.apply(
        [
            event(
                "e1",
                type="mission_change",
                entities=[{"id": None, "type": "area", "label": "הגזרה המזרחית"}],
                new_info="עוברים לגזרה המזרחית.",
            )
        ]
    )

    assert update.summary_trigger is SummaryTrigger.MISSION_CHANGE


def test_closing_an_entity_is_a_major_state_change(manager):
    update = manager.apply(
        [event("e1"), event("e2", relation=InfoRelation.CLOSES, source_ids=["m02"])],
        message_times={"m01": at(0), "m02": at(3)},
    )

    assert update.summary_trigger is SummaryTrigger.MAJOR_STATE_CHANGE


def test_no_change_asks_for_no_summary(manager):
    update = manager.apply([event("e1", confidence=0.1)])

    assert update.summary_trigger is None

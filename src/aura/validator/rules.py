"""The individual validation checks (רכיב 5, משימות 1-4).

Each rule is a pure function: facts in, a :class:`Rejection` or ``None``
out. Nothing here builds an ``Event``, writes a log or decides what to do
about a failure - that is :mod:`aura.validator.validator`'s job. Keeping
them separate is what makes it possible to say *why* a fixture was
rejected in a test without running the whole component.

The rules deliberately hold no knowledge of their own about the domain.
Which event types exist, which entity types each one applies to, which
fields it requires and which types contradict it all come from the
taxonomy; a rule that hardcoded "contact and loss of contact cannot both
be true" would put domain knowledge in software, where the people who own
that knowledge cannot reach it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from aura.schemas import ContextBundle, EntityRef, EventTypeDefinition, LLMEvent
from aura.validator.reasons import RejectionReason


@dataclass(frozen=True)
class Rejection:
    """למה אירוע נדחה: קוד יציב, והסבר לאדם שיבדוק אותו."""

    reason: RejectionReason
    detail: str


@dataclass
class ValidationContext:
    """מה שהוולידטור יודע על החלון שהאירועים נטענים עליו.

    ``check_entity_ids`` הוא ההודאה שהרכיב אינו יודע הכול: כל עוד מנהל
    המצב (רכיב 6) אינו קיים, חבילת הקשר עשויה להגיע בלי ``current_state``
    ואז אין מול מה לבדוק מזהה ישות. במצב כזה עדיף לדלג על הכלל במפורש
    מלדחות כל ישות מזוהה, או - גרוע מכך - להעמיד פנים שנבדקה.
    """

    flight_id: str
    window_id: str
    source_ids_in_window: frozenset[str]
    event_types: dict[str, EventTypeDefinition]
    known_entity_ids: frozenset[str] = frozenset()
    check_entity_ids: bool = False
    # שמות השדות שהטקסונומיה יכולה לדרוש, וכיצד נמדדת "נוכחות" של כל אחד.
    required_field_checks: dict[str, str] = field(
        default_factory=lambda: {"entities": "entities", "new_info": "new_info"}
    )


def context_from_bundle(
    bundle: ContextBundle,
    event_types: Iterable[EventTypeDefinition],
    *,
    known_entity_ids: Iterable[str] | None = None,
) -> ValidationContext:
    """בונה הקשר אימות מחבילת ההקשר שהוזנה למודל.

    מזהי המקור הקבילים הם בדיוק משפטי החלון - לא כל הטיסה. אירוע
    שמצביע על משפט אמיתי מחלון אחר עדיין שגוי: המודל לא ראה אותו, ולכן
    הוא לא יכול היה לגזור ממנו דבר.

    הישויות המוכרות הן אלה שבמצב המשימה ואלה שהופיעו באירועי ההיסטוריה
    שצורפו לחבילה - שני המקומות שבהם המודל *יכול היה* לפגוש מזהה ישות.
    """
    state_ids: set[str] = set()
    if bundle.current_state is not None:
        state_ids.update(bundle.current_state.entities)
    for event in bundle.relevant_history:
        state_ids.update(ref.id for ref in event.entities if ref.id)

    explicit = known_entity_ids is not None
    if explicit:
        state_ids = set(known_entity_ids or ())

    return ValidationContext(
        flight_id=bundle.flight_id,
        window_id=bundle.window_id,
        source_ids_in_window=frozenset(message.id for message in bundle.messages),
        event_types={definition.id: definition for definition in event_types},
        known_entity_ids=frozenset(state_ids),
        # נבדק רק כשיש מול מה לבדוק: מצב משימה בחבילה, או רשימה מפורשת
        # שהקורא העביר (גם רשימה ריקה מפורשת היא טענה - "אין ישויות").
        check_entity_ids=explicit or bundle.current_state is not None,
    )


# --- 1. מבנה, שדות חובה וערכים מותרים ----------------------------------


def check_event_type_is_known(event: LLMEvent, ctx: ValidationContext) -> Rejection | None:
    """סוג האירוע חייב להיות מזהה מהטקסונומיה."""
    if event.type in ctx.event_types:
        return None
    return Rejection(
        RejectionReason.UNKNOWN_EVENT_TYPE,
        f"{event.type!r} is not in the taxonomy ({', '.join(sorted(ctx.event_types))})",
    )


def check_required_fields(event: LLMEvent, ctx: ValidationContext) -> Rejection | None:
    """השדות שהסוג דורש חייבים להיות מלאים, לא רק קיימים.

    ``new_info`` ריק עובר את הסכמה (ברירת המחדל שלו היא מחרוזת ריקה),
    ולכן "יש שדה" ו"יש מידע" אינם אותו דבר, והטקסונומיה מתכוונת לשני.
    """
    definition = ctx.event_types.get(event.type)
    if definition is None:
        return None  # נבדק בכלל הקודם; אין טעם לדווח פעמיים על אותו אירוע.
    for required in definition.required_fields:
        attribute = ctx.required_field_checks.get(required, required)
        value = getattr(event, attribute, None)
        if isinstance(value, str):
            value = value.strip()
        if not value:
            return Rejection(
                RejectionReason.MISSING_REQUIRED_FIELD,
                f"{event.type!r} requires {required!r}, which is empty",
            )
    return None


def check_entity_types_are_applicable(event: LLMEvent, ctx: ValidationContext) -> Rejection | None:
    """ישות שצורפה לאירוע חייבת להיות מסוג שהאירוע חל עליו."""
    definition = ctx.event_types.get(event.type)
    if definition is None:
        return None
    applicable = set(definition.applicable_entity_types)
    for ref in event.entities:
        if ref.type not in applicable:
            return Rejection(
                RejectionReason.ENTITY_TYPE_NOT_APPLICABLE,
                f"{event.type!r} does not apply to a {ref.type.value!r} entity "
                f"({ref.label!r}); it applies to "
                f"{', '.join(sorted(t.value for t in applicable))}",
            )
    return None


# --- 2. מזהי ישויות ------------------------------------------------------


def check_entity_ids_exist(event: LLMEvent, ctx: ValidationContext) -> Rejection | None:
    """ישות עם מזהה חייבת להיות ישות שכבר קיימת.

    ``id`` ריק פירושו "ישות חדשה", וזה תקין לגמרי - הקצאת המזהה היא
    תפקידו של מנהל המצב. מה שאסור הוא מזהה שהומצא: הוא ייראה במורד
    הזרם כהפניה לישות אמיתית, ויקשר אירוע לישות שאיש לא דיבר עליה.
    """
    if not ctx.check_entity_ids:
        return None
    for ref in event.entities:
        if ref.id and ref.id not in ctx.known_entity_ids:
            return Rejection(
                RejectionReason.UNKNOWN_ENTITY_ID,
                f"entity id {ref.id!r} ({ref.label!r}) does not exist; a new entity "
                "must be sent without an id",
            )
    return None


# --- 3. מזהי מקור --------------------------------------------------------


def check_source_ids_in_window(event: LLMEvent, ctx: ValidationContext) -> Rejection | None:
    """כל מזהה מקור חייב להיות משפט מתוך החלון שהוצג למודל."""
    unknown = [sid for sid in event.source_ids if sid not in ctx.source_ids_in_window]
    if not unknown:
        return None
    return Rejection(
        RejectionReason.SOURCE_ID_NOT_IN_WINDOW,
        f"source id(s) {', '.join(repr(s) for s in unknown)} are not sentences in "
        f"window {ctx.window_id!r}",
    )


PER_EVENT_RULES = (
    check_event_type_is_known,
    check_required_fields,
    check_entity_types_are_applicable,
    check_entity_ids_exist,
    check_source_ids_in_window,
)


# --- 4. סתירות ברורות ----------------------------------------------------


def _entity_key(ref: EntityRef) -> tuple[str, str]:
    """זהות ישות לצורך השוואה: מזהה אם יש, אחרת התווית.

    שני האירועים בסתירה טיפוסית מגיעים עם ``id: null`` - הם מדברים על
    אותה מטרה חדשה - ולכן השוואה לפי מזהה בלבד הייתה מפספסת בדיוק את
    המקרה שהכלל נועד לתפוס.
    """
    if ref.id:
        return ("id", ref.id)
    return ("label", " ".join(ref.label.split()))


def _share_an_entity(left: LLMEvent, right: LLMEvent) -> bool:
    return bool({_entity_key(r) for r in left.entities} & {_entity_key(r) for r in right.entities})


def _types_contradict(left: LLMEvent, right: LLMEvent, ctx: ValidationContext) -> bool:
    left_def = ctx.event_types.get(left.type)
    right_def = ctx.event_types.get(right.type)
    if left_def is None or right_def is None:
        return False
    return right.type in left_def.contradicts or left.type in right_def.contradicts


def find_contradictions(
    events: Sequence[LLMEvent], ctx: ValidationContext
) -> dict[int, Rejection]:
    """מאתר זוגות אירועים שאינם יכולים להיות נכונים שניהם.

    שלושה תנאים ביחד, ולא אחד מהם לבדו: המשפטים שמהם נגזרו חופפים, הם
    מדברים על אותה ישות, והטקסונומיה מצהירה ששני הסוגים סותרים. פחות מכך
    היה פוסל מידע תקין - שני אירועים שונים מאותו משפט הם דבר רגיל, ושני
    דיווחים סותרים *על פני זמן* הם כל העניין של ``relation_to_previous:
    contradicts``, ולא פלט פגום.

    שני הצדדים נדחים, לא אחד מהם: שום דבר בפלט לא אומר איזה מהשניים
    נכון, ובחירה שרירותית הייתה מעבירה הלאה טענה שהמערכת אינה יכולה
    להצדיק.
    """
    rejections: dict[int, Rejection] = {}
    for i, left in enumerate(events):
        for j in range(i + 1, len(events)):
            right = events[j]
            shared = set(left.source_ids) & set(right.source_ids)
            if not shared or not _share_an_entity(left, right):
                continue
            if not _types_contradict(left, right, ctx):
                continue
            detail = (
                f"{left.type!r} and {right.type!r} are declared contradictory and both "
                f"come from sentence(s) {', '.join(sorted(shared))}"
            )
            rejections[i] = Rejection(
                RejectionReason.CONTRADICTING_EVENTS_SAME_SOURCE_SENTENCE, detail
            )
            rejections[j] = Rejection(
                RejectionReason.CONTRADICTING_EVENTS_SAME_SOURCE_SENTENCE, detail
            )
    return rejections

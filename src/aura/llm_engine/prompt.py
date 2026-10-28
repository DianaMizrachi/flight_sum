"""Building the prompt from the taxonomy and the context bundle (רכיב 4, משימה 1).

The prompt is Hebrew, because the transcript is Hebrew and the model that
has to read it was trained on Hebrew. Nothing here is hard-coded per event
type: the catalog comes from ``aura.taxonomy``, so adding an event type to
``event_types.yaml`` changes the prompt with no code change.

Two profiles exist, and the difference between them is a measurement, not
a preference:

``MINIMAL`` states the task and the output contract and nothing else.
``GUARDED`` adds the rules that keep a model from counting the same report
twice, reading a question as a report, or ignoring a negation.

Running a model under both separates two very different failures. A model
that fails under MINIMAL and passes under GUARDED did not misread the
Hebrew - it needed to be told the domain convention. A model that fails
under both either cannot read the sentence or cannot follow an
instruction, and the per-trap breakdown in ``scoring`` says which.
"""

from enum import StrEnum

from aura.schemas import ContextBundle, EventTypeDefinition


class PromptProfile(StrEnum):
    """פרופיל ההנחיות: רזה מול מונחה-כללים. ראו docstring של המודול."""

    MINIMAL = "minimal"
    GUARDED = "guarded"


_TASK = """אתה מנתח תמלול קשר של טיסת כלי טיס בלתי מאוכן בזמן אמת.
תפקידך לזהות אירועים משמעותיים בחלון המשפטים שתקבל, ולהחזיר אותם כ-JSON בלבד.

לכל אירוע שאתה מזהה:
- "type": מזהה סוג האירוע מתוך הקטלוג שלהלן, באנגלית ובדיוק כפי שהוא מופיע שם.
- "description": תיאור קצר של האירוע בעברית.
- "entities": הישויות שהאירוע חל עליהן. "label" מועתק מהטקסט כפי שנאמר,
  "type" הוא target (מטרה), area (אזור) או force (כוח שלנו).
  "id" נשאר null כשהישות מופיעה בטיסה בפעם הראשונה.
- "new_info": המידע החדש שהאירוע מוסיף, בעברית ובמשפט אחד.
- "relation_to_previous": new (מידע חדש), updates (מעדכן מידע קיים),
  contradicts (סותר מידע קיים) או closes (סוגר נושא קיים).
- "confidence": מספר בין 0 ל-1 - עד כמה אתה בטוח שהאירוע אכן דווח.
- "source_ids": מזהי המשפטים שמהם הסקת את האירוע, מתוך החלון בלבד."""

_GUARDRAILS = """כללי זיהוי:
1. ציטוט מקור: כל אירוע חייב להצביע על מזהי המשפטים שבהם הוא נאמר. אם אירוע
   אחד מתפרס על כמה משפטים, רשום את כולם ב-source_ids ואל תפצל אותו לכמה
   אירועים.
2. אישורים וחזרות: משפט שמאשר קליטה או חוזר על דיווח קודם ("מאשר", "רשמתי",
   "קיבלתי") אינו אירוע חדש. האירוע נספר פעם אחת בלבד, במשפט שבו דווח לראשונה.
3. שאלות: שאלה אינה דיווח. "האם יש לך קשר?" אינו קשר עם מטרה.
4. שלילה: קרא את השלילה. "שלילי", "אין", "לא" הופכים את משמעות המשפט -
   "אין לי קשר" הוא אובדן קשר, לא קשר.
5. הוראות מותנות: "אם יקרה X אז עשה Y" מתאר מה שעדיין לא קרה, ואינו אירוע.
   גם ביצוע של הוראה שכבר נספרה אינו אירוע נוסף.
6. דיווחי שגרה: מיקום, גובה, דלק או "הכל תקין" אינם אירועים אלא אם סוג
   מתאים קיים בקטלוג.
7. אל תמציא: אם בחלון לא דווח אף אירוע, החזר רשימת events ריקה. זו תשובה
   נכונה ולא כישלון."""

_OUTPUT = """החזר JSON יחיד ותקין במבנה {"events": [...]}, בלי טקסט לפניו ואחריו
ובלי הסברים. הערכים בתוך השדות בעברית, למעט "type" שהוא מזהה מהקטלוג."""


def format_event_catalog(event_types: list[EventTypeDefinition]) -> str:
    """קטלוג סוגי האירועים כפי שהוא מוצג למודל."""
    blocks = []
    for definition in event_types:
        applicable = ", ".join(entity.value for entity in definition.applicable_entity_types)
        lines = [
            f'- {definition.id} ("{definition.label_he}")',
            f"  מתי: {definition.description_he.strip()}",
            f"  חל על ישויות מסוג: {applicable}",
        ]
        if definition.required_fields:
            lines.append(f"  שדות חובה: {', '.join(definition.required_fields)}")
        for example in definition.examples_he:
            lines.append(f"  דוגמה: {example}")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def build_system_prompt(
    event_types: list[EventTypeDefinition],
    profile: PromptProfile = PromptProfile.GUARDED,
) -> str:
    """הנחיות המערכת: המשימה, הקטלוג, הכללים (לפי הפרופיל) ומבנה הפלט."""
    sections = [_TASK, "קטלוג סוגי האירועים:\n" + format_event_catalog(event_types)]
    if profile is PromptProfile.GUARDED:
        sections.append(_GUARDRAILS)
    sections.append(_OUTPUT)
    return "\n\n".join(sections)


def build_user_prompt(bundle: ContextBundle) -> str:
    """ההקשר עצמו: מצב המשימה, התקציר, ההיסטוריה הרלוונטית, ואז החלון."""
    sections: list[str] = []

    if bundle.mission_settings:
        settings = "\n".join(f"- {key}: {value}" for key, value in bundle.mission_settings.items())
        sections.append(f"הגדרות המשימה:\n{settings}")

    if bundle.current_state is not None and bundle.current_state.entities:
        lines = []
        for entity in bundle.current_state.entities.values():
            attributes = ", ".join(
                f"{name}: {attribute.current.value}"
                for name, attribute in entity.attributes.items()
            )
            suffix = f" ({attributes})" if attributes else ""
            lines.append(
                f"- {entity.label} [{entity.type.value}, {entity.status.value}]{suffix}"
            )
        sections.append("מצב המשימה הנוכחי:\n" + "\n".join(lines))

    if bundle.last_summary is not None:
        sections.append(f"תקציר הטיסה עד כה:\n{bundle.last_summary.text}")

    if bundle.relevant_history:
        lines = [
            f"- {event.type}: {event.new_info or event.description}"
            f" (משפטי מקור: {', '.join(event.source_ids)})"
            for event in bundle.relevant_history
        ]
        sections.append("אירועים קודמים רלוונטיים:\n" + "\n".join(lines))

    window = "\n".join(
        f"[{message.id}] ({message.source}) {message.text}" for message in bundle.messages
    )
    sections.append(
        "חלון המשפטים לניתוח - ציטוט ב-source_ids חייב להיות אחד המזהים שבסוגריים:\n" + window
    )
    return "\n\n".join(sections)

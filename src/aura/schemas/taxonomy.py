from pydantic import BaseModel, ConfigDict, Field

from aura.schemas.enums import EntityType


class EventTypeDefinition(BaseModel):
    """הגדרת סוג אירוע בטקסונומיה (שלב 0, משימה 3).

    משמש גם כתוכן ל-prompt של מנוע ה-LLM (רכיב 4): ``label_he``,
    ``description_he`` ו-``examples_he`` מוזנים למודל כהגדרת הסוג.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, description="מזהה יציב של סוג האירוע, למשל loss_of_contact")
    label_he: str = Field(min_length=1)
    description_he: str = Field(min_length=1)
    applicable_entity_types: list[EntityType] = Field(min_length=1)
    required_fields: list[str] = Field(
        default_factory=list,
        description="שדות חובה נוספים על הסכמה הבסיסית, לאימות ברכיב 5",
    )
    contradicts: list[str] = Field(
        default_factory=list,
        description=(
            "סוגי אירוע שאינם יכולים לדור בכפיפה אחת עם זה על אותה ישות ומאותו "
            "משפט מקור. רכיב 5 משתמש בזה לזיהוי סתירות; הרשימה מוצהרת כאן ולא "
            "מקודדת בוולידטור, כי 'מה סותר מה' הוא ידע תחומי ולא כלל תוכנה."
        ),
    )
    state_attribute: str | None = Field(
        default=None,
        description=(
            "המאפיין במצב המשימה שסוג האירוע הזה מדבר עליו (רכיב 6). שני סוגים "
            "שמדברים על אותו היבט - 'קשר עם מטרה' ו'אובדן קשר' - חולקים מאפיין "
            "אחד, וכך 'הקשר חודש' מחליף את 'הקשר אבד' במקום לשבת לידו."
        ),
    )
    examples_he: list[str] = Field(default_factory=list)

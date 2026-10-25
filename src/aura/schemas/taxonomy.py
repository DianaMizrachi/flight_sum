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
    examples_he: list[str] = Field(default_factory=list)

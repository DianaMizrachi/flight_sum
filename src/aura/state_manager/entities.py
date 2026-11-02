"""Resolving an event's entity references to entities in the state (רכיב 6).

An ``EntityRef`` arrives in one of two shapes, and the difference is the
whole job here. With an ``id`` it names an entity that already exists -
component 5 has already refused the ones that do not. Without an ``id``
it is the model saying "a new one", and somebody has to decide whether
that is true: a second sentence about "מטרה אלפא" is the *same* target,
not a second one, and the model has no way to tell us so.

So matching falls back to the label. That is a judgement call and it can
be wrong in both directions - two real targets a crew happened to call
the same thing would merge, and one target named two ways would split -
but the alternative is a new entity per sentence, which makes mission
state useless within a minute. The label is normalised before comparing,
because "מטרה אלפא" and "מטרה  אלפא" are the same words.
"""

from __future__ import annotations

import re
import unicodedata

from aura.schemas import Entity, EntityRef, EntityType, MissionState

# תעתיק מצומצם לעברית, לשימוש במזהה ישות. אינו מתיימר להיות תעתיק נכון -
# הוא צריך להיות יציב, קריא-למחצה וחסר התנגשויות סבירות, ותו לא.
_HEBREW_TRANSLITERATION = {
    "א": "a", "ב": "b", "ג": "g", "ד": "d", "ה": "h", "ו": "v", "ז": "z",
    "ח": "ch", "ט": "t", "י": "y", "כ": "k", "ך": "k", "ל": "l", "מ": "m",
    "ם": "m", "נ": "n", "ן": "n", "ס": "s", "ע": "a", "פ": "p", "ף": "f",
    "צ": "tz", "ץ": "tz", "ק": "k", "ר": "r", "ש": "sh", "ת": "t",
}


def normalize_label(label: str) -> str:
    """תווית מנורמלת להשוואה: NFC, רווחים מכווצים, בלי ניקוד."""
    text = unicodedata.normalize("NFC", label)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.split())


def entity_key(entity_type: EntityType | str, label: str) -> tuple[str, str]:
    """המפתח שלפיו ישות מזוהה כ"אותה ישות" כשאין מזהה.

    הסוג הוא חלק מהמפתח: אזור בשם "צפון" וכוח בשם "צפון" אינם אותו דבר.

    ``EntityType`` הוא ``StrEnum``, ולכן הסוג מתקבל גם כמחרוזת - קורא
    שמחזיק ``"target"`` ולא ``EntityType.TARGET`` מתכוון לאותו דבר,
    ואין סיבה שהוא ייפול כאן.
    """
    return (EntityType(entity_type).value, normalize_label(label))


def slugify_label(label: str) -> str:
    """תווית -> חלק קריא במזהה. עברית מתועתקת, שאר התווים נשמרים."""
    normalized = normalize_label(label).lower()
    out = []
    for char in normalized:
        if char in _HEBREW_TRANSLITERATION:
            out.append(_HEBREW_TRANSLITERATION[char])
        elif char.isalnum():
            out.append(char)
        elif char in " -_":
            out.append("-")
    slug = re.sub(r"-+", "-", "".join(out)).strip("-")
    return slug or "unnamed"


def default_entity_id(entity_type: EntityType | str, label: str, taken: set[str]) -> str:
    """``target-mtrh-alpha`` - יציב לאותה תווית, וייחודי בתוך הטיסה.

    מזהה שנגזר מהתווית עדיף על מונה רץ: הוא אותו מזהה בכל הרצה על אותה
    טיסה, ולכן שחזור מצב (שלב 7) והשוואה בין גרסת זמן אמת לסופית (שלב
    11) משווים תפוחים לתפוחים. ההתנגשות הנדירה נפתרת בסיומת מספרית.
    """
    base = f"{EntityType(entity_type).value}-{slugify_label(label)}"
    if base not in taken:
        return base
    index = 2
    while f"{base}-{index}" in taken:
        index += 1
    return f"{base}-{index}"


class EntityResolver:
    """ממפה הפניות ישות במצב, ומקצה מזהים לישויות חדשות."""

    def __init__(self, state: MissionState) -> None:
        self.state = state
        self._by_key: dict[tuple[str, str], str] = {}
        for entity_id, entity in state.entities.items():
            self._by_key[entity_key(entity.type, entity.label)] = entity_id

    def resolve(self, ref: EntityRef) -> tuple[str, bool]:
        """מחזיר ``(entity_id, is_new)`` להפניה אחת.

        מזהה מפורש גובר תמיד: רכיב 5 כבר ודא שהוא קיים, והמודל התכוון
        לישות מסוימת. בלעדיו - התאמה לפי סוג+תווית, ורק אם גם זה נכשל
        נוצרת ישות חדשה.
        """
        if ref.id and ref.id in self.state.entities:
            return ref.id, False

        key = entity_key(ref.type, ref.label)
        existing = self._by_key.get(key)
        if existing is not None:
            return existing, False

        new_id = ref.id or default_entity_id(ref.type, ref.label, set(self.state.entities))
        self._by_key[key] = new_id
        return new_id, True

    def ensure(self, ref: EntityRef) -> tuple[Entity, bool]:
        """מחזיר את הישות מהמצב, ויוצר אותה אם אינה קיימת."""
        entity_id, is_new = self.resolve(ref)
        entity = self.state.entities.get(entity_id)
        if entity is None:
            entity = Entity(id=entity_id, type=ref.type, label=normalize_label(ref.label))
            self.state.entities[entity_id] = entity
            return entity, True
        return entity, is_new

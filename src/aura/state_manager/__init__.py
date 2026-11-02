"""רכיב 6 - מנהל מצב המשימה (שלב 6).

אירועים מאומתים נכנסים, מצב משימה מעודכן יוצא, ולצדו רשומת
``StateChange`` לכל שינוי - append-only, כדי ששלב 7 יוכל לשחזר את המצב
בכל נקודת זמן בהרצה חוזרת שלהן.
"""

from aura.state_manager.entities import (
    EntityResolver,
    default_entity_id,
    entity_key,
    normalize_label,
    slugify_label,
)
from aura.state_manager.manager import (
    FALLBACK_ATTRIBUTE,
    MissionStateManager,
    StateUpdate,
)

__all__ = [
    "FALLBACK_ATTRIBUTE",
    "EntityResolver",
    "MissionStateManager",
    "StateUpdate",
    "default_entity_id",
    "entity_key",
    "normalize_label",
    "slugify_label",
]

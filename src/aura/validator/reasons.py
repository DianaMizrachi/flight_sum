"""The reasons output validation rejects an event (רכיב 5).

Every rejection carries one of these codes, and the code is the part that
is counted, filtered and compared - the free-text explanation lives in
``RejectedEvent.detail``. Keeping the two apart is what lets a fixture
state ``reason: contradicting_events_same_source_sentence`` and have the
test compare it exactly, instead of matching a prefix of a sentence that
someone will reword.
"""

from enum import StrEnum


class RejectionReason(StrEnum):
    """קודי הדחייה של רכיב 5, לפי סדר הבדיקות בשלב 5."""

    # הפלט כולו אינו בר-פענוח: לא JSON, או לא תואם לסכמת ``LLMResult``.
    NOT_JSON = "not_json"
    SCHEMA_INVALID = "schema_invalid"

    # ערכים מותרים: סוג האירוע חייב להיות מהטקסונומיה, והשדות שהסוג
    # דורש חייבים להיות מלאים.
    UNKNOWN_EVENT_TYPE = "unknown_event_type"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    ENTITY_TYPE_NOT_APPLICABLE = "entity_type_not_applicable"

    # מזהים: ישות שהמודל מפנה אליה חייבת להתקיים או להיות מסומנת כחדשה,
    # וכל מזהה מקור חייב להיות משפט שנמצא בחלון.
    UNKNOWN_ENTITY_ID = "unknown_entity_id"
    SOURCE_ID_NOT_IN_WINDOW = "source_id_not_in_window"

    # סתירה ברורה: אותו משפט מקור מייצר שתי טענות שאינן יכולות להתקיים יחד.
    CONTRADICTING_EVENTS_SAME_SOURCE_SENTENCE = "contradicting_events_same_source_sentence"

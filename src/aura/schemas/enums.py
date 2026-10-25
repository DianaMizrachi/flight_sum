"""Shared enums for all cross-component contracts.

Hebrew labels in comments mirror the terms used in
flight-transcript-work-plan.md so the two stay traceable to each other.
"""

from enum import StrEnum


class VersionTag(StrEnum):
    """תגית גרסה: טיוטת זמן אמת מול גרסה סופית אחרי מעבר התיקון."""

    REALTIME = "realtime"
    FINAL = "final"


class EntityType(StrEnum):
    """סוגי ישויות במצב המשימה: מטרה, אזור, כוח."""

    TARGET = "target"
    AREA = "area"
    FORCE = "force"


class EntityStatus(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"


class CertaintyLevel(StrEnum):
    """רמת ודאות של מאפיין במצב המשימה."""

    VERIFIED = "verified"
    REPORTED = "reported"
    UNCERTAIN = "uncertain"


class InfoRelation(StrEnum):
    """יחס בין אירוע חדש למידע קודם: חדש / מעדכן / סותר / סוגר."""

    NEW = "new"
    UPDATES = "updates"
    CONTRADICTS = "contradicts"
    CLOSES = "closes"


class WindowCloseReason(StrEnum):
    """תנאי סגירת חלון משפטים (רכיב 3)."""

    SILENCE_TIMEOUT = "silence_timeout"
    SPEAKER_CHANGE = "speaker_change"
    CHANNEL_CHANGE = "channel_change"
    MAX_DURATION = "max_duration"
    MAX_SENTENCES = "max_sentences"
    FLIGHT_END = "flight_end"


class MessageStatus(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    DEAD_LETTER = "dead_letter"


class ValidationStatus(StrEnum):
    """תוצאת שלב האימות (רכיב 5): תקין / תוקן בניסיון חזרה / לבדיקה ידנית."""

    OK = "ok"
    RETRIED = "retried"
    MANUAL_REVIEW = "manual_review"


class ReconciliationAction(StrEnum):
    """סיווג אירוע במעבר התיקון (רכיב 11): אושר / תוקן / נוסף / בוטל."""

    CONFIRMED = "confirmed"
    CORRECTED = "corrected"
    ADDED = "added"
    CANCELLED = "cancelled"


class SummaryTrigger(StrEnum):
    """טריגרים להפעלת מנגנון התקצירים (רכיב 8)."""

    MISSION_CHANGE = "mission_change"
    SIGNIFICANT_EVENT = "significant_event"
    MAJOR_STATE_CHANGE = "major_state_change"
    THRESHOLD_EXCEEDED = "threshold_exceeded"
    POST_FLIGHT = "post_flight"

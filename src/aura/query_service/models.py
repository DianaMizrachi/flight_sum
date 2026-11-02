"""What a query returns (שלב 9).

The stage has a pass criterion that is about shape rather than content:
*"an internal interface that exposes neither the LLM engine's structures
nor the update mechanism's"*. This module is that boundary. Nothing here
is an :class:`~aura.schemas.event.Event` or a
:class:`~aura.schemas.state_change.StateChange`; every query returns one
of these read models instead, built from them.

That costs a translation layer, and it buys two things.

The first is that the stored types carry fields that are true but not
answers. ``Event.window_id`` says which batch of sentences happened to be
sent to the model together - an artefact of component 3's batching rules,
not a fact about the flight. ``Event.confidence`` is a float the model
emitted; a reader shown "0.82" will reasonably believe the system
measured something to two decimal places, and it did not. So the window
id is dropped and the confidence appears only as the certainty *level*
that the mission state itself records - the same three words, from the
same thresholds, as :func:`aura.state_manager.manager.certainty_for`.

The second is that ``StateChange`` is the update mechanism's own ledger.
Reading it is how state is rebuilt, but a caller asking "what is the
state" should not have to know that the answer is a replay, any more than
it should know which windows the events arrived in. :class:`StateView`
answers the question; how it was derived stays inside.

One field is here *because* it is user-facing rather than internal:
``spoken_at``. Events carry ``created_at``, the moment the validator
built them, and for a flight processed after the fact that is the same
instant for every event in it. The time a reader wants is when the
sentence was said, which lives in the raw store - so the service resolves
it and every model that has a time exposes the spoken one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class SourceSentence:
    """משפט תמלול כפי שהוא מוצג לקורא: "מאירוע -> המקור שלו".

    ``missing`` מסמן מזהה מקור שהאירוע מצביע עליו אך אינו נמצא במאגר
    הגולמי. זה לא אמור לקרות - הוולידטור (שלב 5) דוחה אירוע שמצביע על
    משפט שאינו בחלון - ולכן עדיף להחזיר את החוסר במפורש מלהשמיט את
    השורה בשקט, שהיה הופך פער נתונים לאירוע עם פחות מקורות.
    """

    message_id: str
    flight_id: str
    spoken_at: datetime | None = None
    source: str | None = None
    channel: str | None = None
    text: str | None = None
    missing: bool = False


@dataclass(frozen=True)
class EntityMention:
    """ישות כפי שאירוע מזכיר אותה.

    ``id`` יכול להיות ``None``: המודל מציג ישות חדשה בשם בלבד, ומנהל
    המצב הוא שמקצה לה מזהה - והוא אינו כותב אותו בחזרה לאירוע השמור.
    """

    type: str
    label: str
    id: str | None = None


@dataclass(frozen=True)
class TimelineEntry:
    """אירוע בציר הזמן של הטיסה.

    אין כאן ``window_id`` ואין ציון ביטחון מספרי - ראו הסבר במודול.
    """

    event_id: str
    flight_id: str
    type: str
    description: str
    new_info: str
    relation: str
    certainty: str
    source_ids: tuple[str, ...]
    version: str
    recorded_at: datetime
    spoken_at: datetime | None = None
    entities: tuple[EntityMention, ...] = ()
    related_event_id: str | None = None
    reconciliation: str | None = None
    reconciliation_reason: str | None = None
    reconciles_event_id: str | None = None

    @property
    def at(self) -> datetime:
        """הזמן שמציבים אותו על הציר: זמן האמירה אם ידוע."""
        return self.spoken_at or self.recorded_at

    @property
    def text(self) -> str:
        return self.new_info or self.description


@dataclass(frozen=True)
class AttributeValue:
    """ערך בודד של מאפיין ישות, נוכחי או היסטורי."""

    value: str
    certainty: str
    updated_at: datetime
    source_event_id: str


@dataclass(frozen=True)
class AttributeView:
    """מאפיין ישות: הערך הנוכחי, ומה שהיה לפניו.

    ``contradicted`` נשמר כשדה נפרד ולא נגזר מההיסטוריה: סתירה היא
    טענה על *איכות* המידע ("נאמרו שני דברים שאינם יכולים להתקיים
    יחד"), ועדכון רגיל שמחליף ערך אינו סתירה. שלב 10 מבדיל ביניהם
    חזותית, ולכן ההבדל חייב להגיע עד כאן.
    """

    name: str
    current: AttributeValue
    contradicted: bool = False
    history: tuple[AttributeValue, ...] = ()


@dataclass(frozen=True)
class EntityView:
    """ישות במצב המשימה, עם מאפייניה."""

    id: str
    type: str
    label: str
    status: str
    attributes: tuple[AttributeView, ...] = ()
    closed_at: datetime | None = None
    closed_by_event_id: str | None = None

    @property
    def active(self) -> bool:
        return self.status == "active"

    def attribute(self, name: str) -> AttributeView | None:
        for attribute in self.attributes:
            if attribute.name == name:
                return attribute
        return None


@dataclass(frozen=True)
class StateView:
    """מצב המשימה בנקודת זמן - עכשיו, או בעבר.

    ``as_of`` הוא נקודת הזמן שהמצב משקף, ו-``rebuilt`` אומר אם הוא
    שוחזר מרצף השינויים או נקרא מתמונת המצב השמורה. הקורא אינו צריך
    את ההבדל כדי להבין את התשובה, אבל הוא כן צריך אותו כדי לדעת
    שהתשובה לנקודת זמן בעבר אינה "מה שמוצג במסך עכשיו".
    """

    flight_id: str
    version: str
    as_of: datetime
    entities: tuple[EntityView, ...] = ()
    rebuilt: bool = False

    @property
    def active(self) -> tuple[EntityView, ...]:
        return tuple(e for e in self.entities if e.active)

    @property
    def closed(self) -> tuple[EntityView, ...]:
        return tuple(e for e in self.entities if not e.active)

    def entity(self, entity_id: str) -> EntityView | None:
        for entity in self.entities:
            if entity.id == entity_id:
                return entity
        return None


@dataclass(frozen=True)
class SummaryView:
    """תקציר טיסה בגרסה ובמספר גרסה נתונים.

    ``covers_up_to`` הוא זמן האמירה של המשפט האחרון שהתקציר מכסה,
    שנפתר מהמאגר הגולמי. ``covers_up_to_message_id`` לבדו הוא מזהה
    שאין לקורא מה לעשות איתו.
    """

    flight_id: str
    version: str
    version_number: int
    text: str
    trigger: str
    created_at: datetime
    covers_up_to_message_id: str | None = None
    covers_up_to: datetime | None = None
    entities_covered: tuple[str, ...] = ()


@dataclass(frozen=True)
class EventPair:
    """אירוע גרסת זמן האמת מול מה שנעשה לו בגרסה הסופית."""

    action: str
    realtime: TimelineEntry | None = None
    final: TimelineEntry | None = None
    reason: str | None = None


@dataclass(frozen=True)
class VersionDiff:
    """ההבדל בין גרסת זמן האמת לגרסה הסופית.

    ``final_exists=False`` הוא התשובה הנכונה ברוב החיים של טיסה - מעבר
    התיקון (שלב 11) רץ בסופה - ולכן היא תשובה ולא שגיאה. השאילתה
    מוגדרת כאן ועובדת כבר עכשיו, כדי שהגרסה הסופית תהיה ניתנת להצגה
    ברגע שיהיה מי שיכתוב אותה.
    """

    flight_id: str
    final_exists: bool
    realtime_count: int = 0
    final_count: int = 0
    confirmed: tuple[EventPair, ...] = ()
    corrected: tuple[EventPair, ...] = ()
    added: tuple[EventPair, ...] = ()
    cancelled: tuple[EventPair, ...] = ()
    unmatched: tuple[EventPair, ...] = field(default=())

    @property
    def changed(self) -> tuple[EventPair, ...]:
        """כל מה שאינו "אושר כמו שהוא"."""
        return self.corrected + self.added + self.cancelled

    @property
    def identical(self) -> bool:
        return self.final_exists and not self.changed and not self.unmatched

"""Grouping sentences into windows (רכיב 3, שלב 3 - משימה 1).

A window closes on one of the four conditions the plan names: silence above
the threshold, a speaker or channel change, maximum duration, or maximum
sentence count. Consecutive windows overlap by 1-2 sentences so context is
not cut mid-exchange. Every threshold lives in ``aura.config``.

Two semantics worth stating, because the plan leaves them open:

* ``max_sentences`` counts the overlap as well, since the model sees those
  sentences too; ``max_duration_seconds`` is measured only over the window's
  *own* sentences, so a long silence carried in from the previous window
  cannot instantly close the new one.
* A sentence that arrives late and belongs to a window that has already
  closed is reported as an orphan rather than forced into the open window -
  it keeps its original timestamp, and the stage 11 reconciler sees it when
  it re-runs the full transcript. ``include_late_messages`` flips that.
"""

from collections.abc import Iterable
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from aura.config import DEFAULT_SETTINGS, WindowConfig
from aura.schemas import Message, WindowCloseReason


class SentenceWindow(BaseModel):
    """חלון משפטים אחד, כפי שהוא נמסר לבניית חבילת ההקשר.

    ``overlap_count`` הוא מספר המשפטים בראש החלון שנגררו מהחלון הקודם -
    הם הקשר, ולא תוכן חדש.
    """

    model_config = ConfigDict(extra="forbid")

    window_id: str = Field(min_length=1)
    flight_id: str = Field(min_length=1)
    messages: list[Message] = Field(min_length=1)
    close_reason: WindowCloseReason
    overlap_count: int = Field(default=0, ge=0)

    @property
    def own_messages(self) -> list[Message]:
        """המשפטים החדשים בחלון, בלי החפיפה שנגררה מקודמו."""
        return self.messages[self.overlap_count :]

    @property
    def started_at(self) -> datetime:
        return self.messages[0].timestamp

    @property
    def ended_at(self) -> datetime:
        return self.messages[-1].timestamp

    @property
    def duration_seconds(self) -> float:
        own = self.own_messages
        if not own:
            return 0.0
        return (own[-1].timestamp - own[0].timestamp).total_seconds()

    def text(self) -> str:
        return "\n".join(f"[{m.source}] {m.text}" for m in self.messages)


class WindowAssembler:
    """בונה חלונות מהודעות שנכנסות אחת-אחת (זרימת זמן אמת).

    ``push`` מחזיר את החלונות שנסגרו בגלל ההודעה הזו - לרוב רשימה ריקה.
    בסוף הטיסה קוראים ל-``flush`` כדי לסגור את מה שנותר פתוח.
    """

    def __init__(self, flight_id: str, config: WindowConfig | None = None) -> None:
        self.flight_id = flight_id
        self.config = config if config is not None else DEFAULT_SETTINGS.window
        self.orphans: list[Message] = []
        self._current: list[Message] = []
        self._overlap_count = 0
        self._window_index = 0
        self._closed_through: datetime | None = None

    # --- זרימה ----------------------------------------------------------

    def push(self, message: Message) -> list[SentenceWindow]:
        if message.flight_id != self.flight_id:
            raise ValueError(f"message {message.id} belongs to flight {message.flight_id}")

        if self._is_late(message):
            self.orphans.append(message)
            if not self.config.include_late_messages:
                return []

        closed: list[SentenceWindow] = []
        reason = self._close_reason_for(message)
        if reason is not None:
            closed.append(self._close(reason))

        self._current.append(message)
        if len(self._current) >= self._effective_max_sentences():
            closed.append(self._close(WindowCloseReason.MAX_SENTENCES))
        return closed

    def flush(
        self, reason: WindowCloseReason = WindowCloseReason.FLIGHT_END
    ) -> SentenceWindow | None:
        """סגירת החלון הפתוח, אם יש בו תוכן חדש."""
        if not self._has_own_content():
            return None
        return self._close(reason)

    @property
    def open_messages(self) -> list[Message]:
        return list(self._current)

    # --- כללי הסגירה ----------------------------------------------------

    def _close_reason_for(self, message: Message) -> WindowCloseReason | None:
        if not self._has_own_content():
            # החלון מכיל חפיפה בלבד - אין מה לסגור, אחרת ייווצר חלון ריק.
            return None

        last = self._current[-1]
        silence = (message.timestamp - last.timestamp).total_seconds()
        if silence > self.config.silence_threshold_seconds:
            return WindowCloseReason.SILENCE_TIMEOUT
        if self.config.close_on_speaker_change and message.source != last.source:
            return WindowCloseReason.SPEAKER_CHANGE
        if self.config.close_on_channel_change and message.channel != last.channel:
            return WindowCloseReason.CHANNEL_CHANGE

        own_start = self._current[self._overlap_count].timestamp
        if (message.timestamp - own_start).total_seconds() > self.config.max_duration_seconds:
            return WindowCloseReason.MAX_DURATION
        return None

    def _close(self, reason: WindowCloseReason) -> SentenceWindow:
        self._window_index += 1
        # בתוך החלון הסדר הוא לפי החותמת המקורית, גם אם ההודעות נכנסו
        # מעורבבות - מנהל המצב (שלב 6) מעבד אירועים לפי סדר המשפטים.
        ordered = sorted(self._current, key=lambda m: (m.timestamp, m.id))
        window = SentenceWindow(
            window_id=f"{self.flight_id}-w{self._window_index:03d}",
            flight_id=self.flight_id,
            messages=ordered,
            close_reason=reason,
            overlap_count=self._overlap_count,
        )
        self._closed_through = ordered[-1].timestamp

        overlap = min(self.config.overlap_sentences, len(self._current))
        carry = self._current[-overlap:] if overlap else []
        self._current = list(carry)
        self._overlap_count = len(carry)
        return window

    def _has_own_content(self) -> bool:
        return len(self._current) > self._overlap_count

    def _effective_max_sentences(self) -> int:
        # חלון חייב להכיל לפחות משפט חדש אחד מעל החפיפה.
        return max(self.config.max_sentences, self._overlap_count + 1)

    def _is_late(self, message: Message) -> bool:
        """האם ההודעה שייכת לחלון שכבר נסגר.

        ההשוואה היא מול תחילת התוכן של החלון הפתוח: הודעה שזמנה מוקדם
        ממנה הייתה נכנסת לחלון קודם לו הגיעה בזמן. כשאין חלון פתוח,
        ההשוואה היא מול סוף החלון האחרון שנסגר.
        """
        if self._closed_through is None:
            return False
        if self._has_own_content():
            own_start = self._current[self._overlap_count].timestamp
            return message.timestamp < own_start
        return message.timestamp < self._closed_through


def assemble_windows(
    messages: Iterable[Message],
    config: WindowConfig | None = None,
    close_reason: WindowCloseReason = WindowCloseReason.FLIGHT_END,
) -> list[SentenceWindow]:
    """חלונות מתוך רצף הודעות שלם - למשל טיסה שנטענה מהמאגר הגולמי.

    ההודעות ממוינות לפי החותמת המקורית לפני החלוקה, ולכן הודעה שהגיעה
    באיחור משויכת לחלון שלפי זמנה ואין כאן יתומים. זו הצורה שבה מעבר
    התיקון (שלב 11) ירוץ על התמלול המלא.
    """
    ordered = sorted(messages, key=lambda m: (m.timestamp, m.id))
    if not ordered:
        return []

    flight_ids = {m.flight_id for m in ordered}
    if len(flight_ids) > 1:
        raise ValueError(f"messages span several flights: {sorted(flight_ids)}")

    assembler = WindowAssembler(ordered[0].flight_id, config)
    windows: list[SentenceWindow] = []
    for message in ordered:
        windows.extend(assembler.push(message))
    tail = assembler.flush(close_reason)
    if tail is not None:
        windows.append(tail)
    return windows

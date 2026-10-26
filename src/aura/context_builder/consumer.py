"""Draining the internal queue into windows (שלב 3).

Stage 1 handed accepted messages to the queue and nothing read them. This
is the first consumer: it pulls what the queue holds, feeds the per-flight
assemblers and returns the windows that closed - with the bundles built, if
a builder is supplied. Wiring it to run continuously belongs to stage 6,
where the full 1->6 pipeline is connected.
"""

from aura.context_builder.builder import ContextBuilder
from aura.context_builder.windowing import SentenceWindow, WindowAssembler
from aura.ingestion.queue import InMemoryQueue
from aura.schemas import ContextBundle, Message, WindowCloseReason


class QueueWindowPump:
    """צורכן התור: הודעות -> חלונות, עם assembler נפרד לכל טיסה."""

    def __init__(self, queue: InMemoryQueue, builder: ContextBuilder | None = None) -> None:
        self.queue = queue
        self.builder = builder
        self._assemblers: dict[str, WindowAssembler] = {}

    def drain(self) -> list[SentenceWindow]:
        """חלונות שנסגרו מההודעות שהיו בתור ברגע הקריאה."""
        closed: list[SentenceWindow] = []
        for message in self.queue.drain():
            closed.extend(self.push(message))
        return closed

    def push(self, message: Message) -> list[SentenceWindow]:
        assembler = self._assemblers.get(message.flight_id)
        if assembler is None:
            assembler = WindowAssembler(message.flight_id)
            self._assemblers[message.flight_id] = assembler
        return assembler.push(message)

    def close_flight(
        self, flight_id: str, reason: WindowCloseReason = WindowCloseReason.FLIGHT_END
    ) -> SentenceWindow | None:
        """סגירת החלון הפתוח של טיסה שהסתיימה."""
        assembler = self._assemblers.get(flight_id)
        return None if assembler is None else assembler.flush(reason)

    def bundles(self, windows: list[SentenceWindow]) -> list[ContextBundle]:
        """בניית חבילות ההקשר לחלונות שנסגרו (דורש builder)."""
        if self.builder is None:
            raise ValueError("no ContextBuilder was supplied")
        return [self.builder.build(window) for window in windows]

    @property
    def orphans(self) -> list[Message]:
        """הודעות שהגיעו באיחור אחרי שהחלון שלהן נסגר, בכל הטיסות."""
        return [m for assembler in self._assemblers.values() for m in assembler.orphans]

"""The internal queue the ingestion service hands accepted messages to
(שלב 1, משימה 5).

Only the Protocol matters to the rest of the pipeline; the in-memory
implementation is what stage 1 and the tests run against. A Redis-backed
implementation (see docker-compose.yml) lands together with its first real
consumer, the context builder in stage 3.
"""

from typing import Protocol

from aura.schemas import Message


class MessageQueue(Protocol):
    """תור פנימי בין הקליטה (רכיב 1) לעיבוד האסינכרוני (רכיב 3 והלאה)."""

    def publish(self, message: Message) -> None: ...


class InMemoryQueue:
    """תור בזיכרון, FIFO - מספיק לשלב 1, שבו עוד אין צורך בעמידות."""

    def __init__(self) -> None:
        self._messages: list[Message] = []

    def publish(self, message: Message) -> None:
        self._messages.append(message)

    def drain(self) -> list[Message]:
        """שליפת כל ההודעות שבתור וריקונו."""
        messages = self._messages
        self._messages = []
        return messages

    @property
    def published(self) -> list[Message]:
        """הצצה לתור בלי לרוקן אותו (לבדיקות)."""
        return list(self._messages)

    def __len__(self) -> int:
        return len(self._messages)

"""Token accounting for the context bundle (שלב 3, משימה 4).

The counter is behind a Protocol because stage 3 has no model yet: the
heuristic here estimates Hebrew subword tokens from character count, and
stage 4 swaps in the tokenizer of whichever model is chosen, without
changing the builder. The heuristic is deliberately pessimistic - it adds a
per-item overhead for the formatting around every sentence and every
history entry - so a bundle that fits the estimate also fits the real
budget.
"""

from collections.abc import Iterable
from math import ceil
from typing import Protocol


class TokenCounter(Protocol):
    """מונה טוקנים. רכיב 4 יחליף את המימוש בטוקנייזר של המודל."""

    def count(self, text: str) -> int: ...


class HeuristicTokenCounter:
    """אומדן טוקנים לפי אורך הטקסט, בלי תלות בספק מודל.

    ``chars_per_token`` נמוך = אומדן שמרני (יותר טוקנים מהמציאות), וזה
    הכיוון הבטוח: עדיף לקצץ היסטוריה מיותרת מאשר לחרוג מהתקציב.
    """

    def __init__(self, chars_per_token: float = 2.5, per_item_overhead: int = 4) -> None:
        if chars_per_token <= 0:
            raise ValueError("chars_per_token must be positive")
        self.chars_per_token = chars_per_token
        self.per_item_overhead = per_item_overhead

    def count(self, text: str) -> int:
        if not text:
            return 0
        return ceil(len(text) / self.chars_per_token)

    def count_items(self, texts: Iterable[str]) -> int:
        """סכימת פריטים נפרדים, כל אחד עם תקורת העיטוף שלו ב-prompt."""
        return sum(self.count(text) + self.per_item_overhead for text in texts)

"""Assembling the context bundle (רכיב 3, שלב 3 - משימות 2 ו-4).

The bundle is what component 4 sends to the model: the window itself, the
current mission state, the last summary, the mission settings, the event
type catalog, and the historical events relevant to the entities mentioned
in the window.

Trimming follows the plan's priority exactly - the window, the summary and
the state are always kept, and history is cut first, oldest entry first.
If the irreducible core alone exceeds the budget, the bundle is still
returned and ``token_budget.used_estimate`` exceeds ``budget``, so the
overrun is visible to component 4 instead of being hidden by silently
dropping the sentences under analysis.
"""

from aura.config import DEFAULT_SETTINGS, Settings
from aura.context_builder.history import EventHistoryProvider, select_relevant_events
from aura.context_builder.tokens import HeuristicTokenCounter, TokenCounter
from aura.context_builder.windowing import SentenceWindow
from aura.schemas import (
    ContextBundle,
    Event,
    EventTypeDefinition,
    MissionState,
    Summary,
    TokenBudgetInfo,
)
from aura.taxonomy import load_event_taxonomy


class ContextBuilder:
    """בונה חבילת הקשר מחלון משפטים, בתוך תקציב טוקנים קבוע."""

    def __init__(
        self,
        settings: Settings | None = None,
        token_counter: TokenCounter | None = None,
        history: EventHistoryProvider | None = None,
        event_types: list[EventTypeDefinition] | None = None,
    ) -> None:
        self.settings = settings if settings is not None else DEFAULT_SETTINGS
        self.tokens = (
            token_counter
            if token_counter is not None
            else HeuristicTokenCounter(self.settings.token_budget.chars_per_token)
        )
        self.history = history
        self.event_types = event_types if event_types is not None else load_event_taxonomy()

    def build(
        self,
        window: SentenceWindow,
        current_state: MissionState | None = None,
        last_summary: Summary | None = None,
        mission_settings: dict[str, str] | None = None,
    ) -> ContextBundle:
        budget = self.settings.token_budget.max_tokens_per_window
        relevant = self._relevant_history(window, current_state)

        core_tokens = self._core_tokens(window, current_state, last_summary, mission_settings)
        dropped = 0
        # היסטוריה נחתכת ראשונה, והפריט הישן ביותר יוצא קודם.
        while relevant and core_tokens + self._history_tokens(relevant) > budget:
            relevant.pop(0)
            dropped += 1

        used = core_tokens + self._history_tokens(relevant)
        return ContextBundle(
            window_id=window.window_id,
            flight_id=window.flight_id,
            messages=list(window.messages),
            window_close_reason=window.close_reason,
            current_state=current_state,
            last_summary=last_summary,
            mission_settings=dict(mission_settings or {}),
            event_type_catalog=list(self.event_types),
            relevant_history=relevant,
            token_budget=TokenBudgetInfo(
                budget=budget, used_estimate=used, history_items_dropped=dropped
            ),
        )

    # --- פנימי ----------------------------------------------------------

    def _relevant_history(
        self, window: SentenceWindow, current_state: MissionState | None
    ) -> list[Event]:
        if self.history is None:
            return []
        events = self.history.events_for_flight(window.flight_id)
        if not events:
            return []
        return select_relevant_events(
            window.text(),
            events,
            current_state,
            max_events=self.settings.token_budget.max_history_events,
        )

    def _core_tokens(
        self,
        window: SentenceWindow,
        current_state: MissionState | None,
        last_summary: Summary | None,
        mission_settings: dict[str, str] | None,
    ) -> int:
        total = self.tokens.count_items(f"[{m.source}] {m.text}" for m in window.messages)
        total += self._catalog_tokens()
        if current_state is not None:
            total += self.tokens.count_items(self._state_texts(current_state))
        if last_summary is not None:
            total += self.tokens.count(last_summary.text) + 8
        if mission_settings:
            total += self.tokens.count_items(f"{k}: {v}" for k, v in mission_settings.items())
        return total

    def _catalog_tokens(self) -> int:
        texts: list[str] = []
        for definition in self.event_types:
            texts.append(f"{definition.id} {definition.label_he} {definition.description_he}")
            texts.extend(definition.examples_he)
        return self.tokens.count_items(texts)

    @staticmethod
    def _state_texts(state: MissionState) -> list[str]:
        texts = []
        for entity in state.entities.values():
            attributes = " ".join(
                f"{name}={attribute.current.value}" for name, attribute in entity.attributes.items()
            )
            texts.append(f"{entity.type.value} {entity.label} {entity.status.value} {attributes}")
        return texts

    def _history_tokens(self, events: list[Event]) -> int:
        if not events:
            return 0
        return self.tokens.count_items(
            f"{event.type} {event.description} {event.new_info} "
            + " ".join(ref.label for ref in event.entities)
            for event in events
        )

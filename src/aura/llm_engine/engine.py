"""The LLM event-detection engine (רכיב 4).

Bundle in, ``LLMResult`` out. The engine owns three things the model must
not be trusted with:

``window_id`` comes from the bundle, never from the response - a model
echoing an id back is a chance to get it wrong for no benefit.
``llm_model`` is whatever actually answered, taken from the client.
``raw_response`` keeps the model's own text so a bad result can be
inspected after the fact without re-running the model.

What the engine does *not* do is reject a result for being wrong. Checking
the event type against the taxonomy, checking that ``source_ids`` point at
real sentences, and retrying on a bad answer are component 5's job
(שלב 5), and duplicating them here would mean two places to fix when the
rules change. The engine's only failure mode is a response it cannot parse
into the schema at all.
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from aura.config import DEFAULT_SETTINGS, Settings
from aura.llm_engine.client import LLMClient, LLMError
from aura.llm_engine.prompt import PromptProfile, build_system_prompt, build_user_prompt
from aura.llm_engine.response import ModelEventsResponse, response_json_schema
from aura.schemas import ContextBundle, EventTypeDefinition, LLMResult
from aura.taxonomy import load_event_taxonomy


class LLMResponseFormatError(LLMError):
    """התשובה אינה JSON תקין או אינה תואמת לסכמת הפלט.

    עם פענוח מוגבל-סכמה זה אמור להיות בלתי אפשרי, ולכן הופעה של השגיאה
    הזאת מול ollama מצביעה על תקלה בהרצה - למשל ``format`` שלא נשלח, או
    מודל שנחתך באמצע בגלל ``num_ctx`` קטן מדי - ולא על מודל גרוע.
    """

    def __init__(self, message: str, raw_response: str) -> None:
        super().__init__(message)
        self.raw_response = raw_response


class EventDetectionEngine:
    """מזהה אירועים בחלון משפטים בעזרת מודל שפה."""

    def __init__(
        self,
        client: LLMClient,
        settings: Settings | None = None,
        event_types: list[EventTypeDefinition] | None = None,
        profile: PromptProfile = PromptProfile.GUARDED,
    ) -> None:
        self.client = client
        self.settings = settings if settings is not None else DEFAULT_SETTINGS
        self.event_types = event_types if event_types is not None else load_event_taxonomy()
        self.profile = profile
        self._schema = response_json_schema()

    def detect(self, bundle: ContextBundle) -> LLMResult:
        """מריץ את המודל על חבילת הקשר ומחזיר ``LLMResult`` מאומת-סכמה."""
        # הקטלוג שנשלח למודל הוא זה שבחבילה אם רכיב 3 צירף אותו, אחרת
        # הטקסונומיה של המנוע - כדי שלא יהיו שתי גרסאות קטלוג בהרצה אחת.
        catalog = bundle.event_type_catalog or self.event_types
        system_prompt = build_system_prompt(catalog, self.profile)
        user_prompt = build_user_prompt(bundle)

        completion = self.client.complete(system_prompt, user_prompt, self._schema)
        parsed = self._parse(completion.text)

        return LLMResult(
            window_id=bundle.window_id,
            events=parsed.events,
            llm_model=completion.model,
            raw_response=completion.text,
        )

    def last_prompts(self, bundle: ContextBundle) -> tuple[str, str]:
        """אותם prompts בדיוק, בלי לקרוא למודל - ל-debug ולסקריפטים."""
        catalog = bundle.event_type_catalog or self.event_types
        return build_system_prompt(catalog, self.profile), build_user_prompt(bundle)

    def _parse(self, text: str) -> ModelEventsResponse:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMResponseFormatError(f"model did not return JSON: {exc}", text) from exc
        try:
            return ModelEventsResponse.model_validate(payload)
        except ValidationError as exc:
            raise LLMResponseFormatError(
                f"model JSON does not match the output schema: {exc.error_count()} error(s)", text
            ) from exc

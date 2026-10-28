"""Abstract client layer for the model (רכיב 4, משימה 2).

The plan asks for an abstraction so the provider can be swapped and a mock
injected. The seam is deliberately low: a client takes two strings and a
JSON schema and returns the raw text the model produced. Parsing and
validation belong to the engine, so switching from ollama to vLLM or
llama.cpp means one new class and no change to the engine, the scoring or
any test.

ollama runs on the host, not in docker-compose: the compose file used to
publish port 11434 itself, which collided with the host install that was
already serving it.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Protocol, runtime_checkable

import httpx

from aura.config import DEFAULT_SETTINGS, LLMConfig


class LLMError(RuntimeError):
    """שגיאת בסיס של שכבת הלקוח."""


class LLMTimeoutError(LLMError):
    """המודל לא ענה בזמן. נבדק במפורש בשלב 4."""


class LLMProviderError(LLMError):
    """הספק ענה בשגיאה או בתשובה שאינה במבנה המצופה."""


class LLMCompletion:
    """התשובה הגולמית מהמודל, עם המדידות שההשוואה בין מודלים צריכה."""

    __slots__ = ("text", "model", "duration_seconds", "prompt_tokens", "completion_tokens")

    def __init__(
        self,
        text: str,
        model: str,
        duration_seconds: float,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> None:
        self.text = text
        self.model = model
        self.duration_seconds = duration_seconds
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"LLMCompletion(model={self.model!r}, duration={self.duration_seconds:.2f}s, "
            f"chars={len(self.text)})"
        )


@runtime_checkable
class LLMClient(Protocol):
    """ממשק הלקוח. ``json_schema`` כופה את מבנה הפלט כשהספק תומך בכך."""

    @property
    def model(self) -> str: ...

    def complete(
        self, system_prompt: str, user_prompt: str, json_schema: dict
    ) -> LLMCompletion: ...


class OllamaClient:
    """לקוח ל-ollama מקומי דרך ``/api/chat``.

    ``format`` מקבל סכמת JSON מלאה ולא רק את המחרוזת "json", ולכן הפלט
    נכפה למבנה בזמן הפענוח - מודל אינו *יכול* להחזיר JSON פגום. זו הסיבה
    שקריטריון תאימות הסכמה (98%) אינו מדד שמבדיל בין מודלים אלא בדיקת
    תקינות של הרצה.
    """

    def __init__(
        self,
        model: str | None = None,
        config: LLMConfig | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.config = config if config is not None else DEFAULT_SETTINGS.llm
        self._model = model if model is not None else self.config.model
        self._client = client
        self._owns_client = client is None

    @property
    def model(self) -> str:
        return self._model

    def complete(self, system_prompt: str, user_prompt: str, json_schema: dict) -> LLMCompletion:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "format": json_schema,
            "stream": False,
            "keep_alive": self.config.keep_alive,
            "options": {
                "temperature": self.config.temperature,
                "num_ctx": self.config.num_ctx,
            },
        }
        if self.config.think is not None:
            payload["think"] = self.config.think
        started = time.monotonic()
        try:
            response = self._http().post("/api/chat", json=payload)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(
                f"{self._model} did not answer within {self.config.timeout_seconds}s"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise LLMProviderError(
                f"ollama returned {exc.response.status_code}: {exc.response.text[:300]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMProviderError(
                f"could not reach ollama at {self.config.base_url}: {exc}"
            ) from exc

        duration = time.monotonic() - started
        try:
            body = response.json()
            text = body["message"]["content"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise LLMProviderError(
                f"unexpected ollama response shape: {response.text[:300]}"
            ) from exc

        return LLMCompletion(
            text=text,
            model=self._model,
            duration_seconds=duration,
            prompt_tokens=body.get("prompt_eval_count"),
            completion_tokens=body.get("eval_count"),
        )

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def __enter__(self) -> OllamaClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.config.base_url, timeout=self.config.timeout_seconds
            )
        return self._client


class MockLLMClient:
    """לקוח מדומה לבדיקות היחידה - ללא רשת, לפי עקרון "LLM מדומה בבדיקות".

    ``responses`` הוא או טקסט אחד שיוחזר תמיד, או רשימה שנצרכת בסדר, או
    חריגה שתיזרק (לבדיקת timeout ושגיאות ספק). כל הקריאות נרשמות ב-
    ``calls`` כדי שאפשר לבדוק מה נשלח למודל בפועל.
    """

    def __init__(
        self,
        responses: str | list[str] | Exception,
        model: str = "mock-model",
        duration_seconds: float = 0.0,
    ) -> None:
        self._responses = responses
        self._index = 0
        self._model = model
        self._duration = duration_seconds
        self.calls: list[dict] = []

    @property
    def model(self) -> str:
        return self._model

    def complete(self, system_prompt: str, user_prompt: str, json_schema: dict) -> LLMCompletion:
        self.calls.append({"system": system_prompt, "user": user_prompt, "schema": json_schema})
        if isinstance(self._responses, Exception):
            raise self._responses
        if isinstance(self._responses, str):
            text = self._responses
        else:
            if self._index >= len(self._responses):
                raise AssertionError(
                    f"MockLLMClient ran out of responses after {self._index} calls"
                )
            text = self._responses[self._index]
            self._index += 1
        return LLMCompletion(text=text, model=self._model, duration_seconds=self._duration)


class RecordingLLMClient:
    """עוטף לקוח אמיתי ושומר כל תשובה כ-fixture (רכיב 4, משימה 3).

    כך נבנה סט ה-fixtures מתשובות אמיתיות, והבדיקות ממשיכות לרוץ ללא
    מודל ובלי רשת. שם הקובץ נגזר מהמודל וממזהה החלון, כדי שהרצה חוזרת
    תדרוס את אותו קובץ ולא תייצר עוד אחד.
    """

    def __init__(self, inner: LLMClient, out_dir: Path, window_id: str | None = None) -> None:
        self.inner = inner
        self.out_dir = Path(out_dir)
        self.window_id = window_id
        self.written: list[Path] = []

    @property
    def model(self) -> str:
        return self.inner.model

    def complete(self, system_prompt: str, user_prompt: str, json_schema: dict) -> LLMCompletion:
        completion = self.inner.complete(system_prompt, user_prompt, json_schema)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / f"{_slug(self.model)}__{self.window_id or 'window'}.json"
        path.write_text(
            json.dumps(
                {
                    "model": completion.model,
                    "window_id": self.window_id,
                    "duration_seconds": round(completion.duration_seconds, 3),
                    "prompt_tokens": completion.prompt_tokens,
                    "completion_tokens": completion.completion_tokens,
                    "system_prompt": system_prompt,
                    "user_prompt": user_prompt,
                    "response": completion.text,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self.written.append(path)
        return completion


def _slug(text: str) -> str:
    return "".join(char if char.isalnum() or char in "-_." else "-" for char in text).strip("-")

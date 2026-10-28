"""The system-state payload behind the dashboard (part of שלב 10).

One request answers "what is running, what is built, and what is wired to
what" - which is otherwise spread across the work plan, the README and
whatever happens to be in the SQLite file.

Two distinctions are kept explicit here because conflating them is what
makes a status page lie:

``built`` vs ``exposed_over_http`` - components 3 and 4 are implemented
and tested but are libraries; nothing calls them over HTTP yet. A
dashboard that showed only "implemented stages" would imply the pipeline
runs end to end, which it does not until stage 6.

The configured model vs the *available* models - ``LLMConfig.model`` is a
string in a config file. Whether ollama actually holds those weights is a
different question, and the answer changes without any code change.
"""

from __future__ import annotations

import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field

from aura.config import DEFAULT_SETTINGS

# מצב הרכיבים מול תכנית העבודה. "built" = ממומש ונבדק; "exposed" = קיים
# גם כנקודת קצה ב-API. רכיבים 3 ו-4 בנויים אך אינם חשופים: הצינור
# המלא מתחבר בשלב 6.
COMPONENTS: list[dict] = [
    {"id": 0, "name_he": "תשתית, חוזים וסט זהב", "stage": 0, "built": True, "exposed": True},
    {"id": 1, "name_he": "קליטת משפטים", "stage": 1, "built": True, "exposed": True},
    {"id": 2, "name_he": "מאגר תמלול גולמי", "stage": 2, "built": True, "exposed": True},
    {"id": 3, "name_he": "בניית הקשר", "stage": 3, "built": True, "exposed": False},
    {"id": 4, "name_he": "מנוע LLM לזיהוי אירועים", "stage": 4, "built": True, "exposed": False},
    {"id": 5, "name_he": "אימות ופענוח פלט", "stage": 5, "built": False, "exposed": False},
    {"id": 6, "name_he": "מנהל מצב המשימה", "stage": 6, "built": False, "exposed": False},
    {"id": 7, "name_he": "מאגר אירועים ומצב", "stage": 7, "built": False, "exposed": False},
    {"id": 8, "name_he": "תקצירים", "stage": 8, "built": False, "exposed": False},
    {"id": 9, "name_he": "שירות שאילתות", "stage": 9, "built": False, "exposed": False},
    {"id": 10, "name_he": "API וממשק", "stage": 10, "built": False, "exposed": True},
    {"id": 11, "name_he": "מעבר תיקון בסוף טיסה", "stage": 11, "built": False, "exposed": False},
]

TOTAL_STAGES = 13

_STARTED_AT = datetime.now(UTC)


class ComponentStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name_he: str
    stage: int
    built: bool = Field(description="ממומש ונבדק")
    exposed_over_http: bool = Field(description="קיים גם כנקודת קצה ב-API הזה")


class StoreStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    exists: bool
    size_bytes: int
    flight_count: int
    message_count: int
    append_only: bool = Field(default=True, description="נאכף בטריגרים של מסד הנתונים")


class DeadLetterStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int
    by_reason: dict[str, int]


class ModelStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    reachable: bool
    error: str | None = None
    configured_model: str
    configured_model_available: bool
    available_models: list[str] = Field(default_factory=list)
    loaded_models: list[str] = Field(default_factory=list)


class WindowSettingsStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    silence_threshold_seconds: int
    max_duration_seconds: int
    max_sentences: int
    overlap_sentences: int
    close_on_speaker_change: bool
    max_tokens_per_window: int


class SystemStatus(BaseModel):
    """מצב המערכת כולה בתשובה אחת."""

    model_config = ConfigDict(extra="forbid")

    status: str
    version: str
    started_at: datetime
    uptime_seconds: float
    stages_total: int
    stages_built: list[int]
    components: list[ComponentStatus]
    store: StoreStatus
    queue_depth: int
    dead_letters: DeadLetterStatus
    llm: ModelStatus
    window_settings: WindowSettingsStatus


def _store_status(store, path: str) -> StoreStatus:
    db = Path(path)
    # ‎:memory: אינו קובץ; stat היה נכשל עליו.
    exists = path != ":memory:" and db.exists()
    return StoreStatus(
        path=path,
        exists=exists,
        size_bytes=db.stat().st_size if exists else 0,
        flight_count=len(store.flights()),
        message_count=store.count(),
    )


def _llm_status(timeout_seconds: float = 2.0) -> ModelStatus:
    """שואל את ollama מה באמת מותקן וטעון.

    הזמן קצוב בכוונה קצר: מודל שלא זמין הוא מידע מעניין לדשבורד, ולא
    סיבה להשאיר את הדף תלוי.
    """
    config = DEFAULT_SETTINGS.llm
    status = ModelStatus(
        base_url=config.base_url,
        reachable=False,
        configured_model=config.model,
        configured_model_available=False,
    )
    try:
        with httpx.Client(base_url=config.base_url, timeout=timeout_seconds) as client:
            tags = client.get("/api/tags").raise_for_status().json()
            status.available_models = sorted(
                item["name"] for item in tags.get("models", []) if item.get("name")
            )
            running = client.get("/api/ps").raise_for_status().json()
            status.loaded_models = sorted(
                item["name"] for item in running.get("models", []) if item.get("name")
            )
        status.reachable = True
        status.configured_model_available = config.model in status.available_models
    except Exception as exc:  # noqa: BLE001 - כל כשל כאן הוא "לא זמין"
        status.error = f"{type(exc).__name__}: {exc}"[:200]
    return status


def build_status(store, service, version: str, store_path: str) -> SystemStatus:
    now = datetime.now(UTC)
    reasons = Counter(record.reason.value for record in service.dead_letters)
    queue = service.queue
    return SystemStatus(
        status="ok",
        version=version,
        started_at=_STARTED_AT,
        uptime_seconds=round((now - _STARTED_AT).total_seconds(), 1),
        stages_total=TOTAL_STAGES,
        stages_built=sorted({c["stage"] for c in COMPONENTS if c["built"]}),
        components=[
            ComponentStatus(
                id=c["id"],
                name_he=c["name_he"],
                stage=c["stage"],
                built=c["built"],
                exposed_over_http=c["exposed"],
            )
            for c in COMPONENTS
        ],
        store=_store_status(store, store_path),
        queue_depth=len(queue) if hasattr(queue, "__len__") else 0,
        dead_letters=DeadLetterStatus(total=len(service.dead_letters), by_reason=dict(reasons)),
        llm=_llm_status(),
        window_settings=WindowSettingsStatus(
            silence_threshold_seconds=DEFAULT_SETTINGS.window.silence_threshold_seconds,
            max_duration_seconds=DEFAULT_SETTINGS.window.max_duration_seconds,
            max_sentences=DEFAULT_SETTINGS.window.max_sentences,
            overlap_sentences=DEFAULT_SETTINGS.window.overlap_sentences,
            close_on_speaker_change=DEFAULT_SETTINGS.window.close_on_speaker_change,
            max_tokens_per_window=DEFAULT_SETTINGS.token_budget.max_tokens_per_window,
        ),
    )


def resolved_store_path() -> str:
    from aura.api.dependencies import DEFAULT_RAW_STORE_PATH

    return os.environ.get("AURA_RAW_STORE_PATH", DEFAULT_RAW_STORE_PATH)

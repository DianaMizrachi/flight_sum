"""Default thresholds, per the open questions in flight-transcript-work-plan.md.

Values marked PLAN DEFAULT come from the plan's own "למשל" (e.g.) suggestions.
Values marked PLACEHOLDER are not specified anywhere in the plan and are
filled in with a reasonable number so components 3 and 11 have something to
run against; both are listed under "שאלות לבירור לפני שלב 0" and must be
confirmed before they're relied on for real traffic.
"""

from pydantic import BaseModel, Field


class WindowConfig(BaseModel):
    silence_threshold_seconds: int = Field(
        default=20, description="PLACEHOLDER - not specified in the plan, pending approval"
    )
    max_duration_seconds: int = Field(
        default=90, description="PLAN DEFAULT; measured over the window's own sentences"
    )
    max_sentences: int = Field(
        default=15, description="PLAN DEFAULT; counts the overlap too, since the LLM sees it"
    )
    overlap_sentences: int = Field(default=2, description="PLAN DEFAULT (range is 1-2)")
    close_on_speaker_change: bool = Field(
        default=True,
        description=(
            "PLAN DEFAULT. On alternating radio traffic this fragments windows to 1-2 "
            "sentences; see README. Turn off when `source` quality is unknown."
        ),
    )
    close_on_channel_change: bool = Field(default=True, description="PLAN DEFAULT")
    include_late_messages: bool = Field(
        default=False,
        description=(
            "PLACEHOLDER - not in the plan. False: a sentence that belongs to an already "
            "closed window is reported as an orphan and left to the stage 11 reconciler. "
            "True: it joins the open window, out of its chronological place."
        ),
    )


class FlightEndConfig(BaseModel):
    idle_timeout_minutes: int = Field(default=15, description="PLAN DEFAULT, flagged 'לאישור'")


class TokenBudgetConfig(BaseModel):
    max_tokens_per_window: int = Field(
        default=4000, description="PLACEHOLDER - not specified in the plan, pending approval"
    )
    chars_per_token: float = Field(
        default=2.5,
        description=(
            "PLACEHOLDER - rough Hebrew subword ratio for the heuristic counter. Stage 4 "
            "replaces it with the model's own tokenizer."
        ),
    )
    max_history_events: int = Field(
        default=20, description="PLACEHOLDER - upper bound before the budget trims further"
    )


class LLMConfig(BaseModel):
    """הגדרות מנוע ה-LLM (רכיב 4). ollama רץ על המארח, לא בקונטיינר."""

    model: str = Field(
        default="hf.co/dicta-il/DictaLM-3.0-Nemotron-12B-Instruct-GGUF:Q4_K_M",
        description=(
            "PLACEHOLDER - נבחר בהשוואה של tests/fixtures/model_eval. ברירת המחדל "
            "היא הדגם שנכנס ל-16GB VRAM עם מרווח ל-KV cache."
        ),
    )
    base_url: str = Field(default="http://localhost:11434")
    timeout_seconds: float = Field(
        default=120.0,
        description="מודל מקומי בטעינה ראשונה אטי; הבדיקה של שלב 4 כוללת timeout",
    )
    temperature: float = Field(
        default=0.0, description="דטרמיניסטי: השוואת מודלים חייבת להיות ניתנת לשחזור"
    )
    keep_alive: str = Field(
        default="10m", description="כמה זמן ollama משאיר את המודל טעון בין קריאות"
    )
    num_ctx: int = Field(
        default=8192,
        description=(
            "חלון ההקשר שollama מקצה. חייב להיות גדול מתקציב הטוקנים של רכיב 3 "
            "בתוספת ההנחיות והפלט."
        ),
    )


class ReconcilerConfig(BaseModel):
    window_minutes: int = Field(default=7, description="PLAN DEFAULT (range is 5-10 minutes)")


class Settings(BaseModel):
    window: WindowConfig = Field(default_factory=WindowConfig)
    flight_end: FlightEndConfig = Field(default_factory=FlightEndConfig)
    token_budget: TokenBudgetConfig = Field(default_factory=TokenBudgetConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    reconciler: ReconcilerConfig = Field(default_factory=ReconcilerConfig)


DEFAULT_SETTINGS = Settings()

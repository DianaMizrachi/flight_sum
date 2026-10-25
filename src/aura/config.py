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
    max_duration_seconds: int = Field(default=90, description="PLAN DEFAULT")
    max_sentences: int = Field(default=15, description="PLAN DEFAULT")
    overlap_sentences: int = Field(default=2, description="PLAN DEFAULT (range is 1-2)")


class FlightEndConfig(BaseModel):
    idle_timeout_minutes: int = Field(default=15, description="PLAN DEFAULT, flagged 'לאישור'")


class TokenBudgetConfig(BaseModel):
    max_tokens_per_window: int = Field(
        default=4000, description="PLACEHOLDER - not specified in the plan, pending approval"
    )


class ReconcilerConfig(BaseModel):
    window_minutes: int = Field(default=7, description="PLAN DEFAULT (range is 5-10 minutes)")


class Settings(BaseModel):
    window: WindowConfig = Field(default_factory=WindowConfig)
    flight_end: FlightEndConfig = Field(default_factory=FlightEndConfig)
    token_budget: TokenBudgetConfig = Field(default_factory=TokenBudgetConfig)
    reconciler: ReconcilerConfig = Field(default_factory=ReconcilerConfig)


DEFAULT_SETTINGS = Settings()

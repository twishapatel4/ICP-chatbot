"""Application settings, read from environment variables or a local .env file."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Model access. "developer" = Gemini Developer API key (prototype); "vertex" = Vertex AI.
    llm_backend: Literal["developer", "vertex"] = "developer"
    gemini_api_key: str = ""
    google_cloud_project: str = ""
    google_application_credentials: str = ""  # service-account JSON file; else the default login
    google_cloud_location: str = "global"  # spec decision D3: global endpoint for now

    # Model per step (spec section 3).
    model_extraction: str = "gemini-3.1-flash-lite"
    model_question: str = "gemini-3.8-flash"
    model_interviewer: str = ""  # the model-led interviewer; empty uses model_question
    interview_engine: Literal["classic", "agent"] = "classic"  # agent: the model leads the chat
    model_final_icp: str = "gemini-2.5-pro"  # spec decision D7: General Availability models only

    # Database. Tests and local development use SQLite; production uses Postgres.
    database_url: str = "sqlite+aiosqlite:///./icp_chatbot.db"
    auto_create_tables: bool = False  # development convenience; production uses Alembic

    # Interview limits (spec section 5.5).
    quick_mode_question_cap: int = Field(
        20, ge=1
    )  # a safety ceiling: the interview ends when the ICP details are known
    detail_mode_total_cap: int = Field(25, ge=1)
    detail_mode_extra_questions: int = Field(5, ge=1)
    max_answer_chars: int = Field(2000, ge=1)
    vary_question_order: bool = True  # every session asks its topics in its own order

    # Time budgets.
    enrichment_timeout_seconds: float = Field(40.0, gt=0)
    enrichment_cache_days: int = Field(30, ge=0)  # reuse a site's earlier read; 0 reads it again
    llm_timeout_seconds: float = Field(20.0, gt=0)

    # Abuse and cost controls (spec section 12).
    daily_session_cap_per_tenant: int = Field(200, ge=1)
    daily_token_cap_per_tenant: int = Field(2_000_000, ge=1)
    global_daily_session_cap: int = Field(2_000, ge=1)
    global_daily_token_cap: int = Field(20_000_000, ge=1)
    requests_per_minute_per_tenant: int = Field(60, ge=1)
    abandon_after_hours: int = Field(48, ge=1)

    @model_validator(mode="after")
    def _detail_cap_not_below_quick_cap(self):
        if self.detail_mode_total_cap < self.quick_mode_question_cap:
            raise ValueError("detail_mode_total_cap must be >= quick_mode_question_cap")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

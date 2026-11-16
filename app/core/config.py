# app/core/config.py
"""Central application settings, loaded from environment variables / .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # model_config tells pydantic-settings HOW to load values.
    model_config = SettingsConfigDict(
        env_file=".env",          # read variables from a local .env file if present
        extra="ignore",           # unknown variables in .env are skipped, not errors
        env_ignore_empty=True,    # "REDIS_URL=" (blank) counts as "not set" -> default is used
    )

    # ---- General -------------------------------------------------------
    env: str = "dev"  # "dev" or "prod"; lets code behave differently per environment

    # ---- Databases / cache ---------------------------------------------
    # App data (trips, users, feedback). SQLite file keeps local dev zero-setup.
    database_url: str = "sqlite+aiosqlite:///./yatra.db"
    # LangGraph checkpoints (Postgres in prod). Empty until we wire it up in week 4.
    checkpoint_db_url: str = ""
    # Redis cache URL; None means "use the SQLite/in-memory cache instead".
    redis_url: str | None = None

    # ---- LLM providers --------------------------------------------------
    # Empty-string defaults so the app can boot before you have keys (week 1).
    groq_api_key: str = ""
    google_api_key: str = ""
    # Model names live in .env because free-tier model line-ups change often.
    groq_fast_model: str = ""   # cheap model for parsing / critique
    groq_smart_model: str = ""  # stronger model, used as a fallback for composing
    gemini_model: str = ""      # primary "smart" model
    ollama_enabled: bool = False  # local backup model, off unless you run Ollama
    ollama_model: str = "llama3.1"

    # ---- Data providers --------------------------------------------------
    geoapify_api_key: str = ""
    railradar_api_key: str | None = None  # None -> use the static train timetable only
    use_mock_flights: bool = True  # no good free flight API, so mock by default
    mock_seed: int = 42  # fixed seed -> the mock data is repeatable (important for evals)

    # ---- Observability ----------------------------------------------------
    langfuse_public_key: str | None = None   # None -> tracing is simply disabled
    langfuse_secret_key: str | None = None
    langfuse_host: str = "https://cloud.langfuse.com"


@lru_cache  # build Settings once, then reuse the same object on every call
def get_settings() -> Settings:
    return Settings()
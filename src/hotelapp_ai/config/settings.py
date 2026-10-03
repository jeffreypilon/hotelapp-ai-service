"""Environment validated once at startup. A malformed value refuses to start the app rather than
failing on the first request that needs it.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="forbid")

    # Which backend to call. No SQL access to business data -- see architecture-specification.md.
    hotelapp_api_base_url: str = "http://localhost:8080/api/v1"

    # Provider. Absent `OPENAI_API_KEY` is a valid, supported state: the service starts and
    # reports AI features unavailable rather than crashing.
    llm_provider: Literal["openai", "ollama"] = "openai"
    openai_api_key: str | None = None
    embedding_model: str = "text-embedding-3-small"

    # Model tiers per ai-enablement-overview.md §11. Generation is the one output a guest reads;
    # rewrite and grading are classification-shaped and use the cheaper nano tier.
    generation_model: str = "gpt-5.4-mini"
    grading_model: str = "gpt-5.4-nano"

    # Assumption: no ceiling is fixed in api-contracts.md beyond "the configured ceiling" for
    # QUESTION_TOO_LONG. 2000 characters is roughly 400-500 tokens -- generous for a guest
    # question, cheap to reject before any model call.
    question_max_length: int = 2000

    # ai_* tables only.
    database_url: str | None = None

    port: int = 8000
    log_level: str = "info"

    # Never a wildcard. Kept as a raw comma-separated string, not `list[str]` -- pydantic-settings
    # JSON-decodes complex-typed env values *before* any field validator runs, which rejects a
    # plain comma-separated string outright rather than reaching a custom parser.
    cors_allowed_origins_raw: str = Field(default="", validation_alias="CORS_ALLOWED_ORIGINS")

    oauth_issuer: str | None = None
    oauth_signing_key: str | None = None

    langfuse_host: str | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None

    @property
    def cors_allowed_origins(self) -> list[str]:
        origins = self.cors_allowed_origins_raw.split(",")
        return [origin.strip() for origin in origins if origin.strip()]

    @property
    def provider_configured(self) -> bool:
        return bool(self.openai_api_key)

    def require_database_url(self) -> str:
        if not self.database_url:
            raise RuntimeError(
                "DATABASE_URL is required in AI Step 1. Point it at the Compose pgvector "
                "database on localhost:5433."
            )
        return self.database_url


@lru_cache
def get_settings() -> Settings:
    return Settings()

"""settings come from the environment (and a local .env when running outside docker).

nothing here is a secret by default. LLM keys are only ever read from the environment;
the app never falls back to ambient credentials from the machine it runs on.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

LlmProvider = Literal["anthropic", "openai", "none"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "console"

    database_url: str = "postgresql+psycopg://onboarding:onboarding@localhost:5433/onboarding"

    # the migration layer talks to the target platform over http, even though the mock
    # target is served by this same process. that keeps the seam realistic.
    target_api_base_url: str = "http://localhost:8000/target/v1"

    llm_provider: LlmProvider = "none"
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-opus-5"
    openai_api_key: str | None = None
    openai_model: str = "gpt-5"

    data_dir: str = Field(default="sample_customer/data", description="where source files live")

    @property
    def effective_llm_provider(self) -> LlmProvider:
        """the provider we can actually use. a provider without its key means manual mode,
        never a faked model response."""
        if self.llm_provider == "anthropic" and self.anthropic_api_key:
            return "anthropic"
        if self.llm_provider == "openai" and self.openai_api_key:
            return "openai"
        return "none"

    @property
    def llm_model(self) -> str | None:
        provider = self.effective_llm_provider
        if provider == "anthropic":
            return self.anthropic_model
        if provider == "openai":
            return self.openai_model
        return None


@lru_cache
def get_settings() -> Settings:
    return Settings()

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
    llm_timeout_seconds: float = 300.0
    llm_max_output_tokens: int = 16000
    # datasets are asked about in parallel, this many at a time; one call runs about two minutes
    llm_parallel_calls: int = 4
    # a proposal that fails our own checks (unknown target, missing field) is sent back with the
    # problems listed, this many times in total, before the call is reported as failed
    llm_max_attempts: int = 3

    # mapping review thresholds. at or above high: bulk-approvable and shown green. below low:
    # shown red. nothing is approved without a person either way; these only steer attention.
    mapping_high_confidence: float = 0.85
    mapping_low_confidence: float = 0.6

    data_dir: str = Field(default="sample_customer/data", description="where source files live")

    # directories a source file may be attached from, relative to the project root. anything
    # outside these (or reached through "..") is refused by the api.
    source_roots: str = "sample_customer/data,generated"
    # directories a context document (the customer's business rules, kickoff notes) may be read
    # from. same rules as source_roots.
    document_roots: str = "sample_customer,generated"

    # the customer's legacy billing system, simulated by this same process under
    # /mock/billing/v1. the loader pages through it over http like any external feed.
    billing_api_base_url: str = "http://localhost:8000/mock/billing/v1"
    billing_api_token: str = "legacybill-readonly-demo"
    billing_source_file: str = "sample_customer/data/subscriptions.json"
    billing_page_size: int = 200

    @property
    def source_root_paths(self) -> list[str]:
        return [r.strip() for r in self.source_roots.split(",") if r.strip()]

    @property
    def document_root_paths(self) -> list[str]:
        return [r.strip() for r in self.document_roots.split(",") if r.strip()]

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

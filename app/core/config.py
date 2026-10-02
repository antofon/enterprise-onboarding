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
    target_api_token: str = "meridian-staging-demo"
    target_request_timeout_seconds: float = 20.0
    # writes carry an id, so a retry after a 5xx, a timeout or a malformed answer is safe
    target_max_attempts: int = 3
    target_retry_backoff_seconds: float = 0.25
    # a 429 or 503 that says Retry-After is waited out, up to this long per retry
    target_retry_max_wait_seconds: float = 10.0
    # this many records in a row that failed after their retries means the target is down, not
    # flaky: the run stops writing, counts the rest as not reached and says why
    target_breaker_threshold: int = Field(default=10, ge=1)

    # fault injection in the mock target, off by default: a plain run should fail only where the
    # customer's data is actually bad. deterministic in the record id, so a demo repeats exactly.
    target_fault_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    target_fault_modes: str = "server_error,timeout,malformed"
    target_fault_seed: int = 1337
    target_fault_timeout_seconds: float = 30.0
    # 0: a record drawn for a fault fails on every attempt. n: only its first n attempts fail,
    # which is what a transient fault (a blip, a rate limit) looks like to a client that retries
    target_fault_attempts: int = Field(default=0, ge=0)
    # what the rate_limited fault puts in Retry-After
    target_fault_retry_after_seconds: int = Field(default=1, ge=0)

    # customer-specific value maps for the transformation stage: reviewed configuration, not code
    transformation_config: str = "sample_customer/transformation_config.yaml"
    # how many validation issues one run stores before it keeps counting without storing
    validation_issue_limit: int = 5000
    # how many rejected or failed records one dry run stores in full
    migration_failure_limit: int = 2000
    # a run still `running` with no heartbeat for this long belongs to a process that stopped;
    # it is marked failed when the api starts and before the project's next run
    run_stale_after_seconds: float = Field(default=600.0, gt=0)

    # readiness policy: an entity where fewer than this share of in-scope records landed in the
    # rehearsal blocks go-live. anything short of all of them is a condition the customer signs
    readiness_min_entity_coverage: float = Field(default=0.95, ge=0.0, le=1.0)

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
    # the loader's side: a 429 or a 5xx is retried, waiting what Retry-After says (capped) or a
    # short linear backoff, before the profile step gives up on the feed
    billing_max_attempts: int = Field(default=4, ge=1)
    billing_retry_backoff_seconds: float = 0.5
    billing_retry_max_wait_seconds: float = 10.0
    # the mock feed's side: LegacyBill allows this many requests per window and answers 429 with
    # Retry-After past it. 0 is no limit, the default, so a plain profile is not slowed down
    billing_rate_limit: int = Field(default=0, ge=0)
    billing_rate_window_seconds: float = Field(default=1.0, gt=0)

    @property
    def target_fault_mode_list(self) -> list[str]:
        return [m.strip() for m in self.target_fault_modes.split(",") if m.strip()]

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

"""one interface over the hosted model providers.

`complete()` sends a system prompt and a user prompt and gets back an instance of the pydantic
model the caller asked for, plus what it cost. the provider is chosen by LLM_PROVIDER and the
matching key; a provider without its key is manual mode, never a faked response.

what never happens here: logging a key, logging a prompt, retrying on our own for transient
failures (both sdks already retry 429s and 5xx twice with backoff). malformed or unusable
answers are the caller's to retry, with feedback, because only the caller knows what "usable"
means for its schema."""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Any, Protocol, TypeVar

from fastapi import Depends
from pydantic import BaseModel

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.logging import get_logger

log = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)


class LlmError(AppError):
    """the provider could not give a usable answer: transport, auth, refusal, truncation."""

    status_code = 502
    error_type = "llm_error"


class LlmUnavailableError(AppError):
    """manual mode: no provider configured. the rest of the workflow keeps working."""

    status_code = 409
    error_type = "llm_unavailable"


@dataclass(frozen=True)
class StructuredResult[T: BaseModel]:
    parsed: T
    provider: str
    model: str
    latency_ms: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    request_id: str | None = None


class LlmProvider(Protocol):
    name: str
    model: str

    def complete(self, *, system: str, user: str, output: type[T], max_tokens: int) -> Any: ...


class NullProvider:
    """LLM_PROVIDER=none, or a provider without its key."""

    name = "none"
    model = ""

    def complete(self, *, system: str, user: str, output: type[T], max_tokens: int) -> Any:
        raise LlmUnavailableError(
            "no model provider is configured; set LLM_PROVIDER and its key, or map by hand",
            details={"hint": "LLM_PROVIDER=anthropic|openai with ANTHROPIC_API_KEY|OPENAI_API_KEY"},
        )


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str, timeout: float) -> None:
        import anthropic

        self.model = model
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=2)

    def complete(
        self, *, system: str, user: str, output: type[T], max_tokens: int
    ) -> StructuredResult[T]:
        import anthropic

        started = time.perf_counter()
        try:
            response = self._client.messages.parse(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=output,
            )
        except anthropic.AuthenticationError as exc:
            raise LlmError(
                "anthropic rejected the api key", details={"provider": self.name}
            ) from exc
        except anthropic.RateLimitError as exc:
            raise LlmError(
                "anthropic rate limit reached after retries", details={"provider": self.name}
            ) from exc
        except anthropic.APIStatusError as exc:
            raise LlmError(
                f"anthropic answered {exc.status_code}",
                details={"provider": self.name, "status": exc.status_code},
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise LlmError(
                f"anthropic unreachable: {exc.__class__.__name__}",
                details={"provider": self.name},
            ) from exc
        latency_ms = round((time.perf_counter() - started) * 1000, 1)

        if response.stop_reason == "refusal":
            raise LlmError(
                "the model declined this request",
                details={"provider": self.name, "stop_reason": "refusal"},
            )
        if response.stop_reason == "max_tokens":
            raise LlmError(
                "the model's answer was cut off at max_tokens",
                details={"provider": self.name, "max_tokens": max_tokens},
            )
        parsed = response.parsed_output
        if parsed is None:
            raise LlmError(
                "the model returned no structured output",
                details={"provider": self.name, "stop_reason": response.stop_reason},
            )
        usage = response.usage
        return StructuredResult(
            parsed=parsed,
            provider=self.name,
            model=response.model or self.model,
            latency_ms=latency_ms,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            request_id=getattr(response, "_request_id", None),
        )


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str, model: str, timeout: float) -> None:
        import openai

        self.model = model
        self._client = openai.OpenAI(api_key=api_key, timeout=timeout, max_retries=2)

    def complete(
        self, *, system: str, user: str, output: type[T], max_tokens: int
    ) -> StructuredResult[T]:
        import openai

        started = time.perf_counter()
        try:
            response = self._client.responses.parse(
                model=self.model,
                instructions=system,
                input=user,
                text_format=output,
                max_output_tokens=max_tokens,
            )
        except openai.AuthenticationError as exc:
            raise LlmError("openai rejected the api key", details={"provider": self.name}) from exc
        except openai.RateLimitError as exc:
            raise LlmError(
                "openai rate limit reached after retries", details={"provider": self.name}
            ) from exc
        except openai.APIStatusError as exc:
            raise LlmError(
                f"openai answered {exc.status_code}",
                details={"provider": self.name, "status": exc.status_code},
            ) from exc
        except openai.APIConnectionError as exc:
            raise LlmError(
                f"openai unreachable: {exc.__class__.__name__}", details={"provider": self.name}
            ) from exc
        latency_ms = round((time.perf_counter() - started) * 1000, 1)

        if getattr(response, "status", None) == "incomplete":
            details = getattr(response, "incomplete_details", None)
            raise LlmError(
                "the model's answer is incomplete",
                details={"provider": self.name, "reason": getattr(details, "reason", None)},
            )
        parsed = response.output_parsed
        if parsed is None:
            refusal = next(
                (
                    getattr(part, "refusal", None)
                    for item in getattr(response, "output", [])
                    for part in getattr(item, "content", []) or []
                    if getattr(part, "type", "") == "refusal"
                ),
                None,
            )
            raise LlmError(
                "the model declined this request"
                if refusal
                else "the model returned no structured output",
                details={"provider": self.name},
            )
        usage = response.usage
        return StructuredResult(
            parsed=parsed,
            provider=self.name,
            model=getattr(response, "model", None) or self.model,
            latency_ms=latency_ms,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            request_id=getattr(response, "_request_id", None),
        )


def build_provider(settings: Settings | None = None) -> LlmProvider:
    settings = settings or get_settings()
    provider = settings.effective_llm_provider
    if provider == "anthropic":
        assert settings.anthropic_api_key
        return AnthropicProvider(
            settings.anthropic_api_key, settings.anthropic_model, settings.llm_timeout_seconds
        )
    if provider == "openai":
        assert settings.openai_api_key
        return OpenAIProvider(
            settings.openai_api_key, settings.openai_model, settings.llm_timeout_seconds
        )
    return NullProvider()


@lru_cache
def _cached_provider() -> LlmProvider:
    return build_provider()


def reset_provider() -> None:
    """tests swap providers; forget the cached one."""
    _cached_provider.cache_clear()


def get_llm_provider() -> Iterator[LlmProvider]:
    """fastapi dependency. tests override it with a fake so no request ever leaves the box."""
    yield _cached_provider()


LlmProviderDep = Annotated[LlmProvider, Depends(get_llm_provider)]

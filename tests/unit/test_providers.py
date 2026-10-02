"""the two hosted providers, offline: the real sdk builds the request and parses the answer, and
a mock transport plays the provider. What is checked is our side of the seam: the structured
output comes back as the pydantic model, usage and request ids are kept, and every way the call
can go wrong becomes an LlmError with a sentence, never a raw sdk exception."""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest
from pydantic import BaseModel

from app.ai.provider import (
    AnthropicProvider,
    LlmError,
    LlmUnavailableError,
    NullProvider,
    OpenAIProvider,
    build_provider,
)
from app.core.config import Settings


class Answer(BaseModel):
    target: str
    confidence: float


ANSWER = {"target": "organization.organization_id", "confidence": 0.97}


def transport(status: int, body: dict[str, Any], seen: list[httpx2.Request] | None = None):
    def handler(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(request)
        return httpx2.Response(status, json=body, headers={"request-id": "req_test_1"})

    return httpx2.Client(transport=httpx2.MockTransport(handler))


def anthropic_message(text: str, stop_reason: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 1200, "output_tokens": 80},
    }


def openai_response(text: str | None, status: str = "completed", refusal: str | None = None):
    content: list[dict[str, Any]] = []
    if text is not None:
        content.append({"type": "output_text", "text": text, "annotations": []})
    if refusal is not None:
        content.append({"type": "refusal", "refusal": refusal})
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 1790000000,
        "status": status,
        "model": "gpt-5",
        "output": [
            {
                "type": "message",
                "id": "msg_1",
                "status": "completed",
                "role": "assistant",
                "content": content,
            }
        ],
        "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None,
        "usage": {
            "input_tokens": 900,
            "output_tokens": 60,
            "total_tokens": 960,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
    }


def anthropic(status: int, body: dict[str, Any], seen: list | None = None) -> AnthropicProvider:
    return AnthropicProvider(
        "sk-test", "claude-opus-5", 5.0, http_client=transport(status, body, seen), max_retries=0
    )


def openai(status: int, body: dict[str, Any]) -> OpenAIProvider:
    return OpenAIProvider(
        "sk-test", "gpt-5", 5.0, http_client=transport(status, body), max_retries=0
    )


def ask(provider) -> Any:
    return provider.complete(system="map columns", user="the brief", output=Answer, max_tokens=500)


# --- anthropic ----------------------------------------------------------------------------------


def test_anthropic_structured_answer_comes_back_as_the_model_with_usage() -> None:
    seen: list[httpx2.Request] = []
    result = ask(anthropic(200, anthropic_message(json.dumps(ANSWER)), seen))
    assert result.parsed == Answer(**ANSWER)
    assert (result.provider, result.model) == ("anthropic", "claude-opus-5")
    assert (result.input_tokens, result.output_tokens) == (1200, 80)
    assert result.request_id == "req_test_1"
    sent = json.loads(seen[0].content)
    assert sent["model"] == "claude-opus-5" and sent["max_tokens"] == 500
    assert sent["system"] == "map columns"
    # the key travels in the header the provider expects, and nowhere in the body
    assert seen[0].headers["x-api-key"] == "sk-test"
    assert "sk-test" not in seen[0].content.decode()


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (401, "rejected the api key"),
        (429, "rate limit reached after retries"),
        (529, "anthropic answered 529"),
        (500, "anthropic answered 500"),
    ],
)
def test_anthropic_errors_become_sentences(status: int, message: str) -> None:
    body = {"type": "error", "error": {"type": "api_error", "message": "nope"}}
    with pytest.raises(LlmError) as raised:
        ask(anthropic(status, body))
    assert message in raised.value.message
    assert raised.value.status_code == 502


@pytest.mark.parametrize(
    ("stop_reason", "message"),
    [("refusal", "declined"), ("max_tokens", "cut off at max_tokens")],
)
def test_anthropic_answers_that_cannot_be_used_are_refused(stop_reason: str, message: str) -> None:
    with pytest.raises(LlmError) as raised:
        ask(anthropic(200, anthropic_message(json.dumps(ANSWER), stop_reason=stop_reason)))
    assert message in raised.value.message


def test_anthropic_unreachable_is_an_llm_error() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    provider = AnthropicProvider(
        "sk-test",
        "claude-opus-5",
        5.0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(refuse)),
        max_retries=0,
    )
    with pytest.raises(LlmError) as raised:
        ask(provider)
    assert "anthropic unreachable" in raised.value.message


# --- openai -------------------------------------------------------------------------------------


def test_openai_structured_answer_comes_back_as_the_model_with_usage() -> None:
    result = ask(openai(200, openai_response(json.dumps(ANSWER))))
    assert result.parsed == Answer(**ANSWER)
    assert (result.provider, result.model) == ("openai", "gpt-5")
    assert (result.input_tokens, result.output_tokens) == (900, 60)


def test_openai_incomplete_answer_is_refused_with_the_reason() -> None:
    with pytest.raises(LlmError) as raised:
        ask(openai(200, openai_response(json.dumps(ANSWER), status="incomplete")))
    assert raised.value.message == "the model's answer is incomplete"
    assert raised.value.details["reason"] == "max_output_tokens"


@pytest.mark.parametrize("which", ["anthropic", "openai"])
def test_an_answer_cut_off_mid_json_is_an_llm_error_not_a_crash(which: str) -> None:
    """found by this test: the sdk raises pydantic's ValidationError from inside parse() when
    the json is truncated, before any stop reason can be checked, and it escaped as a 500."""
    cut = json.dumps(ANSWER)[:10]
    provider = (
        anthropic(200, anthropic_message(cut, stop_reason="max_tokens"))
        if which == "anthropic"
        else openai(200, openai_response(cut, status="incomplete"))
    )
    with pytest.raises(LlmError) as raised:
        ask(provider)
    assert "could not be read as the requested structure" in raised.value.message
    assert raised.value.status_code == 502


def test_openai_refusal_is_named() -> None:
    with pytest.raises(LlmError) as raised:
        ask(openai(200, openai_response(None, refusal="I can't help with that")))
    assert raised.value.message == "the model declined this request"


@pytest.mark.parametrize(
    ("status", "message"),
    [(401, "rejected the api key"), (429, "rate limit"), (503, "openai answered 503")],
)
def test_openai_errors_become_sentences(status: int, message: str) -> None:
    body = {"error": {"message": "nope", "type": "server_error", "code": None, "param": None}}
    with pytest.raises(LlmError) as raised:
        ask(openai(status, body))
    assert message in raised.value.message


# --- choosing a provider ------------------------------------------------------------------------


def test_a_provider_without_its_key_is_manual_mode_never_a_fake_answer() -> None:
    provider = build_provider(Settings(llm_provider="anthropic", anthropic_api_key=None))
    assert isinstance(provider, NullProvider)
    with pytest.raises(LlmUnavailableError):
        ask(provider)


def test_the_configured_provider_and_model_are_built() -> None:
    chosen = build_provider(
        Settings(llm_provider="openai", openai_api_key="sk-x", openai_model="gpt-5")
    )
    assert (chosen.name, chosen.model) == ("openai", "gpt-5")
    chosen = build_provider(
        Settings(llm_provider="anthropic", anthropic_api_key="sk-x", anthropic_model="m")
    )
    assert (chosen.name, chosen.model) == ("anthropic", "m")

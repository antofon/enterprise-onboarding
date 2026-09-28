from app.core.config import Settings


def test_defaults_are_manual_mode() -> None:
    s = Settings(_env_file=None)
    assert s.effective_llm_provider == "none"
    assert s.llm_model is None


def test_provider_without_key_falls_back_to_manual_mode() -> None:
    s = Settings(_env_file=None, llm_provider="anthropic", anthropic_api_key=None)
    assert s.effective_llm_provider == "none"


def test_provider_with_key_is_enabled() -> None:
    s = Settings(_env_file=None, llm_provider="anthropic", anthropic_api_key="sk-test")
    assert s.effective_llm_provider == "anthropic"
    assert s.llm_model == "claude-opus-5"

    s = Settings(_env_file=None, llm_provider="openai", openai_api_key="sk-test")
    assert s.effective_llm_provider == "openai"
    assert s.llm_model == s.openai_model

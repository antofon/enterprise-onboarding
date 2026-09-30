"""the parts of the mapping step that need no database: the contract check on the model's
answer, the retry with feedback, and manual mode's proposal from the comparison."""

from pathlib import Path

import pytest

from app.ai.prompts import build_mapping_prompt
from app.ai.provider import LlmError, NullProvider, build_provider
from app.ai.schemas import MappingProposal, SuggestedMapping
from app.core.config import Settings
from app.services.comparison import compare, field_comparison_dict
from app.services.mapping import check_proposal, drafts_from_comparison, propose
from app.services.profiling import load_source, profile_frame
from app.target.catalog import target_field_catalog
from tests.fakes import FakeProvider, bad_target_responder

DATA = Path("sample_customer/data")


@pytest.fixture(scope="module")
def orgs():
    return profile_frame(
        load_source("csv", str(DATA / "organizations.csv")), name="organizations.csv"
    )


@pytest.fixture(scope="module")
def comparisons(orgs):
    return field_comparison_dict(compare([orgs]))["organizations.csv"]


@pytest.fixture(scope="module")
def catalog_paths():
    return {f.path for f in target_field_catalog()}


def _mapping(name: str, target: str | None, **overrides) -> SuggestedMapping:
    base = dict(
        source_field=name,
        target_field=target,
        confidence=0.9,
        reason="because",
        transformation_required=False,
        transformation=None,
        clarification_required=False,
        clarification_question=None,
    )
    return SuggestedMapping(**{**base, **overrides})


def test_check_proposal_names_every_problem(orgs, catalog_paths) -> None:
    names = [f.name for f in orgs.fields]
    proposal = MappingProposal(
        dataset="organizations.csv",
        entity="organization",
        mappings=[
            _mapping(names[0], "organization.nope"),
            _mapping(names[1], "organization.name", confidence=1.4),
            _mapping(names[1], "organization.name"),
            _mapping("ghost", None),
            _mapping(names[2], None, clarification_required=True),
            _mapping(names[3], "organization.website", transformation_required=True),
        ],
        observations=[],
    )
    problems = check_proposal(proposal, orgs, catalog_paths)
    joined = "\n".join(problems)
    assert "missing source fields" in joined
    assert "do not exist in this dataset: ghost" in joined
    assert f"answered more than once: {names[1]}" in joined
    assert "'organization.nope' is not in the catalog" in joined
    assert "confidence 1.4" in joined
    assert "clarification_required is true but no question" in joined
    assert "transformation_required is true but no rule" in joined


def test_check_proposal_accepts_a_complete_answer(orgs, catalog_paths) -> None:
    proposal = MappingProposal(
        dataset="organizations.csv",
        entity="organization",
        mappings=[_mapping(f.name, None, confidence=0.5) for f in orgs.fields],
        observations=[],
    )
    assert check_proposal(proposal, orgs, catalog_paths) == []


def _bundle(orgs, comparisons, provider):
    return build_mapping_prompt(
        profile=orgs,
        entity="organization",
        comparisons=comparisons,
        catalog=target_field_catalog(),
        customer="Apex",
        rules_text=None,
        project_notes=None,
        provider=provider.name,
        model=provider.model,
    )


def test_propose_sends_problems_back_and_succeeds(orgs, comparisons, catalog_paths) -> None:
    provider = FakeProvider(fail_first=1)
    outcome = propose(
        provider,
        _bundle(orgs, comparisons, provider),
        profile=orgs,
        catalog_paths=catalog_paths,
        settings=Settings(_env_file=None, llm_max_attempts=3),
    )
    assert outcome.attempts == 2
    assert "Your previous answer had these problems" in provider.calls[1]["user"]
    assert "organization.does_not_exist" in provider.calls[1]["user"]
    assert "Your previous answer" not in provider.calls[0]["user"]
    assert outcome.input_tokens == 2000 and outcome.output_tokens == 800
    assert outcome.problems_seen


def test_propose_gives_up_after_max_attempts(orgs, comparisons, catalog_paths) -> None:
    provider = FakeProvider(bad_target_responder)
    with pytest.raises(LlmError) as exc:
        propose(
            provider,
            _bundle(orgs, comparisons, provider),
            profile=orgs,
            catalog_paths=catalog_paths,
            settings=Settings(_env_file=None, llm_max_attempts=2),
        )
    assert len(provider.calls) == 2
    assert exc.value.details["attempts"] == 2
    assert exc.value.status_code == 502


def test_manual_mode_proposes_from_the_comparison_without_confidence(orgs, comparisons) -> None:
    drafts = {d.source_field: d for d in drafts_from_comparison(orgs, comparisons)}
    assert all(d.confidence is None for d in drafts.values())
    assert drafts["website"].target == "organization.website"
    assert drafts["website"].transformation_required is True
    assert drafts["company_name"].target == "organization.name"
    # semantic calls are left open, never guessed
    assert drafts["customer_tier"].target is None
    assert drafts["acct_num"].target is None
    ambiguous = [d for d in drafts.values() if d.clarification_required]
    for d in ambiguous:
        assert d.question and d.source_field in d.question


def test_provider_factory_respects_manual_mode() -> None:
    assert isinstance(build_provider(Settings(_env_file=None)), NullProvider)
    assert isinstance(
        build_provider(Settings(_env_file=None, llm_provider="anthropic")), NullProvider
    )
    anthropic_provider = build_provider(
        Settings(_env_file=None, llm_provider="anthropic", anthropic_api_key="sk-test")
    )
    assert anthropic_provider.name == "anthropic" and anthropic_provider.model == "claude-opus-5"
    openai_provider = build_provider(
        Settings(_env_file=None, llm_provider="openai", openai_api_key="sk-test")
    )
    assert openai_provider.name == "openai" and openai_provider.model == "gpt-5"


def test_null_provider_refuses_loudly() -> None:
    with pytest.raises(Exception) as exc:
        NullProvider().complete(system="", user="", output=MappingProposal, max_tokens=10)
    assert exc.value.error_type == "llm_unavailable"

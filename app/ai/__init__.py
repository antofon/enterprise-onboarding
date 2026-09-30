"""the model, behind one interface.

provider   -> anthropic | openai | none, chosen by environment, structured output parsed into
              pydantic, usage and latency reported back, keys never logged
prompts    -> what the model is shown: field statistics and sanitized samples, the target
              catalog, the deterministic comparison, the customer's rules. never the rows.
schemas    -> the shape the model must answer in
"""

from app.ai.provider import (
    LlmError,
    LlmProvider,
    LlmUnavailableError,
    StructuredResult,
    build_provider,
    get_llm_provider,
    reset_provider,
)
from app.ai.schemas import MappingProposal, SuggestedMapping

__all__ = [
    "LlmError",
    "LlmProvider",
    "LlmUnavailableError",
    "MappingProposal",
    "StructuredResult",
    "SuggestedMapping",
    "build_provider",
    "get_llm_provider",
    "reset_provider",
]

"""what the model must answer in. every field is required and nullable where it can be empty:
both providers' structured output modes want a closed schema with no defaults.

range checks (confidence in 0..1) and semantic checks (the target exists in the catalog, every
source field is answered exactly once) live in the mapping service, not here, so the json
schema handed to the providers stays within what constrained decoding supports."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SuggestedMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_field: str = Field(description="the source column name, copied exactly as given")
    target_field: str | None = Field(
        description=(
            "the target field as entity.field from the catalog, or null when the field has no "
            "home in the target or a person has to choose"
        )
    )
    confidence: float = Field(
        description=(
            "0 to 1: how likely it is that target_field is the right destination. about the "
            "choice of target, not about data quality. 0.9 and above only when the name, the "
            "values and the customer's rules all agree; 0.5 and below when a business decision "
            "is needed"
        )
    )
    reason: str = Field(
        description="why, in one to three sentences an implementation engineer can check"
    )
    transformation_required: bool = Field(
        description=(
            "true when the values need a deterministic rule to fit the target: format parsing, "
            "an enum mapping table, a split or a join, a cast, normalization"
        )
    )
    transformation: str | None = Field(
        description=(
            "the rule in plain english, deterministic and complete enough to implement without "
            "guessing (for example: map Legacy Gold to enterprise unless seat_count < 10, then "
            "professional). null when no transformation is required"
        )
    )
    clarification_required: bool = Field(
        description=(
            "true when the customer has to answer a question before this field can be mapped or "
            "transformed safely"
        )
    )
    clarification_question: str | None = Field(
        description=(
            "the exact question to send the customer: quote the source field, the values seen "
            "and the candidate target fields, and say what each choice would mean. null when no "
            "clarification is required"
        )
    )


class MappingProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset: str = Field(description="the dataset name, copied exactly as given")
    entity: str | None = Field(
        description="the target entity this dataset is about (organization, contact, ...)"
    )
    mappings: list[SuggestedMapping] = Field(
        description="one entry per source field, in the order the fields were given"
    )
    observations: list[str] = Field(
        description=(
            "things the engineer should know that are not about one field: conflicts between "
            "the customer's rules and the data, records that should not be migrated, "
            "assumptions made. empty list when there are none"
        )
    )


class BlockerExplanation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(description="the work item code, copied exactly as given")
    explanation: str = Field(
        description=(
            "two or three sentences for the customer's project sponsor: what is wrong, why "
            "Meridian cannot take these records as they are, and what the customer has to do. "
            "only numbers that appear in the facts"
        )
    )


class ReadinessSummary(BaseModel):
    """the executive summary of a readiness report. the status and every number are decided by
    code before the model is asked; the model writes the sentences around them."""

    model_config = ConfigDict(extra="forbid")

    headline: str = Field(
        description="one sentence that states the readiness status exactly as given"
    )
    summary: str = Field(
        description=(
            "one or two short paragraphs for the customer's sponsor and the implementation lead: "
            "where the migration stands, what the rehearsal showed, what stands between the "
            "customer and go-live. only numbers that appear in the facts, quoted exactly"
        )
    )
    blocker_explanations: list[BlockerExplanation] = Field(
        description="one entry for each work item listed under explain, in the order given"
    )

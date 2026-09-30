"""what the model is shown, and nothing more.

the brief for a dataset is built from the profiler's statistics, the deterministic comparison,
the target catalog and the customer's rules. rows never leave the database. sample values are
sent only for kinds of column where the values are the business vocabulary (statuses, plans,
tiers, codes, dates, amounts); for emails, phones, people's names, company names, free text and
urls the model gets shapes and counts instead.

`input_hash` is the cache key: same brief, same model, same prompt version, same answer."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from app.services.comparison import FieldComparison
from app.services.profiling import inference as inf
from app.services.profiling.types import DatasetProfile, FieldProfile
from app.target.catalog import TargetField
from app.target.schema import CROSS_RECORD_RULES, PLATFORM_NAME

PROMPT_VERSION = "2026-09-30.2"
RULES_MAX_CHARS = 24_000
NOTES_MAX_CHARS = 4_000
VALUES_MAX = 25
EXAMPLES_MAX = 3
SHAPES_MAX = 3

# free text a person wrote; the model gets its shape and length, not its content
_FREE_TEXT_HINTS = ("note", "comment", "description", "remark", "memo", "owner")
# values are sent for these kinds only when the column is a small vocabulary
_NEVER_VALUES = {"email", "phone", "url", "text"}

SYSTEM_PROMPT = f"""You are the field-mapping assistant inside an internal enterprise onboarding tool. An implementation engineer is migrating a customer's legacy data into {PLATFORM_NAME}, a B2B SaaS platform. You propose; the engineer and the customer decide.

You are given one source dataset at a time: its column names with statistics and a few sanitized example values, a deterministic name-based comparison against the target catalog, the full target catalog, and the customer's own business rules. You never see the data itself. Do not claim facts about the data beyond what the statistics say. Percentages in the brief run from 0 to 100 (null_percent 1.0 means one percent of rows).

For every source field, propose the target field it should land in, or null.

Rules:
- target_field must be a path from the catalog, written exactly as entity.field. Never invent a field.
- Prefer the entity this dataset is about. Map across entities only when the customer's rules or the field's meaning clearly call for it (an email on the account row that the rules say belongs to a contact), and say so in reason.
- A field with no home in the target gets target_field null. If it is clearly not needed for the migration (an internal owner, a marketing flag with no target field, a currency column the rules make redundant), set clarification_required false and explain why. If a person must decide what happens to it, set clarification_required true.
- When two targets are plausible, when the customer's rules contradict the data, or when the meaning of the values is a business decision: set clarification_required true and write the exact question to send the customer. Quote the source field, the values seen, and the candidate target fields, and say what each choice would mean for the migration.
- clarification_required is for mapping and transformation decisions only: which target, what a value means, how to translate a vocabulary the rules do not cover. How to handle blank keys, duplicates, malformed values and defaults for blanks is decided later, at validation, by the engineer; put those points in observations, not in a question.
- confidence is the probability that your target_field is the right destination. It is about the choice, not about data quality. Use 0.9 and above only when the name, the values and the rules all agree. Use 0.5 and below whenever a business decision is needed. A null target with a clear reason can carry high confidence.
- transformation_required is true whenever the values need a deterministic rule to fit the target: date formats, enum mapping tables, splitting a full name, casting text to numbers, normalizing phones to E.164, ISO country codes, defaults for blanks. Describe the rule in plain English in transformation, complete enough to implement without guessing, using the customer's rules where they apply. Cite the rule number when you use one. No code.
- The deterministic comparison is context, not truth. Its score is a name similarity. Where it says AMBIGUOUS or UNMAPPED, that is exactly where your judgment is needed; where it says MATCHED, check that the meaning agrees, not only the name.
- Put anything that is not about one field in observations: rule conflicts visible in the statistics, records that should not be migrated at all, assumptions you made.
- Answer every source field exactly once, in the order given, with source_field copied exactly. Answer in the requested structured format and nothing else."""


@dataclass(frozen=True)
class PromptBundle:
    system: str
    user: str
    inputs: dict[str, Any]
    input_hash: str
    provider: str
    model: str


def _is_free_text(name: str) -> bool:
    lowered = name.lower()
    return inf.has_name_hint(name) or any(h in lowered for h in _FREE_TEXT_HINTS)


def field_brief(field: FieldProfile) -> dict[str, Any]:
    """the statistics for one column, with sample values only where they are vocabulary."""
    s = field.stats
    t = field.inferred_type
    brief: dict[str, Any] = {
        "name": field.name,
        "type": t,
        "null_count": field.null_count,
        "null_percent": field.null_pct,
        "unique_percent": field.unique_pct,
        "distinct": field.distinct_count,
    }
    for key in ("placeholder_count", "whitespace_count", "normalized_duplicates"):
        if s.get(key):
            brief[key] = s[key]
    if s.get("raw_types"):
        brief["json_types"] = s["raw_types"]
    if s.get("case_variants"):
        brief["case_variant_groups"] = s.get("case_variant_groups", len(s["case_variants"]))

    shapes = [x["shape"] for x in s.get("shapes", [])[:SHAPES_MAX]]
    values = s.get("value_counts")
    small_vocabulary = (
        values is not None
        and len(values) <= VALUES_MAX
        and t not in _NEVER_VALUES
        and not _is_free_text(field.name)
    )

    if small_vocabulary:
        brief["values"] = dict(list(values.items())[:VALUES_MAX])  # type: ignore[union-attr]
    elif t in ("integer", "decimal"):
        if s.get("min") is not None:
            brief["range"] = [s["min"], s["max"]]
        if s.get("mean") is not None:
            brief["mean"] = s["mean"]
        for key in ("numeric_text_count", "float_text_count", "parsed_count"):
            if s.get(key):
                brief[key] = s[key]
        brief["examples"] = field.sample_values[:EXAMPLES_MAX]
    elif t in ("date", "datetime"):
        brief["formats"] = list(s.get("format_counts", {}))[:6]
        if s.get("min"):
            brief["range"] = [s["min"], s["max"]]
        brief["iso_percent"] = s.get("iso_pct")
        if s.get("future_count"):
            brief["future_count"] = s["future_count"]
    elif t == "email":
        brief["shape"] = "local@domain"
        if s.get("uppercase_count"):
            brief["uppercase_count"] = s["uppercase_count"]
    elif t == "phone":
        brief["shapes"] = shapes
        brief["e164_count"] = s.get("e164_count", 0)
    elif t == "url":
        brief["shapes"] = shapes
        if s.get("no_scheme_count"):
            brief["no_scheme_count"] = s["no_scheme_count"]
    elif t == "boolean":
        brief["spellings"] = list(s.get("spellings", {}))
    elif t in ("identifier", "code"):
        brief["shapes"] = shapes
        brief["examples"] = field.sample_values[:EXAMPLES_MAX]
    elif _is_free_text(field.name) or t == "text" and field.unique_pct > 50:
        brief["shapes"] = shapes
        brief["max_length"] = s.get("max_length")
    else:
        brief["examples"] = field.sample_values[:EXAMPLES_MAX]

    if s.get("malformed_count"):
        brief["malformed_count"] = s["malformed_count"]
        if t not in ("email", "phone") and s.get("malformed_examples"):
            brief["malformed_examples"] = s["malformed_examples"][:EXAMPLES_MAX]
    return brief


def comparison_brief(fc: FieldComparison | None) -> dict[str, Any] | None:
    if fc is None:
        return None
    return {
        "classification": fc.classification.value,
        "target": fc.target,
        "reason": fc.reason,
        "candidates": [
            {
                "target": c.target,
                "name_similarity": c.score,
                "compatibility": c.compatibility,
                "notes": c.notes[:2],
            }
            for c in fc.candidates[:3]
        ],
    }


def catalog_brief(catalog: list[TargetField]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for f in catalog:
        entry: dict[str, Any] = {
            "path": f.path,
            "type": f.type,
            "required": f.required,
        }
        if f.nullable:
            entry["nullable"] = True
        if f.enum_values:
            entry["enum_values"] = list(f.enum_values)
        if f.description:
            entry["description"] = f.description
        constraints = {k: v for k, v in f.constraints.items() if k in ("pattern", "format")}
        if constraints:
            entry["constraints"] = constraints
        if f.default is not None:
            entry["default"] = f.default.value if hasattr(f.default, "value") else f.default
        out.setdefault(f.entity, []).append(entry)
    return out


def _clip(text: str | None, limit: int) -> str | None:
    if not text:
        return None
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[truncated at {limit} characters]"


def build_mapping_prompt(
    *,
    profile: DatasetProfile,
    entity: str | None,
    comparisons: dict[str, FieldComparison],
    catalog: list[TargetField],
    customer: str,
    rules_text: str | None,
    project_notes: str | None,
    provider: str,
    model: str,
) -> PromptBundle:
    inputs: dict[str, Any] = {
        "customer": customer,
        "project_notes": _clip(project_notes, NOTES_MAX_CHARS),
        "dataset": {
            "name": profile.name,
            "entity_guess": entity,
            "row_count": profile.row_count,
            "column_count": profile.column_count,
            "key_column": profile.key_column,
            "key_missing": profile.key_missing,
            "key_duplicates": profile.key_duplicates,
            "exact_duplicate_rows": profile.exact_duplicate_rows,
            "references": [
                {
                    "column": r.column,
                    "references": r.references,
                    "resolved": r.resolved,
                    "orphans": r.orphans,
                }
                for r in profile.references
            ],
        },
        "fields": [
            {
                **field_brief(f),
                "deterministic_comparison": comparison_brief(comparisons.get(f.name)),
            }
            for f in profile.fields
        ],
        "target_catalog": catalog_brief(catalog),
        "target_cross_record_rules": list(CROSS_RECORD_RULES),
        "customer_business_rules": _clip(rules_text, RULES_MAX_CHARS),
    }
    user = (
        f"Propose a target field for every source field of the dataset `{profile.name}` "
        f"described below. Entity guess from the dataset name: {entity or 'unknown'}.\n\n"
        "```json\n" + json.dumps(inputs, indent=1, default=str, ensure_ascii=False) + "\n```"
    )
    digest = hashlib.sha256(
        json.dumps(
            {
                "prompt_version": PROMPT_VERSION,
                "system": SYSTEM_PROMPT,
                "provider": provider,
                "model": model,
                "inputs": inputs,
            },
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()
    return PromptBundle(
        system=SYSTEM_PROMPT,
        user=user,
        inputs=inputs,
        input_hash=digest,
        provider=provider,
        model=model,
    )


def clarification_template(
    source_field: str, values: list[str] | None, candidates: list[str]
) -> str:
    """a serviceable question when neither the model nor a person wrote one."""
    seen = f" with values such as {', '.join(values[:6])}" if values else ""
    if len(candidates) >= 2:
        options = ", ".join(candidates[:-1]) + f" or {candidates[-1]}"
        return (
            f"We found a source field named `{source_field}`{seen}. {PLATFORM_NAME} has "
            f"{options}. Which of these does `{source_field}` represent, and how should values "
            "that fit none of them be handled?"
        )
    if candidates:
        return (
            f"We found a source field named `{source_field}`{seen}. Should it be migrated as "
            f"{candidates[0]}? If not, what does it represent?"
        )
    return (
        f"We found a source field named `{source_field}`{seen} that has no obvious home in "
        f"{PLATFORM_NAME}. What does it represent, and does it need to be migrated?"
    )

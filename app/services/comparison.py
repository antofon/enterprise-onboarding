"""source schema vs target schema, deterministically.

this is the pass that runs before any model is involved. it can only know three kinds of thing:
what the column is called, what the profiler measured about its values, and what the target
catalog requires. so it says MATCHED only when the name lines up (exactly, or through a small
synonym table) inside the entity the file is about, it says AMBIGUOUS when there is a
plausible candidate a human should confirm, and it says UNMAPPED when nothing in the name helps.
the semantic work (acct_num -> organization_id, customer_tier -> ?) is left to the mapping
stage, where the model proposes and a person decides. the score here is name similarity, not
confidence, and the ui must call it that.
"""

from __future__ import annotations

import enum
import re
from collections import defaultdict
from typing import Any

from pydantic import BaseModel, Field

from app.services.profiling.types import DatasetProfile, FieldProfile
from app.target.catalog import TargetField, target_field_catalog


class Classification(enum.StrEnum):
    matched = "MATCHED"
    transformation_required = "TRANSFORMATION_REQUIRED"
    ambiguous = "AMBIGUOUS"
    unmapped = "UNMAPPED"
    incompatible = "INCOMPATIBLE"


class Candidate(BaseModel):
    target: str
    entity: str
    score: float = Field(description="name similarity, 0 to 1. not a confidence")
    match: str = Field(description="exact | synonym | partial")
    in_entity: bool
    compatibility: str = Field(description="ok | transform | incompatible")
    notes: list[str] = Field(default_factory=list)


class FieldComparison(BaseModel):
    dataset: str
    entity: str | None
    source_field: str
    inferred_type: str
    classification: Classification
    target: str | None = None
    candidates: list[Candidate] = Field(default_factory=list)
    reason: str


class EntityCoverage(BaseModel):
    entity: str
    datasets: list[str]
    required: list[str]
    covered: list[str]
    candidate_only: list[str]
    missing: list[str]


class ComparisonReport(BaseModel):
    datasets: dict[str, str | None]
    counts: dict[str, int]
    fields: list[FieldComparison]
    coverage: list[EntityCoverage]


# --- names -------------------------------------------------------------------------------------

SYNONYMS: dict[str, str] = {
    "acct": "account",
    "acc": "account",
    "account": "organization",
    "company": "organization",
    "co": "organization",
    "org": "organization",
    "num": "number",
    "no": "number",
    "nbr": "number",
    "ref": "id",
    "identifier": "id",
    "key": "id",
    "uid": "id",
    "mail": "email",
    "tel": "phone",
    "telephone": "phone",
    "mobile": "phone",
    "cell": "phone",
    "dt": "date",
    "dte": "date",
    "ts": "datetime",
    "timestamp": "datetime",
    "amt": "amount",
    "qty": "quantity",
    "cnt": "count",
    "freq": "cycle",
    "frequency": "cycle",
    "period": "cycle",
    "interval": "cycle",
    "sub": "subscription",
    "subscr": "subscription",
    "renew": "renewal",
    "desc": "description",
    "descr": "description",
    "web": "website",
    "url": "website",
    "site": "website",
    "homepage": "website",
    "st": "region",
    "state": "region",
    "province": "region",
    "ctry": "country",
    "cntry": "country",
    "type": "kind",
    "category": "kind",
    "stat": "status",
    "rev": "revenue",
    "emp": "employee",
}
# tokens that appear everywhere and therefore say little on their own
GENERIC = frozenset(
    {"id", "name", "number", "code", "count", "date", "datetime", "kind", "value", "flag", "usd"}
    | {"customer", "organization", "hq", "billing", "primary", "is", "at", "on", "the"}
    | {"last", "first", "full", "total", "new", "old"}
)
ENTITY_WORDS: dict[str, str] = {
    "organization": "organization",
    "account": "organization",
    "company": "organization",
    "customer": "organization",
    "contact": "contact",
    "person": "contact",
    "people": "contact",
    "subscription": "subscription",
    "activity": "activity",
    "event": "activity",
}
ENTITY_PREFIXES: dict[str, set[str]] = {
    "organization": {"organization", "account", "company", "org", "acct", "co"},
    "contact": {"contact"},
    "subscription": {"subscription", "sub"},
    "activity": {"activity"},
}

MATCH_THRESHOLD = 0.7
AMBIGUOUS_THRESHOLD = 0.4
CROSS_ENTITY_THRESHOLD = 0.6
CLEAR_LEAD = 0.25

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SPLIT = re.compile(r"[^a-z0-9]+")


def singular(token: str) -> str:
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("s") and not token.endswith(("ss", "us", "is")) and len(token) > 3:
        return token[:-1]
    return token


def tokens(name: str) -> list[str]:
    """'acct_num' -> ['organization', 'number'], 'contactMail' -> ['contact', 'email']."""
    parts = _SPLIT.split(_CAMEL.sub("_", name).lower())
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        part = singular(part)
        part = SYNONYMS.get(part, part)
        part = SYNONYMS.get(part, part)  # acct -> account -> organization
        out.append(part)
    return out


def infer_entity(dataset_name: str) -> str | None:
    for token in tokens(dataset_name.rsplit(".", 1)[0]):
        if token in ENTITY_WORDS:
            return ENTITY_WORDS[token]
    return None


def _weight(token: str) -> float:
    return 0.5 if token in GENERIC else 1.0


def name_score(source: list[str], target: list[str]) -> float:
    """weighted jaccard and containment, averaged. exact token sets score 1."""
    s, t = set(source), set(target)
    if not s or not t:
        return 0.0
    if s == t:
        return 1.0
    shared = s & t
    inter = sum(_weight(x) for x in shared)
    if inter == 0:
        return 0.0
    union = sum(_weight(x) for x in s | t)
    if shared <= GENERIC:
        # only generic words in common ("name", "last"): no containment bonus, jaccard alone
        return round(inter / union, 2)
    smaller = min(sum(_weight(x) for x in s), sum(_weight(x) for x in t))
    return round((inter / union + inter / smaller) / 2, 2)


def _strip_entity(toks: list[str], entity: str | None) -> list[str]:
    if (
        entity
        and len(toks) > 1
        and toks[0] in {SYNONYMS.get(p, p) for p in ENTITY_PREFIXES[entity]}
    ):
        return toks[1:]
    return toks


# --- compatibility ---------------------------------------------------------------------------


def _norm_enum(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", value.strip().lower())).strip("_")


def _enum_compatibility(field: FieldProfile, target: TargetField) -> tuple[str, list[str]]:
    values: dict[str, int] | None = field.stats.get("value_counts")
    enum_values = target.enum_values or ()
    if values is None:
        return "incompatible", [
            f"{field.distinct_count} distinct values, too many for a {len(enum_values)}-value enum"
        ]
    allowed = {_norm_enum(e): e for e in enum_values}
    unknown = [v for v in values if _norm_enum(v) not in allowed]
    matched_rows = sum(c for v, c in values.items() if _norm_enum(v) in allowed)
    if not unknown:
        if all(v in enum_values for v in values):
            return "ok", []
        return "transform", ["values match the enum once case and spacing are normalized"]
    shown = ", ".join(unknown[:5]) + ("…" if len(unknown) > 5 else "")
    if matched_rows == 0:
        return "transform", [f"no value matches the enum, a mapping table is needed ({shown})"]
    return "transform", [f"{len(unknown)} of {len(values)} values are not in the enum: {shown}"]


def _pattern_failures(field: FieldProfile, pattern: str) -> tuple[int, int, list[str]]:
    """how many of the known distinct values fail the target's regex."""
    stats = field.stats
    sample: list[str] = list(stats.get("value_counts") or stats.get("distinct_sample") or [])
    rx = re.compile(pattern)
    failing = [v for v in sample if not rx.match(v)]
    return len(failing), len(sample), failing[:5]


def compatibility(field: FieldProfile, target: TargetField) -> tuple[str, list[str]]:
    """ok: values can land as they are. transform: a deterministic rule gets them there.
    incompatible: no rule we have turns this column into that field."""
    s = field.stats
    st, tt = field.inferred_type, target.type
    notes: list[str] = []
    if st == "empty":
        return "incompatible", ["source column is empty"]
    if tt == "enum":
        return _enum_compatibility(field, target)

    if tt in ("date", "datetime"):
        if st not in ("date", "datetime"):
            return "incompatible", [f"source looks like {st}, target needs a {tt}"]
        formats = s.get("format_counts", {})
        if len(formats) > 1 or s.get("iso_pct", 100) < 100:
            notes.append(f"{len(formats)} date formats, {s.get('iso_pct', 0)}% ISO 8601")
        if s.get("malformed_count"):
            notes.append(f"{s['malformed_count']} values do not parse as dates")
        if st == "date" and tt == "datetime":
            notes.append("dates only, no time of day")
        if st == "datetime" and tt == "date":
            notes.append("time of day will be dropped")
        return ("transform" if notes else "ok"), notes

    if tt in ("integer", "decimal"):
        if st not in ("integer", "decimal"):
            return "incompatible", [f"source looks like {st}, target needs a {tt}"]
        if s.get("numeric_text_count"):
            notes.append(f"{s['numeric_text_count']} values carry symbols or separators")
        if s.get("float_text_count") and tt == "integer":
            notes.append(f"{s['float_text_count']} whole numbers written with a decimal point")
        if s.get("placeholder_count"):
            notes.append(f"{s['placeholder_count']} placeholders (n/a, unknown) become null")
        if st == "decimal" and tt == "integer":
            notes.append("decimals into an integer field, a rounding rule is needed")
        if len(s.get("raw_types", {})) > 1:
            notes.append("typed inconsistently in the export, needs a cast")
        minimum = target.constraints.get("minimum")
        if minimum is not None and s.get("min") is not None and s["min"] < minimum:
            notes.append(f"values below the target minimum of {minimum} (min {s['min']})")
        return ("transform" if notes else "ok"), notes

    if tt == "boolean":
        if st != "boolean":
            return "incompatible", [f"source looks like {st}, target needs true/false"]
        spellings = s.get("spellings", {})
        if set(spellings) - {"true", "false"}:
            notes.append(f"{len(spellings)} spellings of yes/no")
        return ("transform" if notes else "ok"), notes

    if tt == "email":
        if st != "email":
            return "incompatible", [f"source looks like {st}, target needs an email"]
        if s.get("malformed_count"):
            notes.append(f"{s['malformed_count']} malformed emails will be rejected or fixed")
        if s.get("uppercase_count"):
            notes.append(f"{s['uppercase_count']} upper-case emails to normalize")
        return ("transform" if notes else "ok"), notes

    if tt == "url":
        if st != "url":
            return "incompatible", [f"source looks like {st}, target needs a url"]
        if s.get("no_scheme_count"):
            notes.append(f"{s['no_scheme_count']} urls without http(s)://")
        if s.get("placeholder_count"):
            notes.append(f"{s['placeholder_count']} placeholders (N/A) become null")
        if s.get("malformed_count"):
            notes.append(f"{s['malformed_count']} values are not urls")
        return ("transform" if notes else "ok"), notes

    if tt.startswith("list["):
        return "transform", ["target is a list, source is a scalar; a split rule is needed"]

    # plain strings, identifiers, country codes, phones: constraints decide
    if st == "phone":
        if s.get("e164_count", 0) < s.get("non_null_count", 0):
            notes.append("E.164 normalization")
        if s.get("malformed_count"):
            notes.append(f"{s['malformed_count']} numbers are too short to be valid")
        return ("transform" if notes else "ok"), notes
    pattern = target.constraints.get("pattern")
    if pattern:
        failing, checked, examples = _pattern_failures(field, pattern)
        if failing:
            shown = ", ".join(examples)
            notes.append(f"{failing} of {checked} known values fail the target pattern ({shown})")
            if failing == checked and st in ("text", "category"):
                return "incompatible", notes
    max_length = target.constraints.get("maxLength")
    if max_length and s.get("max_length", 0) > max_length:
        notes.append(f"values up to {s['max_length']} chars, target allows {max_length}")
    status = "transform" if notes else "ok"
    if s.get("whitespace_count"):
        # the target models strip whitespace themselves, so this is a note, not a transform
        notes.append(f"{s['whitespace_count']} values need trimming")
    return status, notes


# --- the comparison ----------------------------------------------------------------------------


def _match_kind(source_name: str, target: TargetField, score: float) -> str:
    if score < 1.0:
        return "partial"
    src = _SPLIT.split(source_name.lower())
    tgt = _SPLIT.split(target.name.lower())
    return "exact" if [p for p in src if p] == [p for p in tgt if p] else "synonym"


def _candidates(
    field: FieldProfile, entity: str | None, catalog: list[TargetField]
) -> list[Candidate]:
    raw = tokens(field.name)
    stripped = _strip_entity(raw, entity)
    out: list[Candidate] = []
    for target in catalog:
        t_raw = tokens(target.name)
        t_stripped = _strip_entity(t_raw, target.entity)
        # the entity's own name never counts as a match: "sub_status" vs "subscription_id"
        # share nothing once "sub" and "subscription" are set aside
        score = name_score(stripped, t_stripped)
        if t_stripped != t_raw:
            score = max(score, name_score(stripped, t_raw))
        if score < AMBIGUOUS_THRESHOLD:
            continue
        in_entity = entity is None or target.entity == entity
        if not in_entity and score < CROSS_ENTITY_THRESHOLD:
            continue
        compat, notes = compatibility(field, target)
        out.append(
            Candidate(
                target=target.path,
                entity=target.entity,
                score=score,
                match=_match_kind(field.name, target, score),
                in_entity=in_entity,
                compatibility=compat,
                notes=notes,
            )
        )
    out.sort(key=lambda c: (not c.in_entity, -c.score, c.target))
    return out


def _classify(
    field: FieldProfile, dataset: str, entity: str | None, catalog: list[TargetField]
) -> FieldComparison:
    candidates = _candidates(field, entity, catalog)
    in_entity = [c for c in candidates if c.in_entity]
    base: dict[str, Any] = dict(
        dataset=dataset,
        entity=entity,
        source_field=field.name,
        inferred_type=field.inferred_type,
        candidates=candidates[:5],
    )
    if not candidates:
        return FieldComparison(
            **base,
            classification=Classification.unmapped,
            reason="no target field shares a name token; needs semantic mapping",
        )
    best = in_entity[0] if in_entity else None
    runner_up = in_entity[1].score if len(in_entity) > 1 else 0.0
    clear = (
        best is not None
        and best.score >= MATCH_THRESHOLD
        and (runner_up < MATCH_THRESHOLD or best.score - runner_up >= CLEAR_LEAD)
    )
    if clear and entity is None:
        # nobody told us which entity the file is about: a strong name in two entities
        # ("status" in organization and subscription) is a question, not a match
        strong = {c.entity for c in in_entity if c.score >= MATCH_THRESHOLD}
        clear = len(strong) == 1
    if not clear:
        top = candidates[0]
        where = "" if top.in_entity else f" in another entity ({top.entity})"
        reason = f"best name candidate {top.target}{where} at {top.score}; a person confirms"
        if best is not None and runner_up >= MATCH_THRESHOLD:
            reason = f"{best.target} and {in_entity[1].target} both fit the name; a person picks"
        return FieldComparison(**base, classification=Classification.ambiguous, reason=reason)
    assert best is not None
    how = {"exact": "exact name match", "synonym": "name match after synonyms"}.get(
        best.match, f"partial name match at {best.score}"
    )
    if best.compatibility == "incompatible":
        cls, reason = Classification.incompatible, f"{how}, but {'; '.join(best.notes)}"
    elif best.compatibility == "transform":
        cls, reason = Classification.transformation_required, f"{how}; {'; '.join(best.notes)}"
    else:
        extra = f" ({'; '.join(best.notes)})" if best.notes else ""
        cls, reason = Classification.matched, f"{how}, values fit as they are{extra}"
    return FieldComparison(**base, classification=cls, target=best.target, reason=reason)


def compare(
    profiles: list[DatasetProfile],
    catalog: list[TargetField] | None = None,
    entities: dict[str, str | None] | None = None,
) -> ComparisonReport:
    """classify every source field against the target catalog. `entities` overrides the entity
    inferred from a dataset's name (a human's call always beats the heuristic)."""
    catalog = catalog if catalog is not None else target_field_catalog()
    entities = entities or {}
    datasets: dict[str, str | None] = {}
    fields: list[FieldComparison] = []
    for profile in profiles:
        entity = entities.get(profile.name, infer_entity(profile.name))
        datasets[profile.name] = entity
        for field in profile.fields:
            fields.append(_classify(field, profile.name, entity, catalog))

    counts = {c.value: 0 for c in Classification}
    for fc in fields:
        counts[fc.classification.value] += 1

    coverage: list[EntityCoverage] = []
    by_entity: dict[str, list[str]] = defaultdict(list)
    for name, entity in datasets.items():
        if entity:
            by_entity[entity].append(name)
    for entity, names in sorted(by_entity.items()):
        required = [t.path for t in catalog if t.entity == entity and t.required]
        covered: set[str] = set()
        candidate_only: set[str] = set()
        for fc in fields:
            if fc.dataset not in names:
                continue
            if fc.target and fc.classification in (
                Classification.matched,
                Classification.transformation_required,
            ):
                covered.add(fc.target)
            elif fc.classification == Classification.ambiguous:
                candidate_only.update(c.target for c in fc.candidates if c.in_entity)
        coverage.append(
            EntityCoverage(
                entity=entity,
                datasets=names,
                required=required,
                covered=[r for r in required if r in covered],
                candidate_only=[r for r in required if r not in covered and r in candidate_only],
                missing=[r for r in required if r not in covered and r not in candidate_only],
            )
        )
    return ComparisonReport(datasets=datasets, counts=counts, fields=fields, coverage=coverage)


def field_comparison_dict(report: ComparisonReport) -> dict[str, dict[str, Any]]:
    """{dataset: {source_field: comparison}} for callers that index by name."""
    out: dict[str, dict[str, Any]] = defaultdict(dict)
    for fc in report.fields:
        out[fc.dataset][fc.source_field] = fc
    return out

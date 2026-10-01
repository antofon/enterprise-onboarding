"""check every transformed record before anything is sent anywhere.

Three layers, all deterministic, none of them asking a model anything:

  the contract      every record is validated against the pydantic model in app/target/schema.py,
                    which is the same contract the target api enforces. types, enums, patterns,
                    required fields, renewal after start.
  the invariants     rules that need the whole batch and belong to the platform whatever the
                    customer: identifiers unique per entity, every child's organization present,
                    one primary contact per organization, one address per organization, no active
                    subscription under a dormant account, no dates in the future.
  the customer's     checks from the customer's own rules document, enabled and parameterised in
                    configuration, that flag a conflict rather than change the data.

A record carrying an error is never sent to the target. A record carrying only warnings is sent,
and the warning stays on the run so somebody reads it before go-live.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pydantic import ValidationError

from app.core.logging import get_logger
from app.models.target import ID_COLUMN, WRITE_ORDER
from app.services.profiling.types import Severity
from app.services.transform.config import TransformationConfig
from app.services.transform.engine import TransformResult
from app.services.transform.types import RecordDraft, RecordIssue
from app.target.schema import ENTITIES

log = get_logger(__name__)

# pydantic's own error codes, said the way an implementation engineer would say them
ERROR_TYPES: dict[str, str] = {
    "missing": "missing_required",
    "enum": "invalid_enum_value",
    "string_pattern_mismatch": "invalid_format",
    "string_too_short": "invalid_format",
    "string_too_long": "value_too_long",
    "value_error": "invalid_value",
    "greater_than_equal": "out_of_range",
    "less_than_equal": "out_of_range",
    "int_parsing": "invalid_number",
    "decimal_parsing": "invalid_number",
    "date_from_datetime_parsing": "invalid_date",
    "date_parsing": "invalid_date",
    "datetime_parsing": "invalid_date",
    "url_parsing": "invalid_format",
    "url_scheme": "invalid_format",
    "bool_parsing": "invalid_boolean",
    "extra_forbidden": "unknown_target_field",
}
ACTIVE_SUBSCRIPTION = ("trial", "active")
DORMANT_ORGANIZATION = ("inactive", "churned")


@dataclass
class ValidationOutcome:
    """what validation decided, per entity, plus every issue it found."""

    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    issues: list[RecordIssue] = field(default_factory=list)
    valid: dict[str, list[RecordDraft]] = field(default_factory=dict)
    duration_ms: float = 0.0

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity is Severity.error)

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity is Severity.warning)

    def issue_counts(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for issue in self.issues:
            counts[issue.error_type] += 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def by_severity(self) -> dict[str, int]:
        counts = {s.value: 0 for s in Severity}
        for issue in self.issues:
            counts[issue.severity.value] += 1
        return counts

    def blocked(self) -> dict[str, int]:
        """records that cannot migrate, per entity."""
        return {e: c["invalid"] for e, c in self.counts.items() if c["invalid"]}


def _field_path(location: tuple[Any, ...]) -> str | None:
    return ".".join(str(part) for part in location) if location else None


def _validate_contract(draft: RecordDraft) -> None:
    """the target's own pydantic model, which is also what the api will apply."""
    model = ENTITIES[draft.entity]
    try:
        model.model_validate(draft.payload)
    except ValidationError as exc:
        for error in exc.errors():
            code = str(error.get("type", "value_error"))
            draft.add_issue(
                ERROR_TYPES.get(code, code),
                f"{_field_path(error.get('loc', ())) or draft.entity}: {error.get('msg')}",
                severity=Severity.error,
                field_name=_field_path(error.get("loc", ())),
                value=error.get("input"),
            )


def _check_unique_identifiers(drafts: list[RecordDraft], entity: str) -> None:
    seen: dict[str, RecordDraft] = {}
    for draft in drafts:
        if draft.skipped or draft.errors:
            continue
        record_id = draft.record_id
        if record_id is None:
            continue
        first = seen.get(record_id)
        if first is None:
            seen[record_id] = draft
            continue
        draft.add_issue(
            "duplicate_identifier",
            f"{ID_COLUMN[entity]} {record_id} already appears in {first.dataset} row "
            f"{first.source_row}; the target would refuse the second record",
            severity=Severity.error,
            field_name=ID_COLUMN[entity],
            value=record_id,
        )


def _check_parents(records: dict[str, list[RecordDraft]]) -> None:
    """a child whose organization will not migrate cannot migrate either.

    Two different problems wear the same shape, and an implementation engineer fixes them
    differently, so they are reported differently: the account is in the export but cannot
    migrate (fix the account and the children follow), or the account is not in the export at
    all (the customer owes us the data).
    """
    migrating: set[str] = set()
    rejected: dict[str, RecordDraft] = {}
    for draft in records.get("organization", []):
        if not draft.record_id:
            continue
        if not draft.skipped and not draft.errors:
            migrating.add(draft.record_id)
        else:
            rejected[draft.record_id] = draft

    for entity in ("contact", "subscription", "activity"):
        for draft in records.get(entity, []):
            if draft.skipped or draft.errors:
                continue
            parent = draft.payload.get("organization_id")
            if parent is None or str(parent) in migrating:
                continue
            blocked = rejected.get(str(parent))
            if blocked is not None:
                reasons = ", ".join(sorted({i.error_type for i in blocked.errors})) or "skipped"
                draft.add_issue(
                    "parent_record_rejected",
                    f"organization {parent} is in the export but cannot migrate ({reasons}), so "
                    f"this {entity} has nothing to hang off. Fixing the account releases it",
                    severity=Severity.error,
                    field_name="organization_id",
                    value=parent,
                )
            else:
                draft.add_issue(
                    "missing_relationship",
                    f"organization {parent} is not in the customer's export at all, so this "
                    f"{entity} points at an account that does not exist",
                    severity=Severity.error,
                    field_name="organization_id",
                    value=parent,
                )


def _check_contacts(records: dict[str, list[RecordDraft]]) -> None:
    by_org: dict[str, list[RecordDraft]] = defaultdict(list)
    for draft in records.get("contact", []):
        if draft.skipped or draft.errors:
            continue
        org_id = draft.payload.get("organization_id")
        if org_id:
            by_org[str(org_id)].append(draft)

    for org_id, contacts in by_org.items():
        primaries = [c for c in contacts if c.payload.get("is_primary")]
        for extra in primaries[1:]:
            extra.add_issue(
                "duplicate_primary_contact",
                f"organization {org_id} already has a primary contact "
                f"({primaries[0].payload.get('contact_id')}); Meridian allows one",
                severity=Severity.error,
                field_name="is_primary",
                rule="platform rule: one primary contact per organization",
            )
        if not primaries:
            contacts[0].add_issue(
                "no_primary_contact",
                f"organization {org_id} has {len(contacts)} contacts and none is the primary; "
                "customer rule 1 says every account has exactly one",
                severity=Severity.warning,
                field_name="is_primary",
                rule="customer rule 1",
            )
        seen: dict[str, RecordDraft] = {}
        for contact in contacts:
            email = str(contact.payload.get("email", "")).lower()
            if not email:
                continue
            first = seen.setdefault(email, contact)
            if first is not contact:
                contact.add_issue(
                    "duplicate_contact_email",
                    f"{email} is already contact {first.payload.get('contact_id')} on "
                    f"organization {org_id}; Meridian keeps addresses unique per account",
                    severity=Severity.error,
                    field_name="email",
                    value=email,
                )


def _check_lifecycle(records: dict[str, list[RecordDraft]]) -> None:
    """Meridian refuses a live subscription under a dormant account. Where the account only became
    dormant because of the customer's own dormancy rule, the message says so: the conflict is
    between two of the customer's rules, and a person has to settle it."""
    organizations = {
        d.record_id: d for d in records.get("organization", []) if not d.skipped and d.record_id
    }
    for draft in records.get("subscription", []):
        if draft.skipped or draft.errors:
            continue
        org_id = str(draft.payload.get("organization_id", ""))
        org = organizations.get(org_id)
        if org is None:
            continue
        org_status = org.payload.get("lifecycle_status")
        if org_status not in DORMANT_ORGANIZATION:
            continue
        if draft.payload.get("status") not in ACTIVE_SUBSCRIPTION:
            continue
        dormancy = next(
            (r for r in org.applied_rules if r.startswith("dormant_account_inactive")), None
        )
        because = (
            f"the account was set {org_status} by {dormancy} rather than by the CRM, and billing "
            "still shows the subscription live"
            if dormancy
            else f"the CRM says the account is {org_status} and billing says the subscription is "
            f"{draft.payload.get('status')}"
        )
        draft.add_issue(
            "business_rule_violation",
            f"organization {org_id} migrates as {org_status} and cannot hold a "
            f"{draft.payload.get('status')} subscription: {because}. Somebody has to decide which "
            "system is right",
            severity=Severity.error,
            field_name="status",
            value=draft.payload.get("status"),
            rule="platform rule: no live subscription under a dormant account",
        )


def _check_future_dates(records: dict[str, list[RecordDraft]], as_of: date) -> None:
    watched = {
        "organization": ("customer_since",),
        "subscription": ("start_date",),
        "activity": ("occurred_at",),
    }
    for entity, fields in watched.items():
        for draft in records.get(entity, []):
            if draft.skipped or draft.errors:
                continue
            for name in fields:
                value = draft.payload.get(name)
                if not value:
                    continue
                when = str(value)[:10]
                try:
                    parsed = date.fromisoformat(when)
                except ValueError:
                    continue
                if parsed > as_of:
                    draft.add_issue(
                        "date_in_future",
                        f"{name} is {when}, after the {as_of.isoformat()} migration date",
                        severity=Severity.warning,
                        field_name=name,
                        value=when,
                    )


# --- the customer's own cross-checks -------------------------------------------------------------


def _check_strategic_plan(
    records: dict[str, list[RecordDraft]], params: dict[str, Any], citation: str | None
) -> None:
    """Customer rule 4: strategic accounts always carry an Enterprise subscription, and where
    billing shows something else billing is wrong.

    This flags the conflict and changes nothing. The tier column it reads has no target field of
    its own (the customer still owes us an answer on where tier belongs), and rewriting a
    subscription plan from a column nobody has agreed to map would be exactly the kind of quiet
    decision this tool exists to prevent.
    """
    column = str(params.get("source_field", "customer_tier"))
    tier = str(params.get("tier", "strategic")).lower()
    required = str(params.get("required_plan", "enterprise"))
    strategic = {
        d.record_id
        for d in records.get("organization", [])
        if not d.skipped and str(d.raw.get(column, "")).strip().lower() == tier and d.record_id
    }
    if not strategic:
        return
    for draft in records.get("subscription", []):
        if draft.skipped or draft.errors:
            continue
        org_id = str(draft.payload.get("organization_id", ""))
        if org_id in strategic and draft.payload.get("plan") != required:
            draft.add_issue(
                "plan_conflicts_with_tier",
                f"organization {org_id} is {params.get('tier', 'Strategic')} in the CRM, which "
                f"customer rule 4 says means a {required} subscription, and billing says "
                f"{draft.payload.get('plan')}. Flagged, not changed",
                severity=Severity.warning,
                field_name="plan",
                value=draft.payload.get("plan"),
                rule=f"customer rule {citation}" if citation else "customer rule",
            )


CUSTOMER_CHECKS = {"strategic_requires_enterprise": _check_strategic_plan}


def validate(
    transformed: TransformResult, *, config: TransformationConfig | None = None
) -> ValidationOutcome:
    started = time.perf_counter()
    records = transformed.records
    as_of = transformed.plan.as_of

    for entity in WRITE_ORDER:
        for draft in records.get(entity, []):
            if draft.skipped or draft.errors:
                continue
            _validate_contract(draft)

    # order matters: a record already rejected is not re-reported by the batch checks
    for entity in WRITE_ORDER:
        if records.get(entity):
            _check_unique_identifiers(records[entity], entity)
    _check_parents(records)
    _check_contacts(records)
    _check_lifecycle(records)
    _check_future_dates(records, as_of)

    if config is not None:
        for rule_id, settings in config.validation_rules.items():
            check = CUSTOMER_CHECKS.get(rule_id)
            if settings.enabled and check is not None:
                check(records, settings.params, settings.rule)

    outcome = ValidationOutcome()
    for entity in transformed.plan.entities:
        drafts = records.get(entity, [])
        skipped = [d for d in drafts if d.skipped]
        live = [d for d in drafts if not d.skipped]
        valid = [d for d in live if not d.errors]
        outcome.valid[entity] = valid
        outcome.counts[entity] = {
            "built": len(drafts),
            "skipped_by_rule": len(skipped),
            "valid": len(valid),
            "invalid": len(live) - len(valid),
            "with_warnings": sum(
                1 for d in valid if any(i.severity is Severity.warning for i in d.issues)
            ),
        }
        outcome.issues.extend(issue for d in drafts for issue in d.issues)

    outcome.duration_ms = round((time.perf_counter() - started) * 1000, 1)
    log.info(
        "validation_complete",
        duration_ms=outcome.duration_ms,
        errors=outcome.error_count,
        warnings=outcome.warning_count,
        **{e: c["valid"] for e, c in outcome.counts.items()},
    )
    return outcome


__all__ = ["ValidationOutcome", "validate"]

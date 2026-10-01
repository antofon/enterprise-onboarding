"""the customer's rules that need more than one column, or more than one file.

A converter sees one value. These see the whole record, and the cross-dataset ones see every
record. Each is a named function, enabled and parameterised from configuration, citing the rule
in the customer's own document that it implements. None of them is generated, and none of them
runs code that came from a model.

Every rule leaves its name on the record it touched, so the migration report can say "847
accounts were set inactive by customer rule 2" and a person can go and check one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.core.logging import get_logger
from app.services.profiling.types import Severity
from app.services.transform.config import RecordRuleConfig, TransformationConfig
from app.services.transform.converters import lookup_key, parse_date_value
from app.services.transform.types import RecordDraft

log = get_logger(__name__)

# ISO 3166-2:CA. not customer-specific, so it belongs in code rather than in a customer's file
CA_PROVINCES = frozenset(
    {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}
)


@dataclass
class RuleContext:
    rule_id: str
    config: TransformationConfig
    params: dict[str, Any]
    citation: str | None = None

    @property
    def label(self) -> str:
        return f"{self.rule_id} (customer rule {self.citation})" if self.citation else self.rule_id


RecordRule = Callable[[RecordDraft, RuleContext], None]
REGISTRY: dict[str, RecordRule] = {}


def record_rule(name: str) -> Callable[[RecordRule], RecordRule]:
    def register(fn: RecordRule) -> RecordRule:
        REGISTRY[name] = fn
        return fn

    return register


def _raw_for(draft: RecordDraft, target_path: str) -> Any:
    """the source value behind a target field, as it was in the file."""
    source_field = draft.sources.get(target_path)
    return None if source_field is None else draft.raw.get(source_field)


def _months_before(anchor: date, months: int) -> date:
    """calendar months back, clamped to the end of the month (31 March minus one month is 28/29
    February). `timedelta` cannot express this and 30-day arithmetic drifts."""
    total = anchor.year * 12 + (anchor.month - 1) - months
    year, month_index = divmod(total, 12)
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    lengths = (31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    return date(year, month_index + 1, min(anchor.day, lengths[month_index]))


@record_rule("ca_country_from_region")
def ca_country_from_region(draft: RecordDraft, ctx: RuleContext) -> None:
    """Customer rule 5. `CA` means Canada in the billing system, but in old CRM rows it sometimes
    meant California. The state column settles it; where it cannot, the row is flagged rather
    than decided."""
    if draft.entity != "organization":
        return
    raw_country = _raw_for(draft, "organization.billing_country")
    if raw_country is None or lookup_key(str(raw_country)) != "ca":
        return
    region = draft.payload.get("billing_region")
    if region in CA_PROVINCES:
        draft.applied(ctx.label)
        draft.note("billing_country", f"CA with province {region}: read as Canada (rule 5)")
        return
    if region:
        draft.payload["billing_country"] = "US"
        draft.applied(ctx.label)
        draft.note("billing_country", f"CA with state {region}: read as California, US (rule 5)")
        return
    draft.add_issue(
        "ambiguous_country",
        "country is CA and there is no state to tell Canada from California (customer rule 5)",
        severity=Severity.warning,
        field_name="billing_country",
        value=raw_country,
        rule=ctx.label,
    )


@record_rule("dormant_account_inactive")
def dormant_account_inactive(draft: RecordDraft, ctx: RuleContext) -> None:
    """Customer rule 2. No recorded activity in 18 months means the account is inactive whatever
    the CRM status says. The date column has no target field of its own; it is read from the
    source row."""
    if draft.entity != "organization":
        return
    column = str(ctx.params.get("source_field", "last_activity_date"))
    months = int(ctx.params.get("months", 18))
    last_activity = parse_date_value(draft.raw.get(column), ctx.config)
    if last_activity is None:
        return
    cutoff = _months_before(ctx.config.as_of, months)
    if last_activity >= cutoff:
        return
    if draft.payload.get("lifecycle_status") not in ("active", "prospect"):
        return
    was = draft.payload["lifecycle_status"]
    draft.payload["lifecycle_status"] = "inactive"
    draft.applied(ctx.label)
    draft.note(
        "lifecycle_status",
        f"{was} in the CRM but last activity {last_activity.isoformat()}, "
        f"over {months} months before {ctx.config.as_of.isoformat()}: migrated as inactive",
    )


@record_rule("legacy_gold_seat_split")
def legacy_gold_seat_split(draft: RecordDraft, ctx: RuleContext) -> None:
    """Customer rule 7. `Legacy Gold` is the old Enterprise plan, unless it has fewer than ten
    seats, in which case it is Professional. The value map handles the common case; the seat
    count is what this rule adds."""
    if draft.entity != "subscription":
        return
    alias = str(ctx.params.get("alias", "legacy gold"))
    raw_plan = _raw_for(draft, "subscription.plan")
    if raw_plan is None or lookup_key(str(raw_plan)) != alias:
        return
    seats = draft.payload.get("seats")
    if seats is None:
        draft.add_issue(
            "undecidable_plan",
            f"{raw_plan!r} needs the seat count to choose a plan (customer rule 7) and seats "
            "is empty",
            severity=Severity.error,
            field_name="plan",
            value=raw_plan,
            rule=ctx.label,
        )
        return
    minimum = int(ctx.params.get("min_seats", 10))
    if int(seats) < minimum:
        small = str(ctx.params.get("small_plan", "professional"))
        draft.payload["plan"] = small
        draft.applied(ctx.label)
        draft.note(
            "plan", f"{raw_plan!r} with {seats} seats (under {minimum}): migrated as {small}"
        )


@record_rule("drop_dead_trials")
def drop_dead_trials(draft: RecordDraft, ctx: RuleContext) -> None:
    """Customer rule 11. A trial older than ninety days is dead and does not migrate at all.
    The record is skipped, counted and named, never silently dropped."""
    if draft.entity != "subscription":
        return
    if draft.payload.get("status") != "trial":
        return
    start = draft.payload.get("start_date")
    if not start:
        return
    age_days = (ctx.config.as_of - date.fromisoformat(str(start))).days
    maximum = int(ctx.params.get("max_age_days", 90))
    if age_days > maximum:
        draft.applied(ctx.label)
        draft.skip(
            f"trial started {start}, {age_days} days before {ctx.config.as_of.isoformat()}: "
            f"dead trials over {maximum} days do not migrate (customer rule 11)",
            rule=ctx.label,
        )


# --- rules that need every record ---------------------------------------------------------------

CrossDatasetRule = Callable[[dict[str, list[RecordDraft]], RuleContext], dict[str, int]]
CROSS_REGISTRY: dict[str, CrossDatasetRule] = {}


def cross_dataset_rule(name: str) -> Callable[[CrossDatasetRule], CrossDatasetRule]:
    def register(fn: CrossDatasetRule) -> CrossDatasetRule:
        CROSS_REGISTRY[name] = fn
        return fn

    return register


@cross_dataset_rule("primary_from_account_email")
def primary_from_account_email(
    records: dict[str, list[RecordDraft]], ctx: RuleContext
) -> dict[str, int]:
    """Customer rule 1. Every account has exactly one primary contact, and where the CRM flags
    none, the `primary_contact_email` on the account row is authoritative.

    What this rule does is flag the contact that address belongs to. What it refuses to do is
    create a contact out of an email address: Meridian requires a first and last name, and a name
    this tool invented would be worse than a missing record. Where the named address is not in the
    contact export, the account is flagged for the customer instead.
    """
    counts = {"flagged": 0, "already_primary": 0, "email_not_in_contacts": 0, "no_contacts": 0}
    organizations = records.get("organization", [])
    contacts = records.get("contact", [])
    if not organizations or not contacts:
        return counts

    by_org: dict[str, list[RecordDraft]] = {}
    for contact in contacts:
        if contact.skipped:
            continue
        org_id = contact.payload.get("organization_id")
        if org_id:
            by_org.setdefault(str(org_id), []).append(contact)

    for org in organizations:
        if org.skipped:
            continue
        wanted = org.context.get("contact.email")
        org_id = org.record_id
        if not wanted or not org_id:
            continue
        theirs = by_org.get(str(org_id), [])
        if not theirs:
            counts["no_contacts"] += 1
            org.add_issue(
                "primary_contact_missing",
                f"the account names {wanted} as its primary contact and the contact export has "
                "no contacts for this account at all (customer rule 1)",
                severity=Severity.warning,
                field_name="primary_contact_email",
                value=wanted,
                rule=ctx.label,
            )
            continue
        if any(c.payload.get("is_primary") for c in theirs):
            counts["already_primary"] += 1
            continue
        match = next(
            (c for c in theirs if str(c.payload.get("email", "")).lower() == str(wanted).lower()),
            None,
        )
        if match is None:
            counts["email_not_in_contacts"] += 1
            org.add_issue(
                "primary_contact_not_found",
                f"the account names {wanted} as its primary contact, no contact on this account "
                "is flagged primary, and no contact has that address (customer rule 1). Meridian "
                "needs a name, so no contact was invented",
                severity=Severity.warning,
                field_name="primary_contact_email",
                value=wanted,
                rule=ctx.label,
            )
            continue
        match.payload["is_primary"] = True
        match.applied(ctx.label)
        match.note(
            "is_primary",
            f"flagged primary from the account's primary_contact_email ({wanted}), "
            "no contact on the account carried the flag (customer rule 1)",
        )
        counts["flagged"] += 1
    return counts


def contexts_for(
    config: TransformationConfig, enabled: dict[str, RecordRuleConfig]
) -> list[RuleContext]:
    out: list[RuleContext] = []
    for rule_id, settings in enabled.items():
        if rule_id not in REGISTRY and rule_id not in CROSS_REGISTRY:
            log.warning("unknown_record_rule", rule=rule_id)
            continue
        out.append(
            RuleContext(
                rule_id=rule_id, config=config, params=settings.params, citation=settings.rule
            )
        )
    return out


__all__ = [
    "CA_PROVINCES",
    "CROSS_REGISTRY",
    "REGISTRY",
    "RuleContext",
    "contexts_for",
]

"""what each kind of issue asks of somebody, and the small formatting rules every part of the
report shares.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

REPORT_VERSION = "1"
PURPOSE_SUMMARY = "readiness_summary"
BLOCKER = "blocker"
CONDITION = "condition"
PASS = "pass"
PLURAL = {
    "organization": "organizations",
    "contact": "contacts",
    "subscription": "subscriptions",
    "activity": "activities",
}
# issue types whose offending values are vocabulary, safe to quote in a report. emails, phones,
# names and whole records are personal data and are never quoted, only counted
VOCABULARY_TYPES = {
    "unmapped_value",
    "invalid_enum_value",
    "undecidable_plan",
    "ambiguous_country",
    "plan_conflicts_with_tier",
}
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_STATUS_WORDS = ("READY WITH CONDITIONS", "READY", "BLOCKED")


# --- what each kind of issue asks of somebody ----------------------------------------------------


@dataclass(frozen=True)
class Action:
    """how one kind of issue becomes work. `kind` decides where it lands in the report."""

    kind: str  # decision | data_fix | dependent | review
    title: str
    question: str | None = None


ACTIONS: dict[str, Action] = {
    "unmapped_value": Action(
        "decision",
        "{n} {entities} carry {field} values that no rule maps ({values})",
        "Which {platform} value should {values_plain} in {field} become? Until a rule says so, "
        "{these} {n} {entities} stay behind.",
    ),
    "invalid_enum_value": Action(
        "decision",
        "{n} {entities} carry {field} values {platform} does not accept ({values})",
        "What should {values_plain} in {field} become in {platform}?",
    ),
    "undecidable_plan": Action(
        "decision",
        "{n} {entities} cannot be given a {field}: {values} depends on a value that is empty",
        "Which plan should {these} {n} {entities} get? The rule that decides {values_plain} "
        "needs a value the export does not have.",
    ),
    "business_rule_violation": Action(
        "decision",
        "{n} {entities} conflict with a {platform} rule ({rule})",
        "{These} {n} {entities} conflict with a {platform} rule ({rule}). How should each one "
        "be resolved before migration?",
    ),
    "plan_conflicts_with_tier": Action(
        "decision",
        "{n} {entities} are on a plan the customer's own rules say they should not have",
        "{n} {entities} sit on accounts whose tier calls for a different plan ({rule}). Is the "
        "billing plan right, or should these accounts move plan?",
    ),
    "ambiguous_country": Action(
        "decision",
        "{n} {entities} have country {values} and nothing that tells which country it is",
        "Which country are {these} {n} {entities} in? The export says {values_plain} and has "
        "no state or region to decide it.",
    ),
    "missing_identifier": Action("data_fix", "{n} {entities} have no identifier"),
    "duplicate_identifier": Action(
        "data_fix", "{n} {entities} reuse an identifier that is already in the export"
    ),
    "missing_required": Action(
        "data_fix", "{n} {entities} are missing values {platform} requires ({fields})"
    ),
    "malformed_email": Action("data_fix", "{n} {entities} have an email address that is not valid"),
    "phone_too_short": Action("data_fix", "{n} {entities} have a phone number with too few digits"),
    "duplicate_contact_email": Action(
        "data_fix", "{n} {entities} repeat an email address already used on the same account"
    ),
    "duplicate_primary_contact": Action(
        "data_fix", "{n} {entities} are marked primary on accounts that already have one"
    ),
    "missing_relationship": Action(
        "data_fix", "{n} {entities} belong to accounts that are not in the export at all"
    ),
    "invalid_value": Action("data_fix", "{n} {entities} fail a value check {platform} applies"),
    "invalid_date": Action("data_fix", "{n} {entities} have dates that cannot be read ({fields})"),
    "invalid_format": Action(
        "data_fix", "{n} {entities} have values in a format {platform} refuses ({fields})"
    ),
    "parent_record_rejected": Action(
        "dependent",
        "{n} {entities} wait on an account that cannot migrate yet; fixing the account "
        "releases them",
    ),
    "no_primary_contact": Action(
        "review", "{n} {entities} belong to accounts that have no primary contact ({rule})"
    ),
    "primary_contact_not_found": Action(
        "review",
        "{n} {entities} name a primary contact that the contact export does not contain",
    ),
    "primary_contact_missing": Action(
        "review", "{n} {entities} name a primary contact and have no contacts in the export"
    ),
    "date_in_future": Action(
        "review", "{n} {entities} have dates after the migration date ({fields})"
    ),
}
# a platform rule the validation stage names, and the question it puts to the customer. keyed by
# a phrase from the rule text, which is this tool's own vocabulary, not a customer's
RULE_QUESTIONS: dict[str, str] = {
    "dormant account": (
        "{These} {n} {entities} are live in billing, but the account each one belongs to "
        "migrates as inactive, and {platform} allows no live subscription under an inactive "
        "account. For each account: should it stay active in {platform}, or should the "
        "subscription end before migration?"
    ),
}
FALLBACK_ERROR = Action("data_fix", "{n} {entities}: {error_type} on {fields}")
FALLBACK_WARNING = Action("review", "{n} {entities}: {error_type} on {fields}")
OWNERS = {
    "decision": "customer decision",
    "data_fix": "customer data correction",
    "dependent": "released by other fixes",
    "review": "review and sign-off",
}


def _fmt(n: int) -> str:
    return f"{n:,}"


def _pct(part: int, whole: int) -> str:
    return f"{(100.0 * part / whole):.1f}%" if whole else "n/a"


def _plain_values(values: list[tuple[str, int]]) -> str:
    shown = [f"'{v}'" for v, _ in values[:4]]
    if len(values) > 4:
        return ", ".join(shown) + f" and {len(values) - 4} more"
    return shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + f" or {shown[-1]}"


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _when(iso: str | None) -> str:
    """2026-10-02T07:38:31+00:00 -> 2026-10-02 07:38 UTC"""
    return f"{iso[:10]} {iso[11:16]} UTC" if iso and len(iso) >= 16 else (iso or "")


def _quote_values(values: list[tuple[str, int]]) -> str:
    shown = [f"'{v}' ({_fmt(c)})" for v, c in values[:4]]
    more = len(values) - 4
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")

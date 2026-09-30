"""from a profile to the list a human reads: what is wrong, how much of it, how bad."""

from __future__ import annotations

from app.services.profiling.types import DatasetProfile, FieldProfile, QualityIssue, Severity

HIGH_NULL_PCT = 20.0
_ORDER = {Severity.error: 0, Severity.warning: 1, Severity.info: 2}


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def _issue(
    code: str,
    severity: Severity,
    count: int,
    whole: int,
    message: str,
    column: str | None = None,
) -> QualityIssue:
    return QualityIssue(
        code=code,
        severity=severity,
        column=column,
        count=count,
        pct=_pct(count, whole) if whole else None,
        message=message,
    )


def _field_issues(f: FieldProfile, rows: int) -> list[QualityIssue]:
    s = f.stats
    out: list[QualityIssue] = []
    non_null = s.get("non_null_count", 0)
    name = f.name

    def add(code: str, severity: Severity, count: int, message: str, whole: int = rows) -> None:
        if count > 0:
            out.append(_issue(code, severity, count, whole, message, column=name))

    if f.inferred_type == "empty":
        add("empty_column", Severity.info, rows, f"{name} has no values at all")
        return out
    if f.null_pct >= HIGH_NULL_PCT:
        add(
            "null_rate_high",
            Severity.warning,
            f.null_count,
            f"{name} is empty in {f.null_pct}% of rows",
        )
    add(
        "placeholder_values",
        Severity.warning,
        s.get("placeholder_count", 0),
        f"{name} uses placeholder text (n/a, unknown, -) instead of a blank",
    )

    malformed = s.get("malformed_count", 0)
    if malformed:
        examples = ", ".join(s.get("malformed_examples", [])[:3])
        blocking = f.inferred_type in ("email", "identifier")
        add(
            f"malformed_{f.inferred_type}",
            Severity.error if blocking else Severity.warning,
            malformed,
            f"{malformed} {name} values are not valid {f.inferred_type}s (e.g. {examples})",
            non_null,
        )

    formats = s.get("format_counts", {})
    if len(formats) > 1:
        parsed = sum(formats.values())
        iso = s.get("iso_pct", 0)
        add(
            "mixed_date_formats",
            Severity.warning,
            parsed - s.get("iso_count", 0),
            f"{name} arrives in {len(formats)} formats, {iso}% ISO 8601",
            parsed,
        )
    add(
        "future_dates",
        Severity.warning,
        s.get("future_count", 0),
        f"{name} has dates in the future",
        non_null,
    )
    add(
        "numeric_text",
        Severity.warning,
        s.get("numeric_text_count", 0),
        f'{name} carries currency symbols, separators or suffixes ("$7,050,000", "38.6M")',
        non_null,
    )
    add(
        "float_text",
        Severity.info,
        s.get("float_text_count", 0),
        f'{name} is a whole number written with a decimal point ("120.0")',
        non_null,
    )
    raw_types = s.get("raw_types", {})
    if len(raw_types) > 1:
        parts = ", ".join(f"{t} {c}" for t, c in raw_types.items())
        add(
            "mixed_json_types",
            Severity.warning,
            non_null - max(raw_types.values()),
            f"{name} is typed inconsistently in the export ({parts})",
            non_null,
        )
    add(
        "uppercase_emails",
        Severity.info,
        s.get("uppercase_count", 0),
        f"{name} has upper-case emails",
        non_null,
    )
    add(
        "url_no_scheme",
        Severity.info,
        s.get("no_scheme_count", 0),
        f"{name} urls are missing http(s)://",
        non_null,
    )
    if f.inferred_type == "phone":
        not_e164 = non_null - s.get("e164_count", 0) - malformed
        add(
            "phone_not_e164", Severity.info, not_e164, f"{name} needs E.164 normalization", non_null
        )
    if f.inferred_type == "boolean" and s.get("spelling_count", 0) > 2:
        spellings = ", ".join(list(s.get("spellings", {}))[:6])
        add(
            "boolean_spellings",
            Severity.info,
            s.get("variant_count", 0),
            f"{name} spells yes/no {s['spelling_count']} ways: {spellings}",
            non_null,
        )
    groups = s.get("case_variant_groups", 0)
    if groups and f.inferred_type in ("category", "code", "text", "email"):
        first = " / ".join(s.get("case_variants", [[]])[0])
        add(
            "case_variants",
            Severity.info,
            groups,
            f"{name} has {groups} values spelled in more than one case ({first})",
            0,
        )
    add(
        "whitespace",
        Severity.info,
        s.get("whitespace_count", 0),
        f"{name} has stray leading, trailing or double spaces",
        non_null,
    )
    dupes = s.get("normalized_duplicates", 0)
    if dupes and f.unique_pct >= 50:
        add(
            "normalized_duplicates",
            Severity.warning,
            dupes,
            f"{dupes} {name} values repeat once case and spacing are ignored",
            non_null,
        )
    return out


def assess(profile: DatasetProfile) -> list[QualityIssue]:
    rows = profile.row_count
    issues: list[QualityIssue] = []
    key = profile.key_column
    if key:
        if profile.key_missing:
            issues.append(
                _issue(
                    "key_missing",
                    Severity.error,
                    profile.key_missing,
                    rows,
                    f"{profile.key_missing} rows have no {key}; "
                    "they cannot be migrated without an id decision",
                    column=key,
                )
            )
        if profile.key_duplicates:
            issues.append(
                _issue(
                    "key_duplicates",
                    Severity.error,
                    profile.key_duplicates,
                    rows,
                    f"{profile.key_duplicates} rows repeat a {key} that already appears",
                    column=key,
                )
            )
    else:
        issues.append(
            _issue(
                "no_key_column",
                Severity.warning,
                rows,
                rows,
                "no column looks like a stable record id; re-runs cannot be idempotent",
            )
        )
    if profile.exact_duplicate_rows:
        issues.append(
            _issue(
                "duplicate_rows",
                Severity.warning,
                profile.exact_duplicate_rows,
                rows,
                f"{profile.exact_duplicate_rows} rows are exact copies of another row",
            )
        )
    for ref in profile.references:
        if ref.orphans:
            issues.append(
                _issue(
                    "orphan_references",
                    Severity.error,
                    ref.orphans,
                    ref.checked,
                    f"{ref.orphans} {ref.column} values point at a {ref.references} "
                    "that does not exist",
                    column=ref.column,
                )
            )
    for f in profile.fields:
        issues.extend(_field_issues(f, rows))
    issues.sort(key=lambda i: (_ORDER[i.severity], -i.count))
    return issues

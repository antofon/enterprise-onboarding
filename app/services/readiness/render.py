"""the markdown export: the executive part, then the technical appendix."""

from __future__ import annotations

from typing import Any

from app.ai.report_prompt import REPORT_PROMPT_VERSION
from app.services.readiness.vocabulary import _fmt, _pct, _when
from app.target.schema import PLATFORM_NAME


def _md_table(headers: list[str], rows: list[list[Any]], align: list[str] | None = None) -> str:
    align = align or ["l"] * len(headers)
    rule = ["---:" if a == "r" else "---" for a in align]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(rule) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(c).replace("|", "\\|") for c in row) + " |")
    return "\n".join(lines)


def render_markdown(content: dict[str, Any]) -> str:
    """the report as markdown: an executive part for the customer, then the technical appendix."""
    facts = content["facts"]
    project = facts["project"]
    run = facts["dry_run"]
    rec = facts["reconciliation"]
    summary = content["summary"]
    out: list[str] = []
    w = out.append

    w(f"# Implementation readiness: {project['customer']}")
    w("")
    w(f"**Status: {content['status']}**")
    w("")
    w(
        f"Project: {project['name']} · Target: {PLATFORM_NAME} {project['target_environment']} · "
        f"Generated {_when(facts['generated_at'])}"
        + (f" · Report {content['report_id'][:8]}" if content.get("report_id") else "")
    )
    w("")
    w("## Executive summary")
    w("")
    w(f"**{summary['headline']}**")
    w("")
    w(summary["summary"])
    w("")
    if summary.get("blocker_explanations"):
        w("### What stands in the way")
        w("")
        for b in summary["blocker_explanations"]:
            w(f"- **{b['code']}**: {b['explanation']}")
        w("")
    origin = (
        f"Drafted by {summary['model']} from the facts below and checked against them."
        if summary["origin"] == "model"
        else "Written from the facts below by the tool."
    )
    if summary.get("note"):
        origin += f" Note: {summary['note']}."
    w(f"_{origin}_")
    w("")

    w("## Blockers")
    w("")
    if content["blockers"]:
        for g in content["blockers"]:
            w(f"- **{g['title']}.** {g['detail']}")
    else:
        w("None.")
    w("")
    w("## Conditions")
    w("")
    if content["conditions"]:
        for g in content["conditions"]:
            w(f"- **{g['title']}.** {g['detail']}")
    else:
        w("None.")
    w("")

    items = content["work_items"]
    if items:
        w("## Work before go-live")
        w("")
        w(
            _md_table(
                ["work", "owner", "records", "blocks records"],
                [
                    [
                        i["title"],
                        i["owner"],
                        _fmt(i["records"]),
                        "yes" if i["blocks_records"] else "no",
                    ]
                    for i in items
                ],
                ["l", "l", "r", "l"],
            )
        )
        w("")
        w(
            "A record with two problems appears in both rows, so the records column can add up "
            "to more than the records excluded. The reconciliation in the appendix counts each "
            "record once, by its first error."
        )
        w("")

    w("## Customer questions")
    w("")
    if content["customer_questions"]:
        for n, q in enumerate(content["customer_questions"], 1):
            w(f"{n}. {q['question']}")
    else:
        w("None.")
    w("")
    w("## Next steps")
    w("")
    for n, step in enumerate(content["next_steps"], 1):
        w(f"{n}. {step}")
    w("")
    w("## Technical risks")
    w("")
    if content["technical_risks"]:
        for r in content["technical_risks"]:
            w(f"- **{r['title']}.** {r['detail']}")
    else:
        w("None recorded.")
    w("")

    w("---")
    w("")
    w("# Technical appendix")
    w("")
    w("## Readiness gates")
    w("")
    w(
        _md_table(
            ["gate", "outcome", "detail"],
            [[g["title"], g["outcome"], g["detail"]] for g in content["gates"]],
        )
    )
    w("")
    w(
        f"Policy: an entity below {facts['policy']['min_entity_coverage'] * 100:.0f}% of its "
        "in-scope records landing is a blocker; any record left behind or any warning is a "
        "condition."
    )
    w("")

    w("## Data profile")
    w("")
    profile = facts["data_profile"]
    w(
        _md_table(
            ["dataset", "kind", "rows", "columns", "errors", "warnings", "info"],
            [
                [
                    d["name"],
                    d["kind"],
                    _fmt(d["rows"] or 0),
                    _fmt(d["columns"] or 0),
                    _fmt(d["errors"]),
                    _fmt(d["warnings"]),
                    _fmt(d["info"]),
                ]
                for d in profile["datasets"]
            ],
            ["l", "l", "r", "r", "r", "r", "r"],
        )
    )
    w("")
    w(f"{_fmt(profile['rows'])} rows across {_fmt(profile['profiled'])} profiled sources.")
    w("")

    w("## Mappings")
    w("")
    m = facts["mappings"]
    w(
        _md_table(
            ["approved", "not migrated", "rejected", "undecided", "open questions"],
            [
                [
                    _fmt(m["approved"]),
                    _fmt(m["ignored"]),
                    _fmt(m["rejected"]),
                    _fmt(m["undecided"]),
                    _fmt(m["open_questions"]),
                ]
            ],
            ["r"] * 5,
        )
    )
    w("")
    if m["approved_by_origin"]:
        origins = ", ".join(f"{k} {_fmt(v)}" for k, v in sorted(m["approved_by_origin"].items()))
        w(f"Approved mappings by origin: {origins}.")
        w("")
    if m["required_missing"]:
        w("Required target fields with no approved mapping: " + ", ".join(m["required_missing"]))
        w("")

    if run is not None:
        w("## Validation")
        w("")
        t = run["totals"]
        w(
            _md_table(
                ["source rows", "skipped by rule", "valid", "excluded", "with warnings"],
                [
                    [
                        _fmt(int(t.get("source_rows", 0))),
                        _fmt(int(t.get("skipped_by_rule", 0))),
                        _fmt(int(t.get("valid", 0))),
                        _fmt(int(t.get("invalid", 0))),
                        _fmt(int(t.get("with_warnings", 0))),
                    ]
                ],
                ["r"] * 5,
            )
        )
        w("")
        groups = facts["issue_groups"]
        if groups:
            w(
                _md_table(
                    ["severity", "entity", "problem", "records", "fields"],
                    [
                        [
                            g["severity"],
                            g["entity"],
                            g["error_type"],
                            _fmt(g["records"]),
                            ", ".join(g["fields"]),
                        ]
                        for g in groups
                    ],
                    ["l", "l", "l", "r", "l"],
                )
            )
            w("")
        if run["applied_rules"]:
            w("Customer rules applied:")
            w("")
            for rule, count in run["applied_rules"].items():
                w(f"- {rule}: {_fmt(int(count))} records")
            w("")

        w("## Migration dry run")
        w("")
        w(
            _md_table(
                [
                    "entity",
                    "valid",
                    "sent",
                    "accepted",
                    "refused",
                    "failed",
                    "blocked",
                    "not reached",
                    "retries",
                ],
                [
                    [
                        e,
                        _fmt(int(s.get("valid", 0))),
                        _fmt(int(s.get("attempted", 0))),
                        _fmt(int(s.get("accepted", 0))),
                        _fmt(int(s.get("rejected", 0))),
                        _fmt(int(s.get("failed", 0))),
                        _fmt(int(s.get("blocked", 0))),
                        _fmt(int(s.get("not_attempted", 0))),
                        _fmt(int(s.get("retries", 0))),
                    ]
                    for e, s in run["stats"].items()
                ],
                ["l"] + ["r"] * 8,
            )
        )
        w("")
        w(
            f"Dry run {run['id']} started {_when(run['started_at'])}, took "
            f"{run['duration_seconds']} seconds, configuration {run['config_version']}, dates "
            f"measured from {run['as_of']}."
        )
        w("")

    w("## Reconciliation")
    w("")
    if rec is None:
        w("No reconciliation recorded.")
        w("")
    else:
        w(
            f"**{rec['status']}**: {_fmt(rec['checks_passed'])} of {_fmt(rec['checks_total'])} "
            f"checks passed ({rec['trigger'].replace('_', ' ')}, {_when(rec['created_at'])})."
        )
        w("")
        w(
            _md_table(
                [
                    "entity",
                    "source rows",
                    "distinct ids",
                    "skipped by rule",
                    "in scope",
                    "excluded",
                    "valid",
                    "accepted",
                    "in target",
                    "landed",
                ],
                [
                    [
                        e,
                        _fmt(s["source_rows"]),
                        _fmt(s["distinct_ids"]),
                        _fmt(s["skipped_by_rule"]),
                        _fmt(s["in_scope"]),
                        _fmt(s["excluded"]),
                        _fmt(s["valid"]),
                        _fmt(s["accepted"]),
                        _fmt(s["in_target"]),
                        _pct(s["accepted"], s["in_scope"]),
                    ]
                    for e, s in rec["entities"].items()
                ],
                ["l"] + ["r"] * 9,
            )
        )
        w("")
        w("Why records were excluded (first error on each record):")
        w("")
        for e, s in rec["entities"].items():
            ordered = sorted(s["excluded_by_reason"].items(), key=lambda kv: (-kv[1], kv[0]))
            reasons = ", ".join(f"{k} {_fmt(v)}" for k, v in ordered)
            w(f"- {e}: {reasons or 'none'}")
        w("")
        if rec["failed_checks"]:
            w("Failed checks:")
            w("")
            for c in rec["failed_checks"]:
                w(
                    f"- {c['entity']} · {c['label']}: expected {_fmt(c['expected'])}, "
                    f"counted {_fmt(c['actual'])}. {c['detail']}"
                )
            w("")
        for d in rec["discrepancies"]:
            sample = ", ".join(d["sample_ids"][:5])
            w(
                f"- **{d['entity']} {d['kind']}** ({_fmt(d['count'])}): {d['explanation']}"
                + (f" Sample: {sample}." if sample else "")
            )
        if rec["discrepancies"]:
            w("")

    w("## Provenance")
    w("")
    w(f"- Project {project['id']}, stage {project['stage']}")
    if run is not None:
        w(f"- Dry run {run['id']}, staging namespace {run['namespace']}")
    if rec is not None:
        w(f"- Reconciliation {rec['id']}")
    w(
        f"- Summary: {summary['origin']}"
        + (f" ({summary['model']}, prompt {REPORT_PROMPT_VERSION})" if summary.get("model") else "")
    )
    w(
        "- Every number in this report is a count of stored rows or arithmetic over them. "
        "No number was produced by a model."
    )
    w("")
    return "\n".join(out)

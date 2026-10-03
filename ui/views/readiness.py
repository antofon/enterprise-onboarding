"""readiness report: can this customer go live, what stands in the way, and does the rehearsal
reconcile against the target."""

from __future__ import annotations

import json

import streamlit as st

from common import (
    api,
    api_health,
    api_json,
    chip,
    error_text,
    esc,
    load_project,
    model_label,
    n,
    stage_line,
    table,
)

REPORT_TIMEOUT = 600
ENTITY_ORDER = ("organization", "contact", "subscription", "activity")
STATUS_KIND = {"READY": "ready", "READY WITH CONDITIONS": "conditions", "BLOCKED": "blocked"}
OUTCOME_KIND = {"blocker": "error", "condition": "warning", "pass": "MATCHED"}
WORK_KIND = {
    "decision": "AMBIGUOUS",
    "data_fix": "error",
    "dependent": "muted",
    "review": "warning",
}


def _ordered(mapping: dict) -> list[str]:
    known = [e for e in ENTITY_ORDER if e in mapping]
    return known + sorted(e for e in mapping if e not in ENTITY_ORDER)


def _plural(count: int, word: str) -> str:
    return f"{n(count)} {word}" + ("" if count == 1 else "s")


def _pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "–"


def _metric(col, label: str, value: str, sub: str | None = None) -> None:
    col.markdown(
        f'<div class="label">{esc(label)}</div><div class="big">{esc(value)}</div>'
        + (f'<div class="kv">{esc(sub)}</div>' if sub else ""),
        unsafe_allow_html=True,
    )


def _controls(project_id: str) -> None:
    health = api_health()
    has_model = health.get("llm_provider") not in (None, "none")
    with st.container(border=True):
        st.markdown("**Generate**")
        st.caption(
            "The status, every number, the work and the next steps are computed from the latest "
            "dry run and its reconciliation. The executive summary can be drafted by the model; "
            "it is checked against the facts and written by code instead if it strays."
        )
        c1, c2 = st.columns([1, 3])
        use_model = c2.checkbox(
            f"let {model_label(health) if has_model else 'the model'} draft the summary",
            value=has_model,
            disabled=not has_model,
            help=None if has_model else "no model is configured; the summary is written by code",
        )
        if c1.button("Generate report", type="primary", use_container_width=True):
            with st.spinner("assessing the rehearsal and writing the report"):
                ok, body = api_json(
                    "POST",
                    f"/api/v1/projects/{project_id}/reports/readiness",
                    json={"use_model": use_model, "generated_by": "workbench"},
                    timeout=REPORT_TIMEOUT,
                )
            if ok:
                st.session_state["report_id"] = body["id"]
                st.rerun()
            st.error(error_text(body))


def _banner(report: dict) -> None:
    content = report["content"]
    status = report["status"]
    summary = content["summary"]
    kind = STATUS_KIND.get(status, "blocked")
    st.markdown(
        f'<div class="status-banner status-{kind}">'
        f'<div class="status-word">{esc(status)}</div>'
        f'<div class="status-head">{esc(summary["headline"])}</div>'
        "</div>",
        unsafe_allow_html=True,
    )
    if not report["stored"]:
        st.caption(
            "A preview computed just now and stored nowhere. Generate the report to keep it, "
            "export it, and let the model draft the summary."
        )


def _headline_metrics(content: dict) -> None:
    run = content["facts"]["dry_run"]
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    if run:
        t = run["totals"]
        in_scope = t.get("built", 0) - t.get("skipped_by_rule", 0)
        _metric(c1, "Source rows", n(t.get("source_rows")), "followed through the rehearsal")
        _metric(c2, "In scope", n(in_scope), f"{n(t.get('skipped_by_rule'))} set aside by rule")
        _metric(c3, "Landed", n(t.get("accepted")), "accepted by the target")
        _metric(c4, "Share landed", _pct(t.get("accepted", 0), in_scope), "of records in scope")
    else:
        for col, label in ((c1, "Source rows"), (c2, "In scope"), (c3, "Landed"), (c4, "Share")):
            _metric(col, label, "–", "no rehearsal yet")
    _metric(
        c5,
        "Blockers",
        n(len(content["blockers"])),
        _plural(len(content["conditions"]), "condition"),
    )
    _metric(c6, "Customer questions", n(len(content["customer_questions"])), "to send")


def _summary(content: dict) -> None:
    summary = content["summary"]
    st.markdown("#### Executive summary")
    st.write(summary["summary"])
    if summary.get("blocker_explanations"):
        st.markdown("**What stands in the way**")
        for b in summary["blocker_explanations"]:
            st.markdown(
                f'<div class="explain"><span class="chip chip-type">{esc(b["code"])}</span> '
                f"{esc(b['explanation'])}</div>",
                unsafe_allow_html=True,
            )
    origin = (
        f"Drafted by {summary['model']} from the facts and checked against them: the status as "
        "decided, only numbers that appear in the facts."
        if summary["origin"] == "model"
        else "Written from the facts by the tool."
    )
    if summary.get("note"):
        origin += f" Note: {summary['note']}."
    st.caption(origin)


def _gates_and_questions(content: dict) -> None:
    left, right = st.columns([3, 2])
    with left:
        st.markdown("#### Blockers and conditions")
        rows = [
            [
                chip(g["outcome"], OUTCOME_KIND[g["outcome"]]),
                f"<b>{esc(g['title'])}</b>",
                esc(g["detail"]),
            ]
            for g in content["blockers"] + content["conditions"]
        ]
        if rows:
            st.markdown(
                table(["", "gate", "detail"], rows, ["", "", "dim"]), unsafe_allow_html=True
            )
        else:
            st.write("Nothing blocks go-live and no condition applies.")
        passed = [g for g in content["gates"] if g["outcome"] == "pass"]
        if passed:
            st.caption("Passed: " + "; ".join(g["title"].lower() for g in passed) + ".")
    with right:
        st.markdown("#### Customer questions")
        questions = content["customer_questions"]
        if questions:
            items = "".join(f"<li>{esc(q['question'])}</li>" for q in questions)
            st.markdown(f'<ol class="questions">{items}</ol>', unsafe_allow_html=True)
        else:
            st.write("No question is waiting on the customer.")


def _work(content: dict) -> None:
    items = content["work_items"]
    if not items:
        return
    st.markdown("#### Work before go-live")
    rows = [
        [
            chip(i["owner"], WORK_KIND[i["kind"]]),
            esc(i["title"]),
            n(i["records"]),
            "yes" if i["blocks_records"] else "no",
        ]
        for i in items
    ]
    st.markdown(
        table(["owner", "work", "records", "holds records back"], rows, ["", "", "num", "dim"]),
        unsafe_allow_html=True,
    )
    st.caption(
        "A record with two problems appears in both rows; the reconciliation below counts each "
        "record once, by its first error."
    )


def _steps_and_risks(content: dict) -> None:
    left, right = st.columns(2)
    with left:
        st.markdown("#### Next steps")
        steps = "".join(f"<li>{esc(s)}</li>" for s in content["next_steps"])
        st.markdown(f'<ol class="questions">{steps}</ol>', unsafe_allow_html=True)
    with right:
        st.markdown("#### Technical risks")
        if content["technical_risks"]:
            for risk in content["technical_risks"]:
                st.markdown(
                    f'<div class="explain"><b>{esc(risk["title"])}.</b> '
                    f"{esc(risk['detail'])}</div>",
                    unsafe_allow_html=True,
                )
        else:
            st.write("None recorded.")


def _reconciliation(project_id: str, content: dict) -> None:
    st.markdown("#### Reconciliation: source to target")
    run = content["facts"]["dry_run"]
    if not run:
        st.write("No dry run yet. Rehearse the migration on the Dry run page first.")
        return
    ok, rec = api_json(
        "GET", f"/api/v1/projects/{project_id}/migrations/{run['id']}/reconciliation"
    )
    if not ok:
        st.info(error_text(rec))
        return
    passed = sum(1 for c in rec["checks"] if c["ok"])
    balanced = rec["status"] == "balanced"
    st.markdown(
        chip(rec["status"], "MATCHED" if balanced else "error")
        + f' <span class="kv">{n(passed)} of {n(len(rec["checks"]))} checks passed · '
        f"{esc(rec['trigger'].replace('_', ' '))} · "
        f"{esc(rec['created_at'][:16].replace('T', ' '))} UTC"
        f" · dry run {esc(run['id'][:8])}</span>",
        unsafe_allow_html=True,
    )
    entities = rec["entities"]
    rows = []
    for e in _ordered(entities):
        s = entities[e]
        rows.append(
            [
                esc(e),
                n(s["source_rows"]),
                n(s["distinct_ids"]),
                n(s["skipped_by_rule"]),
                n(s["in_scope"]),
                n(s["excluded"]),
                n(s["valid"]),
                n(s["attempted"]),
                n(s["accepted"]),
                n(s["in_target"]),
                _pct(s["accepted"], s["in_scope"]),
            ]
        )
    st.markdown(
        table(
            [
                "entity",
                "source rows",
                "distinct ids",
                "skipped by rule",
                "in scope",
                "excluded",
                "valid",
                "sent",
                "accepted",
                "in target",
                "landed",
            ],
            rows,
            ["mono"] + ["num"] * 10,
        ),
        unsafe_allow_html=True,
    )
    if rec["discrepancies"]:
        for d in rec["discrepancies"]:
            sample = ", ".join(d["sample_ids"][:5])
            st.error(
                f"{d['entity']} · {d['kind']} ({d['count']:,}): {d['explanation']}"
                + (f" Sample: {sample}." if sample else "")
            )
    else:
        st.caption(
            "Every source row is accounted for, every exclusion has a reason, and the target "
            "holds exactly the identifiers the run accepted, read back over its own api."
        )
    c1, c2 = st.columns([1, 3])
    if c1.button("Re-check against the target", use_container_width=True):
        with st.spinner("reading the staging namespace again"):
            ok, body = api_json(
                "POST", f"/api/v1/projects/{project_id}/migrations/{run['id']}/reconcile"
            )
        if ok:
            st.rerun()
        st.error(error_text(body))
    c2.caption(
        "Reads the target again and compares it with what the run saw: catches a namespace "
        "purged or written to after the rehearsal. Generate the report again to use the result."
    )
    with st.expander("Why records were excluded, and every check", expanded=False):
        reason_rows = []
        for e in _ordered(entities):
            for reason, count in sorted(
                entities[e]["excluded_by_reason"].items(), key=lambda kv: (-kv[1], kv[0])
            ):
                reason_rows.append([esc(e), esc(reason), n(count)])
        if reason_rows:
            st.markdown(
                table(["entity", "first error", "records"], reason_rows, ["mono", "mono", "num"]),
                unsafe_allow_html=True,
            )
        check_rows = [
            [
                chip("ok" if c["ok"] else "failed", "MATCHED" if c["ok"] else "error"),
                esc(c["entity"]),
                esc(c["label"]),
                n(c["expected"]),
                n(c["actual"]),
                esc(c["detail"]),
            ]
            for c in rec["checks"]
        ]
        st.markdown(
            table(
                ["", "entity", "check", "expected", "counted", "how"],
                check_rows,
                ["", "mono", "", "num", "num", "dim"],
            ),
            unsafe_allow_html=True,
        )


def _appendix(content: dict) -> None:
    facts = content["facts"]
    with st.expander("Technical appendix", expanded=False):
        st.markdown("**Readiness gates**")
        rows = [
            [chip(g["outcome"], OUTCOME_KIND[g["outcome"]]), esc(g["title"]), esc(g["detail"])]
            for g in content["gates"]
        ]
        st.markdown(table(["", "gate", "detail"], rows, ["", "", "dim"]), unsafe_allow_html=True)
        st.caption(
            f"Policy: an entity below {facts['policy']['min_entity_coverage'] * 100:.0f}% of its "
            "in-scope records landing blocks go-live; any record left behind or any warning is a "
            "condition."
        )
        st.markdown("**Data profile**")
        rows = [
            [
                esc(d["name"]),
                esc(d["kind"]),
                n(d["rows"]),
                n(d["columns"]),
                n(d["errors"]),
                n(d["warnings"]),
            ]
            for d in facts["data_profile"]["datasets"]
        ]
        st.markdown(
            table(
                ["dataset", "kind", "rows", "columns", "errors", "warnings"],
                rows,
                ["mono", "", "num", "num", "num", "num"],
            ),
            unsafe_allow_html=True,
        )
        m = facts["mappings"]
        st.markdown(
            f"**Mappings:** {n(m['approved'])} approved, {n(m['ignored'])} not migrated, "
            f"{n(m['rejected'])} rejected, {n(m['undecided'])} undecided, "
            f"{n(m['open_questions'])} open questions."
        )
        run = facts["dry_run"]
        rec = facts["reconciliation"]
        lines = [f"project `{facts['project']['id']}`"]
        if run:
            lines.append(
                f"dry run `{run['id']}`, namespace `{run['namespace']}`, configuration "
                f"`{run['config_version']}`, dates from {run['as_of']}"
            )
        if rec:
            lines.append(f"reconciliation `{rec['id']}`")
        if content.get("report_id"):
            lines.append(f"report `{content['report_id']}`")
        st.markdown("**Provenance:** " + " · ".join(lines))


def _downloads(project_id: str, report: dict) -> None:
    if not report["stored"]:
        return
    customer = report["content"]["facts"]["project"]["customer"]
    slug = "".join(c if c.isalnum() else "-" for c in customer.lower()).strip("-")
    try:
        md = api("GET", f"/api/v1/projects/{project_id}/reports/{report['id']}?format=markdown")
        markdown = md.text if md.is_success else ""
    except Exception:  # noqa: BLE001 - the download is a convenience, the page still renders
        markdown = ""
    c1, c2, _ = st.columns([1, 1, 2])
    c1.download_button(
        "Download markdown",
        data=markdown,
        file_name=f"readiness-{slug}-{report['id'][:8]}.md",
        mime="text/markdown",
        use_container_width=True,
        disabled=not markdown,
    )
    c2.download_button(
        "Download json",
        data=json.dumps(report["content"], indent=2, ensure_ascii=False),
        file_name=f"readiness-{slug}-{report['id'][:8]}.json",
        mime="application/json",
        use_container_width=True,
    )


def _history(project_id: str, current: str | None) -> None:
    ok, reports = api_json("GET", f"/api/v1/projects/{project_id}/reports")
    if not ok or not reports:
        return
    st.markdown("#### Reports")
    rows = [
        [
            "▶" if r["id"] == current else "",
            chip(r["status"], {"READY": "MATCHED", "BLOCKED": "error"}.get(r["status"], "warning")),
            esc(r["created_at"][:16].replace("T", " ")),
            n(r["blockers"]),
            n(r["conditions"]),
            esc(r["summary_origin"]),
            esc(r["id"][:8]),
        ]
        for r in reports
    ]
    st.markdown(
        table(
            ["", "status", "generated (utc)", "blockers", "conditions", "summary", "report"],
            rows,
            ["", "", "dim", "num", "num", "dim", "mono"],
        ),
        unsafe_allow_html=True,
    )
    labels = {r["id"]: f"{r['status']} · {r['created_at'][:16].replace('T', ' ')}" for r in reports}
    ids = list(labels)
    chosen = st.selectbox(
        "show report",
        options=ids,
        index=ids.index(current) if current in ids else 0,
        format_func=lambda rid: labels[rid],
        label_visibility="collapsed",
    )
    if chosen != current:
        st.session_state["report_id"] = chosen
        st.rerun()


def render() -> None:
    project_id = st.session_state.get("project_id")
    project = load_project(project_id) if project_id else None
    st.markdown('<div class="eyebrow">Implementation workbench</div>', unsafe_allow_html=True)
    st.title("Readiness report")
    if not project:
        st.write("Select a project in the sidebar.")
        return
    st.markdown(stage_line(project["stage"]), unsafe_allow_html=True)

    _controls(project_id)

    chosen = st.session_state.get("report_id")
    report = None
    if chosen:
        ok, body = api_json("GET", f"/api/v1/projects/{project_id}/reports/{chosen}")
        report = body if ok else None
    if report is None:
        ok, body = api_json("GET", f"/api/v1/projects/{project_id}/reports/readiness")
        if not ok:
            st.error(error_text(body))
            return
        report = body
    content = report["content"]

    _banner(report)
    _downloads(project_id, report)
    _headline_metrics(content)
    st.markdown("")
    _summary(content)
    _gates_and_questions(content)
    _work(content)
    _steps_and_risks(content)
    _reconciliation(project_id, content)
    _appendix(content)
    _history(project_id, report["id"] if report["stored"] else None)

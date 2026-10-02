"""project overview: who the customer is, where the project stands, what to do next."""

from __future__ import annotations

import streamlit as st

from common import api_json, error_text, esc, load_project, n, stage_line


def _metric(col, label: str, value: str, sub: str | None = None) -> None:
    col.markdown(
        f'<div class="label">{esc(label)}</div><div class="big">{esc(value)}</div>'
        + (f'<div class="kv">{esc(sub)}</div>' if sub else ""),
        unsafe_allow_html=True,
    )


STATUS_KIND = {"READY": "ready", "READY WITH CONDITIONS": "conditions", "BLOCKED": "blocked"}


def _readiness_card(project_id: str) -> None:
    """the current readiness state: the latest stored report, or the status computed now."""
    ok, report = api_json("GET", f"/api/v1/projects/{project_id}/reports/readiness")
    if not ok:
        return
    status = report["status"]
    content = report["content"]
    when = (
        f"report of {report['created_at'][:16].replace('T', ' ')} UTC"
        if report["stored"]
        else "computed now, no report generated yet"
    )
    blockers = len(content["blockers"])
    questions = len(content["customer_questions"])
    st.markdown(
        f'<div class="status-banner status-{STATUS_KIND.get(status, "blocked")}">'
        f'<div class="label">Current readiness</div>'
        f'<div class="status-word">{esc(status)}</div>'
        f'<div class="status-head">{n(blockers)} blocker{"" if blockers == 1 else "s"} · '
        f"{n(questions)} customer question{'' if questions == 1 else 's'}"
        f" · {esc(when)}</div></div>",
        unsafe_allow_html=True,
    )


def render() -> None:
    project_id = st.session_state.get("project_id")
    project = load_project(project_id) if project_id else None

    st.markdown('<div class="eyebrow">Implementation workbench</div>', unsafe_allow_html=True)
    st.title(project["customer_name"] if project else "Project overview")

    if project:
        st.markdown(stage_line(project["stage"]), unsafe_allow_html=True)
        datasets = project["datasets"]
        profiled = [d for d in datasets if d.get("profiled_at")]
        rows = sum(d.get("row_count") or 0 for d in profiled)
        errors = sum(
            (d.get("quality") or {}).get("issue_counts", {}).get("error", 0) for d in profiled
        )
        warnings = sum(
            (d.get("quality") or {}).get("issue_counts", {}).get("warning", 0) for d in profiled
        )
        st.caption(f"{project['project_name']} · target {project['target_environment']}")
        c1, c2, c3, c4 = st.columns(4)
        _metric(c1, "Sources attached", n(len(datasets)), f"{len(profiled)} profiled")
        _metric(c2, "Source rows", n(rows) if profiled else "–", "across profiled sources")
        _metric(c3, "Errors", n(errors) if profiled else "–", "block migration until resolved")
        _metric(c4, "Warnings", n(warnings) if profiled else "–", "need a rule or a decision")
        st.markdown("")
        left, right = st.columns([3, 2])
        with left:
            systems = ", ".join(project["source_systems"]) or "none listed"
            st.markdown(f"**Source systems:** {esc(systems)}", unsafe_allow_html=True)
            if project.get("notes"):
                st.markdown(f"**Notes:** {esc(project['notes'])}", unsafe_allow_html=True)
            if datasets:
                st.markdown("**Attached sources**")
                for d in datasets:
                    state = (
                        f"profiled, {n(d['row_count'])} rows"
                        if d.get("profiled_at")
                        else "not profiled"
                    )
                    st.markdown(f"- `{d['name']}` · {d['kind']} · {state}")
        with right:
            _readiness_card(project["id"])
            with st.container(border=True):
                st.markdown("**Next step**")
                if not datasets:
                    st.write(
                        "Attach the customer's exports and the billing feed, then profile them."
                    )
                elif not profiled:
                    st.write(
                        "Sources are attached. Profile them to see data quality "
                        "and the schema comparison."
                    )
                elif project["stage"] == "profiled":
                    st.write(
                        "Profiling is done. Review the quality issues and the schema comparison, "
                        "then get a proposed target for every field on the Mapping review page."
                    )
                    st.markdown("[Open mapping review →](mappings)")
                elif project["stage"] in ("mapped", "in_review"):
                    ok, summary = api_json(
                        "GET", f"/api/v1/projects/{project['id']}/mappings/summary"
                    )
                    if ok:
                        c = summary["counts"]
                        pending = c["suggested"] + c["needs_clarification"]
                        st.write(
                            f"{n(pending)} of {n(summary['total'])} fields still need a decision, "
                            f"{n(summary['open_questions'])} questions are with the customer."
                        )
                    else:
                        st.write("Proposals are in. Decide each field on the Mapping review page.")
                    st.markdown("[Open mapping review →](mappings)")
                elif project["stage"] == "ready_to_transform":
                    st.write(
                        "Every field is decided and no question is open. Next: transform and "
                        "validate every record, then rehearse the migration against the target."
                    )
                    st.markdown("[Open dry run →](dry-run)")
                elif project["stage"] == "validated":
                    st.write(
                        "Validation has run. Read what it found, then send the valid records "
                        "to the target platform with a dry run."
                    )
                    st.markdown("[Open dry run →](dry-run)")
                elif project["stage"] == "dry_run_complete":
                    st.write(
                        "The migration has been rehearsed and reconciled against the target. "
                        "Next: generate the readiness report."
                    )
                    st.markdown("[Open readiness →](readiness)")
                elif project["stage"] == "reported":
                    st.write(
                        "A readiness report exists. Send the customer its questions and record "
                        "lists; rehearse again when the corrected export arrives."
                    )
                    st.markdown("[Open readiness →](readiness)")
                else:
                    st.write("Continue with the current stage.")
                if project["stage"] in ("created", "profiled"):
                    st.markdown("[Open source assessment →](sources)")
    else:
        st.write("No onboarding project selected. Open one below.")

    with st.expander("Open a new onboarding project", expanded=project is None):
        with st.form("new_project", clear_on_submit=True):
            customer_name = st.text_input("Customer", placeholder="Apex Equipment Services")
            project_name = st.text_input("Project", placeholder="Apex go-live migration")
            systems = st.text_input(
                "Source systems (comma separated)",
                placeholder="legacy crm export, billing api, business rules doc",
            )
            target_env = st.selectbox("Target environment", ["staging", "sandbox", "production"])
            notes = st.text_area("Notes / assumptions", height=80)
            if st.form_submit_button("Create project", type="primary"):
                payload = {
                    "customer_name": customer_name,
                    "project_name": project_name,
                    "source_systems": [s.strip() for s in systems.split(",") if s.strip()],
                    "target_environment": target_env,
                    "notes": notes or None,
                }
                ok, body = api_json("POST", "/api/v1/projects", json=payload)
                if ok:
                    st.session_state["project_id"] = body["id"]
                    st.success(f"created {body['project_name']}")
                    st.rerun()
                else:
                    st.error(error_text(body))

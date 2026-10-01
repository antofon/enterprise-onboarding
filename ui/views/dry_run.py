"""dry run: what the approved mappings will do, what validation found, and what the target
platform said when the records were actually sent."""

from __future__ import annotations

import streamlit as st

from common import api_json, chip, error_text, esc, load_project, n, stage_line, table

RUN_TIMEOUT = 900
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
# the order the migration writes in; jsonb does not keep key order, so the api cannot promise it
ENTITY_ORDER = ("organization", "contact", "subscription", "activity")


def _entities(stats: dict) -> list[str]:
    known = [e for e in ENTITY_ORDER if e in stats]
    return known + sorted(e for e in stats if e not in ENTITY_ORDER)


def _metric(col, label: str, value: str, sub: str | None = None) -> None:
    col.markdown(
        f'<div class="label">{esc(label)}</div><div class="big">{esc(value)}</div>'
        + (f'<div class="kv">{esc(sub)}</div>' if sub else ""),
        unsafe_allow_html=True,
    )


def _runs(project_id: str) -> list[dict]:
    ok, body = api_json("GET", f"/api/v1/projects/{project_id}/migrations")
    return body if ok and isinstance(body, list) else []


def _run(project_id: str, run_id: str) -> dict | None:
    ok, body = api_json("GET", f"/api/v1/projects/{project_id}/migrations/{run_id}")
    return body if ok else None


def _plan_section(project_id: str) -> None:
    ok, plan = api_json("GET", f"/api/v1/projects/{project_id}/transformation-plan")
    if not ok:
        st.info(error_text(plan))
        return
    with st.expander("Transformation plan: what happens to every approved column", expanded=False):
        st.caption(
            f"configuration {esc(plan['config_version'])} for {esc(plan['customer'])}, dates "
            f"measured from {esc(plan['as_of'])}. Record rules: "
            + ", ".join(f"`{r}`" for r in plan["record_rules"] + plan["cross_dataset_rules"])
        )
        for dataset in plan["datasets"]:
            emits = ", ".join(dataset["emits"]) or "nothing (no identifier mapped)"
            st.markdown(f"**{esc(dataset['dataset'])}** · emits {esc(emits)}")
            rows = []
            for rule in dataset["field_rules"] + dataset["context_rules"]:
                targets = ", ".join(rule["emits"])
                chain = " › ".join(rule["converters"])
                why = rule["chosen_by"]
                if rule in dataset["context_rules"]:
                    why = "context for cross-dataset rules"
                rows.append(
                    [
                        esc(rule["source_field"]),
                        esc(targets),
                        f'<span class="chip chip-type">{esc(chain)}</span>',
                        esc(why),
                        esc(rule["note"] or ""),
                    ]
                )
            for dropped in dataset["dropped"]:
                rows.append(
                    [
                        esc(dropped["source_field"]),
                        chip("not migrated", "muted"),
                        "",
                        esc(dropped["status"]),
                        esc(dropped["why"]),
                    ]
                )
            st.markdown(
                table(
                    ["source column", "lands in", "normalizers", "chosen by", "note"],
                    rows,
                    ["mono", "mono", "", "dim", "dim"],
                ),
                unsafe_allow_html=True,
            )


def _controls(project_id: str, project: dict) -> None:
    with st.container(border=True):
        st.markdown("**Run**")
        st.caption(
            "Validate transforms and checks every record and writes nothing. Dry run goes on to "
            "send every valid record to the target platform, one request each, into a staging "
            "namespace that cannot touch live data."
        )
        c1, c2, c3 = st.columns([1, 1, 2])
        limit = c3.number_input(
            "records per entity (0 = all)",
            min_value=0,
            value=0,
            step=50,
            help="a quick look at a slice; children whose organization falls past the limit "
            "are reported as blocked, not attempted",
        )
        purge = c3.checkbox(
            "throw the staging records away when the run finishes",
            value=False,
            help="by default the namespace is kept so it can be inspected and reconciled",
        )
        options: dict = {}
        if limit:
            options["limit_per_entity"] = int(limit)
        if c1.button("Validate", type="secondary", use_container_width=True):
            with st.spinner("transforming and validating every record"):
                ok, body = api_json(
                    "POST",
                    f"/api/v1/projects/{project_id}/validate",
                    json=options,
                    timeout=RUN_TIMEOUT,
                )
            if ok:
                st.session_state["run_id"] = body["id"]
                st.rerun()
            st.error(error_text(body))
        if c2.button("Dry run", type="primary", use_container_width=True):
            with st.spinner("sending every valid record to the target platform"):
                ok, body = api_json(
                    "POST",
                    f"/api/v1/projects/{project_id}/migrations/dry-run",
                    json={**options, "purge_namespace_after": purge},
                    timeout=RUN_TIMEOUT,
                )
            if ok:
                st.session_state["run_id"] = body["id"]
                st.rerun()
            st.error(error_text(body))


def _headline(run: dict) -> None:
    totals = run["totals"]
    is_dry = run["kind"] == "dry_run"
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    _metric(c1, "Source rows", n(totals["source_rows"]), "across every emitting source")
    _metric(c2, "Valid", n(totals["valid"]), f"{n(totals['with_warnings'])} with warnings")
    _metric(c3, "Blocked by errors", n(totals["invalid"]), "cannot migrate as they stand")
    _metric(c4, "Skipped by rule", n(totals["skipped_by_rule"]), "the customer's own rules")
    if is_dry:
        _metric(
            c5, "Accepted by target", n(totals["accepted"]), f"of {n(totals['attempted'])} sent"
        )
        refused = totals["rejected"] + totals["failed"]
        _metric(c6, "Refused or failed", n(refused), f"{n(totals['blocked'])} never attempted")
    else:
        _metric(c5, "Sent to target", "–", "validation writes nothing")
        _metric(c6, "Duration", f"{(run['duration_ms'] or 0) / 1000:.1f}s", "transform and check")


def _entity_table(run: dict) -> None:
    is_dry = run["kind"] == "dry_run"
    headers = ["entity", "source rows", "built", "skipped", "valid", "invalid", "warnings"]
    classes = ["mono", "num", "num", "num", "num", "num", "num"]
    if is_dry:
        headers += ["attempted", "accepted", "rejected", "failed", "blocked", "retries"]
        classes += ["num"] * 6
    rows = []
    for entity in _entities(run["stats"]):
        s = run["stats"][entity]
        row = [
            esc(entity),
            n(s.get("source_rows")),
            n(s.get("built")),
            n(s.get("skipped_by_rule")),
            n(s.get("valid")),
            n(s.get("invalid")),
            n(s.get("with_warnings")),
        ]
        if is_dry:
            row += [
                n(s.get("attempted", 0)),
                n(s.get("accepted", 0)),
                n(s.get("rejected", 0)),
                n(s.get("failed", 0)),
                n(s.get("blocked", 0)),
                n(s.get("retries", 0)),
            ]
        rows.append(row)
    st.markdown(table(headers, rows, classes), unsafe_allow_html=True)
    if is_dry and run.get("target_counts"):
        held = run["target_counts"]
        counts = ", ".join(f"{k} {n(held[k])}" for k in _entities(held))
        st.caption(
            f"what the target platform holds in namespace `{run['namespace']}`: {counts}. "
            f"{(run['duration_ms'] or 0) / 1000:.1f}s end to end."
        )
    elif is_dry:
        st.caption(f"staging namespace `{run['namespace']}` was purged when the run finished.")


def _issues_section(project_id: str, run: dict) -> None:
    st.markdown("#### What validation found")
    ok, breakdown = api_json(
        "GET", f"/api/v1/projects/{project_id}/migrations/{run['id']}/issue-breakdown"
    )
    if not ok or not breakdown:
        st.write("No issues. Every record passed the contract and the cross-record checks.")
        return
    rows = [
        [
            chip(b["severity"], b["severity"]),
            esc(b["entity"]),
            esc(b["error_type"]),
            n(b["count"]),
            esc(", ".join(b["fields"])),
        ]
        for b in sorted(
            breakdown, key=lambda b: (SEVERITY_ORDER.get(b["severity"], 9), -b["count"])
        )
    ]
    st.markdown(
        table(
            ["", "entity", "problem", "records", "fields"], rows, ["", "mono", "mono", "num", "dim"]
        ),
        unsafe_allow_html=True,
    )
    if run.get("issues_truncated"):
        st.caption("more issues were found than stored; the counts above are complete.")

    with st.expander("Look at the records", expanded=False):
        f1, f2, f3 = st.columns(3)
        entity = f1.selectbox("entity", ["any", *sorted({b["entity"] for b in breakdown})])
        severity = f2.selectbox("severity", ["any", "error", "warning", "info"])
        kinds = sorted({b["error_type"] for b in breakdown})
        error_type = f3.selectbox("problem", ["any", *kinds])
        params = {"limit": 100}
        if entity != "any":
            params["entity"] = entity
        if severity != "any":
            params["severity"] = severity
        if error_type != "any":
            params["error_type"] = error_type
        ok, issues = api_json(
            "GET", f"/api/v1/projects/{project_id}/migrations/{run['id']}/issues", params=params
        )
        if ok and issues:
            rows = [
                [
                    chip(i["severity"], i["severity"]),
                    esc(i["entity"]),
                    esc(i["record_id"] or ""),
                    f"{esc(i['dataset'] or '')} row {n(i['source_row'])}",
                    esc(i["field"] or ""),
                    esc(i["value"] or ""),
                    esc(i["message"]),
                ]
                for i in issues
            ]
            st.markdown(
                table(
                    ["", "entity", "record", "source", "field", "value", "what is wrong"],
                    rows,
                    ["", "mono", "mono", "dim", "mono", "mono", ""],
                ),
                unsafe_allow_html=True,
            )
            st.caption(f"first {len(issues)} matching issues")
        elif ok:
            st.write("nothing matches that filter")
        else:
            st.error(error_text(issues))


def _failures_section(project_id: str, run: dict) -> None:
    totals = run["totals"]
    if run["kind"] != "dry_run":
        return
    st.markdown("#### What the target platform said")
    refused = totals["rejected"] + totals["failed"] + totals["blocked"]
    if not refused:
        st.write(
            f"Every one of the {n(totals['attempted'])} records sent was accepted. Validation "
            "caught everything the target would have refused."
        )
        return
    ok, failures = api_json(
        "GET",
        f"/api/v1/projects/{project_id}/migrations/{run['id']}/failures",
        params={"limit": 200},
    )
    if not ok:
        st.error(error_text(failures))
        return
    rows = [
        [
            chip(f["stage"], "warning" if f["stage"] == "blocked" else "error"),
            esc(f["entity"]),
            esc(f["record_id"] or ""),
            esc(f["http_status"] or ""),
            esc(f["error_type"]),
            n(f["attempts"]),
            esc(f["message"]),
            esc(f["request_id"] or ""),
        ]
        for f in failures
    ]
    st.markdown(
        table(
            ["", "entity", "record", "status", "answer", "tries", "detail", "request id"],
            rows,
            ["", "mono", "mono", "num", "mono", "num", "", "mono"],
        ),
        unsafe_allow_html=True,
    )
    if run.get("failures_truncated"):
        st.caption("more refusals than stored; the counts in the table above are complete.")


def _rules_section(run: dict) -> None:
    left, right = st.columns(2)
    with left:
        st.markdown("#### Customer rules applied")
        rules = run.get("applied_rules") or {}
        if rules:
            rows = [[esc(rule), n(count)] for rule, count in rules.items()]
            st.markdown(table(["rule", "records"], rows, ["", "num"]), unsafe_allow_html=True)
        else:
            st.write("no record rule touched a record")
    with right:
        st.markdown("#### What normalization changed")
        notes = run.get("normalizations") or {}
        if notes:
            rows = [[esc(note), n(count)] for note, count in notes.items()]
            st.markdown(table(["change", "values"], rows, ["", "num"]), unsafe_allow_html=True)
        else:
            st.write("nothing needed normalizing")


def _history(project_id: str, runs: list[dict], current: str | None) -> None:
    st.markdown("#### Runs")
    rows = []
    for r in runs:
        t = r["totals"] or {}
        marker = "▶" if r["id"] == current else ""
        summary = f"{n(t.get('valid'))} valid · {n(t.get('invalid'))} blocked"
        if r["kind"] == "dry_run":
            refused = t.get("rejected", 0) + t.get("failed", 0)
            summary += f" · {n(t.get('accepted'))} accepted · {n(refused)} refused"
        rows.append(
            [
                esc(marker),
                chip(r["kind"].replace("_", " "), "info" if r["kind"] == "dry_run" else "muted"),
                chip(r["status"], "MATCHED" if r["status"] == "completed" else "error"),
                esc(r["started_at"][:19].replace("T", " ")),
                f"{(r['duration_ms'] or 0) / 1000:.1f}s",
                esc(summary),
                esc(r["id"][:8]),
            ]
        )
    st.markdown(
        table(
            ["", "kind", "status", "started (utc)", "took", "summary", "run"],
            rows,
            ["", "", "", "dim", "num", "", "mono"],
        ),
        unsafe_allow_html=True,
    )
    labels = {
        r["id"]: f"{r['kind'].replace('_', ' ')} · {r['started_at'][:19].replace('T', ' ')}"
        for r in runs
    }
    ids = list(labels)
    chosen = st.selectbox(
        "show run",
        options=ids,
        index=ids.index(current) if current in ids else 0,
        format_func=lambda rid: labels[rid],
        label_visibility="collapsed",
    )
    if chosen != current:
        st.session_state["run_id"] = chosen
        st.rerun()


def render() -> None:
    project_id = st.session_state.get("project_id")
    project = load_project(project_id) if project_id else None
    st.markdown('<div class="eyebrow">Implementation workbench</div>', unsafe_allow_html=True)
    st.title("Dry run")
    if not project:
        st.write("Select a project in the sidebar.")
        return
    st.markdown(stage_line(project["stage"]), unsafe_allow_html=True)

    if project["stage"] in ("created", "profiled", "mapped", "in_review"):
        st.info(
            "Transformation runs on approved mappings. Every column has to be approved, rejected "
            "or ignored and every customer question answered first. "
            "[Open mapping review →](mappings)"
        )
        return

    _plan_section(project_id)
    _controls(project_id, project)

    runs = _runs(project_id)
    if not runs:
        st.write("No runs yet. Validate first, then rehearse the migration with a dry run.")
        return
    current = st.session_state.get("run_id")
    if current not in {r["id"] for r in runs}:
        current = runs[0]["id"]
        st.session_state["run_id"] = current
    run = _run(project_id, current)
    if not run:
        st.error("that run could not be loaded")
        return

    kind = "Dry run" if run["kind"] == "dry_run" else "Validation pass"
    st.markdown(f"### {kind} · {esc(run['started_at'][:19].replace('T', ' '))} UTC")
    if run["status"] != "completed":
        st.error(f"this run {run['status']}: {esc(run.get('error') or 'no detail')}")
    _headline(run)
    st.markdown("")
    _entity_table(run)
    _issues_section(project_id, run)
    _failures_section(project_id, run)
    _rules_section(run)
    _history(project_id, runs, current)

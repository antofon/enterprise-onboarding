"""mapping review: what was proposed, what the deterministic pass said, and a person decides.
nothing on this page approves anything by itself."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from common import api_health, api_json, chip, error_text, esc, load_project, n, table

STATUS_CHIP = {
    "suggested": "info",
    "needs_clarification": "AMBIGUOUS",
    "approved": "MATCHED",
    "rejected": "INCOMPATIBLE",
    "ignored": "muted",
}
STATUS_HELP = {
    "suggested": "proposed, nobody has decided",
    "needs_clarification": "a question to the customer is open",
    "approved": "a person approved the target",
    "rejected": "a person rejected the proposal",
    "ignored": "the field is not migrated",
}
ORIGIN_LABEL = {"model": "model", "heuristic": "comparison", "manual": "person"}
ACTIONS = ["approve", "reject", "ignore", "edit", "clarify", "reopen"]
ACTION_LABEL = {
    "approve": "Approve",
    "reject": "Reject",
    "ignore": "Ignore field",
    "edit": "Edit target / rule",
    "clarify": "Ask the customer",
    "reopen": "Reopen",
}


@st.cache_data(ttl=300)
def _catalog_paths() -> list[str]:
    ok, body = api_json("GET", "/api/v1/target-fields")
    return [f["path"] for f in body] if ok else []


@st.cache_data(ttl=60)
def _documents() -> list[dict]:
    ok, body = api_json("GET", "/api/v1/sources/documents")
    return body if ok else []


def _metric(col, label: str, value: str, sub: str | None = None) -> None:
    col.markdown(
        f'<div class="label">{esc(label)}</div><div class="big">{esc(value)}</div>'
        + (f'<div class="kv">{esc(sub)}</div>' if sub else ""),
        unsafe_allow_html=True,
    )


def _confidence(conf: float | None, thresholds: dict) -> str:
    if conf is None:
        return chip("no confidence", "muted")
    if conf >= thresholds.get("high", 0.85):
        kind = "MATCHED"
    elif conf < thresholds.get("low", 0.6):
        kind = "INCOMPATIBLE"
    else:
        kind = "TRANSFORMATION_REQUIRED"
    return chip(f"{conf:.2f}", kind)


def _suggest(project_id: str, rules: str | None, force: bool) -> None:
    payload: dict = {"force": force}
    if rules:
        payload["rules_document"] = rules
    with st.spinner(
        "Asking for proposals for every profiled source. A model call runs a minute or two…"
    ):
        ok, body = api_json(
            "POST", f"/api/v1/projects/{project_id}/mappings/suggest", json=payload, timeout=900
        )
    if ok:
        origin = {d["origin"] for d in body["datasets"]}
        cached = sum(1 for d in body["datasets"] if d["cached"])
        how = "from the comparison (manual mode)" if origin == {"heuristic"} else body["model"]
        st.toast(
            f"{sum(d['written'] for d in body['datasets'])} proposals {how}, "
            f"{cached} from cache, {body['duration_ms']:.0f} ms",
            icon="✅",
        )
        st.rerun()
    else:
        st.error(error_text(body))


def _controls(project: dict, summary: dict | None) -> None:
    project_id = project["id"]
    health = api_health()
    provider = health.get("llm_provider", "-")
    model = health.get("llm_model") or ""
    docs = _documents()
    with st.container(border=True):
        c1, c2, c3, c4 = st.columns([3, 1.2, 1.3, 1.6], vertical_alignment="bottom")
        with c1:
            options = [d["location"] for d in docs]
            default = next((i for i, d in enumerate(docs) if "rules" in d["name"]), None)
            rules = st.selectbox(
                "Customer rules document",
                options=options,
                index=default,
                placeholder="none",
                help="read by the mapping step; must live under a document root",
            )
        with c2:
            force = st.checkbox(
                "Start over", help="replace decided mappings, re-ask, skip the cache"
            )
        with c3:
            label = "Suggest mappings" if not summary or not summary["total"] else "Re-suggest"
            if st.button(label, type="primary", use_container_width=True):
                _suggest(project_id, rules, force)
        with c4:
            pending = summary["high_confidence_pending"] if summary else 0
            high = summary["thresholds"]["high"] if summary else 0.85
            if st.button(
                f"Approve {pending} at ≥ {high:.2f}",
                disabled=not pending,
                use_container_width=True,
                help="approves suggested mappings with a target at or above the threshold",
            ):
                ok, body = api_json(
                    "POST",
                    f"/api/v1/projects/{project_id}/mappings/bulk-approve",
                    json={"min_confidence": high},
                )
                if ok:
                    st.toast(
                        f"approved {body['approved']}, {body['skipped']} still pending", icon="✅"
                    )
                    st.rerun()
                st.error(error_text(body))
        mode = (
            f"model <b>{esc(provider)}</b> · {esc(model)}"
            if provider != "none"
            else "<b>manual mode</b>: no model configured, proposals come from the comparison"
        )
        st.markdown(f'<div class="kv">{mode}</div>', unsafe_allow_html=True)


def _summary(summary: dict) -> None:
    c = summary["counts"]
    cols = st.columns(6)
    _metric(cols[0], "Fields", n(summary["total"]))
    _metric(
        cols[1],
        "Suggested",
        n(c["suggested"]),
        f"{n(summary['low_confidence_pending'])} low confidence",
    )
    _metric(
        cols[2],
        "Asking customer",
        n(c["needs_clarification"]),
        f"{n(summary['open_questions'])} open questions",
    )
    _metric(cols[3], "Approved", n(c["approved"]))
    _metric(cols[4], "Rejected", n(c["rejected"]))
    _metric(cols[5], "Ignored", n(c["ignored"]))
    if summary["coverage"]:
        st.markdown("")
        st.markdown("##### Required target fields, by approved mapping")
        rows = []
        for cov in summary["coverage"]:
            rows.append(
                [
                    f"<b>{esc(cov['entity'])}</b>",
                    f"{len(cov['approved'])} / {len(cov['required'])}",
                    " ".join(chip(p.split(".", 1)[1], "MATCHED") for p in cov["approved"]) or "–",
                    " ".join(chip(p.split(".", 1)[1], "info") for p in cov["pending"]) or "–",
                    " ".join(chip(p.split(".", 1)[1], "INCOMPATIBLE") for p in cov["missing"])
                    or "–",
                ]
            )
        st.markdown(
            table(
                ["entity", "approved", "approved fields", "pending", "nothing lands here"],
                rows,
                ["", "num", "", "", ""],
            ),
            unsafe_allow_html=True,
        )


def _table(mappings: list[dict], thresholds: dict) -> None:
    rows = []
    for m in mappings:
        flags = []
        if m["transformation_required"]:
            flags.append(chip("rule", "TRANSFORMATION_REQUIRED"))
        if m["open_question_count"]:
            flags.append(chip(f"{m['open_question_count']} open", "AMBIGUOUS"))
        target = f'<span class="mono">{esc(m["target_path"])}</span>' if m["target_path"] else "–"
        status = chip(m["status"].replace("_", " "), STATUS_CHIP[m["status"]])
        origin = chip(ORIGIN_LABEL.get(m["origin"], m["origin"]), "type")
        rows.append(
            [
                f'<span class="mono">{esc(m["source_field"])}</span>',
                chip(m["inferred_type"] or "-", "type"),
                target,
                _confidence(m["confidence"], thresholds),
                f"{status} {origin}",
                " ".join(flags) or "",
                f'<span class="kv">{esc(m["reason"] or "")}</span>',
            ]
        )
    st.markdown(
        table(
            ["source field", "type", "target", "confidence", "status", "", "why"],
            rows,
            ["mono", "", "", "", "", "", ""],
        ),
        unsafe_allow_html=True,
    )


def _decide(project_id: str, dataset: str, mappings: list[dict]) -> None:
    names = [m["source_field"] for m in mappings]
    key = f"decide-{dataset}"
    pick = st.selectbox("Field", names, key=f"{key}-field")
    m = next(x for x in mappings if x["source_field"] == pick)
    left, right = st.columns([1.2, 1])
    with left:
        facts = [
            ("status", m["status"].replace("_", " ") + f" · {STATUS_HELP[m['status']]}"),
            (
                "proposed by",
                ORIGIN_LABEL.get(m["origin"], m["origin"])
                + (f" · {m['model']}" if m.get("model") else ""),
            ),
            ("target", m["target_path"] or "none"),
            (
                "confidence",
                f"{m['confidence']:.2f}" if m["confidence"] is not None else "not given",
            ),
            ("comparison said", m["comparison_class"] or "-"),
        ]
        if m["candidates"]:
            facts.append(
                (
                    "name candidates",
                    ", ".join(f"{c['target']} ({c['score']:.2f})" for c in m["candidates"]),
                )
            )
        if m["transformation"]:
            facts.append(("rule", m["transformation"]))
        if m["decided_by"]:
            facts.append(
                ("decided by", f"{m['decided_by']} · {m['decided_at'][:16].replace('T', ' ')}")
            )
        if m["decision_note"]:
            facts.append(("note", m["decision_note"]))
        st.markdown(
            table(["", ""], [[esc(k), esc(v)] for k, v in facts], ["dim", ""]),
            unsafe_allow_html=True,
        )
        if m["reason"]:
            st.markdown(f"**Why:** {esc(m['reason'])}")
    with right:
        with st.form(f"{key}-form", border=True):
            action = st.radio(
                "Decision",
                ACTIONS,
                format_func=lambda a: ACTION_LABEL[a],
                horizontal=True,
                key=f"{key}-action",
            )
            paths = _catalog_paths()
            current = paths.index(m["target_path"]) if m["target_path"] in paths else None
            target = st.selectbox(
                "Target field",
                paths,
                index=current,
                placeholder="none",
                help="approve and edit may change it; required to approve",
            )
            transformation = st.text_area(
                "Deterministic rule (plain English)",
                value=m["transformation"] or "",
                height=80,
            )
            question = st.text_area(
                "Question for the customer (ask the customer only)",
                height=80,
                placeholder="We found … with values … Which does it represent?",
            )
            note = st.text_input("Note", placeholder="why")
            reviewer = st.text_input("Reviewer", value="implementation engineer")
            if st.form_submit_button(ACTION_LABEL[action], type="primary"):
                payload: dict = {
                    "action": action,
                    "reviewer": reviewer or "implementation engineer",
                }
                if action in ("approve", "edit") and target:
                    payload["target_path"] = target
                if action in ("approve", "edit") and transformation != (m["transformation"] or ""):
                    payload["transformation"] = transformation
                if note:
                    payload["note"] = note
                if action == "clarify" and question:
                    payload["question"] = question
                ok, body = api_json(
                    "PATCH", f"/api/v1/projects/{project_id}/mappings/{m['id']}", json=payload
                )
                if ok:
                    st.toast(f"{m['source_field']}: {body['status'].replace('_', ' ')}", icon="✅")
                    st.rerun()
                st.error(error_text(body))


def _questions(project_id: str) -> None:
    ok, questions = api_json("GET", f"/api/v1/projects/{project_id}/clarifications")
    if not ok:
        st.error(error_text(questions))
        return
    open_q = [q for q in questions if q["status"] == "open"]
    done = [q for q in questions if q["status"] != "open"]
    st.markdown("##### Questions for the customer")
    if not open_q:
        st.caption("nothing open")
    paths = _catalog_paths()
    for q in open_q:
        title = f"{q['dataset_name'] or ''} · {q['source_field'] or 'general'}"
        with st.expander(title, expanded=len(open_q) <= 3):
            st.markdown(esc(q["question"]))
            ctx = q.get("context") or {}
            bits = []
            if ctx.get("values"):
                bits.append("values seen: " + ", ".join(f"`{v}`" for v in ctx["values"][:8]))
            if ctx.get("candidates"):
                bits.append("candidates: " + ", ".join(f"`{c}`" for c in ctx["candidates"]))
            if bits:
                st.caption("  ·  ".join(bits))
            with st.form(f"answer-{q['id']}", border=False):
                answer = st.text_area("Customer's answer", height=80)
                c1, c2 = st.columns(2)
                with c1:
                    resolution = st.selectbox(
                        "Apply to the mapping",
                        ["decide later", "approve", "ignore", "reject"],
                        help="with 'approve', pick the target on the right",
                    )
                with c2:
                    default = (
                        paths.index(ctx["proposed_target"])
                        if ctx.get("proposed_target") in paths
                        else None
                    )
                    target = st.selectbox("Target field", paths, index=default, placeholder="none")
                rule = st.text_input(
                    "Rule (optional)",
                    placeholder="Gold and Platinum -> priority, Strategic -> strategic",
                )
                who = st.text_input("Answered by", value="customer")
                a, b = st.columns([1, 1])
                if a.form_submit_button("Record answer", type="primary"):
                    payload: dict = {"answer": answer, "answered_by": who or "customer"}
                    if resolution != "decide later":
                        payload["resolution"] = {"action": resolution}
                        if resolution == "approve":
                            payload["resolution"]["target_path"] = target
                        if rule:
                            payload["resolution"]["transformation"] = rule
                    ok, body = api_json(
                        "PATCH",
                        f"/api/v1/projects/{project_id}/clarifications/{q['id']}",
                        json=payload,
                    )
                    if ok:
                        st.toast("answer recorded", icon="✅")
                        st.rerun()
                    st.error(error_text(body))
                if b.form_submit_button("Withdraw"):
                    ok, body = api_json(
                        "DELETE", f"/api/v1/projects/{project_id}/clarifications/{q['id']}"
                    )
                    if ok:
                        st.rerun()
                    st.error(error_text(body))
    if done:
        with st.expander(f"Answered or withdrawn ({len(done)})"):
            rows = [
                [
                    esc(f"{q['dataset_name'] or ''} · {q['source_field'] or 'general'}"),
                    chip(q["status"], "MATCHED" if q["status"] == "answered" else "muted"),
                    esc(q["question"]),
                    esc(q.get("answer") or (q.get("resolution") or {}).get("withdrawn") or ""),
                ]
                for q in done
            ]
            st.markdown(
                table(["field", "", "question", "answer"], rows, ["mono", "", "", ""]),
                unsafe_allow_html=True,
            )


def _llm_calls(project_id: str) -> None:
    ok, calls = api_json("GET", f"/api/v1/projects/{project_id}/llm-calls")
    if not ok or not calls:
        return
    with st.expander(f"Model calls ({len(calls)})"):
        frame = pd.DataFrame(
            [
                {
                    "when": c["created_at"][:19].replace("T", " "),
                    "dataset": c["dataset"],
                    "provider": c["provider"],
                    "model": c["model"],
                    "status": c["status"],
                    "cached": c["cached"],
                    "attempts": c["attempts"],
                    "in tokens": c["input_tokens"],
                    "out tokens": c["output_tokens"],
                    "ms": c["latency_ms"],
                    "request id": c["request_id"] or "",
                    "error": c["error"] or "",
                }
                for c in calls
            ]
        )
        st.dataframe(frame, hide_index=True, use_container_width=True)
        st.caption(
            "prompts carry statistics, sanitized samples, the catalog and the customer's rules; "
            "never rows, never keys. a cached row means the same brief was answered before."
        )


def render() -> None:
    project_id = st.session_state.get("project_id")
    project = load_project(project_id) if project_id else None
    st.markdown('<div class="eyebrow">Mapping review</div>', unsafe_allow_html=True)
    if not project:
        st.title("Mapping review")
        st.info("Open or select a project on the Overview page first.")
        return
    st.title(f"{project['customer_name']}: field mappings")
    profiled = [d for d in project["datasets"] if d.get("profiled_at")]
    if not profiled:
        st.info("Profile the sources on the Source assessment page first.")
        return

    ok, summary = api_json("GET", f"/api/v1/projects/{project_id}/mappings/summary")
    summary = summary if ok else None
    _controls(project, summary)
    if not summary or not summary["total"]:
        st.info(
            "No proposals yet. Suggest mappings to get a target per source field: from the model "
            "when one is configured, from the deterministic comparison otherwise."
        )
        return

    _summary(summary)
    st.markdown("")
    ok, mappings = api_json("GET", f"/api/v1/projects/{project_id}/mappings")
    if not ok:
        st.error(error_text(mappings))
        return
    by_dataset: dict[str, list[dict]] = {}
    for m in mappings:
        by_dataset.setdefault(m["dataset_name"], []).append(m)
    tabs = st.tabs(list(by_dataset) + ["Customer questions"])
    for tab, (name, rows) in zip(tabs, by_dataset.items(), strict=False):
        with tab:
            _table(rows, summary["thresholds"])
            st.markdown("##### Decide")
            _decide(project_id, name, rows)
    with tabs[-1]:
        _questions(project_id)
    st.markdown("")
    _llm_calls(project_id)
    st.caption(
        "Confidence is the model's estimate that the target is right, about the choice not the "
        "data. Nothing is approved without a person; the bulk button only acts at or above the "
        "configured threshold."
    )

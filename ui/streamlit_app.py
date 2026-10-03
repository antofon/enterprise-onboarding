"""implementation workbench ui. talks to the api over http, never to the database directly,
so the ui exercises the same surface a customer integration would."""

from __future__ import annotations

import streamlit as st

from common import api_health, inject_css, load_projects, model_label
from views import dry_run, mapping_review, overview, readiness, source_assessment

st.set_page_config(page_title="Enterprise Onboarding", page_icon="🧭", layout="wide")
inject_css()

with st.sidebar:
    st.markdown("### Implementation Workbench")
    st.caption("onboarding a new enterprise customer into Meridian")
    health = api_health()
    ok = health.get("status") == "ok"
    dot = f'<span style="color:{"#16A34A" if ok else "#DC2626"}">●</span>'
    line = f"AI: **{model_label(health)}**" if ok else f"service **{health.get('status')}**"
    st.markdown(f"{dot} {line}", unsafe_allow_html=True)
    st.divider()
    projects = load_projects()
    labels = {p["id"]: f"{p['customer_name']} · {p['project_name']}" for p in projects}
    current = st.session_state.get("project_id")
    ids = list(labels)
    index = ids.index(current) if current in ids else (0 if ids else None)
    selected = st.selectbox(
        "Project",
        options=ids,
        format_func=lambda pid: labels[pid],
        index=index,
        placeholder="no projects yet",
    )
    st.session_state["project_id"] = selected

pages = [
    st.Page(overview.render, title="Overview", icon=":material/dashboard:", default=True),
    st.Page(
        source_assessment.render,
        title="Source assessment",
        icon=":material/fact_check:",
        url_path="sources",
    ),
    st.Page(
        mapping_review.render,
        title="Mapping review",
        icon=":material/alt_route:",
        url_path="mappings",
    ),
    st.Page(
        dry_run.render,
        title="Dry run",
        icon=":material/rocket_launch:",
        url_path="dry-run",
    ),
    st.Page(
        readiness.render,
        title="Readiness",
        icon=":material/verified:",
        url_path="readiness",
    ),
]
st.navigation(pages, position="top").run()

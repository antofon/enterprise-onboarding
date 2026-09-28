"""implementation workbench ui. talks to the api over http, never to the database directly,
so the ui exercises the same surface a customer integration would."""

from __future__ import annotations

import os

import httpx
import streamlit as st

API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000")

st.set_page_config(page_title="Enterprise Onboarding", layout="wide")


def api(method: str, path: str, **kwargs) -> httpx.Response:
    return httpx.request(method, f"{API_BASE_URL}{path}", timeout=10, **kwargs)


@st.cache_data(ttl=5)
def api_health() -> dict:
    try:
        return api("GET", "/health").json()
    except Exception as exc:  # noqa: BLE001
        return {"status": "unreachable", "database": "unknown", "error": str(exc)}


def load_projects() -> list[dict]:
    try:
        return api("GET", "/api/v1/projects").json()
    except Exception:  # noqa: BLE001
        return []


with st.sidebar:
    st.markdown("### Implementation Workbench")
    st.caption("onboarding a new enterprise customer into the platform")
    health = api_health()
    ok = health.get("status") == "ok"
    dot = "🟢" if ok else "🔴"
    st.markdown(
        f"{dot} api **{health.get('status')}** · db **{health.get('database')}** · "
        f"llm **{health.get('llm_provider', '-')}**"
    )
    st.divider()
    projects = load_projects()
    labels = {p["id"]: f"{p['customer_name']} · {p['project_name']}" for p in projects}
    selected = st.selectbox(
        "Project",
        options=list(labels.keys()),
        format_func=lambda pid: labels[pid],
        index=0 if labels else None,
        placeholder="no projects yet",
    )
    st.session_state["project_id"] = selected

st.title("Project overview")

if selected:
    project = api("GET", f"/api/v1/projects/{selected}").json()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Customer", project["customer_name"])
    c2.metric("Stage", project["stage"].replace("_", " "))
    c3.metric("Target", project["target_environment"])
    c4.metric("Source datasets", len(project["datasets"]))
    st.markdown("**Source systems:** " + (", ".join(project["source_systems"]) or "none listed"))
    if project.get("notes"):
        st.markdown(f"**Notes:** {project['notes']}")
    st.info(
        "Next: attach the customer's source files and profile them. Profiling arrives on Day 2."
    )
else:
    st.write("No onboarding projects yet. Open the first one below.")

with st.expander("Open a new onboarding project", expanded=not bool(selected)):
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
            r = api("POST", "/api/v1/projects", json=payload)
            if r.status_code == 201:
                st.success(f"created {r.json()['project_name']}")
                st.rerun()
            else:
                err = r.json().get("error", {})
                st.error(f"{err.get('type', r.status_code)}: {err.get('message', r.text)}")

"""implementation workbench ui. talks to the api over http, never to the database directly,
so the ui exercises the same surface a customer integration would."""

from __future__ import annotations

import os

import httpx
import streamlit as st

API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000")

st.set_page_config(page_title="Enterprise Onboarding", layout="wide")


@st.cache_data(ttl=5)
def api_health() -> dict:
    try:
        r = httpx.get(f"{API_BASE_URL}/health", timeout=3)
        return r.json()
    except Exception as exc:  # noqa: BLE001
        return {"status": "unreachable", "database": "unknown", "error": str(exc)}


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

st.title("Project overview")
st.write(
    "Day 1 scaffold. The api, postgres and this ui come up together with `docker compose up`. "
    "Projects, source profiling, mapping review, dry runs and the readiness report arrive over "
    "the next milestones."
)

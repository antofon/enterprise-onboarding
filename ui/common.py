"""shared bits for the workbench pages: api calls, formatting, the small amount of css."""

from __future__ import annotations

import html
import os
from typing import Any

import httpx
import streamlit as st

API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000")

STAGES = [
    "created",
    "profiled",
    "mapped",
    "in_review",
    "ready_to_transform",
    "validated",
    "dry_run_complete",
    "reported",
]

CSS = """
<style>
.chip{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600;
  letter-spacing:.02em;word-spacing:.12em;white-space:nowrap;line-height:18px}
.chip-error{background:#FEE2E2;color:#991B1B}
.chip-warning{background:#FEF3C7;color:#92400E}
.chip-info{background:#E0F2FE;color:#075985}
.chip-MATCHED{background:#D1FAE5;color:#065F46}
.chip-TRANSFORMATION_REQUIRED{background:#FEF3C7;color:#92400E}
.chip-AMBIGUOUS{background:#EDE9FE;color:#5B21B6}
.chip-UNMAPPED{background:#E2E8F0;color:#334155}
.chip-INCOMPATIBLE{background:#FEE2E2;color:#991B1B}
.chip-type{background:#F1F5F9;color:#334155;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  font-weight:500;font-size:11.5px}
.chip-stage{background:#0F766E;color:#fff}
.chip-muted{background:#F1F5F9;color:#94A3B8;font-weight:500}
table.q{width:100%;border-collapse:collapse;font-size:13px;margin:4px 0 12px}
table.q th{text-align:left;color:#64748B;font-weight:600;font-size:11px;text-transform:uppercase;
  letter-spacing:.06em;padding:6px 8px;border-bottom:1px solid #E2E8F0;white-space:nowrap}
table.q td{padding:7px 8px;border-bottom:1px solid #F1F5F9;vertical-align:top}
table.q td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
table.q td.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;
  white-space:nowrap}
table.q td.dim{color:#64748B}
.eyebrow{color:#0F766E;font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
  margin-bottom:-6px}
.stageline{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0 14px}
.kv{color:#64748B;font-size:12.5px}
.big{font-size:28px;font-weight:700;line-height:1.1;font-variant-numeric:tabular-nums}
.label{color:#64748B;font-size:12px;text-transform:uppercase;letter-spacing:.06em;font-weight:600}
.status-banner{border-radius:10px;padding:16px 20px;margin:6px 0 14px;border:1px solid}
.status-word{font-size:26px;font-weight:800;letter-spacing:.04em;line-height:1.1}
.status-head{font-size:15px;margin-top:4px}
.status-blocked{background:#FEF2F2;border-color:#FECACA;color:#991B1B}
.status-conditions{background:#FFFBEB;border-color:#FDE68A;color:#92400E}
.status-ready{background:#ECFDF5;border-color:#A7F3D0;color:#065F46}
.status-head{color:#1E293B}
.explain{font-size:14px;margin:4px 0 8px;line-height:1.5}
ol.questions{padding-left:20px;margin:4px 0 12px}
ol.questions li{margin-bottom:6px;font-size:14px;line-height:1.45}
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def api(method: str, path: str, **kwargs: Any) -> httpx.Response:
    kwargs.setdefault("timeout", 120)
    return httpx.request(method, f"{API_BASE_URL}{path}", **kwargs)


def api_json(method: str, path: str, **kwargs: Any) -> tuple[bool, Any]:
    """(ok, body). errors come back as the api's envelope, network failures as one too."""
    try:
        r = api(method, path, **kwargs)
    except httpx.HTTPError as exc:
        return False, {"error": {"type": "unreachable", "message": str(exc), "details": {}}}
    if r.status_code == 204:
        return True, None
    try:
        body = r.json()
    except ValueError:
        body = {"error": {"type": f"http_{r.status_code}", "message": r.text[:200], "details": {}}}
    return r.is_success, body


def error_text(body: Any) -> str:
    err = (body or {}).get("error", {}) if isinstance(body, dict) else {}
    return f"{err.get('type', 'error')}: {err.get('message', 'request failed')}"


@st.cache_data(ttl=5)
def api_health() -> dict:
    ok, body = api_json("GET", "/health")
    return body if isinstance(body, dict) else {"status": "unreachable"}


def load_projects() -> list[dict]:
    ok, body = api_json("GET", "/api/v1/projects")
    return body if ok and isinstance(body, list) else []


def load_project(project_id: str) -> dict | None:
    ok, body = api_json("GET", f"/api/v1/projects/{project_id}")
    return body if ok else None


def chip(text: str, kind: str) -> str:
    return f'<span class="chip chip-{html.escape(kind)}">{html.escape(str(text))}</span>'


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def n(value: Any) -> str:
    """1030 -> 1,030"""
    if value is None:
        return "–"
    if isinstance(value, float):
        return f"{value:,.1f}"
    return f"{int(value):,}"


def pct(value: Any) -> str:
    return "–" if value is None else f"{value:.1f}%"


def stage_line(current: str) -> str:
    parts = []
    passed = True
    for stage in STAGES:
        label = stage.replace("_", " ")
        if stage == current:
            parts.append(chip(label, "stage"))
            passed = False
        elif passed:
            parts.append(chip(label, "MATCHED"))
        else:
            parts.append(chip(label, "muted"))
    return f'<div class="stageline">{"".join(parts)}</div>'


def table(headers: list[str], rows: list[list[str]], classes: list[str] | None = None) -> str:
    """rows hold html already escaped by the caller. classes are per column (num, mono, dim)."""
    classes = classes or [""] * len(headers)
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join(
        "<tr>"
        + "".join(f'<td class="{classes[i]}">{cell}</td>' for i, cell in enumerate(row))
        + "</tr>"
        for row in rows
    )
    return f'<table class="q"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'

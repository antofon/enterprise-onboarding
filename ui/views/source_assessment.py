"""source assessment: attach the customer's sources, profile them, read the damage."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from common import api_json, chip, error_text, esc, load_project, n, pct, table

CLASS_ORDER = ["MATCHED", "TRANSFORMATION_REQUIRED", "AMBIGUOUS", "UNMAPPED", "INCOMPATIBLE"]
CLASS_HELP = {
    "MATCHED": "the name lines up inside the entity and the values fit as they are",
    "TRANSFORMATION_REQUIRED": "the name lines up, a deterministic rule gets the values there",
    "AMBIGUOUS": "a plausible candidate exists, a person confirms",
    "UNMAPPED": "nothing in the name helps, the mapping step proposes and a person decides",
    "INCOMPATIBLE": "no rule we have turns this column into that field",
}


def _profile(project_id: str) -> None:
    with st.spinner("Loading and profiling every attached source…"):
        ok, body = api_json("POST", f"/api/v1/projects/{project_id}/sources/profile")
    if ok:
        st.toast(
            f"profiled {len(body['datasets'])} sources in {body['duration_ms']:.0f} ms", icon="✅"
        )
        st.rerun()
    else:
        st.error(error_text(body))


def _controls(project: dict) -> None:
    project_id = project["id"]
    datasets = project["datasets"]
    attached = {d["name"] for d in datasets}
    ok, available = api_json("GET", "/api/v1/sources/available")
    options = [s for s in (available if ok else []) if s["name"] not in attached]

    with st.container(border=True):
        c1, c2, c3 = st.columns([4, 1.2, 1.4], vertical_alignment="bottom")
        with c1:
            picked = st.multiselect(
                "Attach sources",
                options=range(len(options)),
                format_func=lambda i: (
                    f"{options[i]['name']}  ·  {options[i]['kind']}  ·  {options[i]['location']}"
                ),
                placeholder="the customer's exports and the billing feed",
            )
        with c2:
            if st.button("Attach", disabled=not picked, use_container_width=True):
                failures = []
                for i in picked:
                    src = options[i]
                    ok, body = api_json(
                        "POST",
                        f"/api/v1/projects/{project_id}/sources",
                        json={
                            "name": src["name"],
                            "kind": src["kind"],
                            "location": src["location"],
                        },
                    )
                    if not ok:
                        failures.append(f"{src['name']}: {error_text(body)}")
                if failures:
                    st.error("\n".join(failures))
                else:
                    st.rerun()
        with c3:
            if st.button(
                "Profile all sources",
                type="primary",
                disabled=not datasets,
                use_container_width=True,
            ):
                _profile(project_id)

        if datasets:
            with st.expander(f"Attached sources ({len(datasets)})", expanded=False):
                for d in datasets:
                    a, b = st.columns([6, 1], vertical_alignment="center")
                    when = (
                        d["profiled_at"][:16].replace("T", " ") + " UTC"
                        if d.get("profiled_at")
                        else "not profiled"
                    )
                    a.markdown(
                        f"**{esc(d['name'])}** {chip(d['kind'], 'type')} "
                        f'<span class="kv">{esc(d["location"])} · {when}</span>',
                        unsafe_allow_html=True,
                    )
                    if b.button("Detach", key=f"detach-{d['id']}", use_container_width=True):
                        ok, body = api_json(
                            "DELETE", f"/api/v1/projects/{project_id}/sources/{d['id']}"
                        )
                        if ok:
                            st.rerun()
                        st.error(error_text(body))


def _metric(col, label: str, value: str, sub: str | None = None) -> None:
    col.markdown(
        f'<div class="label">{esc(label)}</div><div class="big">{esc(value)}</div>'
        + (f'<div class="kv">{esc(sub)}</div>' if sub else ""),
        unsafe_allow_html=True,
    )


def _dataset(project_id: str, ds: dict) -> None:
    q = ds.get("quality") or {}
    counts = q.get("issue_counts", {})
    key = q.get("key_column")

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    _metric(c1, "Rows", n(ds["row_count"]))
    _metric(c2, "Columns", n(ds["column_count"]))
    _metric(
        c3,
        "Key column",
        key or "none",
        f"{n(q.get('key_missing'))} missing · {n(q.get('key_duplicates'))} duplicated"
        if key
        else "re-runs cannot be idempotent",
    )
    _metric(c4, "Duplicate rows", n(q.get("exact_duplicate_rows")), "exact copies")
    _metric(c5, "Errors", n(counts.get("error", 0)), "block migration")
    _metric(c6, "Warnings", n(counts.get("warning", 0)), f"{n(counts.get('info', 0))} info")

    refs = q.get("references") or []
    if refs:
        bits = []
        for r in refs:
            kind = "error" if r["orphans"] else "MATCHED"
            bits.append(
                chip(f"{r['column']} → {r['references']}", "type")
                + " "
                + chip(
                    f"{n(r['resolved'])} resolve · {n(r['orphans'])} orphans "
                    f"({pct(r['orphan_pct'])})",
                    kind,
                )
            )
        st.markdown(
            "<div style='margin:10px 0 4px'>" + " &nbsp; ".join(bits) + "</div>",
            unsafe_allow_html=True,
        )

    st.markdown("")
    left = st.container()
    right = st.container()
    with left:
        st.markdown("##### Quality issues")
        issues = q.get("issues") or []
        if not issues:
            st.success("nothing to report")
        else:
            rows = [
                [
                    chip(i["severity"], i["severity"]),
                    f'<span class="mono">{esc(i.get("column") or "")}</span>',
                    esc(i["message"]),
                    n(i["count"]),
                    pct(i.get("pct")) if i.get("pct") is not None else "",
                ]
                for i in issues
            ]
            st.markdown(
                table(
                    ["", "column", "what", "rows", "share"],
                    rows,
                    ["", "mono", "", "num", "num dim"],
                ),
                unsafe_allow_html=True,
            )

    ok, detail = api_json("GET", f"/api/v1/projects/{project_id}/sources/{ds['id']}")
    fields = detail.get("fields", []) if ok else []
    with right:
        st.markdown("##### Columns")
        if fields:
            frame = pd.DataFrame(
                [
                    {
                        "column": f["name"],
                        "type": f["inferred_type"],
                        "null %": f["null_pct"],
                        "unique %": f["unique_pct"],
                        "distinct": f["distinct_count"],
                        "malformed": f["stats"].get("malformed_count"),
                        "samples": " · ".join(f["sample_values"][:3]),
                    }
                    for f in fields
                ]
            )
            st.dataframe(
                frame,
                hide_index=True,
                use_container_width=True,
                height=min(38 + 35 * len(frame), 600),
                column_config={
                    "null %": st.column_config.ProgressColumn(
                        format="%.1f%%", min_value=0, max_value=100
                    ),
                    "unique %": st.column_config.ProgressColumn(
                        format="%.1f%%", min_value=0, max_value=100
                    ),
                    "distinct": st.column_config.NumberColumn(format="%d"),
                    "malformed": st.column_config.NumberColumn(format="%d"),
                },
            )

    if fields:
        st.markdown("##### Column detail")
        names = [f["name"] for f in fields]
        pick = st.selectbox("Column", names, key=f"col-{ds['id']}", label_visibility="collapsed")
        f = next(x for x in fields if x["name"] == pick)
        s = f["stats"]
        a, b = st.columns([1, 1.4])
        with a:
            facts = [
                ("inferred type", f["inferred_type"]),
                ("non-null", n(s.get("non_null_count"))),
                (
                    "blank / placeholder",
                    f"{n(s.get('blank_count'))} / {n(s.get('placeholder_count'))}",
                ),
                ("distinct", n(f["distinct_count"])),
            ]
            for key_name, label in (
                ("malformed_count", "malformed"),
                ("numeric_text_count", "with symbols"),
                ("float_text_count", "whole numbers as x.0"),
                ("uppercase_count", "upper-case"),
                ("no_scheme_count", "urls without scheme"),
                ("e164_count", "already E.164"),
                ("whitespace_count", "stray whitespace"),
                ("normalized_duplicates", "duplicates once normalized"),
                ("future_count", "in the future"),
                ("iso_pct", "ISO 8601 share"),
                ("min", "min"),
                ("max", "max"),
                ("mean", "mean"),
            ):
                if s.get(key_name) not in (None, 0, 0.0, "", []):
                    val = s[key_name]
                    facts.append(
                        (
                            label,
                            pct(val)
                            if key_name == "iso_pct"
                            else (n(val) if isinstance(val, (int, float)) else str(val)),
                        )
                    )
            if s.get("raw_types"):
                facts.append(
                    ("json types", ", ".join(f"{k} {v}" for k, v in s["raw_types"].items()))
                )
            st.markdown(
                table(["fact", "value"], [[esc(k), esc(v)] for k, v in facts], ["dim", "mono"]),
                unsafe_allow_html=True,
            )
            if s.get("malformed_examples"):
                st.markdown(
                    "**malformed examples**  \n"
                    + "  \n".join(f"`{x}`" for x in s["malformed_examples"])
                )
            if s.get("shapes"):
                st.markdown(
                    table(
                        ["shape", "rows", "example"],
                        [
                            [
                                f'<span class="mono">{esc(x["shape"])}</span>',
                                n(x["count"]),
                                esc(x["example"]),
                            ]
                            for x in s["shapes"]
                        ],
                        ["mono", "num", "dim"],
                    ),
                    unsafe_allow_html=True,
                )
        with b:
            if s.get("format_counts"):
                st.markdown('<div class="label">date formats seen</div>', unsafe_allow_html=True)
                fmt = pd.DataFrame(
                    {"format": list(s["format_counts"]), "rows": list(s["format_counts"].values())}
                )
                st.bar_chart(
                    fmt, x="format", y="rows", color="#0F766E", horizontal=True, height=200
                )
            top = s.get("top_values") or []
            if top and f["inferred_type"] not in (
                "identifier",
                "email",
                "phone",
                "url",
                "date",
                "datetime",
            ):
                st.markdown('<div class="label">most common values</div>', unsafe_allow_html=True)
                tv = pd.DataFrame(
                    {"value": [str(v) or "(blank)" for v, _ in top], "rows": [c for _, c in top]}
                )
                st.bar_chart(
                    tv,
                    x="value",
                    y="rows",
                    color="#0F766E",
                    horizontal=True,
                    height=min(60 + 26 * len(tv), 320),
                )
            if s.get("case_variants"):
                st.markdown(
                    '<div class="label">same value, different case</div>', unsafe_allow_html=True
                )
                st.markdown(
                    "  \n".join(" / ".join(f"`{x}`" for x in g) for g in s["case_variants"][:6])
                )
            if s.get("spellings"):
                st.markdown('<div class="label">spellings</div>', unsafe_allow_html=True)
                st.markdown(
                    " ".join(chip(f"{k} · {v}", "type") for k, v in s["spellings"].items()),
                    unsafe_allow_html=True,
                )


def _comparison(project_id: str) -> None:
    ok, report = api_json("GET", f"/api/v1/projects/{project_id}/schema-comparison")
    if not ok:
        st.info(error_text(report))
        return
    counts = report["counts"]
    cols = st.columns(5)
    for col, cls in zip(cols, CLASS_ORDER, strict=True):
        col.markdown(
            f'<div class="big">{counts.get(cls, 0)}</div>{chip(cls.replace("_", " ").lower(), cls)}'
            f'<div class="kv" style="margin-top:4px">{esc(CLASS_HELP[cls])}</div>',
            unsafe_allow_html=True,
        )
    st.markdown("")
    st.markdown("##### Required target fields")
    st.markdown(
        '<div class="kv">per entity: what the sources already cover, what only has a candidate, '
        "and what nothing in the sources looks like. "
        "the gaps are the mapping step's to-do list.</div>",
        unsafe_allow_html=True,
    )
    rows = []
    for cov in report["coverage"]:
        rows.append(
            [
                f"<b>{esc(cov['entity'])}</b><br>"
                f"<span class='kv'>{esc(', '.join(cov['datasets']))}</span>",
                f"{len(cov['covered'])} / {len(cov['required'])}",
                " ".join(chip(p.split(".", 1)[1], "MATCHED") for p in cov["covered"]) or "–",
                " ".join(chip(p.split(".", 1)[1], "AMBIGUOUS") for p in cov["candidate_only"])
                or "–",
                " ".join(chip(p.split(".", 1)[1], "INCOMPATIBLE") for p in cov["missing"]) or "–",
            ]
        )
    st.markdown(
        table(
            ["entity", "covered", "from a source field", "candidate only", "no source"],
            rows,
            ["", "num", "", "", ""],
        ),
        unsafe_allow_html=True,
    )

    by_dataset: dict[str, list[dict]] = {}
    for fc in report["fields"]:
        by_dataset.setdefault(fc["dataset"], []).append(fc)
    for name, fcs in by_dataset.items():
        entity = report["datasets"].get(name) or "entity not inferred"
        st.markdown(f"##### {esc(name)} {chip(entity, 'type')}", unsafe_allow_html=True)
        rows = []
        for fc in fcs:
            if fc["target"]:
                target = f'<span class="mono">{esc(fc["target"])}</span>'
            elif fc["candidates"]:
                parts = []
                for c in fc["candidates"][:2]:
                    where = "" if c["in_entity"] else " · other entity"
                    parts.append(
                        f'<span class="mono">{esc(c["target"])}</span> '
                        f'<span class="kv">{c["score"]:.2f}{where}</span>'
                    )
                target = " ".join(parts)
            else:
                target = "–"
            rows.append(
                [
                    f'<span class="mono">{esc(fc["source_field"])}</span>',
                    chip(fc["inferred_type"], "type"),
                    chip(fc["classification"].replace("_", " ").lower(), fc["classification"]),
                    target,
                    f'<span class="kv">{esc(fc["reason"])}</span>',
                ]
            )
        st.markdown(
            table(
                ["source field", "type", "classification", "target", "why"],
                rows,
                ["mono", "", "", "", ""],
            ),
            unsafe_allow_html=True,
        )
    st.caption(
        "Scores are name similarity, not confidence. This pass never guesses at meaning: "
        "acct_num → organization_id and customer_tier → ? are left for the AI-assisted "
        "mapping step, where a person signs off."
    )


def render() -> None:
    project_id = st.session_state.get("project_id")
    project = load_project(project_id) if project_id else None
    st.markdown('<div class="eyebrow">Source assessment</div>', unsafe_allow_html=True)
    if not project:
        st.title("Source assessment")
        st.info("Open or select a project on the Overview page first.")
        return
    st.title(f"{project['customer_name']}: sources")
    _controls(project)

    datasets = project["datasets"]
    if not datasets:
        st.info("Nothing attached yet. Pick the customer's exports and the billing feed above.")
        return
    profiled = [d for d in datasets if d.get("profiled_at")]
    if not profiled:
        st.warning(
            "Sources are attached but not profiled. "
            "Profile them to see data quality and the schema comparison."
        )
        return

    tabs = st.tabs([d["name"] for d in profiled] + ["Schema comparison"])
    for tab, ds in zip(tabs, profiled, strict=False):
        with tab:
            _dataset(project["id"], ds)
    with tabs[-1]:
        _comparison(project["id"])

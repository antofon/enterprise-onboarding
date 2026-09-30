"""which columns carry another dataset's key, and how many of them point at nothing.
the rule is deliberately dumb: same column name as another dataset's key column. anything
smarter (acct_num vs account_id) is the mapping step's job, where a human signs off."""

from __future__ import annotations

import pandas as pd

from app.services.profiling import inference as inf
from app.services.profiling.types import DatasetProfile, Reference

ORPHAN_EXAMPLES = 5


def _values(df: pd.DataFrame, column: str) -> list[str | None]:
    return [None if inf.is_null_like(v) else str(v).strip() for v in df[column].tolist()]


def infer_references(
    frames: dict[str, pd.DataFrame], profiles: dict[str, DatasetProfile]
) -> dict[str, list[Reference]]:
    keys = {name: p.key_column for name, p in profiles.items() if p.key_column and name in frames}
    key_sets: dict[str, set[str]] = {
        name: {v for v in _values(frames[name], col) if v is not None} for name, col in keys.items()
    }
    out: dict[str, list[Reference]] = {name: [] for name in profiles}
    for child, cdf in frames.items():
        for parent, parent_key in keys.items():
            if parent == child or parent_key not in cdf.columns:
                continue
            if profiles[child].key_column == parent_key:
                continue  # that is this dataset's own id, not a reference
            values = [v for v in _values(cdf, parent_key) if v is not None]
            parent_values = key_sets[parent]
            orphans = [v for v in values if v not in parent_values]
            examples: list[str] = []
            for v in orphans:
                if v not in examples:
                    examples.append(v)
                    if len(examples) >= ORPHAN_EXAMPLES:
                        break
            out[child].append(
                Reference(
                    column=parent_key,
                    references=f"{parent}.{parent_key}",
                    checked=len(values),
                    resolved=len(values) - len(orphans),
                    orphans=len(orphans),
                    orphan_pct=round(100.0 * len(orphans) / len(values), 1) if values else 0.0,
                    orphan_examples=examples,
                )
            )
    return out

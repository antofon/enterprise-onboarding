"""turn a dataframe of raw values into a DatasetProfile. every number here is counted."""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from datetime import date, datetime
from typing import Any

import pandas as pd

from app.services.profiling import inference as inf
from app.services.profiling.types import DatasetProfile, FieldProfile

SAMPLE_VALUES = 5
SAMPLE_LEN = 60
TOP_VALUES = 10
FULL_VALUE_COUNTS_UP_TO = 50
DISTINCT_SAMPLE = 50
TOP_SHAPES = 5
DOMINANT_SHARE = 0.6

# tags that count towards each column type
_NUMERIC_TAGS = {inf.INTEGER, inf.DECIMAL, inf.NUMERIC_TEXT}
_DATE_TAGS = {inf.DATE, inf.DATETIME}
_EMAIL_TAGS = {inf.EMAIL, inf.EMAIL_MALFORMED}
_URL_TAGS = {inf.URL, inf.URL_NO_SCHEME}
_PHONE_TAGS = {inf.PHONE, inf.PHONE_SHORT}
_NO_MALFORMED = {"boolean", "category", "text", "code", "empty"}


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def _truncate(text: str) -> str:
    return text if len(text) <= SAMPLE_LEN else text[: SAMPLE_LEN - 1] + "…"


def _as_text(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return str(value)


def _norm_key(text: str) -> str:
    return " ".join(text.lower().split()).strip(" .,;")


class _Column:
    """one pass over a column, then a FieldProfile."""

    def __init__(self, name: str, position: int, values: list[Any], row_count: int) -> None:
        self.name = name
        self.position = position
        self.row_count = row_count
        self.raw_types: Counter[str] = Counter()
        self.tags: Counter[str] = Counter()
        self.present: list[str] = []  # stripped text of every non-null cell, in row order
        self.present_tags: list[str] = []  # the tag of each entry in `present`
        self.raw_present: list[str] = []  # as written (whitespace kept), for whitespace counts
        self.blank = 0
        self.placeholder = 0
        for value in values:
            if value is not None and not (isinstance(value, float) and value != value):
                self.raw_types[type(value).__name__] += 1
            tag = inf.tag_value(value)
            if tag == inf.BLANK:
                self.blank += 1
                continue
            if tag == inf.PLACEHOLDER:
                self.placeholder += 1
                continue
            self.tags[tag] += 1
            text = _as_text(value)
            self.raw_present.append(text)
            self.present.append(text.strip())
            self.present_tags.append(tag)

    # --- type decision ---------------------------------------------------------------------

    def _share(self, tags: set[str]) -> float:
        return sum(self.tags[x] for x in tags) / len(self.present)

    def decide_type(self) -> str:
        n = len(self.present)
        if n == 0:
            return "empty"
        distinct_lower = {v.lower() for v in self.present}
        if distinct_lower <= inf.BOOL_TOKENS:
            # "0"/"1" alone is only a boolean when the name says so; otherwise it is a count
            if not distinct_lower <= {"0", "1"} or inf.has_bool_hint(self.name):
                return "boolean"
        t = self.tags
        if inf.has_phone_hint(self.name) and self._share(_PHONE_TAGS | {inf.INTEGER}) >= 0.6:
            return "phone"
        if self._share({inf.IDENTIFIER}) >= DOMINANT_SHARE:
            return "identifier"
        if self._share(_NUMERIC_TAGS) >= DOMINANT_SHARE:
            all_ints = t[inf.INTEGER] == sum(t[x] for x in _NUMERIC_TAGS)
            if inf.has_key_hint(self.name) and all_ints:
                return "identifier"  # an id that happens to be digits is not a quantity
            if all_ints:
                return "integer"
            parsed = [inf.parse_number(v) for v in self.present]
            if all(p.is_integer() for p in parsed if p is not None):
                return "integer"
            return "decimal"
        if self._share(_DATE_TAGS) >= DOMINANT_SHARE:
            return "datetime" if t[inf.DATETIME] >= t[inf.DATE] else "date"
        if self._share(_EMAIL_TAGS) >= DOMINANT_SHARE:
            return "email"
        if self._share(_URL_TAGS) >= DOMINANT_SHARE:
            return "url"
        if self._share(_PHONE_TAGS) >= DOMINANT_SHARE:
            return "phone"
        if self._share({inf.CODE}) >= DOMINANT_SHARE:
            return "code"
        distinct = len(set(self.present))
        if distinct <= 25 and n >= 20 and distinct / n <= 0.1:
            return "category"
        return "text"

    # --- stats -----------------------------------------------------------------------------

    def build(self) -> FieldProfile:
        inferred = self.decide_type()
        n = len(self.present)
        counts = Counter(self.present)
        distinct = len(counts)
        stats: dict[str, Any] = {
            "non_null_count": n,
            "blank_count": self.blank,
            "placeholder_count": self.placeholder,
            "type_counts": dict(self.tags.most_common()),
            "whitespace_count": sum(1 for v in self.raw_present if v != v.strip() or "  " in v),
            "max_length": max((len(v) for v in self.present), default=0),
        }
        if len(self.raw_types) > 1:
            stats["raw_types"] = dict(self.raw_types.most_common())
        stats["top_values"] = [[v, c] for v, c in counts.most_common(TOP_VALUES)]
        if distinct <= FULL_VALUE_COUNTS_UP_TO:
            stats["value_counts"] = {v: c for v, c in counts.most_common()}
        else:
            stats["distinct_sample"] = self._first_distinct(DISTINCT_SAMPLE)

        shapes: Counter[str] = Counter()
        shape_example: dict[str, str] = {}
        for v in self.present:
            s = inf.shape(v)
            shapes[s] += 1
            shape_example.setdefault(s, v)
        stats["shapes"] = [
            {"shape": s, "count": c, "example": _truncate(shape_example[s])}
            for s, c in shapes.most_common(TOP_SHAPES)
        ]

        groups: dict[str, set[str]] = defaultdict(set)
        for v in counts:
            groups[v.lower()].add(v)
        variants = [sorted(g) for g in groups.values() if len(g) > 1]
        if variants:
            stats["case_variants"] = sorted(variants)[:10]
            stats["case_variant_groups"] = len(variants)

        malformed: list[str] = []
        if inferred in ("integer", "decimal"):
            self._number_stats(stats, malformed, inferred)
        elif inferred in ("date", "datetime"):
            self._date_stats(stats, malformed, inferred)
        elif inferred == "email":
            self._email_stats(stats, malformed)
        elif inferred == "url":
            self._url_stats(stats, malformed)
        elif inferred == "phone":
            self._phone_stats(stats, malformed)
        elif inferred == "boolean":
            self._boolean_stats(stats, counts)
        elif inferred == "identifier":
            for v, tag in zip(self.present, self.present_tags, strict=True):
                if tag not in (inf.IDENTIFIER, inf.INTEGER, inf.CODE):
                    malformed.append(v)
        if inferred not in _NO_MALFORMED:
            stats["malformed_count"] = len(malformed)
            if malformed:
                stats["malformed_examples"] = [_truncate(v) for v in malformed[:5]]

        if inferred == "email" or inf.has_name_hint(self.name):
            seen_norm: set[str] = set()
            dupes = 0
            for v in self.present:
                k = _norm_key(v)
                if k in seen_norm:
                    dupes += 1
                else:
                    seen_norm.add(k)
            stats["normalized_duplicates"] = dupes

        null_count = self.blank + self.placeholder
        return FieldProfile(
            name=self.name,
            position=self.position,
            inferred_type=inferred,
            null_count=null_count,
            null_pct=_pct(null_count, self.row_count),
            distinct_count=distinct,
            unique_pct=_pct(distinct, n),
            sample_values=[_truncate(s) for s in self._first_distinct(SAMPLE_VALUES)],
            stats=stats,
        )

    def _first_distinct(self, limit: int) -> list[str]:
        seen: list[str] = []
        for v in self.present:
            if v not in seen:
                seen.append(v)
                if len(seen) >= limit:
                    break
        return seen

    def _number_stats(self, stats: dict[str, Any], malformed: list[str], inferred: str) -> None:
        nums: list[float] = []
        numeric_text = 0
        float_text = 0
        for v, tag in zip(self.present, self.present_tags, strict=True):
            if tag == inf.NUMERIC_TEXT:
                numeric_text += 1
            elif tag == inf.DECIMAL and inferred == "integer":
                float_text += 1
            parsed = inf.parse_number(v) if tag in _NUMERIC_TAGS else None
            if parsed is None:
                malformed.append(v)
            else:
                nums.append(parsed)
        if nums:
            stats["min"] = min(nums)
            stats["max"] = max(nums)
            stats["mean"] = round(sum(nums) / len(nums), 2)
        stats["parsed_count"] = len(nums)
        stats["numeric_text_count"] = numeric_text
        stats["float_text_count"] = float_text

    def _date_stats(self, stats: dict[str, Any], malformed: list[str], inferred: str) -> None:
        formats: Counter[str] = Counter()
        lo: datetime | None = None
        hi: datetime | None = None
        future = 0
        today = date.today()
        for v in self.present:
            parsed = inf.parse_datetime(v) if ":" in v else None
            if parsed is None:
                parsed_date = inf.parse_date(v)
                if parsed_date is not None:
                    parsed = (datetime.combine(parsed_date[0], datetime.min.time()), parsed_date[1])
                else:
                    parsed = inf.parse_datetime(v)
            if parsed is None:
                malformed.append(v)
                continue
            when, fmt = parsed
            formats[fmt] += 1
            naive = when.replace(tzinfo=None)
            lo = naive if lo is None or naive < lo else lo
            hi = naive if hi is None or naive > hi else hi
            if naive.date() > today:
                future += 1
        iso = inf.ISO_DATETIMES if inferred == "datetime" else {inf.ISO_DATE}
        parsed_total = sum(formats.values())
        stats["format_counts"] = dict(formats.most_common())
        stats["iso_count"] = sum(c for f, c in formats.items() if f in iso)
        stats["iso_pct"] = _pct(stats["iso_count"], parsed_total)
        stats["future_count"] = future
        if lo is not None and hi is not None:
            stats["min"] = lo.isoformat()
            stats["max"] = hi.isoformat()

    def _email_stats(self, stats: dict[str, Any], malformed: list[str]) -> None:
        upper = 0
        for v, tag in zip(self.present, self.present_tags, strict=True):
            if tag != inf.EMAIL:
                malformed.append(v)
            elif v != v.lower():
                upper += 1
        stats["uppercase_count"] = upper

    def _url_stats(self, stats: dict[str, Any], malformed: list[str]) -> None:
        no_scheme = 0
        for v, tag in zip(self.present, self.present_tags, strict=True):
            if tag == inf.URL_NO_SCHEME:
                no_scheme += 1
            elif tag != inf.URL:
                malformed.append(v)
        stats["no_scheme_count"] = no_scheme

    def _phone_stats(self, stats: dict[str, Any], malformed: list[str]) -> None:
        e164 = 0
        for v in self.present:
            digits = inf.phone_digits(v)
            if digits is None or len(digits) < 10:
                malformed.append(v)
            elif v.startswith("+") and v[1:].isdigit():
                e164 += 1
        stats["e164_count"] = e164

    def _boolean_stats(self, stats: dict[str, Any], counts: Counter[str]) -> None:
        spellings = counts.most_common()
        stats["spellings"] = dict(spellings)
        stats["spelling_count"] = len(spellings)
        # the most common spelling of true and of false are the canon; the rest are variants
        canon = {
            next((v for v, _ in spellings if v.lower() in tokens), None)
            for tokens in (inf.BOOL_TRUE, inf.BOOL_FALSE)
        }
        stats["variant_count"] = sum(c for v, c in spellings if v not in canon)


def _pick_key(fields: list[FieldProfile]) -> FieldProfile | None:
    candidates = [
        f
        for f in fields
        if inf.has_key_hint(f.name)
        and f.inferred_type in ("identifier", "integer", "code", "text")
        and f.unique_pct >= 90
        and f.null_pct <= 20
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda f: (-f.unique_pct, f.position))[0]


def profile_frame(df: pd.DataFrame, *, name: str) -> DatasetProfile:
    started = time.perf_counter()
    row_count = len(df)
    fields = [
        _Column(str(column), position, df[column].tolist(), row_count).build()
        for position, column in enumerate(df.columns)
    ]
    exact_dupes = int(df.astype(str).duplicated().sum()) if row_count else 0

    key = _pick_key(fields)
    key_missing = key_duplicates = 0
    if key is not None:
        seen: set[str] = set()
        for raw in df[key.name].tolist():
            if inf.is_null_like(raw):
                key_missing += 1
                continue
            v = str(raw).strip()
            if v in seen:
                key_duplicates += 1
            else:
                seen.add(v)

    return DatasetProfile(
        name=name,
        row_count=row_count,
        column_count=len(df.columns),
        fields=fields,
        exact_duplicate_rows=exact_dupes,
        key_column=key.name if key else None,
        key_missing=key_missing,
        key_duplicates=key_duplicates,
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )

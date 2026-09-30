"""value-level classification. every non-blank cell gets one tag, and the column's type is
decided from the tag counts (see profiler.py). the tags are what let us count malformed values
per type instead of trusting whatever dtype pandas would have guessed."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

PLACEHOLDERS = frozenset(
    {"n/a", "na", "n.a.", "none", "null", "nil", "unknown", "-", "--", "?", "tbd", "todo"}
    | {"missing"}
)
BOOL_TRUE = frozenset({"y", "yes", "true", "t", "1"})
BOOL_FALSE = frozenset({"n", "no", "false", "f", "0"})
BOOL_TOKENS = BOOL_TRUE | BOOL_FALSE

DATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%d-%b-%Y",
    "%Y/%m/%d",
    "%B %d, %Y",
    "%m/%d/%y",
    "%d/%m/%Y",
    "%Y%m%d",
)
ISO_DATE = "%Y-%m-%d"
DATETIME_FORMATS: tuple[str, ...] = (
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S%z",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%d-%b-%Y %I:%M %p",
    "%d-%b-%Y %H:%M",
)
ISO_DATETIMES = frozenset(
    {
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%S",
    }
)

_INT = re.compile(r"^[+-]?\d+$")
_DEC = re.compile(r"^[+-]?(\d+\.\d*|\.\d+)$")
# "$7,050,000"  "38.60M"  "USD 60"  "1,475.00"  "12%"
_NUM_TEXT = re.compile(r"^(?:[A-Z]{3}\s|[$€£]\s?)?[+-]?\d[\d,]*(?:\.\d+)?\s?(?:[MKkBb]|%)?$")
_EMAIL = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")
_URL = re.compile(r"^https?://\S+$", re.IGNORECASE)
_URL_NO_SCHEME = re.compile(r"^(www\.)?[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}(/\S*)?$", re.IGNORECASE)
_PHONE_CHARS = re.compile(r"^\+?[\d\s().-]{7,24}$")
_NON_DIGIT = re.compile(r"\D")
_CODE = re.compile(r"^[A-Z][A-Z0-9]{1,5}$")
_IDENTIFIER = re.compile(r"^[A-Za-z]{1,6}[-_]?\d{3,}$")
_PHONE_HINT = re.compile(r"(^|_)(phone|tel|telephone|mobile|cell|fax)(_|$)", re.IGNORECASE)
_KEY_HINT = re.compile(r"(^|_)(id|ref|num|no|key|code|number|identifier)(_|$)", re.IGNORECASE)
_NAME_HINT = re.compile(r"(^|_)name(_|$)|^(company|organization|org)$", re.IGNORECASE)
_BOOL_HINT = re.compile(r"^(is|has|can)_|_(flag|enabled|active)$|^opt_", re.IGNORECASE)

# tag names
BLANK = "blank"
PLACEHOLDER = "placeholder"
INTEGER = "integer"
DECIMAL = "decimal"
NUMERIC_TEXT = "numeric_text"
DATE = "date"
DATETIME = "datetime"
EMAIL = "email"
EMAIL_MALFORMED = "email_malformed"
URL = "url"
URL_NO_SCHEME = "url_no_scheme"
PHONE = "phone"
PHONE_SHORT = "phone_short"
IDENTIFIER = "identifier"
CODE = "code"
TEXT = "text"


def is_null_like(value: Any) -> bool:
    """None, NaN, empty after strip, or a placeholder word."""
    if value is None:
        return True
    if isinstance(value, float) and value != value:  # nan
        return True
    text = str(value).strip()
    return text == "" or text.lower() in PLACEHOLDERS


def parse_date(text: str) -> tuple[date, str] | None:
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date(), fmt
        except ValueError:
            continue
    return None


def parse_datetime(text: str) -> tuple[datetime, str] | None:
    for fmt in DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt), fmt
        except ValueError:
            continue
    return None


def parse_number(text: str) -> float | None:
    """the numeric value behind "$7,050,000", "38.60M", "USD 60", "1,475.00", "12"."""
    t = text.strip()
    if _INT.match(t) or _DEC.match(t):
        return float(t)
    if not _NUM_TEXT.match(t):
        return None
    t = re.sub(r"^[A-Z]{3}\s", "", t)
    t = re.sub(r"^[$€£]\s?", "", t)
    mult = 1.0
    if t.endswith("%"):
        t = t[:-1]
    elif t[-1:] in "MKkBb":
        mult = {"M": 1e6, "K": 1e3, "k": 1e3, "B": 1e9, "b": 1e9}[t[-1]]
        t = t[:-1]
    try:
        return float(t.replace(",", "").strip()) * mult
    except ValueError:
        return None


def phone_digits(text: str) -> str | None:
    """the digits of something that looks like a phone number, else None."""
    t = text.strip()
    if not _PHONE_CHARS.match(t):
        return None
    digits = _NON_DIGIT.sub("", t)
    return digits if 7 <= len(digits) <= 15 else None


def tag_value(value: Any) -> str:
    """one tag per cell. order matters: the more specific shapes come first."""
    if is_null_like(value):
        return PLACEHOLDER if value is not None and str(value).strip() else BLANK
    if isinstance(value, bool):
        return TEXT
    if isinstance(value, int):
        return INTEGER
    if isinstance(value, float):
        return INTEGER if value.is_integer() else DECIMAL
    text = str(value).strip()
    if _INT.match(text):
        return INTEGER
    if _DEC.match(text):
        return DECIMAL
    if "@" in text:
        return EMAIL if _EMAIL.match(text) else EMAIL_MALFORMED
    if _URL.match(text):
        return URL
    if _IDENTIFIER.match(text):
        return IDENTIFIER
    if _NUM_TEXT.match(text):
        return NUMERIC_TEXT
    if any(ch.isdigit() for ch in text):
        if ":" in text and parse_datetime(text):
            return DATETIME
        if parse_date(text):
            return DATE
        if parse_datetime(text):
            return DATETIME
        digits = phone_digits(text)
        if digits is not None:
            return PHONE if len(digits) >= 10 else PHONE_SHORT
        if _PHONE_CHARS.match(text):
            return PHONE_SHORT
    if _CODE.match(text):
        return CODE
    if _URL_NO_SCHEME.match(text):
        return URL_NO_SCHEME
    if " at " in text.lower() and "." in text:
        return EMAIL_MALFORMED
    return TEXT


def shape(text: str, limit: int = 24) -> str:
    """'CT-000102' -> 'AA-999999', 'Jane Smith' -> 'Aaaa Aaaaa'. digits to 9, letters to A/a."""
    out = []
    for ch in text[:limit]:
        if ch.isdigit():
            out.append("9")
        elif ch.isalpha():
            out.append("A" if ch.isupper() else "a")
        else:
            out.append(ch)
    return "".join(out) + ("…" if len(text) > limit else "")


def has_phone_hint(name: str) -> bool:
    return bool(_PHONE_HINT.search(name))


def has_key_hint(name: str) -> bool:
    return bool(_KEY_HINT.search(name))


def has_name_hint(name: str) -> bool:
    return bool(_NAME_HINT.search(name))


def has_bool_hint(name: str) -> bool:
    return bool(_BOOL_HINT.search(name))

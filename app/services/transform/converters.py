"""the normalizers. one source value in, one target value out, or a reason it cannot be used.

Every converter is a pure function of (value, context). No converter reads the database, calls a
model, or knows which customer it is working for: the customer-specific part (which words mean
which) arrives as a value map in the context, loaded from reviewed configuration.

Two rules hold throughout:

  nothing is invented   a value that cannot be normalized comes back as an error naming the value,
                        and the record is reported rather than migrated with a guess. That is why
                        `lindsey.herrera at wong.com` is not quietly repaired into an email
                        address: a wrong address sends real mail to the wrong person.
  nothing is silent     a conversion that changed the meaning of a value (Q to monthly, On Hold to
                        active, a name split in two) leaves a note on the record, and the notes
                        end up in the migration report.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.transform.config import TransformationConfig, UnmappedPolicy, ValueMap

NOTHING = object()
"""what a converter returns for "this field has no value": the key is left out of the payload."""

_WHITESPACE = re.compile(r"\s+")
_DIGITS = re.compile(r"\d")
_TITLES = (
    "mr",
    "mrs",
    "ms",
    "miss",
    "dr",
    "prof",
    "sir",
    "rev",
)
_SUFFIXES = ("jr", "sr", "ii", "iii", "iv", "md", "phd", "dds", "dvm", "esq", "cpa", "rn")
_MILLIONS = re.compile(r"^(-?[\d,.]+)\s*m$", re.I)
_THOUSANDS = re.compile(r"^(-?[\d,.]+)\s*k$", re.I)
_CURRENCY_WORDS = re.compile(r"\b(usd|cad|eur|gbp|dollars?)\b", re.I)


@dataclass
class Conversion:
    """what one converter did. `value` is NOTHING when the field should be left unset."""

    value: Any
    note: str | None = None
    error: str | None = None
    error_type: str = "unconvertible_value"

    @property
    def failed(self) -> bool:
        return self.error is not None

    @property
    def empty(self) -> bool:
        return self.value is NOTHING or self.value is None


@dataclass
class ConverterContext:
    """everything a converter is allowed to know."""

    config: TransformationConfig
    target_path: str
    target_type: str
    enum_values: tuple[str, ...] | None = None
    value_map: ValueMap | None = None
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def target_field(self) -> str:
        return self.target_path.split(".", 1)[1]


Converter = Callable[[Any, ConverterContext], Conversion]
REGISTRY: dict[str, Converter] = {}


def converter(name: str) -> Callable[[Converter], Converter]:
    def register(fn: Converter) -> Converter:
        REGISTRY[name] = fn
        return fn

    return register


def _text(value: Any) -> str | None:
    """anything to the string a human would see, or None for an actual null."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def lookup_key(value: str) -> str:
    """how a raw value is matched against a value map: trimmed, collapsed, lowercased."""
    return _WHITESPACE.sub(" ", value).strip().lower()


# --- shape -------------------------------------------------------------------------------------


@converter("trim")
def trim(value: Any, ctx: ConverterContext) -> Conversion:
    """leading and trailing whitespace off, runs of whitespace inside collapsed to one space."""
    text = _text(value)
    if text is None:
        return Conversion(NOTHING)
    cleaned = _WHITESPACE.sub(" ", text).strip()
    note = "whitespace normalized" if cleaned != text else None
    return Conversion(cleaned, note=note)


@converter("blank_to_null")
def blank_to_null(value: Any, ctx: ConverterContext) -> Conversion:
    """the customer's placeholders for "nothing" become an actual absence of value."""
    text = _text(value)
    if text is None:
        return Conversion(NOTHING)
    if lookup_key(text) in ctx.config.null_placeholder_set:
        note = None if text.strip() == "" else f"placeholder {text.strip()!r} read as empty"
        return Conversion(NOTHING, note=note)
    return Conversion(value)


@converter("string")
def as_string(value: Any, ctx: ConverterContext) -> Conversion:
    text = _text(value)
    return Conversion(NOTHING if text is None or text == "" else text)


@converter("identifier")
def identifier(value: Any, ctx: ConverterContext) -> Conversion:
    """Meridian ids allow letters, digits and _.- and must start alphanumeric."""
    text = _text(value)
    if text is None or text.strip() == "":
        return Conversion(NOTHING)
    cleaned = text.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", cleaned):
        return Conversion(
            NOTHING,
            error=f"{cleaned!r} is not a usable identifier",
            error_type="invalid_identifier",
        )
    return Conversion(cleaned)


@converter("title_case")
def title_case(value: Any, ctx: ConverterContext) -> Conversion:
    text = _text(value)
    if text is None or not text.strip():
        return Conversion(NOTHING)
    cleaned = text.strip()
    if cleaned.isupper() or cleaned.islower():
        return Conversion(_titled(cleaned), note="case normalized")
    return Conversion(cleaned)


def _titled(text: str) -> str:
    """ALL CAPS and all lowercase names get cased; anything already mixed (McDonald, DeLuca) is
    left exactly as the customer typed it."""
    return " ".join(_titled_word(part) for part in text.split(" ") if part)


def _titled_word(word: str) -> str:
    if not (word.isupper() or word.islower()):
        return word
    out = word.lower()
    for separator in ("-", "'", "."):
        out = separator.join(part[:1].upper() + part[1:] for part in out.split(separator))
    return out


# --- numbers -----------------------------------------------------------------------------------


def _numeric_text(text: str) -> str:
    """strip the things people put in number columns: symbols, separators, currency words."""
    cleaned = _CURRENCY_WORDS.sub("", text).replace("$", "").replace("\u00a0", " ")
    return cleaned.replace(",", "").strip()


@converter("money")
def money(value: Any, ctx: ConverterContext) -> Conversion:
    """currency symbols, thousands separators, currency words and M/K suffixes off, two decimals.

    Rule 12: amounts in this export are monthly whatever the billing frequency, and CAD amounts
    migrate as USD as they are, so the currency column is not consulted.
    """
    text = _text(value)
    if text is None or not text.strip():
        return Conversion(NOTHING)
    cleaned = _numeric_text(text)
    multiplier = Decimal(1)
    note = None
    if match := _MILLIONS.match(cleaned):
        cleaned, multiplier, note = match.group(1), Decimal(1_000_000), "M suffix read as millions"
    elif match := _THOUSANDS.match(cleaned):
        cleaned, multiplier, note = match.group(1), Decimal(1_000), "K suffix read as thousands"
    try:
        amount = (Decimal(cleaned) * multiplier).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return Conversion(
            NOTHING, error=f"{text.strip()!r} is not an amount", error_type="unparseable_number"
        )
    if amount < 0:
        return Conversion(
            NOTHING,
            error=f"{text.strip()!r} is negative and Meridian amounts cannot be",
            error_type="out_of_range",
        )
    return Conversion(str(amount), note=note)


@converter("integer")
def integer(value: Any, ctx: ConverterContext) -> Conversion:
    """`1200`, `1200.0`, `  1200 ` and `1,200` are all 1200. `1200.7` is not an integer count."""
    text = _text(value)
    if text is None or not text.strip():
        return Conversion(NOTHING)
    cleaned = _numeric_text(text)
    try:
        number = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return Conversion(
            NOTHING, error=f"{text.strip()!r} is not a number", error_type="unparseable_number"
        )
    if number != number.to_integral_value():
        return Conversion(
            NOTHING,
            error=f"{text.strip()!r} is not a whole number",
            error_type="unparseable_number",
        )
    whole = int(number)
    if whole < 0:
        return Conversion(NOTHING, error=f"{text.strip()!r} is negative", error_type="out_of_range")
    note = "stored as text in the source" if text.strip() != str(whole) else None
    return Conversion(whole, note=note)


@converter("boolean")
def boolean(value: Any, ctx: ConverterContext) -> Conversion:
    """Y, yes, TRUE, 1, t and their opposites. nine spellings appear in this customer's export."""
    if isinstance(value, bool):
        return Conversion(value)
    text = _text(value)
    if text is None or not text.strip():
        return Conversion(NOTHING)
    key = lookup_key(text)
    if key in ("y", "yes", "true", "t", "1"):
        return Conversion(True)
    if key in ("n", "no", "false", "f", "0"):
        return Conversion(False)
    return Conversion(
        NOTHING, error=f"{text.strip()!r} is not a yes or a no", error_type="unparseable_boolean"
    )


# --- dates -------------------------------------------------------------------------------------


@converter("date")
def as_date(value: Any, ctx: ConverterContext) -> Conversion:
    text = _text(value)
    if text is None or not text.strip():
        return Conversion(NOTHING)
    cleaned = text.strip()
    for fmt in ctx.config.date_formats:
        try:
            parsed = datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
        note = None if fmt == "%Y-%m-%d" else f"date read as {fmt}"
        return Conversion(parsed.isoformat(), note=note)
    return Conversion(
        NOTHING,
        error=f"{cleaned!r} does not match any configured date format",
        error_type="unparseable_date",
    )


@converter("datetime")
def as_datetime(value: Any, ctx: ConverterContext) -> Conversion:
    text = _text(value)
    if text is None or not text.strip():
        return Conversion(NOTHING)
    cleaned = text.strip().replace("Z", "+0000")
    for fmt in ctx.config.datetime_formats:
        try:
            parsed = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        note: str | None
        if parsed.tzinfo is None:
            # a timestamp with no zone is read as UTC, and the record says so
            parsed = parsed.replace(tzinfo=UTC)
            note = f"timestamp read as {fmt}, no timezone in the source, assumed UTC"
        else:
            note = None if fmt.startswith("%Y-%m-%dT") else f"timestamp read as {fmt}"
        return Conversion(parsed.astimezone(UTC).isoformat().replace("+00:00", "Z"), note=note)
    return Conversion(
        NOTHING,
        error=f"{text.strip()!r} does not match any configured timestamp format",
        error_type="unparseable_datetime",
    )


def parse_date_value(value: Any, config: TransformationConfig) -> date | None:
    """the date parser on its own, for record rules that read a source column directly."""
    ctx = ConverterContext(config=config, target_path="internal.date", target_type="date")
    blanked = blank_to_null(value, ctx)
    if blanked.empty:
        return None
    result = as_date(blanked.value, ctx)
    if result.failed or result.empty:
        return None
    return date.fromisoformat(str(result.value))


# --- contact details ---------------------------------------------------------------------------


@converter("email")
def email(value: Any, ctx: ConverterContext) -> Conversion:
    """trimmed and lowercased. a malformed address is reported, never repaired: `x at y.com` is
    not turned into `x@y.com`, because an address the tool invents reaches a real person."""
    text = _text(value)
    if text is None or not text.strip():
        return Conversion(NOTHING)
    cleaned = text.strip().lower()
    note = "lowercased" if cleaned != text.strip() else None
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", cleaned):
        return Conversion(
            NOTHING,
            error=f"{text.strip()!r} is not an email address",
            error_type="malformed_email",
        )
    return Conversion(cleaned, note=note)


@converter("phone_e164")
def phone_e164(value: Any, ctx: ConverterContext) -> Conversion:
    """North American numbers to E.164. ten digits gain +1; eleven starting with 1 are already
    there. Fewer than ten digits cannot be completed and are reported."""
    text = _text(value)
    if text is None or not text.strip():
        return Conversion(NOTHING)
    raw = text.strip()
    digits = "".join(_DIGITS.findall(raw))
    if not digits:
        return Conversion(NOTHING, error=f"{raw!r} holds no digits", error_type="unparseable_phone")
    if raw.startswith("+") and 7 <= len(digits) <= 15:
        return Conversion(f"+{digits}")
    if len(digits) == 10:
        return Conversion(f"+1{digits}", note="assumed +1 country code")
    if len(digits) == 11 and digits.startswith("1"):
        return Conversion(f"+{digits}")
    if len(digits) < 10:
        return Conversion(
            NOTHING,
            error=f"{raw!r} has {len(digits)} digits, too few for a phone number",
            error_type="phone_too_short",
        )
    return Conversion(
        NOTHING,
        error=f"{raw!r} has {len(digits)} digits and no country code",
        error_type="unparseable_phone",
    )


@converter("url")
def url(value: Any, ctx: ConverterContext) -> Conversion:
    """`www.example.com` gains a scheme. anything without a dot is not a website."""
    text = _text(value)
    if text is None or not text.strip():
        return Conversion(NOTHING)
    cleaned = text.strip()
    note = None
    if not re.match(r"^https?://", cleaned, re.I):
        if "." not in cleaned or " " in cleaned:
            return Conversion(
                NOTHING, error=f"{cleaned!r} is not a website", error_type="unparseable_url"
            )
        cleaned, note = f"https://{cleaned}", "https:// added"
    return Conversion(cleaned, note=note)


@converter("person_name_split")
def person_name_split(value: Any, ctx: ConverterContext) -> Conversion:
    """one name column into Meridian's two. `COLLINS, JILLIAN` is last-first; `Mrs. Dana Munoz MD`
    carries a title and a suffix. A single word has no surname and is reported, because a blank
    last name is not a record Meridian accepts."""
    text = _text(value)
    if text is None or not text.strip():
        return Conversion(NOTHING)
    raw = _WHITESPACE.sub(" ", text).strip()
    notes: list[str] = []
    if "," in raw:
        last_part, _, first_part = raw.partition(",")
        last_tokens, first_tokens = _name_tokens(last_part), _name_tokens(first_part)
        notes.append("read as last-first")
    else:
        tokens = _name_tokens(raw)
        if len(tokens) < 2:
            return Conversion(
                NOTHING,
                error=f"{raw!r} has no surname",
                error_type="incomplete_name",
            )
        first_tokens, last_tokens = tokens[:1], tokens[1:]
    if not first_tokens or not last_tokens:
        return Conversion(NOTHING, error=f"{raw!r} has no surname", error_type="incomplete_name")
    first = _titled(" ".join(first_tokens))
    last = _titled(" ".join(last_tokens))
    if raw != f"{first} {last}":
        notes.append("name split in two")
    return Conversion(
        {"first_name": first[:100], "last_name": last[:100]},
        note="; ".join(notes) if notes else None,
    )


def _name_tokens(part: str) -> list[str]:
    """name words with titles and professional suffixes removed."""
    out: list[str] = []
    for token in part.split(" "):
        word = token.strip().strip(".,")
        if not word:
            continue
        if word.lower() in _TITLES or word.lower() in _SUFFIXES:
            continue
        out.append(word)
    return out


# --- controlled vocabularies -------------------------------------------------------------------


@converter("enum_map")
def enum_map(value: Any, ctx: ConverterContext) -> Conversion:
    """the customer's word to Meridian's, from the value map in configuration. a word that is not
    in the map is never guessed at: the policy on the map decides whether the record is rejected,
    left to Meridian's default, or left empty."""
    text = _text(value)
    if text is None or not text.strip():
        return Conversion(NOTHING)
    raw = text.strip()
    key = lookup_key(raw)
    vmap = ctx.value_map
    if vmap is not None and key in vmap.map:
        mapped = vmap.map[key]
        note = None
        if key != str(mapped).lower():
            note = f"{raw!r} mapped to {mapped!r}"
            if vmap.rule:
                note += f" (customer rule {vmap.rule})"
        return Conversion(mapped, note=note)
    if ctx.enum_values and key in {v.lower() for v in ctx.enum_values}:
        exact = next(v for v in ctx.enum_values if v.lower() == key)
        return Conversion(exact, note=None if exact == raw else "case normalized")
    if vmap is None and ctx.enum_values is None:
        # not a controlled vocabulary at all: leave the value for the next converter
        return Conversion(value)
    policy = vmap.unmapped if vmap else UnmappedPolicy.error
    if policy is UnmappedPolicy.error:
        return Conversion(
            NOTHING,
            error=f"{raw!r} is not a known value for {ctx.target_path}",
            error_type="unmapped_value",
        )
    note = f"{raw!r} is not in the {ctx.target_path} map"
    if policy is UnmappedPolicy.default:
        return Conversion(NOTHING, note=f"{note}, left to Meridian's default")
    return Conversion(NOTHING, note=f"{note}, left empty")


@converter("country_code")
def country_code(value: Any, ctx: ConverterContext) -> Conversion:
    """the customer's country words to ISO alpha-2. The map in configuration decides first; a
    value that is already a two-letter code passes; anything else is reported, not guessed."""
    text = _text(value)
    if text is None or not text.strip():
        return Conversion(NOTHING)
    raw = text.strip()
    mapped = ctx.value_map.map.get(lookup_key(raw)) if ctx.value_map else None
    if mapped:
        note = None if mapped == raw else f"{raw!r} mapped to {mapped!r}"
        return Conversion(mapped, note=note)
    if re.fullmatch(r"[A-Za-z]{2}", raw):
        upper = raw.upper()
        return Conversion(upper, note=None if upper == raw else "uppercased")
    return Conversion(
        NOTHING,
        error=f"{raw!r} is not an ISO country code and is not in the country map",
        error_type="unmapped_value",
    )


@converter("region_code")
def region_code(value: Any, ctx: ConverterContext) -> Conversion:
    """state and province names to codes. A name the map does not cover is dropped with a note
    rather than rejected: Meridian treats the region as optional detail, not as grounds to refuse
    an account."""
    text = _text(value)
    if text is None or not text.strip():
        return Conversion(NOTHING)
    raw = text.strip()
    mapped = ctx.value_map.map.get(lookup_key(raw)) if ctx.value_map else None
    if mapped:
        return Conversion(mapped, note=f"{raw!r} mapped to {mapped!r}")
    if re.fullmatch(r"[A-Za-z]{2}", raw):
        upper = raw.upper()
        return Conversion(upper, note=None if upper == raw else "uppercased")
    return Conversion(NOTHING, note=f"{raw!r} is not a known state or province, dropped")


@converter("string_list")
def string_list(value: Any, ctx: ConverterContext) -> Conversion:
    """a delimited cell into Meridian's list of tags, deduplicated and capped."""
    text = _text(value)
    if text is None or not text.strip():
        return Conversion(NOTHING)
    parts = [p.strip() for p in re.split(r"[;,|]", text) if p.strip()]
    seen: list[str] = []
    for part in parts:
        if part.lower() not in {s.lower() for s in seen}:
            seen.append(part[:50])
    if not seen:
        return Conversion(NOTHING)
    capped = seen[:10]
    note = f"{len(seen)} values found, first 10 kept" if len(seen) > 10 else None
    return Conversion(capped, note=note)


def apply_chain(
    value: Any, converters: tuple[str, ...] | list[str], ctx: ConverterContext
) -> Conversion:
    """run a chain left to right. an empty value or an error stops the chain."""
    notes: list[str] = []
    current = value
    for name in converters:
        fn = REGISTRY.get(name)
        if fn is None:
            raise KeyError(f"unknown converter {name!r}")
        result = fn(current, ctx)
        if result.note:
            notes.append(result.note)
        if result.failed:
            return Conversion(
                NOTHING,
                note="; ".join(notes) or None,
                error=result.error,
                error_type=result.error_type,
            )
        if result.value is NOTHING:
            return Conversion(NOTHING, note="; ".join(notes) or None)
        current = result.value
    return Conversion(current, note="; ".join(notes) or None)


__all__ = [
    "NOTHING",
    "Conversion",
    "ConverterContext",
    "REGISTRY",
    "apply_chain",
    "lookup_key",
    "parse_date_value",
]

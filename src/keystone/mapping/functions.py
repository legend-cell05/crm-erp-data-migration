"""The transformation registry.

Every function a mapping file may name lives here, and nowhere else. A mapping
cannot call arbitrary Python: it names a function from this registry and passes
declared arguments. That is the whole security model of the declarative layer,
and it is why these YAML files can be reviewed by someone who does not read
Python and edited by someone who must not be able to execute it.

Each function takes the incoming value first and returns the transformed one.
Raising ``ValueError`` means "this value is not acceptable"; the engine turns
that into a rejection naming the field and the function, so the report says
`close_dt / parse_date` rather than "error".
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

TransformFn = Callable[..., Any]

_REGISTRY: dict[str, TransformFn] = {}


def transform(name: str) -> Callable[[TransformFn], TransformFn]:
    """Register a transformation under the name mappings will use."""

    def decorator(fn: TransformFn) -> TransformFn:
        if name in _REGISTRY:
            raise RuntimeError(f"transform {name!r} is already registered")
        _REGISTRY[name] = fn
        return fn

    return decorator


def get_transform(name: str) -> TransformFn:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"unknown transform {name!r}; available: {', '.join(sorted(_REGISTRY))}"
        ) from None


def registered_transforms() -> list[str]:
    return sorted(_REGISTRY)


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------


@transform("trim")
def _trim(value: Any) -> Any:
    """Strip surrounding whitespace, and turn an empty result into None.

    The empty string and NULL mean the same thing in a CRM that had no NOT
    NULL constraints, and keeping both alive doubles every downstream check.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@transform("collapse_spaces")
def _collapse_spaces(value: Any) -> Any:
    if value is None:
        return None
    return re.sub(r"\s+", " ", str(value)).strip() or None


@transform("upper")
def _upper(value: Any) -> Any:
    return None if value is None else str(value).upper()


@transform("lower")
def _lower(value: Any) -> Any:
    return None if value is None else str(value).lower()


@transform("title_case")
def _title_case(value: Any) -> Any:
    """Title-case words, leaving known acronyms alone.

    'SARL DUPONT' becomes 'Dupont SARL', not 'Sarl Dupont'. The exception list
    is short on purpose: guessing which words are acronyms is how a migration
    renames half its customers.
    """
    if value is None:
        return None
    keep = {"SA", "SAS", "SARL", "SASU", "SCI", "GIE", "BTP", "IT", "RH", "SNC", "EURL"}
    words = str(value).split()
    out = [word.upper() if word.upper() in keep else word.capitalize() for word in words]
    return " ".join(out) or None


@transform("fix_mojibake")
def _fix_mojibake(value: Any) -> Any:
    """Undo a Latin-1 export that was read as UTF-8.

    'SociÃ©tÃ©' becomes 'Société'. The round-trip is attempted and kept only
    when it succeeds cleanly; a value that is already correct is returned
    untouched, because re-encoding correct text is how accents get destroyed a
    second time.
    """
    if value is None:
        return None
    text = str(value)
    if not any(marker in text for marker in ("Ã", "Â", "â€")):
        return text
    try:
        repaired = text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    return repaired


@transform("strip_accents")
def _strip_accents(value: Any) -> Any:
    if value is None:
        return None
    decomposed = unicodedata.normalize("NFKD", str(value))
    return "".join(char for char in decomposed if not unicodedata.combining(char))


@transform("truncate")
def _truncate(value: Any, *, length: int, on_overflow: str = "truncate") -> Any:
    """Cut a value to the target's column width.

    ``on_overflow: reject`` is available and is the right choice for anything
    a human will read back, such as a name: silently losing the last eight
    characters of a company name is worse than refusing the record.
    """
    if value is None:
        return None
    text = str(value)
    if len(text) <= length:
        return text
    if on_overflow == "reject":
        raise ValueError(f"value is {len(text)} characters, target allows {length}")
    return text[:length]


@transform("default")
def _default(value: Any, *, value_if_missing: Any) -> Any:
    return value_if_missing if value is None or value == "" else value


@transform("constant")
def _constant(value: Any, *, value_to_use: Any) -> Any:
    return value_to_use


@transform("concat")
def _concat(value: Any, *, prefix: str = "", suffix: str = "") -> Any:
    if value is None:
        return None
    return f"{prefix}{value}{suffix}"


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

_DATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%y",
    "%d.%m.%Y",
    "%Y%m%d",
    "%Y-%m-%dT%H:%M:%S",
    "%d/%m/%Y %H:%M",
)


@transform("parse_date")
def _parse_date(
    value: Any,
    *,
    formats: list[str] | None = None,
    day_first: bool = True,
    output: str = "date",
) -> Any:
    """Parse a date written in any of the formats the legacy system allowed.

    ``day_first`` is not a convenience, it is a decision that has to be made
    and recorded: '03/04/2021' is the 3rd of April in the source system and
    the 4th of March to a US reader, and nothing in the data says which. The
    mapping states the answer, a reviewer can see it, and the alternative --
    letting a parser guess per value -- produces a dataset where some rows are
    right and nobody knows which.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"n/a", "na", "-", "00/00/0000", "0"}:
        return None

    candidates = list(formats) if formats else list(_DATE_FORMATS)
    if not day_first:
        candidates = [fmt.replace("%d/%m", "%m/%d") for fmt in candidates]

    for fmt in candidates:
        try:
            parsed = dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
        if output == "datetime":
            return parsed.replace(tzinfo=dt.UTC).isoformat()
        return parsed.date().isoformat()
    raise ValueError(f"no known date format matches {text!r}")


# ---------------------------------------------------------------------------
# Numbers and money
# ---------------------------------------------------------------------------

_CURRENCY_CHARS = str.maketrans("", "", "€$£  ")  # noqa: RUF001 - the NBSP is deliberate: it is what French number
# formatting uses as a thousands separator, and it is exactly the character that
# makes "12 500,00" fail to parse when only the ASCII space is stripped.


@transform("parse_amount")
def _parse_amount(value: Any, *, decimals: int = 2) -> Any:
    """Parse an amount typed by a human into a Decimal.

    Handles '12 500,00', '12,500.00', '€12 500,00', '$ 12,500.00' and '12500'.
    The awkward case is the ambiguity between the thousands separator and the
    decimal mark: the rule applied here is that the LAST separator wins, which
    is right for every sample in this dataset and is stated in the mapping
    documentation rather than buried here.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Currency codes go first, while the spaces that delimit them still
    # exist: stripping spaces first turns '12 500,00 EUR' into
    # '12500,00EUR', where \b no longer matches and the code survives into
    # Decimal().
    text = re.sub(r"(?i)(eur|usd|chf|gbp)", "", text).strip()
    text = text.translate(_CURRENCY_CHARS)
    if not text or not re.search(r"\d", text):
        raise ValueError(f"no number in {value!r}")

    negative = text.startswith("-") or (text.startswith("(") and text.endswith(")"))
    text = text.strip("-()")

    last_comma = text.rfind(",")
    last_dot = text.rfind(".")
    if last_comma > last_dot:
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", "")

    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"cannot parse amount {value!r}") from None
    if negative:
        amount = -amount
    return str(round(amount, decimals))


@transform("parse_percentage")
def _parse_percentage(value: Any) -> Any:
    """Turn '70', '70%' and '0.70' into the integer 70.

    The third form is the trap: a bare 0.7 could be 0.7% or 70%, and in this
    source it is always the latter. The rule -- a value at or below 1 with a
    decimal point is a fraction -- is applied once, here, instead of being
    re-invented by every report.
    """
    if value is None:
        return None
    text = str(value).strip().rstrip("%").replace(",", ".").strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        raise ValueError(f"cannot parse percentage {value!r}") from None
    if 0 < number <= 1 and "." in text:
        number *= 100
    if not 0 <= number <= 100:
        raise ValueError(f"percentage {number} outside 0-100")
    return round(number)


@transform("parse_int")
def _parse_int(value: Any) -> Any:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text.replace(",", ".")))
    except ValueError:
        raise ValueError(f"cannot parse integer {value!r}") from None


# ---------------------------------------------------------------------------
# Contact details
# ---------------------------------------------------------------------------

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


@transform("normalise_email")
def _normalise_email(value: Any, *, on_invalid: str = "reject") -> Any:
    """Lower-case, trim, keep the first address, and validate the shape.

    ``on_invalid: null`` is offered because an unusable email is often not a
    reason to lose an otherwise good contact -- but the choice is the
    mapping's to make and to record, not this function's to assume.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text or text in {"n/a", "na", "-", "none"}:
        return None
    text = re.split(r"[;,]", text)[0].strip()
    if not _EMAIL.match(text):
        if on_invalid == "null":
            return None
        raise ValueError(f"{value!r} is not a usable email address")
    return text


@transform("normalise_phone")
def _normalise_phone(
    value: Any, *, default_country_code: str = "33", on_invalid: str = "null"
) -> Any:
    """Best-effort E.164.

    Deliberately not a full phone-number library: the goal is a consistent
    shape for the 95% that are ordinary French numbers, and a recorded failure
    for the rest, rather than a dependency that would still need the same
    decisions made about extensions and missing country codes.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r"(?i)\b(poste|ext\.?|extension)\b.*$", "", text).strip()
    # '+33 (0)2 19 ...' carries both the country code and the national trunk
    # prefix. Keeping the 0 produces a number one digit too long that looks
    # plausible -- the worst kind of wrong, because it dials.
    text = re.sub(r"\(\s*0\s*\)", "", text)
    digits = re.sub(r"[^\d+]", "", text)
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if digits.startswith("+"):
        body = re.sub(r"\D", "", digits[1:])
        # A trunk prefix that survived the parentheses: +33 0X XX ...
        if body.startswith(f"{default_country_code}0"):
            body = default_country_code + body[len(default_country_code) + 1 :]
        cleaned = f"+{body}"
    elif digits.startswith("0"):
        cleaned = f"+{default_country_code}{digits[1:]}"
    else:
        cleaned = f"+{default_country_code}{digits}" if digits else ""

    body = cleaned.lstrip("+")
    if not 8 <= len(body) <= 15:
        if on_invalid == "reject":
            raise ValueError(f"{value!r} is not a usable phone number")
        return None
    return cleaned


@transform("map_boolean")
def _map_boolean(
    value: Any,
    *,
    true_values: list[str] | None = None,
    false_values: list[str] | None = None,
    default: bool | None = None,
) -> Any:
    """Y/N/O/1/0/true/false to a real boolean, with a declared default.

    The default is how the mapping answers "what does NULL mean here?", which
    is a business question -- for an opt-out flag, guessing wrong is a GDPR
    problem, not a data-quality one.
    """
    if value is None or str(value).strip() == "":
        return default
    text = str(value).strip().upper()
    truthy = {v.upper() for v in (true_values or ["Y", "YES", "O", "OUI", "1", "TRUE", "T"])}
    falsy = {v.upper() for v in (false_values or ["N", "NO", "NON", "0", "FALSE", "F"])}
    if text in truthy:
        return True
    if text in falsy:
        return False
    raise ValueError(f"{value!r} is neither true nor false")


@transform("postal_code")
def _postal_code(value: Any, *, pad_to: int = 5) -> Any:
    """Restore the leading zero a spreadsheet ate."""
    if value is None:
        return None
    text = re.sub(r"\s+", "", str(value)).upper()
    if not text:
        return None
    if text.isdigit() and len(text) < pad_to:
        return text.zfill(pad_to)
    return text

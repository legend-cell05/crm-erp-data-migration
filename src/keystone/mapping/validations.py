"""Record-level validation rules.

Field transformations answer "can this value be converted?". Validations
answer "does this record make sense?" -- a question that needs more than one
field and therefore cannot live in a transformation.

Like transformations, rules are a closed registry: a mapping names a rule and
passes declared arguments. Raising ``ValueError`` rejects the record (or warns,
depending on the rule's declared severity) with a message that names the rule.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

ValidationFn = Callable[..., None]

_RULES: dict[str, ValidationFn] = {}


def rule(name: str) -> Callable[[ValidationFn], ValidationFn]:
    def decorator(fn: ValidationFn) -> ValidationFn:
        if name in _RULES:
            raise RuntimeError(f"validation rule {name!r} is already registered")
        _RULES[name] = fn
        return fn

    return decorator


def get_rule(name: str) -> ValidationFn:
    try:
        return _RULES[name]
    except KeyError:
        raise KeyError(
            f"unknown validation rule {name!r}; available: {', '.join(sorted(_RULES))}"
        ) from None


def registered_rules() -> list[str]:
    return sorted(_RULES)


def _present(payload: Mapping[str, Any], field: str) -> bool:
    value = payload.get(field)
    return value is not None and str(value).strip() != ""


@rule("at_least_one")
def _at_least_one(payload: Mapping[str, Any], fields: list[str], **_: Any) -> None:
    """A contact with neither an email nor a phone cannot be contacted.

    Migrating it is not an error, but it is a decision -- and one the business
    should take knowingly rather than discover in the target.
    """
    if not any(_present(payload, field) for field in fields):
        raise ValueError(f"none of {', '.join(fields)} is present")


@rule("required_together")
def _required_together(payload: Mapping[str, Any], fields: list[str], **_: Any) -> None:
    present = [field for field in fields if _present(payload, field)]
    if present and len(present) != len(fields):
        missing = sorted(set(fields) - set(present))
        raise ValueError(f"{', '.join(present)} present but {', '.join(missing)} missing")


@rule("matches")
def _matches(payload: Mapping[str, Any], fields: list[str], *, pattern: str, **_: Any) -> None:
    compiled = re.compile(pattern)
    for field in fields:
        value = payload.get(field)
        if value is not None and not compiled.match(str(value)):
            raise ValueError(f"{field}={value!r} does not match {pattern}")


@rule("in_set")
def _in_set(payload: Mapping[str, Any], fields: list[str], *, values: list[Any], **_: Any) -> None:
    allowed = set(values)
    for field in fields:
        value = payload.get(field)
        if value is not None and value not in allowed:
            raise ValueError(f"{field}={value!r} is not one of {sorted(map(str, allowed))}")


@rule("max_length")
def _max_length(payload: Mapping[str, Any], fields: list[str], *, length: int, **_: Any) -> None:
    for field in fields:
        value = payload.get(field)
        if value is not None and len(str(value)) > length:
            raise ValueError(f"{field} is {len(str(value))} characters, maximum is {length}")


@rule("not_before")
def _not_before(payload: Mapping[str, Any], fields: list[str], *, earliest: str, **_: Any) -> None:
    """Catches the 1900-01-01 and 0001-01-01 that stand in for 'unknown'."""
    bound = dt.date.fromisoformat(earliest)
    for field in fields:
        value = payload.get(field)
        if value is None:
            continue
        parsed = dt.date.fromisoformat(str(value)[:10])
        if parsed < bound:
            raise ValueError(f"{field}={value} is before {earliest}")


@rule("not_after_today")
def _not_after_today(
    payload: Mapping[str, Any], fields: list[str], *, tolerance_days: int = 0, **_: Any
) -> None:
    limit = dt.date.today() + dt.timedelta(days=tolerance_days)
    for field in fields:
        value = payload.get(field)
        if value is None:
            continue
        parsed = dt.date.fromisoformat(str(value)[:10])
        if parsed > limit:
            raise ValueError(f"{field}={value} is in the future")


@rule("ordered_dates")
def _ordered_dates(payload: Mapping[str, Any], fields: list[str], **_: Any) -> None:
    """``fields`` in chronological order; missing values are skipped."""
    previous_name: str | None = None
    previous: dt.date | None = None
    for field in fields:
        value = payload.get(field)
        if value is None:
            continue
        parsed = dt.date.fromisoformat(str(value)[:10])
        if previous is not None and parsed < previous:
            raise ValueError(f"{field}={value} precedes {previous_name}={previous.isoformat()}")
        previous, previous_name = parsed, field


@rule("non_negative")
def _non_negative(payload: Mapping[str, Any], fields: list[str], **_: Any) -> None:
    for field in fields:
        value = payload.get(field)
        if value is None:
            continue
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            raise ValueError(f"{field}={value!r} is not a number") from None
        if number < 0:
            raise ValueError(f"{field}={value} is negative")


@rule("consistent_stage_probability")
def _consistent_stage_probability(
    payload: Mapping[str, Any],
    fields: list[str],
    *,
    stage_field: str = "stage",
    probability_field: str = "probability",
    closed_stages: list[str] | None = None,
    **_: Any,
) -> None:
    """A won deal at 10% is a data-entry error, not an opinion.

    This is the sort of rule that only exists because someone looked at the
    data: the source has 41 closed-won opportunities with a probability below
    100, and they all date from before the sales team was told the field
    mattered.
    """
    closed = set(closed_stages or ["closed_won"])
    stage = payload.get(stage_field)
    probability = payload.get(probability_field)
    if stage in closed and probability is not None and int(probability) != 100:
        raise ValueError(f"{stage_field}={stage} but {probability_field}={probability}")

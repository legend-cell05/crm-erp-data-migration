"""Applying a mapping to a source record.

One record in, one mapped record out -- or one rejection, naming the field and
the rule that refused it. Nothing here knows where the record came from or
where it is going, which is what makes the whole transformation layer testable
without a database or an API.

The content hash deserves a word. It is computed over the *mapped* payload,
not the source row, and that is the difference between a re-run that rewrites
everything and one that touches only what changed: two source rows that differ
in whitespace, in date format, or in the spelling of a country produce the
same target payload, the same hash, and therefore no write at all.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from keystone.exceptions import (
    MappingError,
    MappingViolation,
    ReferenceNotResolved,
    ValidationViolation,
)
from keystone.mapping.functions import get_transform
from keystone.mapping.lookups import MISSING, get_lookup
from keystone.mapping.models import EntityMapping, FieldSpec
from keystone.mapping.validations import get_rule


class ReferenceResolver(Protocol):
    """Turns a legacy id into the target id it became."""

    def resolve(self, entity: str, source_id: str) -> str | None: ...


@dataclass(frozen=True)
class MappedRecord:
    """A record ready to be sent to the target."""

    entity: str
    source_id: str
    alternate_key: str
    payload: dict[str, Any]
    content_hash: str
    warnings: list[str] = field(default_factory=list)


def content_hash(payload: Mapping[str, Any]) -> str:
    """SHA-256 over a canonical rendering of the payload.

    ``sort_keys`` because dictionary order is not part of the record's
    meaning; ``default=str`` because a Decimal is not JSON-serialisable and
    its string form is exactly what the target will receive.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class MappingEngine:
    """Applies one entity's mapping."""

    def __init__(
        self,
        mapping: EntityMapping,
        *,
        lookup_dir: Path,
        resolver: ReferenceResolver | None = None,
    ) -> None:
        self.mapping = mapping
        self.lookup_dir = lookup_dir
        self.resolver = resolver
        self._validate_against_registries()

    def _validate_against_registries(self) -> None:
        """Fail at load time, not on record 40 000.

        Every transform, rule and lookup a mapping names is resolved once,
        here. A mapping that mentions a function nobody wrote is a broken
        mapping, and finding that out after twenty minutes of migration is an
        avoidable way to find it out.
        """
        for field_spec in self.mapping.fields:
            for step in field_spec.transform:
                try:
                    get_transform(step.fn)
                except KeyError as exc:
                    raise MappingError(str(exc), path=self.mapping.entity) from exc
            if field_spec.lookup is not None:
                get_lookup(field_spec.lookup.table, self.lookup_dir)
            if field_spec.reference is not None and self.resolver is None:
                raise MappingError(
                    f"field {field_spec.target!r} declares a reference to "
                    f"{field_spec.reference.entity!r} but no resolver was provided",
                    path=self.mapping.entity,
                )
        for validation in self.mapping.validations:
            try:
                get_rule(validation.rule)
            except KeyError as exc:
                raise MappingError(str(exc), path=self.mapping.entity) from exc

    # -- Field-level --------------------------------------------------------

    def _apply_transforms(self, field_spec: FieldSpec, value: Any, source_id: str) -> Any:
        for step in field_spec.transform:
            fn = get_transform(step.fn)
            try:
                value = fn(value, **step.args)
            except ValueError as exc:
                raise MappingViolation(
                    str(exc),
                    entity=self.mapping.entity,
                    source_id=source_id,
                    field=field_spec.target,
                    rule=step.fn,
                ) from exc
            except TypeError as exc:  # wrong arguments in the mapping file
                raise MappingError(
                    f"transform {step.fn!r} on field {field_spec.target!r}: {exc}",
                    path=self.mapping.entity,
                ) from exc
        return value

    def _apply_lookup(self, field_spec: FieldSpec, value: Any, source_id: str) -> Any:
        spec = field_spec.lookup
        if spec is None or value is None:
            return value
        table = get_lookup(spec.table, self.lookup_dir)
        result = table.get(value)
        if result is not MISSING:
            return result
        if spec.on_miss == "null":
            return None
        if spec.on_miss == "passthrough":
            return value
        if spec.on_miss == "default":
            return spec.default
        raise MappingViolation(
            f"{value!r} has no entry in lookup {spec.table!r}",
            entity=self.mapping.entity,
            source_id=source_id,
            field=field_spec.target,
            rule=f"lookup:{spec.table}",
        )

    def _apply_reference(self, field_spec: FieldSpec, value: Any, source_id: str) -> Any:
        spec = field_spec.reference
        if spec is None:
            return value
        if value is None:
            if spec.on_missing == "null":
                return None
            raise ReferenceNotResolved(
                f"no {spec.entity} referenced",
                entity=self.mapping.entity,
                source_id=source_id,
                field=field_spec.target,
                rule=f"reference:{spec.entity}",
            )
        assert self.resolver is not None  # guaranteed by _validate_against_registries
        target_id = self.resolver.resolve(spec.entity, str(value))
        if target_id is not None:
            return target_id
        if spec.on_missing == "null":
            return None
        raise ReferenceNotResolved(
            f"{spec.entity} {value!r} has not been migrated",
            entity=self.mapping.entity,
            source_id=source_id,
            field=field_spec.target,
            rule=f"reference:{spec.entity}",
        )

    # -- Record-level -------------------------------------------------------

    def apply(self, row: Mapping[str, Any]) -> MappedRecord:
        """Map one source row. Raises a ``RecordRejected`` subclass on refusal."""
        key_column = self.mapping.source.key
        if key_column not in row:
            raise MappingError(
                f"source row has no key column {key_column!r}", path=self.mapping.entity
            )
        source_id = str(row[key_column]).strip()

        payload: dict[str, Any] = {}
        warnings: list[str] = []

        for field_spec in self.mapping.fields:
            raw = row.get(field_spec.source) if field_spec.source else None
            value = self._apply_transforms(field_spec, raw, source_id)
            value = self._apply_lookup(field_spec, value, source_id)
            value = self._apply_reference(field_spec, value, source_id)

            if field_spec.required and (value is None or str(value).strip() == ""):
                raise ValidationViolation(
                    f"required field {field_spec.target!r} is empty",
                    entity=self.mapping.entity,
                    source_id=source_id,
                    field=field_spec.target,
                    rule="required",
                )
            payload[field_spec.target] = value

        for validation in self.mapping.validations:
            fn = get_rule(validation.rule)
            try:
                fn(payload, validation.fields, **validation.args)
            except ValueError as exc:
                message = validation.message or str(exc)
                if validation.severity == "warn":
                    warnings.append(f"{validation.rule}: {message}")
                    continue
                raise ValidationViolation(
                    message,
                    entity=self.mapping.entity,
                    source_id=source_id,
                    field=validation.fields[0] if validation.fields else None,
                    rule=validation.rule,
                ) from exc

        alternate_key = payload.get(self.mapping.target.alternate_key)
        if alternate_key is None:
            raise ValidationViolation(
                f"alternate key {self.mapping.target.alternate_key!r} is empty",
                entity=self.mapping.entity,
                source_id=source_id,
                field=self.mapping.target.alternate_key,
                rule="alternate_key",
            )

        return MappedRecord(
            entity=self.mapping.entity,
            source_id=source_id,
            alternate_key=str(alternate_key),
            payload=payload,
            content_hash=content_hash(payload),
            warnings=warnings,
        )

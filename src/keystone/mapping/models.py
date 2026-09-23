"""The schema of a mapping file.

A mapping is data, so it needs a schema, and the schema is enforced here with
Pydantic. Everything a mapping file can say is described by these models; a
file that says anything else fails to load, with the offending key named,
rather than being silently ignored.

That strictness is deliberate. The failure mode of a permissive loader is a
typo -- ``requierd: true`` -- that produces a migration which quietly drops a
constraint and is discovered months later in the target.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# A source filter is raw SQL from a mapping file. Mapping files are repository
# content, reviewed like code -- but "reviewed like code" is a process control,
# so there is a technical one as well: no statement terminators, no comment
# markers, no set-returning trickery.
_FORBIDDEN_IN_FILTER = re.compile(
    r"(;|--|/\*|\*/|\bUNION\b|\bINSERT\b|\bUPDATE\b|\bDELETE\b|\bDROP\b)", re.I
)
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TransformStep(Strict):
    """One call into the transform registry."""

    fn: str
    args: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _accept_bare_name(cls, value: Any) -> Any:
        """Allow both ``- trim`` and ``- {fn: truncate, length: 160}``.

        The short form is what ninety per cent of steps need, and forcing the
        long form everywhere makes a mapping file unreadable, which defeats
        the point of having one.
        """
        if isinstance(value, str):
            return {"fn": value, "args": {}}
        if isinstance(value, dict) and "fn" in value:
            args = {k: v for k, v in value.items() if k != "fn"}
            if "args" in value and isinstance(value["args"], dict):
                args = value["args"]
            return {"fn": value["fn"], "args": args}
        return value


class LookupSpec(Strict):
    """A value translation driven by a CSV table."""

    table: str
    on_miss: Literal["reject", "null", "passthrough", "default"] = "reject"
    default: Any = None

    @model_validator(mode="after")
    def _default_requires_value(self) -> LookupSpec:
        if self.on_miss == "default" and self.default is None:
            raise ValueError("on_miss: default requires a `default` value")
        return self


class ReferenceSpec(Strict):
    """A link to another entity, resolved through the crosswalk."""

    entity: str
    on_missing: Literal["reject", "null"] = "reject"


class FieldSpec(Strict):
    """One target field and how to produce it."""

    target: str
    source: str | None = None
    transform: list[TransformStep] = Field(default_factory=list)
    lookup: LookupSpec | None = None
    reference: ReferenceSpec | None = None
    required: bool = False
    description: str | None = None

    @field_validator("target")
    @classmethod
    def _valid_target(cls, value: str) -> str:
        if not _IDENTIFIER.match(value):
            raise ValueError(f"target field {value!r} must be a plain identifier")
        return value

    @model_validator(mode="after")
    def _needs_a_source(self) -> FieldSpec:
        has_constant = any(step.fn == "constant" for step in self.transform)
        if self.source is None and not has_constant:
            raise ValueError(f"field {self.target!r} has no `source` and no `constant` transform")
        return self


class ValidationSpec(Strict):
    """A record-level rule, applied after all fields are mapped."""

    rule: str
    fields: list[str] = Field(default_factory=list)
    args: dict[str, Any] = Field(default_factory=dict)
    severity: Literal["reject", "warn"] = "reject"
    message: str | None = None


class SourceSpec(Strict):
    """Where the records come from."""

    kind: Literal["sql", "csv"]
    key: str
    # sql
    schema_name: str | None = Field(default=None, alias="schema")
    table: str | None = None
    filter: str | None = None
    # csv
    path_glob: str | None = None
    encoding: str = "utf-8"
    delimiter: str = ","
    # Both kinds: rows to drop before mapping. A SQL source could express this
    # in `filter`, but a CSV source has nowhere else to say "the export
    # includes soft-deleted rows", and that exclusion is a business decision
    # that belongs in the mapping either way.
    exclude_when: dict[str, str] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @field_validator("filter")
    @classmethod
    def _safe_filter(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if _FORBIDDEN_IN_FILTER.search(value):
            raise ValueError(
                "source filter may not contain statement terminators, comment "
                "markers or data-modifying keywords"
            )
        return value

    @field_validator("table", "key")
    @classmethod
    def _plain_identifier(cls, value: str | None) -> str | None:
        if value is not None and not _IDENTIFIER.match(value):
            raise ValueError(f"{value!r} must be a plain identifier")
        return value

    @model_validator(mode="after")
    def _coherent(self) -> SourceSpec:
        if self.kind == "sql" and not self.table:
            raise ValueError("a sql source needs a `table`")
        if self.kind == "csv" and not self.path_glob:
            raise ValueError("a csv source needs a `path_glob`")
        return self


class TargetSpec(Strict):
    """Where the records go."""

    entity_set: str
    alternate_key: str
    description: str | None = None


class EntityMapping(Strict):
    """One YAML file: everything about migrating one entity."""

    version: str
    entity: str
    description: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    source: SourceSpec
    target: TargetSpec
    fields: list[FieldSpec]
    validations: list[ValidationSpec] = Field(default_factory=list)

    @field_validator("version")
    @classmethod
    def _semver(cls, value: str) -> str:
        if not re.match(r"^\d+\.\d+\.\d+$", value):
            raise ValueError(f"version {value!r} must look like 1.2.3")
        return value

    @model_validator(mode="after")
    def _unique_targets(self) -> EntityMapping:
        seen: set[str] = set()
        for field_spec in self.fields:
            if field_spec.target in seen:
                raise ValueError(f"target field {field_spec.target!r} is mapped twice")
            seen.add(field_spec.target)
        if self.target.alternate_key not in seen:
            raise ValueError(
                f"alternate key {self.target.alternate_key!r} is not among the mapped fields"
            )
        return self

    @property
    def target_fields(self) -> list[str]:
        return [f.target for f in self.fields]

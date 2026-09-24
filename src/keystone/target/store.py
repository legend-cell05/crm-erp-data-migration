"""In-memory store behind the simulated Atlas Cloud.

It stands in for the target CRM's own database. What matters is not the
storage -- a dictionary -- but that it enforces the things a real cloud CRM
enforces and that a migration has to cope with:

* **upsert on an alternate key**, so the same record sent twice is one record;
* **referential integrity**, so a contact naming an unknown account is
  refused rather than orphaned;
* **per-record results inside a batch**, so one bad row does not fail the
  other 199 -- and the client has to read a result array rather than a status
  code.

Thread-safe because uvicorn serves concurrently and a migration that ran
single-threaded during testing will not stay that way.
"""

from __future__ import annotations

import datetime as dt
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

from keystone.target.schemas import SCHEMAS, EntitySchema


@dataclass
class RecordResult:
    """What happened to one record inside a batch."""

    index: int
    alternate_key: str
    status: str  # created | updated | unchanged | failed
    id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    field: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "index": self.index,
            "alternate_key": self.alternate_key,
            "status": self.status,
        }
        if self.id is not None:
            payload["id"] = self.id
        if self.error_code is not None:
            payload["error"] = {
                "code": self.error_code,
                "message": self.error_message,
                "field": self.field,
            }
        return payload


@dataclass
class _Entity:
    records: dict[str, dict[str, Any]] = field(default_factory=dict)  # id -> record
    by_alternate_key: dict[str, str] = field(default_factory=dict)  # key -> id


class AtlasStore:
    """The target's data, and the rules it applies to it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entities: dict[str, _Entity] = {name: _Entity() for name in SCHEMAS}
        self._writes = 0

    # -- Validation ---------------------------------------------------------

    def _validate(
        self, schema: EntitySchema, payload: dict[str, Any]
    ) -> tuple[str, str, str] | None:
        """Return (code, message, field) when the record is unacceptable."""
        unknown = schema.unknown_fields(payload)
        if unknown:
            return (
                "UNKNOWN_FIELD",
                f"field {unknown[0]!r} does not exist on {schema.name}",
                unknown[0],
            )

        for name, rule in schema.fields.items():
            value = payload.get(name)
            if rule.required and (value is None or str(value).strip() == ""):
                return ("REQUIRED_FIELD", f"{name} is required", name)
            if value is None:
                continue
            if rule.max_length is not None and len(str(value)) > rule.max_length:
                return (
                    "VALUE_TOO_LONG",
                    f"{name} is {len(str(value))} characters, maximum is {rule.max_length}",
                    name,
                )
            if rule.allowed is not None and str(value) not in rule.allowed:
                return (
                    "INVALID_VALUE",
                    f"{name}={value!r} is not an accepted value",
                    name,
                )
            if rule.references is not None:
                referenced = self._entities.get(rule.references)
                if referenced is None or str(value) not in referenced.records:
                    return (
                        "REFERENCE_NOT_FOUND",
                        f"{name}={value!r} does not exist in {rule.references}",
                        name,
                    )
        return None

    # -- Writing ------------------------------------------------------------

    def upsert_batch(self, entity_set: str, records: list[dict[str, Any]]) -> list[RecordResult]:
        """Upsert a batch, returning one result per record, in order."""
        schema = SCHEMAS[entity_set]
        results: list[RecordResult] = []

        with self._lock:
            entity = self._entities[entity_set]
            for index, payload in enumerate(records):
                key = str(payload.get(schema.alternate_key, "")).strip()
                if not key:
                    results.append(
                        RecordResult(
                            index=index,
                            alternate_key="",
                            status="failed",
                            error_code="MISSING_ALTERNATE_KEY",
                            error_message=f"{schema.alternate_key} is required for upsert",
                            field=schema.alternate_key,
                        )
                    )
                    continue

                problem = self._validate(schema, payload)
                if problem is not None:
                    code, message, field_name = problem
                    results.append(
                        RecordResult(
                            index=index,
                            alternate_key=key,
                            status="failed",
                            error_code=code,
                            error_message=message,
                            field=field_name,
                        )
                    )
                    continue

                now = dt.datetime.now(dt.UTC).isoformat()
                existing_id = entity.by_alternate_key.get(key)
                if existing_id is None:
                    new_id = str(uuid.uuid4())
                    stored = dict(payload)
                    stored["id"] = new_id
                    stored["_created_at"] = now
                    stored["_modified_at"] = now
                    entity.records[new_id] = stored
                    entity.by_alternate_key[key] = new_id
                    self._writes += 1
                    results.append(
                        RecordResult(index=index, alternate_key=key, status="created", id=new_id)
                    )
                    continue

                stored = entity.records[existing_id]
                comparable = {
                    k: v for k, v in stored.items() if not k.startswith("_") and k != "id"
                }
                if comparable == payload:
                    # Reporting "unchanged" rather than "updated" is what lets
                    # a re-run prove it changed nothing, instead of asking
                    # everyone to take its word for it.
                    results.append(
                        RecordResult(
                            index=index, alternate_key=key, status="unchanged", id=existing_id
                        )
                    )
                    continue

                stored.update(payload)
                stored["_modified_at"] = now
                self._writes += 1
                results.append(
                    RecordResult(index=index, alternate_key=key, status="updated", id=existing_id)
                )

        return results

    # -- Reading ------------------------------------------------------------

    def get(self, entity_set: str, record_id: str) -> dict[str, Any] | None:
        return self._entities[entity_set].records.get(record_id)

    def get_by_alternate_key(self, entity_set: str, key: str) -> dict[str, Any] | None:
        entity = self._entities[entity_set]
        record_id = entity.by_alternate_key.get(key)
        return entity.records.get(record_id) if record_id else None

    def page(self, entity_set: str, *, skip: int = 0, top: int = 100) -> list[dict[str, Any]]:
        records = list(self._entities[entity_set].records.values())
        records.sort(key=lambda r: str(r.get(SCHEMAS[entity_set].alternate_key, "")))
        return records[skip : skip + top]

    def count(self, entity_set: str) -> int:
        return len(self._entities[entity_set].records)

    def counts(self) -> dict[str, int]:
        return {name: len(entity.records) for name, entity in self._entities.items()}

    def checksum(self, entity_set: str, field_name: str) -> str | None:
        """Sum of a numeric field, as a string. Used by reconciliation.

        A row count proves the records arrived; a sum proves the *values* did.
        A migration that loses a decimal separator keeps its row count.
        """
        from decimal import Decimal, InvalidOperation

        total = Decimal(0)
        seen = False
        for record in self._entities[entity_set].records.values():
            value = record.get(field_name)
            if value is None:
                continue
            try:
                total += Decimal(str(value))
                seen = True
            except InvalidOperation:
                continue
        return str(total) if seen else None

    @property
    def writes(self) -> int:
        return self._writes

    def reset(self) -> None:
        with self._lock:
            self._entities = {name: _Entity() for name in SCHEMAS}
            self._writes = 0

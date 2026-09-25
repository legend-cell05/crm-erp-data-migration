"""Planning: source rows in, decided records out.

The planner is the half of the migration that both the dry-run and the load
need, and it is deliberately the same code in both cases. A dry-run that
exercises a different code path from the load is a dry-run that proves
nothing -- it is the single most common way this kind of tool lies.

What it produces per entity: the records to create, the records to update,
the records to skip, and the rejections with their causes. What it never
does: touch the target.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from keystone.config import Settings, get_settings
from keystone.exceptions import RecordRejected
from keystone.extract import iter_source_rows
from keystone.load.crosswalk import Action, CrosswalkStore
from keystone.load.rejects import Rejection
from keystone.logging_config import get_logger
from keystone.mapping.engine import MappedRecord, MappingEngine
from keystone.mapping.loader import MappingSet

logger = get_logger(__name__)


@dataclass(frozen=True)
class PlannedRecord:
    """A mapped record and what should happen to it."""

    action: Action
    record: MappedRecord


@dataclass
class EntityPlan:
    """The outcome of planning one entity."""

    entity: str
    actions: Counter[str] = field(default_factory=Counter)
    rejections: list[Rejection] = field(default_factory=list)
    warnings: Counter[str] = field(default_factory=Counter)
    records_read: int = 0

    @property
    def rejected(self) -> int:
        return len(self.rejections)

    @property
    def reject_rate_pct(self) -> float:
        return 100.0 * self.rejected / self.records_read if self.records_read else 0.0

    @property
    def to_write(self) -> int:
        return self.actions["create"] + self.actions["update"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "records_read": self.records_read,
            "create": self.actions["create"],
            "update": self.actions["update"],
            "skip": self.actions["skip"],
            "reject": self.rejected,
            "reject_rate_pct": round(self.reject_rate_pct, 2),
            "warnings": dict(self.warnings.most_common()),
        }


def plan_entity(
    entity: str,
    mapping_set: MappingSet,
    crosswalk: CrosswalkStore,
    settings: Settings | None = None,
    *,
    collect: bool = False,
) -> tuple[EntityPlan, list[PlannedRecord]]:
    """Map and classify every source row of one entity.

    ``collect`` decides whether the mapped records are kept. The dry-run does
    not need them -- it needs the counts -- and keeping 8 700 payloads in a
    list to throw them away is how a report that should cost nothing starts
    needing a gigabyte.
    """
    settings = settings or get_settings()
    mapping = mapping_set[entity]
    engine = MappingEngine(mapping, lookup_dir=mapping_set.lookup_dir, resolver=crosswalk)

    plan = EntityPlan(entity=entity)
    collected: list[PlannedRecord] = []

    for row in iter_source_rows(mapping, settings):
        plan.records_read += 1
        try:
            record = engine.apply(row)
        except RecordRejected as exc:
            plan.rejections.append(
                Rejection.from_exception(exc, stage=_stage_of(exc), payload=dict(row))
            )
            continue

        for warning in record.warnings:
            plan.warnings[warning.split(":", 1)[0]] += 1

        action = crosswalk.plan(record)
        plan.actions[action] += 1
        if collect and action != "skip":
            collected.append(PlannedRecord(action=action, record=record))

    logger.info("entity planned", extra=plan.as_dict())
    return plan, collected


def iter_planned(
    entity: str,
    mapping_set: MappingSet,
    crosswalk: CrosswalkStore,
    settings: Settings | None = None,
) -> Iterator[PlannedRecord | Rejection]:
    """Streaming variant used by the loader.

    Yields either a record to write or a rejection, in source order, so the
    loader can batch as it goes instead of materialising the whole entity.
    """
    settings = settings or get_settings()
    mapping = mapping_set[entity]
    engine = MappingEngine(mapping, lookup_dir=mapping_set.lookup_dir, resolver=crosswalk)

    for row in iter_source_rows(mapping, settings):
        try:
            record = engine.apply(row)
        except RecordRejected as exc:
            yield Rejection.from_exception(exc, stage=_stage_of(exc), payload=dict(row))
            continue
        yield PlannedRecord(action=crosswalk.plan(record), record=record)


def _stage_of(exc: RecordRejected) -> str:
    """Which stage refused the record, for the report's `stage` column."""
    from keystone.exceptions import MappingViolation, ReferenceNotResolved, ValidationViolation

    if isinstance(exc, MappingViolation):
        return "map"
    if isinstance(exc, ReferenceNotResolved):
        return "map"
    if isinstance(exc, ValidationViolation):
        return "validate"
    return "extract"

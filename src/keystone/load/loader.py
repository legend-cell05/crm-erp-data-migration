"""Loading: planned records to the target, in batches, in dependency order.

Three things happen here that do not happen in the planner.

**Batching.** Records are sent in batches no larger than the target accepts.
The batch size is configuration, not a constant, because the right value is a
property of the target and the network, not of this code.

**Reading the result array.** The target answers a batch with one result per
record. A client that checks only the status code will happily report a
successful migration in which a quarter of the records were refused -- the
whole reason the simulator returns 207 with per-record errors.

**Remembering.** Every successful record's target id goes into the crosswalk
immediately after its batch, not at the end of the entity. A run that dies
halfway leaves a crosswalk that is correct for what was written, which is what
makes the resume work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from keystone.config import Settings, get_settings
from keystone.load.client import AtlasClient
from keystone.load.crosswalk import CrosswalkStore
from keystone.load.planner import PlannedRecord, iter_planned
from keystone.load.rejects import Rejection, RejectStore
from keystone.logging_config import get_logger
from keystone.mapping.engine import MappedRecord
from keystone.mapping.loader import MappingSet

logger = get_logger(__name__)


@dataclass
class EntityLoadReport:
    """What a single entity's load did."""

    entity: str
    records_read: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    rejected_before_send: int = 0
    rejected_by_target: int = 0
    batches: int = 0
    retries: int = 0
    target_errors: dict[str, int] = field(default_factory=dict)

    @property
    def written(self) -> int:
        return self.created + self.updated

    @property
    def rejected(self) -> int:
        return self.rejected_before_send + self.rejected_by_target

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "records_read": self.records_read,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "rejected": self.rejected,
            "rejected_by_target": self.rejected_by_target,
            "batches": self.batches,
            "retries": self.retries,
        }


def load_entity(
    entity: str,
    mapping_set: MappingSet,
    client: AtlasClient,
    crosswalk: CrosswalkStore,
    rejects: RejectStore,
    run_id: str,
    settings: Settings | None = None,
) -> EntityLoadReport:
    """Migrate one entity into the target."""
    settings = settings or get_settings()
    mapping = mapping_set[entity]
    entity_set = mapping.target.entity_set
    report = EntityLoadReport(entity=entity)

    pending: list[PlannedRecord] = []
    rejections: list[Rejection] = []
    loaded_source_ids: list[str] = []

    def flush() -> None:
        if not pending:
            return
        batch = [planned.record for planned in pending]
        outcome = client.upsert_batch(entity_set, [record.payload for record in batch])
        report.batches += 1
        report.retries += outcome.retries

        remembered: list[tuple[MappedRecord, str]] = []
        for record, result in zip(batch, outcome.outcomes, strict=True):
            if result.succeeded and result.target_id is not None:
                remembered.append((record, result.target_id))
                loaded_source_ids.append(record.source_id)
                if result.status == "created":
                    report.created += 1
                elif result.status == "updated":
                    report.updated += 1
                else:
                    report.unchanged += 1
                continue

            report.rejected_by_target += 1
            code = result.error_code or "UNKNOWN"
            report.target_errors[code] = report.target_errors.get(code, 0) + 1
            rejections.append(
                Rejection(
                    entity=entity,
                    source_id=record.source_id,
                    stage="load",
                    message=f"{code}: {result.error_message}",
                    payload=record.payload,
                    field=result.field,
                    rule=code,
                )
            )

        crosswalk.remember(remembered, run_id)
        pending.clear()

    for item in iter_planned(entity, mapping_set, crosswalk, settings):
        if isinstance(item, Rejection):
            report.records_read += 1
            report.rejected_before_send += 1
            rejections.append(item)
            continue

        report.records_read += 1
        if item.action == "skip":
            report.skipped += 1
            continue

        pending.append(item)
        if len(pending) >= settings.batch_size:
            flush()

    flush()

    if rejections:
        rejects.park(rejections, run_id)
    if loaded_source_ids:
        # A record that failed yesterday and loaded today is no longer a
        # problem, and leaving it PENDING makes every subsequent report wrong.
        rejects.resolve(entity, loaded_source_ids)

    logger.info("entity loaded", extra=report.as_dict())
    return report

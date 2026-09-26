"""The migration as a set of operations.

One function per thing a person can ask for, each independently runnable and
each recording its own run. The CLI is a thin shell over this module and adds
no logic of its own, so what CI exercises, what Docker runs and what an
operator types all go through the same code.

The order of operations is not a suggestion:

    seed -> init-db -> dry-run -> load -> reconcile

``load`` refuses to run without a dry-run for the same mapping version, and
that refusal is the point of the whole design.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from keystone.config import Settings, get_settings
from keystone.db.schema import initialise_database
from keystone.dryrun.gate import GateDecision, check_load_gate
from keystone.dryrun.report import DryRunReport, run_dry_run
from keystone.exceptions import MigrationBlocked
from keystone.generation import generate_legacy, write_activity_exports
from keystone.load.client import AtlasClient
from keystone.load.crosswalk import CrosswalkStore
from keystone.load.loader import EntityLoadReport, load_entity
from keystone.load.rejects import RejectStore
from keystone.logging_config import get_logger
from keystone.mapping.loader import MappingSet, load_mapping_set
from keystone.reconcile.checks import ReconciliationReport, reconcile
from keystone.run_record import RunRecord

logger = get_logger(__name__)


@dataclass
class LoadSummary:
    """What a whole load did, across every entity."""

    run_id: str
    mapping_version: str
    reports: list[EntityLoadReport] = field(default_factory=list)
    duration_seconds: float = 0.0
    status: str = "SUCCESS"

    @property
    def created(self) -> int:
        return sum(r.created for r in self.reports)

    @property
    def updated(self) -> int:
        return sum(r.updated for r in self.reports)

    @property
    def unchanged(self) -> int:
        return sum(r.unchanged for r in self.reports)

    @property
    def skipped(self) -> int:
        return sum(r.skipped for r in self.reports)

    @property
    def rejected(self) -> int:
        return sum(r.rejected for r in self.reports)

    @property
    def records_read(self) -> int:
        return sum(r.records_read for r in self.reports)

    @property
    def batches(self) -> int:
        return sum(r.batches for r in self.reports)

    @property
    def retries(self) -> int:
        return sum(r.retries for r in self.reports)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "mapping_version": self.mapping_version,
            "status": self.status,
            "records_read": self.records_read,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "rejected": self.rejected,
            "batches": self.batches,
            "retries": self.retries,
            "duration_seconds": round(self.duration_seconds, 2),
        }


# ---------------------------------------------------------------------------
# Preparation
# ---------------------------------------------------------------------------


def seed(settings: Settings | None = None) -> dict[str, Any]:
    """Create the simulated legacy CRM and its nightly exports."""
    settings = settings or get_settings()
    initialise_database(settings)
    counts, defects = generate_legacy(settings)
    files = write_activity_exports(settings)
    return {"rows": counts, "defects": defects.as_dict(), "export_files": len(files)}


def init_db(settings: Settings | None = None) -> dict[str, int]:
    return initialise_database(settings or get_settings())


# ---------------------------------------------------------------------------
# The migration itself
# ---------------------------------------------------------------------------


def dry_run(
    settings: Settings | None = None, *, mapping_set: MappingSet | None = None
) -> DryRunReport:
    """Plan the migration and write the impact report. Writes nothing to the target."""
    settings = settings or get_settings()
    mapping_set = mapping_set or load_mapping_set(settings)
    return run_dry_run(mapping_set, settings)


def gate(
    settings: Settings | None = None, *, mapping_set: MappingSet | None = None
) -> GateDecision:
    settings = settings or get_settings()
    mapping_set = mapping_set or load_mapping_set(settings)
    return check_load_gate(mapping_set.version, settings)


def load(
    client: AtlasClient,
    settings: Settings | None = None,
    *,
    mapping_set: MappingSet | None = None,
    entities: list[str] | None = None,
    force: bool = False,
) -> LoadSummary:
    """Migrate into the target, in dependency order.

    ``force`` skips the dry-run gate. It exists because a controlled partial
    load is sometimes the right call -- and it is a named argument on a
    separate flag so that skipping the gate is always a decision somebody
    made, never something that happened.
    """
    settings = settings or get_settings()
    mapping_set = mapping_set or load_mapping_set(settings)

    decision = check_load_gate(mapping_set.version, settings)
    if not decision.allowed:
        if not force:
            raise MigrationBlocked(decision.reason)
        logger.warning("load gate overridden", extra={"reason": decision.reason})
    else:
        logger.info("load gate passed", extra={"reason": decision.reason})

    selected = entities or list(mapping_set.order)
    unknown = sorted(set(selected) - set(mapping_set.order))
    if unknown:
        raise MigrationBlocked(f"unknown entities: {', '.join(unknown)}")
    # Whatever the caller asked for, the dependency order is not negotiable.
    ordered = [entity for entity in mapping_set.order if entity in set(selected)]

    crosswalk = CrosswalkStore(settings)
    crosswalk.load_all(list(mapping_set.order))
    rejects = RejectStore(settings)

    with RunRecord(kind="load", mapping_version=mapping_set.version, settings=settings) as run:
        summary = LoadSummary(run_id=run.run_id, mapping_version=mapping_set.version)
        for entity in ordered:
            summary.reports.append(
                load_entity(entity, mapping_set, client, crosswalk, rejects, run.run_id, settings)
            )

        run.records_read = summary.records_read
        run.records_mapped = summary.records_read - summary.rejected
        run.records_written = summary.created + summary.updated
        run.records_rejected = summary.rejected
        if summary.rejected:
            run.mark_partial(f"{summary.rejected} record(s) rejected")
            summary.status = "PARTIAL"

    summary.duration_seconds = run.duration_seconds
    summary.status = run.status
    return summary


def reconcile_migration(
    client: AtlasClient, settings: Settings | None = None, *, mapping_set: MappingSet | None = None
) -> ReconciliationReport:
    settings = settings or get_settings()
    mapping_set = mapping_set or load_mapping_set(settings)
    with RunRecord(kind="reconcile", mapping_version=mapping_set.version, settings=settings) as run:
        report = reconcile(mapping_set, client, settings)
        if not report.passed:
            run.mark_partial(f"{len(report.mismatches)} mismatch(es)")
    return report


def migrate(
    client: AtlasClient,
    settings: Settings | None = None,
    *,
    mapping_set: MappingSet | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Dry-run, load and reconcile, in that order."""
    settings = settings or get_settings()
    mapping_set = mapping_set or load_mapping_set(settings)

    report = dry_run(settings, mapping_set=mapping_set)
    summary = load(client, settings, mapping_set=mapping_set, force=force)
    reconciliation = reconcile_migration(client, settings, mapping_set=mapping_set)

    return {
        "dry_run": report.as_dict()["totals"],
        "load": summary.as_dict(),
        "reconciliation": reconciliation.as_dict(),
    }

"""The dry-run and its impact report.

What a dry-run must do to be believed:

1. **Run the same code the load runs.** The planner here is the planner the
   loader uses. A dry-run with its own private logic predicts the behaviour of
   a program that does not exist.
2. **Touch nothing.** No client is constructed, so there is no code path that
   could write to the target even if something went wrong.
3. **Project forward.** References resolve against what earlier entities in
   this run would create, or the report is a wall of false rejections.
4. **Leave a record.** The impact rows it writes are what the load's gate
   reads; a dry-run whose result is only printed is a dry-run nobody can
   enforce.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from keystone.config import Settings, get_settings
from keystone.db.engine import get_engine
from keystone.dryrun.profile import ColumnProfile, persist_profiles, profile_entity
from keystone.dryrun.projection import ProjectedCrosswalk
from keystone.exceptions import DatabaseError, RecordRejected
from keystone.extract import iter_source_rows
from keystone.load.crosswalk import CrosswalkStore
from keystone.load.planner import EntityPlan
from keystone.load.rejects import Rejection
from keystone.logging_config import get_logger
from keystone.mapping.engine import MappingEngine
from keystone.mapping.loader import MappingSet
from keystone.run_record import RunRecord

logger = get_logger(__name__)


@dataclass
class DryRunReport:
    """Everything a dry-run found."""

    run_id: str
    mapping_version: str
    generated_at: dt.datetime
    plans: list[EntityPlan] = field(default_factory=list)
    profiles: list[ColumnProfile] = field(default_factory=list)
    rejection_causes: list[dict[str, Any]] = field(default_factory=list)
    duration_seconds: float = 0.0

    @property
    def records_read(self) -> int:
        return sum(plan.records_read for plan in self.plans)

    @property
    def records_rejected(self) -> int:
        return sum(plan.rejected for plan in self.plans)

    @property
    def records_to_write(self) -> int:
        return sum(plan.to_write for plan in self.plans)

    @property
    def reject_rate_pct(self) -> float:
        return 100.0 * self.records_rejected / self.records_read if self.records_read else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "mapping_version": self.mapping_version,
            "generated_at": self.generated_at.isoformat(),
            "duration_seconds": round(self.duration_seconds, 2),
            "totals": {
                "records_read": self.records_read,
                "records_to_write": self.records_to_write,
                "records_rejected": self.records_rejected,
                "reject_rate_pct": round(self.reject_rate_pct, 2),
            },
            "entities": [plan.as_dict() for plan in self.plans],
            "rejection_causes": self.rejection_causes,
            "profiles": [profile.as_dict() for profile in self.profiles],
        }


def _summarise_causes(rejections: list[Rejection]) -> list[dict[str, Any]]:
    """Group rejections by cause, worst first, with one example each.

    The example is what turns a count into a task: 'country_code / lookup:
    country -- 12 records, e.g. "Frnace"'.
    """
    grouped: dict[tuple[str, str | None, str | None], list[Rejection]] = {}
    for rejection in rejections:
        grouped.setdefault((rejection.entity, rejection.field, rejection.rule), []).append(
            rejection
        )

    causes: list[dict[str, Any]] = [
        {
            "entity": entity,
            "field": field_name,
            "rule": rule,
            "records": len(items),
            "example_source_id": items[0].source_id,
            "example_message": items[0].message[:160],
        }
        for (entity, field_name, rule), items in grouped.items()
    ]
    causes.sort(key=lambda cause: int(cause["records"] or 0), reverse=True)
    return causes


def _persist_impact(report: DryRunReport, settings: Settings) -> None:
    schema = settings.migration_schema
    rows = []
    for plan in report.plans:
        for action in ("create", "update", "skip"):
            rows.append(
                {
                    "run_id": report.run_id,
                    "entity": plan.entity,
                    "action": action,
                    "count": plan.actions[action],
                }
            )
        rows.append(
            {
                "run_id": report.run_id,
                "entity": plan.entity,
                "action": "reject",
                "count": plan.rejected,
            }
        )
    try:
        with get_engine(settings).begin() as conn:
            conn.execute(
                text(
                    f"""INSERT INTO {schema}.impact (run_id, entity, action, record_count)
                        VALUES (CAST(:run_id AS UUID), :entity, :action, :count)
                        ON CONFLICT (run_id, entity, action)
                        DO UPDATE SET record_count = EXCLUDED.record_count"""
                ),
                rows,
            )
    except SQLAlchemyError as exc:
        raise DatabaseError(f"persisting the impact report failed: {exc}") from exc


def render_markdown(report: DryRunReport) -> str:
    """The report a human reads before approving the migration."""
    lines = [
        "# Migration impact report",
        "",
        f"- **Mapping version**: `{report.mapping_version}`",
        f"- **Run**: `{report.run_id}`",
        f"- **Generated**: {report.generated_at.isoformat(timespec='seconds')}",
        f"- **Duration**: {report.duration_seconds:.1f} s",
        "",
        "> Nothing was written to the target. This report is a projection of "
        "what a load of this mapping version would do.",
        "",
        "## What would happen",
        "",
        "| Entity | Read | Create | Update | Skip | Reject | Reject rate |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for plan in report.plans:
        lines.append(
            f"| `{plan.entity}` | {plan.records_read:,} | {plan.actions['create']:,} | "
            f"{plan.actions['update']:,} | {plan.actions['skip']:,} | {plan.rejected:,} | "
            f"{plan.reject_rate_pct:.2f}% |"
        )
    lines.append(
        f"| **total** | **{report.records_read:,}** | | | | **{report.records_rejected:,}** | "
        f"**{report.reject_rate_pct:.2f}%** |"
    )

    if report.rejection_causes:
        lines += [
            "",
            "## Why records would be rejected",
            "",
            "Ordered by how many records each cause accounts for. The fix for the "
            "first row is usually worth more than the fix for all the others.",
            "",
            "| Entity | Field | Rule | Records | Example |",
            "| --- | --- | --- | ---: | --- |",
        ]
        for cause in report.rejection_causes[:15]:
            lines.append(
                f"| `{cause['entity']}` | `{cause['field'] or '-'}` | `{cause['rule'] or '-'}` | "
                f"{cause['records']:,} | {cause['example_message']} |"
            )

    warned = [(plan.entity, plan.warnings) for plan in report.plans if plan.warnings]
    if warned:
        lines += [
            "",
            "## Warnings",
            "",
            "These records **would** migrate. They are listed because somebody "
            "should decide whether that is what they want.",
            "",
            "| Entity | Rule | Records |",
            "| --- | --- | ---: |",
        ]
        for entity, warnings in warned:
            for rule, count in warnings.most_common():
                lines.append(f"| `{entity}` | `{rule}` | {count:,} |")

    lines += [
        "",
        "## Source profile",
        "",
        "Columns the mapping reads, as they are in the source. A high empty "
        "percentage on a required field is a rejection waiting to happen.",
        "",
        "| Entity | Column | Rows | Empty | Distinct | Examples |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    for profile in report.profiles:
        if profile.empty_pct >= 1.0 or profile.distinct_count <= 12:
            samples = ", ".join(f"`{value}`" for value in profile.samples[:3])
            lines.append(
                f"| `{profile.entity}` | `{profile.column}` | {profile.row_count:,} | "
                f"{profile.empty_pct:.1f}% | {profile.distinct_count:,} | {samples} |"
            )

    return "\n".join(lines) + "\n"


def run_dry_run(
    mapping_set: MappingSet, settings: Settings | None = None, *, write_files: bool = True
) -> DryRunReport:
    """Plan the whole migration without writing anything to the target."""
    settings = settings or get_settings()
    crosswalk = CrosswalkStore(settings)
    crosswalk.load_all(list(mapping_set.order))
    projection = ProjectedCrosswalk(crosswalk)

    all_rejections: list[Rejection] = []

    with RunRecord(kind="dry_run", mapping_version=mapping_set.version, settings=settings) as run:
        report = DryRunReport(
            run_id=run.run_id,
            mapping_version=mapping_set.version,
            generated_at=run.started_at,
        )

        for entity in mapping_set.order:
            mapping = mapping_set[entity]
            report.profiles.extend(profile_entity(mapping, settings))

            engine = MappingEngine(mapping, lookup_dir=mapping_set.lookup_dir, resolver=projection)
            plan = EntityPlan(entity=entity)
            for row in iter_source_rows(mapping, settings):
                plan.records_read += 1
                try:
                    record = engine.apply(row)
                except RecordRejected as exc:
                    from keystone.load.planner import _stage_of

                    plan.rejections.append(
                        Rejection.from_exception(exc, stage=_stage_of(exc), payload=dict(row))
                    )
                    continue
                for warning in record.warnings:
                    plan.warnings[warning.split(":", 1)[0]] += 1
                action = projection.plan(record)
                plan.actions[action] += 1
                # Whatever survives mapping will exist in the target after
                # this run, including the records that are skipped because
                # they are already there.
                projection.project(entity, record.source_id)

            report.plans.append(plan)
            all_rejections.extend(plan.rejections)

        report.rejection_causes = _summarise_causes(all_rejections)
        _persist_impact(report, settings)
        persist_profiles(report.profiles, run.run_id, settings)

        run.records_read = report.records_read
        run.records_mapped = report.records_read - report.records_rejected
        run.records_rejected = report.records_rejected
        if report.reject_rate_pct > settings.max_reject_rate_pct:
            run.mark_partial(
                f"projected rejection rate {report.reject_rate_pct:.2f}% is above the "
                f"{settings.max_reject_rate_pct:.2f}% ceiling"
            )

    report.duration_seconds = run.duration_seconds

    if write_files:
        paths = write_report_files(report, settings)
        logger.info("dry-run report written", extra={"files": [str(p) for p in paths]})

    return report


def write_report_files(report: DryRunReport, settings: Settings | None = None) -> list[Path]:
    """Write the report as JSON (for machines) and Markdown (for people)."""
    settings = settings or get_settings()
    settings.report_dir.mkdir(parents=True, exist_ok=True)
    stamp = report.generated_at.strftime("%Y%m%dT%H%M%S")

    json_path = settings.report_dir / f"dry_run_{stamp}.json"
    json_path.write_text(json.dumps(report.as_dict(), indent=2, default=str), encoding="utf-8")

    md_path = settings.report_dir / f"dry_run_{stamp}.md"
    md_path.write_text(render_markdown(report), encoding="utf-8")

    latest = settings.report_dir / "dry_run_latest.md"
    latest.write_text(render_markdown(report), encoding="utf-8")
    return [json_path, md_path, latest]

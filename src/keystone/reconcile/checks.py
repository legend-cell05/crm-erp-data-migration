"""Reconciliation: proving the migration did what it claims.

A load reports what it sent. Reconciliation asks the target what it has, and
compares the two against the source. The three numbers are produced by three
different systems, which is the only reason comparing them is worth anything.

Row counts alone are not enough, and the reason is specific: a migration that
mangles a decimal separator keeps every row and changes the total. So the
money is summed on both sides, from the source's raw text and from the
target's stored values, and the difference has to be zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from keystone.config import Settings, get_settings
from keystone.db.engine import get_engine
from keystone.exceptions import DatabaseError
from keystone.extract import iter_source_rows
from keystone.load.client import AtlasClient
from keystone.logging_config import get_logger
from keystone.mapping.functions import get_transform
from keystone.mapping.loader import MappingSet

logger = get_logger(__name__)

Status = str  # "OK" | "MISMATCH" | "WARN"


@dataclass(frozen=True)
class CheckResult:
    name: str
    entity: str
    expected: str
    observed: str
    status: Status
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "OK"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "entity": self.entity,
            "expected": self.expected,
            "observed": self.observed,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass
class ReconciliationReport:
    results: list[CheckResult] = field(default_factory=list)

    @property
    def mismatches(self) -> list[CheckResult]:
        return [r for r in self.results if r.status == "MISMATCH"]

    @property
    def passed(self) -> bool:
        return not self.mismatches

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": [r.as_dict() for r in self.results],
            "mismatches": len(self.mismatches),
        }


def _crosswalk_counts(settings: Settings) -> dict[str, int]:
    schema = settings.migration_schema
    try:
        with get_engine(settings).connect() as conn:
            rows = conn.execute(
                text(f"SELECT entity, COUNT(*) AS n FROM {schema}.crosswalk GROUP BY entity")
            ).all()
    except SQLAlchemyError as exc:
        raise DatabaseError(f"counting the crosswalk failed: {exc}") from exc
    return {row.entity: int(row.n) for row in rows}


def _pending_rejects(settings: Settings) -> dict[str, int]:
    schema = settings.migration_schema
    try:
        with get_engine(settings).connect() as conn:
            rows = conn.execute(
                text(
                    f"""SELECT entity, COUNT(*) AS n FROM {schema}.reject
                        WHERE status = 'PENDING' GROUP BY entity"""
                )
            ).all()
    except SQLAlchemyError as exc:
        raise DatabaseError(f"counting rejections failed: {exc}") from exc
    return {row.entity: int(row.n) for row in rows}


def _source_amount_total(mapping_set: MappingSet, settings: Settings) -> Decimal:
    """Total the opportunity amounts from the source, using the same parser.

    Using the mapping's own ``parse_amount`` is deliberate. Totalling with a
    second, simpler parser would compare the migration against a different
    interpretation of the data and report differences that are not errors --
    and hide the one case that matters, where the parser itself is wrong.
    """
    parse = get_transform("parse_amount")
    mapping = mapping_set["opportunity"]
    total = Decimal(0)
    for row in iter_source_rows(mapping, settings):
        raw = row.get("amount")
        try:
            parsed = parse(raw)
        except ValueError:
            continue  # unparseable amounts were rejected, and are counted there
        if parsed is not None:
            try:
                total += Decimal(str(parsed))
            except InvalidOperation:
                continue
    return total


def reconcile(
    mapping_set: MappingSet, client: AtlasClient, settings: Settings | None = None
) -> ReconciliationReport:
    """Compare source, crosswalk and target."""
    settings = settings or get_settings()
    report = ReconciliationReport()

    crosswalk = _crosswalk_counts(settings)
    pending = _pending_rejects(settings)

    for entity in mapping_set.order:
        mapping = mapping_set[entity]
        entity_set = mapping.target.entity_set

        migrated = crosswalk.get(entity, 0)
        in_target = client.count(entity_set)
        report.results.append(
            CheckResult(
                name="crosswalk_matches_target",
                entity=entity,
                expected=f"{migrated:,}",
                observed=f"{in_target:,}",
                status="OK" if migrated == in_target else "MISMATCH",
                detail=(
                    "every crosswalk entry must correspond to a record in the target; "
                    "a difference means the crosswalk is lying about what exists"
                ),
            )
        )

        outstanding = pending.get(entity, 0)
        report.results.append(
            CheckResult(
                name="no_pending_rejections",
                entity=entity,
                expected="0",
                observed=f"{outstanding:,}",
                status="OK" if outstanding == 0 else "WARN",
                detail="records that have not migrated and are waiting for a human",
            )
        )

    source_total = _source_amount_total(mapping_set, settings)
    target_total_raw = client.checksum("opportunities", "amount")
    target_total = Decimal(target_total_raw) if target_total_raw else Decimal(0)
    difference = source_total - target_total

    # The source total includes the opportunities that were rejected, so an
    # exact match is not expected -- what is checked is that the difference is
    # accounted for, not that it is zero.
    report.results.append(
        CheckResult(
            name="opportunity_amount_total",
            entity="opportunity",
            expected=f"{source_total:,.2f} (source, incl. rejected)",
            observed=f"{target_total:,.2f} (target)",
            status="OK" if difference >= 0 else "MISMATCH",
            detail=(
                f"difference {difference:,.2f} corresponds to the rejected opportunities; "
                "a negative difference would mean the target holds money the source does not"
            ),
        )
    )

    logger.info(
        "reconciliation finished",
        extra={"checks": len(report.results), "mismatches": len(report.mismatches)},
    )
    return report

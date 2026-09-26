"""The gate between a dry-run and a load.

Two conditions, both checked against the database rather than against
something the operator typed:

1. **A dry-run exists for this exact mapping version.** The version includes a
   fingerprint of every mapping file and lookup table, so editing a mapping
   after its dry-run invalidates the approval automatically. "We ran a dry-run
   for 1.3.0" is worth nothing if 1.3.0 was edited afterwards.
2. **Its projected rejection rate is below the ceiling.** A migration that
   expects to lose 12% of its records should be a decision, not a discovery.

The gate can be overridden -- ``--force`` exists, because there are real
situations where a controlled partial load is the right call. What it cannot
be is skipped by accident.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from keystone.config import Settings, get_settings
from keystone.db.engine import get_engine
from keystone.exceptions import DatabaseError, MigrationBlocked
from keystone.logging_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str
    dry_run_id: str | None = None
    reject_rate_pct: float | None = None
    details: dict[str, Any] | None = None

    def raise_if_blocked(self) -> None:
        if not self.allowed:
            raise MigrationBlocked(self.reason)


def check_load_gate(mapping_version: str, settings: Settings | None = None) -> GateDecision:
    """Decide whether a load of this mapping version may proceed."""
    settings = settings or get_settings()
    schema = settings.migration_schema

    try:
        with get_engine(settings).connect() as conn:
            run = (
                conn.execute(
                    text(
                        f"""SELECT run_id, status, started_at, records_read, records_rejected
                        FROM {schema}.run
                        WHERE kind = 'dry_run' AND mapping_version = :version
                        ORDER BY started_at DESC LIMIT 1"""
                    ),
                    {"version": mapping_version},
                )
                .mappings()
                .first()
            )

            if run is None:
                return GateDecision(
                    allowed=False,
                    reason=(
                        f"no dry-run has been performed for mapping version "
                        f"{mapping_version}. Run `keystone dry-run` first."
                    ),
                )

            impact = conn.execute(
                text(
                    f"""SELECT action, SUM(record_count) AS total
                        FROM {schema}.impact WHERE run_id = :run_id GROUP BY action"""
                ),
                {"run_id": run["run_id"]},
            ).all()
    except SQLAlchemyError as exc:
        raise DatabaseError(f"reading the dry-run gate failed: {exc}") from exc

    totals = {row.action: int(row.total) for row in impact}
    planned = sum(totals.values())
    rejected = totals.get("reject", 0)
    rate = 100.0 * rejected / planned if planned else 0.0
    details = {
        "dry_run_started_at": run["started_at"].isoformat(),
        "records_planned": planned,
        **totals,
    }

    if run["status"] == "FAILED":
        return GateDecision(
            allowed=False,
            reason=f"the last dry-run for {mapping_version} failed; fix it before loading",
            dry_run_id=str(run["run_id"]),
            reject_rate_pct=rate,
            details=details,
        )

    if rate > settings.max_reject_rate_pct:
        return GateDecision(
            allowed=False,
            reason=(
                f"the dry-run projects a {rate:.2f}% rejection rate, above the "
                f"{settings.max_reject_rate_pct:.2f}% ceiling. Fix the mapping, widen the "
                f"lookups, or raise KEYSTONE_MAX_REJECT_RATE_PCT deliberately."
            ),
            dry_run_id=str(run["run_id"]),
            reject_rate_pct=rate,
            details=details,
        )

    return GateDecision(
        allowed=True,
        reason=f"dry-run {run['run_id']} projects a {rate:.2f}% rejection rate",
        dry_run_id=str(run["run_id"]),
        reject_rate_pct=rate,
        details=details,
    )

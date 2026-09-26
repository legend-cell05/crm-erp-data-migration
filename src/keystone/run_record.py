"""Recording what each run did.

Every entry point writes one row into ``migration.run``, on the way in and on
the way out. The row exists before the work starts, so a process killed
halfway leaves a ``RUNNING`` row with a start time rather than no trace at
all -- which is the difference between "the migration was interrupted at
11:04" and "nobody knows whether the migration ran".

It is also what the dry-run gate reads. A load looks for a successful dry_run
row carrying the *same mapping version* it is about to apply, and refuses to
start without one.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from keystone.config import Settings, get_settings
from keystone.db.engine import get_engine
from keystone.exceptions import DatabaseError
from keystone.logging_config import get_logger

logger = get_logger(__name__)

RunKind = Literal["dry_run", "load", "retry", "reconcile"]


@dataclass
class RunRecord:
    """Context manager around one run."""

    kind: RunKind
    mapping_version: str
    settings: Settings = field(default_factory=get_settings)
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    records_read: int = 0
    records_mapped: int = 0
    records_written: int = 0
    records_rejected: int = 0
    notes: str | None = None
    status: str = "RUNNING"
    started_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    finished_at: dt.datetime | None = None

    @property
    def duration_seconds(self) -> float:
        end = self.finished_at or dt.datetime.now(dt.UTC)
        return (end - self.started_at).total_seconds()

    def mark_partial(self, note: str) -> None:
        """Finished, but not cleanly. Distinct from FAILED on purpose.

        A load in which the target refused 12 records out of 16 000 did work,
        and reporting it as a failure teaches everyone to ignore the status.
        """
        self.status = "PARTIAL"
        self.notes = note if self.notes is None else f"{self.notes}; {note}"

    def __enter__(self) -> RunRecord:
        schema = self.settings.migration_schema
        try:
            with get_engine(self.settings).begin() as conn:
                conn.execute(
                    text(
                        f"""INSERT INTO {schema}.run
                                (run_id, kind, mapping_version, status, started_at)
                            VALUES (CAST(:run_id AS UUID), :kind, :version, 'RUNNING', :started)"""
                    ),
                    {
                        "run_id": self.run_id,
                        "kind": self.kind,
                        "version": self.mapping_version,
                        "started": self.started_at,
                    },
                )
        except SQLAlchemyError as exc:
            raise DatabaseError(f"could not open a run record: {exc}") from exc
        logger.info(
            "run started",
            extra={
                "run_id": self.run_id,
                "kind": self.kind,
                "mapping_version": self.mapping_version,
            },
        )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.finished_at = dt.datetime.now(dt.UTC)
        if exc_type is not None:
            self.status = "FAILED"
            self.notes = f"{exc_type.__name__}: {exc}"[:2000]
        elif self.status == "RUNNING":
            self.status = "SUCCESS"

        schema = self.settings.migration_schema
        try:
            with get_engine(self.settings).begin() as conn:
                conn.execute(
                    text(
                        f"""UPDATE {schema}.run
                            SET status = :status, finished_at = :finished,
                                records_read = :read, records_mapped = :mapped,
                                records_written = :written, records_rejected = :rejected,
                                notes = :notes
                            WHERE run_id = CAST(:run_id AS UUID)"""
                    ),
                    {
                        "status": self.status,
                        "finished": self.finished_at,
                        "read": self.records_read,
                        "mapped": self.records_mapped,
                        "written": self.records_written,
                        "rejected": self.records_rejected,
                        "notes": self.notes,
                        "run_id": self.run_id,
                    },
                )
        except SQLAlchemyError as db_exc:  # pragma: no cover - reported, never masking
            logger.error("could not close the run record", extra={"error": str(db_exc)})

        logger.info(
            "run finished",
            extra={
                "run_id": self.run_id,
                "status": self.status,
                "duration_seconds": round(self.duration_seconds, 2),
                "written": self.records_written,
                "rejected": self.records_rejected,
            },
        )


def recent_runs(limit: int = 10, settings: Settings | None = None) -> list[dict[str, Any]]:
    settings = settings or get_settings()
    schema = settings.migration_schema
    try:
        with get_engine(settings).connect() as conn:
            rows = (
                conn.execute(
                    text(
                        f"""SELECT run_id, kind, mapping_version, status, started_at, finished_at,
                               records_read, records_written, records_rejected, notes
                        FROM {schema}.run ORDER BY started_at DESC LIMIT :limit"""
                    ),
                    {"limit": limit},
                )
                .mappings()
                .all()
            )
    except SQLAlchemyError as exc:
        raise DatabaseError(f"reading run history failed: {exc}") from exc
    return [dict(row) for row in rows]

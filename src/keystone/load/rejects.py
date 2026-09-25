"""The reject store.

Every record that does not make it is parked here with the field and the rule
that refused it. That is the difference between a migration report that says
"1 812 records failed" and one that says "1 640 of them have a country code
that is not in the lookup, and 1 012 of those say 'Frnace'" -- the second is a
morning's work for one person, the first is a project meeting.

One row per record, not one per attempt: a permanently broken source record
would otherwise fill the table with copies of the same problem. ``attempts``
counts, and ``ABANDONED`` ends it.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from keystone.config import Settings, get_settings
from keystone.db.engine import get_engine
from keystone.exceptions import DatabaseError, RecordRejected
from keystone.logging_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class Rejection:
    """One rejected record, ready to be written."""

    entity: str
    source_id: str
    stage: str
    message: str
    payload: dict[str, Any]
    field: str | None = None
    rule: str | None = None

    @classmethod
    def from_exception(
        cls, exc: RecordRejected, *, stage: str, payload: dict[str, Any]
    ) -> Rejection:
        return cls(
            entity=exc.entity,
            source_id=exc.source_id,
            stage=stage,
            message=str(exc),
            payload=payload,
            field=exc.field,
            rule=exc.rule,
        )


class RejectStore:
    """Persists rejections and answers questions about them."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    def park(self, rejections: list[Rejection], run_id: str) -> int:
        if not rejections:
            return 0
        schema = self._settings.migration_schema
        now = dt.datetime.now(dt.UTC)
        rows = [
            {
                "entity": r.entity,
                "source_id": r.source_id,
                "stage": r.stage,
                "field": r.field,
                "rule": r.rule,
                "message": r.message[:2000],
                # json.dumps with default=str because a source row can carry a
                # date or a Decimal, and the point of keeping the payload is
                # to be able to replay it, not to lose it to a serialiser.
                "payload": json.dumps(r.payload, default=str),
                "run_id": run_id,
                "now": now,
                "max_attempts": self._settings.max_retry_attempts,
            }
            for r in rejections
        ]
        try:
            with get_engine(self._settings).begin() as conn:
                conn.execute(
                    text(
                        f"""INSERT INTO {schema}.reject
                                (entity, source_id, stage, field, rule, message, payload,
                                 status, attempts, first_seen_at, last_seen_at, last_run_id)
                            VALUES (:entity, :source_id, :stage, :field, :rule, :message,
                                    CAST(:payload AS JSONB), 'PENDING', 1, :now, :now,
                                    CAST(:run_id AS UUID))
                            ON CONFLICT (entity, source_id) DO UPDATE
                            SET stage        = EXCLUDED.stage,
                                field        = EXCLUDED.field,
                                rule         = EXCLUDED.rule,
                                message      = EXCLUDED.message,
                                payload      = EXCLUDED.payload,
                                last_seen_at = EXCLUDED.last_seen_at,
                                last_run_id  = EXCLUDED.last_run_id,
                                attempts     = {schema}.reject.attempts + 1,
                                status       = CASE
                                    WHEN {schema}.reject.attempts + 1 >= :max_attempts
                                    THEN 'ABANDONED' ELSE 'PENDING' END"""
                    ),
                    rows,
                )
        except SQLAlchemyError as exc:
            raise DatabaseError(f"parking rejections failed: {exc}") from exc
        return len(rows)

    def resolve(self, entity: str, source_ids: list[str]) -> int:
        """Mark records as resolved once they finally load."""
        if not source_ids:
            return 0
        schema = self._settings.migration_schema
        try:
            with get_engine(self._settings).begin() as conn:
                result = conn.execute(
                    text(
                        f"""UPDATE {schema}.reject
                            SET status = 'RESOLVED', last_seen_at = now()
                            WHERE entity = :entity AND source_id = ANY(:ids)
                              AND status <> 'RESOLVED'"""
                    ),
                    {"entity": entity, "ids": source_ids},
                )
        except SQLAlchemyError as exc:
            raise DatabaseError(f"resolving rejections failed: {exc}") from exc
        return int(result.rowcount or 0)

    def pending(self, entity: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        schema = self._settings.migration_schema
        try:
            with get_engine(self._settings).connect() as conn:
                rows = (
                    conn.execute(
                        text(
                            f"""SELECT entity, source_id, stage, field, rule, message, payload,
                                   attempts
                            FROM {schema}.reject
                            WHERE status = 'PENDING'
                              AND (CAST(:entity AS TEXT) IS NULL OR entity = :entity)
                            ORDER BY entity, source_id
                            LIMIT :limit"""
                        ),
                        {"entity": entity, "limit": limit},
                    )
                    .mappings()
                    .all()
                )
        except SQLAlchemyError as exc:
            raise DatabaseError(f"reading rejections failed: {exc}") from exc
        return [dict(row) for row in rows]

    def summary(self) -> list[dict[str, Any]]:
        """Rejections grouped by what caused them, worst first."""
        schema = self._settings.migration_schema
        try:
            with get_engine(self._settings).connect() as conn:
                rows = (
                    conn.execute(
                        text(
                            f"""SELECT entity, stage, field, rule, status,
                                   COUNT(*) AS records, MAX(attempts) AS max_attempts,
                                   MIN(message) AS example
                            FROM {schema}.reject
                            GROUP BY entity, stage, field, rule, status
                            ORDER BY COUNT(*) DESC"""
                        )
                    )
                    .mappings()
                    .all()
                )
        except SQLAlchemyError as exc:
            raise DatabaseError(f"summarising rejections failed: {exc}") from exc
        return [dict(row) for row in rows]

    def counts_by_status(self) -> dict[str, int]:
        schema = self._settings.migration_schema
        try:
            with get_engine(self._settings).connect() as conn:
                rows = conn.execute(
                    text(f"SELECT status, COUNT(*) AS n FROM {schema}.reject GROUP BY status")
                ).all()
        except SQLAlchemyError as exc:
            raise DatabaseError(f"counting rejections failed: {exc}") from exc
        return {row.status: int(row.n) for row in rows}

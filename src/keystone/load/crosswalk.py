"""The crosswalk: which legacy record became which target record.

This is the table that turns a migration from a one-shot script into an
operation that can be run twice. It answers three questions, in one lookup:

* **Has this record been migrated?** No entry -> create.
* **Has it changed since?** Entry with a different content hash -> update.
* **Is it unchanged?** Same hash -> skip, and do not touch the target at all.

The third case is what keeps a re-run honest. Without it, every re-run
rewrites every record, the target's audit log fills with changes that changed
nothing, and the handful of records that genuinely moved are impossible to
find.

Losing this table is the one unrecoverable failure in the whole project: the
target's records would still exist, but nothing would know where they came
from, and the next run would create all of them again.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from keystone.config import Settings, get_settings
from keystone.db.engine import get_engine
from keystone.exceptions import DatabaseError
from keystone.logging_config import get_logger
from keystone.mapping.engine import MappedRecord

logger = get_logger(__name__)

Action = Literal["create", "update", "skip"]


@dataclass(frozen=True)
class CrosswalkEntry:
    entity: str
    source_id: str
    target_id: str
    alternate_key: str
    content_hash: str


class CrosswalkStore:
    """Reads the crosswalk into memory for a run, writes it back in batches.

    In memory because every referenced record triggers a lookup -- 8 700
    activities each resolving an account, a contact and an opportunity is
    26 000 lookups, and a round trip apiece would dominate the migration.
    The whole table for this dataset is a few megabytes.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._cache: dict[str, dict[str, CrosswalkEntry]] = {}

    # -- Reading ------------------------------------------------------------

    def load_entity(self, entity: str, *, refresh: bool = False) -> dict[str, CrosswalkEntry]:
        if not refresh and entity in self._cache:
            return self._cache[entity]
        schema = self._settings.migration_schema
        try:
            with get_engine(self._settings).connect() as conn:
                rows = conn.execute(
                    text(
                        f"""SELECT entity, source_id, target_id, alternate_key, content_hash
                            FROM {schema}.crosswalk WHERE entity = :entity"""
                    ),
                    {"entity": entity},
                ).all()
        except SQLAlchemyError as exc:
            raise DatabaseError(f"reading the crosswalk for {entity} failed: {exc}") from exc

        entries = {
            row.source_id: CrosswalkEntry(
                entity=row.entity,
                source_id=row.source_id,
                target_id=row.target_id,
                alternate_key=row.alternate_key,
                content_hash=row.content_hash,
            )
            for row in rows
        }
        self._cache[entity] = entries
        logger.debug("crosswalk loaded", extra={"entity": entity, "entries": len(entries)})
        return entries

    def load_all(self, entities: list[str]) -> None:
        for entity in entities:
            self.load_entity(entity, refresh=True)

    # -- ReferenceResolver --------------------------------------------------

    def resolve(self, entity: str, source_id: str) -> str | None:
        """Satisfies :class:`keystone.mapping.engine.ReferenceResolver`."""
        entry = self.load_entity(entity).get(source_id)
        return entry.target_id if entry is not None else None

    # -- Planning -----------------------------------------------------------

    def plan(self, record: MappedRecord) -> Action:
        existing = self.load_entity(record.entity).get(record.source_id)
        if existing is None:
            return "create"
        if existing.content_hash != record.content_hash:
            return "update"
        return "skip"

    # -- Writing ------------------------------------------------------------

    def remember(self, entries: list[tuple[MappedRecord, str]], run_id: str) -> int:
        """Record (or refresh) the mapping between source and target ids."""
        if not entries:
            return 0
        schema = self._settings.migration_schema
        now = dt.datetime.now(dt.UTC)
        rows = [
            {
                "entity": record.entity,
                "source_id": record.source_id,
                "target_id": target_id,
                "alternate_key": record.alternate_key,
                "content_hash": record.content_hash,
                "run_id": run_id,
                "now": now,
            }
            for record, target_id in entries
        ]
        try:
            with get_engine(self._settings).begin() as conn:
                conn.execute(
                    text(
                        f"""INSERT INTO {schema}.crosswalk
                                (entity, source_id, target_id, alternate_key, content_hash,
                                 first_loaded_at, last_loaded_at, last_run_id, load_count)
                            VALUES (:entity, :source_id, :target_id, :alternate_key,
                                    :content_hash, :now, :now, CAST(:run_id AS UUID), 1)
                            ON CONFLICT (entity, source_id) DO UPDATE
                            SET target_id      = EXCLUDED.target_id,
                                alternate_key  = EXCLUDED.alternate_key,
                                content_hash   = EXCLUDED.content_hash,
                                last_loaded_at = EXCLUDED.last_loaded_at,
                                last_run_id    = EXCLUDED.last_run_id,
                                load_count     = {schema}.crosswalk.load_count + 1"""
                    ),
                    rows,
                )
        except SQLAlchemyError as exc:
            raise DatabaseError(f"writing the crosswalk failed: {exc}") from exc

        cache = self._cache.setdefault(entries[0][0].entity, {})
        for record, target_id in entries:
            cache[record.source_id] = CrosswalkEntry(
                entity=record.entity,
                source_id=record.source_id,
                target_id=target_id,
                alternate_key=record.alternate_key,
                content_hash=record.content_hash,
            )
        return len(rows)

    # -- Reporting ----------------------------------------------------------

    def counts(self) -> dict[str, int]:
        schema = self._settings.migration_schema
        try:
            with get_engine(self._settings).connect() as conn:
                rows = conn.execute(
                    text(
                        f"""SELECT entity, COUNT(*) AS n FROM {schema}.crosswalk
                            GROUP BY entity ORDER BY entity"""
                    )
                ).all()
        except SQLAlchemyError as exc:
            raise DatabaseError(f"counting the crosswalk failed: {exc}") from exc
        return {row.entity: int(row.n) for row in rows}

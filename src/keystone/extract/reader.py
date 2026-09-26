"""Extraction: one iterator per source kind, one interface above them.

Everything downstream -- the mapping engine, the dry-run, the loader -- sees
an iterator of dictionaries and does not know whether they came from a table
or from a cp1252 CSV that Excel produced. Adding a third source kind means
adding a function here and nothing anywhere else.

Two details that are not incidental:

**Server-side cursors.** ``yield_per`` keeps the driver from materialising
the whole table before the first row is mapped. On 17 000 rows it makes no
difference; the point is that it makes no difference on 17 million either.

**The filter is rendered, not bound.** A ``WHERE`` clause cannot be a bound
parameter, so the mapping's ``filter`` string is interpolated. That is the one
place in this codebase where SQL comes from a file rather than from a literal,
and it is why ``SourceSpec`` rejects semicolons, comment markers and
data-modifying keywords before this function ever sees it.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from keystone.config import Settings, get_settings
from keystone.db.engine import get_engine
from keystone.exceptions import DatabaseError, MappingError
from keystone.logging_config import get_logger
from keystone.mapping.models import EntityMapping, SourceSpec

logger = get_logger(__name__)

_CHUNK = 2_000


def _excluded(row: dict[str, Any], spec: SourceSpec) -> bool:
    for column, unwanted in spec.exclude_when.items():
        value = row.get(column)
        if value is not None and str(value).strip().upper() == unwanted.strip().upper():
            return True
    return False


def _resolve_schema(spec: SourceSpec, settings: Settings) -> str:
    """Map the mapping file's logical schema name onto the configured one.

    The file says ``schema: legacy``; the deployment may have called it
    something else. Anything not recognised is refused rather than passed
    through, because an unvalidated identifier is the one thing that must not
    reach the DDL.
    """
    declared = spec.schema_name or "legacy"
    known = {
        "legacy": settings.legacy_schema,
        "migration": settings.migration_schema,
    }
    if declared not in known:
        raise MappingError(f"unknown source schema {declared!r}; expected one of {sorted(known)}")
    return known[declared]


def _iter_sql(mapping: EntityMapping, settings: Settings) -> Iterator[dict[str, Any]]:
    spec = mapping.source
    schema = _resolve_schema(spec, settings)
    where = f"WHERE {spec.filter}" if spec.filter else ""
    query = f"SELECT * FROM {schema}.{spec.table} {where} ORDER BY {spec.key}"
    try:
        with get_engine(settings).connect().execution_options(yield_per=_CHUNK) as conn:
            for row in conn.execute(text(query)).mappings():
                record = dict(row)
                if not _excluded(record, spec):
                    yield record
    except SQLAlchemyError as exc:
        raise DatabaseError(f"extraction of {mapping.entity} failed: {exc}") from exc


def _csv_paths(spec: SourceSpec, settings: Settings) -> list[Path]:
    assert spec.path_glob is not None
    paths = sorted(settings.data_dir.glob(spec.path_glob))
    if not paths:
        raise MappingError(
            f"no file matches {spec.path_glob!r} under {settings.data_dir}",
            path=spec.path_glob,
        )
    return paths


def _iter_csv(mapping: EntityMapping, settings: Settings) -> Iterator[dict[str, Any]]:
    spec = mapping.source
    for path in _csv_paths(spec, settings):
        # errors="replace" rather than a crash: one unreadable byte in a
        # 9 000-row export should cost that value, not the file. The record
        # still goes through validation, so a mangled required field is
        # rejected and counted rather than silently imported.
        with path.open(encoding=spec.encoding, errors="replace", newline="") as handle:
            reader = csv.DictReader(handle, delimiter=spec.delimiter)
            if reader.fieldnames is None:
                raise MappingError(f"{path.name} has no header row", path=str(path))
            if spec.key not in reader.fieldnames:
                raise MappingError(
                    f"{path.name} has no column {spec.key!r}; found {', '.join(reader.fieldnames)}",
                    path=str(path),
                )
            for row in reader:
                record = {k: v for k, v in row.items() if k is not None}
                if not _excluded(record, spec):
                    yield record


def iter_source_rows(
    mapping: EntityMapping, settings: Settings | None = None
) -> Iterator[dict[str, Any]]:
    """Yield the source rows for one entity, already filtered."""
    settings = settings or get_settings()
    if mapping.source.kind == "sql":
        yield from _iter_sql(mapping, settings)
    else:
        yield from _iter_csv(mapping, settings)


def count_source_rows(mapping: EntityMapping, settings: Settings | None = None) -> int:
    """How many rows the extraction will yield.

    For SQL this is a COUNT; for CSV there is no way around reading the files,
    which is why the dry-run reports progress rather than a percentage.
    """
    settings = settings or get_settings()
    spec = mapping.source
    if spec.kind == "sql":
        schema = _resolve_schema(spec, settings)
        where = f"WHERE {spec.filter}" if spec.filter else ""
        query = f"SELECT COUNT(*) FROM {schema}.{spec.table} {where}"
        try:
            with get_engine(settings).connect() as conn:
                total = int(conn.execute(text(query)).scalar_one())
        except SQLAlchemyError as exc:
            raise DatabaseError(f"counting {mapping.entity} failed: {exc}") from exc
        if not spec.exclude_when:
            return total
    return sum(1 for _ in iter_source_rows(mapping, settings))

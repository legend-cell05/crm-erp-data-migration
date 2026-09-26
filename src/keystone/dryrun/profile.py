"""Profiling the source before anything is transformed.

A migration plan built without looking at the data is a guess. Profiling
answers the questions that decide whether a mapping is right: how many rows
have no country at all, how many distinct spellings of it exist, what a
typical value looks like.

It runs before the mapping, deliberately. The mapping's rejections tell you
what the rules refuse; the profile tells you what is actually there, which is
how you find out that a rule is refusing the wrong thing.
"""

from __future__ import annotations

import csv
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from keystone.config import Settings, get_settings
from keystone.db.engine import get_engine
from keystone.exceptions import DatabaseError
from keystone.extract.reader import _csv_paths, _resolve_schema
from keystone.logging_config import get_logger
from keystone.mapping.models import EntityMapping

logger = get_logger(__name__)

_MAX_SAMPLES = 5


@dataclass(frozen=True)
class ColumnProfile:
    entity: str
    column: str
    row_count: int
    null_count: int
    blank_count: int
    distinct_count: int
    samples: list[str]

    @property
    def empty_pct(self) -> float:
        if not self.row_count:
            return 0.0
        return 100.0 * (self.null_count + self.blank_count) / self.row_count

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "column": self.column,
            "row_count": self.row_count,
            "null_count": self.null_count,
            "blank_count": self.blank_count,
            "empty_pct": round(self.empty_pct, 2),
            "distinct_count": self.distinct_count,
            "samples": self.samples,
        }


def _source_columns(mapping: EntityMapping) -> list[str]:
    """Only the columns the mapping actually reads.

    Profiling every column of a 40-column legacy table produces a report
    nobody reads. The ones that matter are the ones a rule depends on.
    """
    return sorted({f.source for f in mapping.fields if f.source})


def _profile_sql(mapping: EntityMapping, settings: Settings) -> list[ColumnProfile]:
    schema = _resolve_schema(mapping.source, settings)
    table = mapping.source.table
    where = f"WHERE {mapping.source.filter}" if mapping.source.filter else ""
    columns = _source_columns(mapping)

    # One pass for the aggregates rather than one query per column: on a wide
    # legacy table that is the difference between a profile that takes a
    # second and one that takes a minute.
    parts = ["COUNT(*) AS row_count"]
    for index, column in enumerate(columns):
        parts.append(f"COUNT(*) FILTER (WHERE {column} IS NULL) AS null_{index}")
        parts.append(f"COUNT(*) FILTER (WHERE TRIM(CAST({column} AS TEXT)) = '') AS blank_{index}")
        parts.append(f"COUNT(DISTINCT {column}) AS distinct_{index}")
    query = f"SELECT {', '.join(parts)} FROM {schema}.{table} {where}"

    try:
        with get_engine(settings).connect() as conn:
            row = conn.execute(text(query)).mappings().one()
            profiles = []
            for index, column in enumerate(columns):
                samples: Sequence[Any] = (
                    conn.execute(
                        text(
                            f"""SELECT DISTINCT CAST({column} AS TEXT) AS value
                            FROM {schema}.{table} {where}
                            {"AND" if where else "WHERE"} {column} IS NOT NULL
                            ORDER BY 1 LIMIT :limit"""
                        ),
                        {"limit": _MAX_SAMPLES},
                    )
                    .scalars()
                    .all()
                )
                profiles.append(
                    ColumnProfile(
                        entity=mapping.entity,
                        column=column,
                        row_count=int(row["row_count"]),
                        null_count=int(row[f"null_{index}"]),
                        blank_count=int(row[f"blank_{index}"]),
                        distinct_count=int(row[f"distinct_{index}"]),
                        samples=[str(value)[:60] for value in samples],
                    )
                )
    except SQLAlchemyError as exc:
        raise DatabaseError(f"profiling {mapping.entity} failed: {exc}") from exc
    return profiles


def _profile_csv(mapping: EntityMapping, settings: Settings) -> list[ColumnProfile]:
    columns = _source_columns(mapping)
    counters: dict[str, Counter[str]] = {column: Counter() for column in columns}
    nulls = dict.fromkeys(columns, 0)
    blanks = dict.fromkeys(columns, 0)
    rows = 0

    for path in _csv_paths(mapping.source, settings):
        with path.open(encoding=mapping.source.encoding, errors="replace", newline="") as handle:
            reader = csv.DictReader(handle, delimiter=mapping.source.delimiter)
            for record in reader:
                excluded = any(
                    str(record.get(column, "")).strip().upper() == value.strip().upper()
                    for column, value in mapping.source.exclude_when.items()
                )
                if excluded:
                    continue
                rows += 1
                for column in columns:
                    value = record.get(column)
                    if value is None:
                        nulls[column] += 1
                    elif str(value).strip() == "":
                        blanks[column] += 1
                    else:
                        counters[column][str(value)] += 1

    return [
        ColumnProfile(
            entity=mapping.entity,
            column=column,
            row_count=rows,
            null_count=nulls[column],
            blank_count=blanks[column],
            distinct_count=len(counters[column]),
            samples=[value[:60] for value, _ in counters[column].most_common(_MAX_SAMPLES)],
        )
        for column in columns
    ]


def profile_entity(mapping: EntityMapping, settings: Settings | None = None) -> list[ColumnProfile]:
    settings = settings or get_settings()
    if mapping.source.kind == "sql":
        return _profile_sql(mapping, settings)
    return _profile_csv(mapping, settings)


def persist_profiles(
    profiles: list[ColumnProfile], run_id: str, settings: Settings | None = None
) -> int:
    settings = settings or get_settings()
    if not profiles:
        return 0
    schema = settings.migration_schema
    rows = [
        {
            "run_id": run_id,
            "entity": profile.entity,
            "column_name": profile.column,
            "row_count": profile.row_count,
            "null_count": profile.null_count,
            "blank_count": profile.blank_count,
            "distinct_count": profile.distinct_count,
            "samples": profile.samples,
        }
        for profile in profiles
    ]
    try:
        with get_engine(settings).begin() as conn:
            conn.execute(
                text(
                    f"""INSERT INTO {schema}.profile
                            (run_id, entity, column_name, row_count, null_count,
                             blank_count, distinct_count, sample_values)
                        VALUES (CAST(:run_id AS UUID), :entity, :column_name, :row_count,
                                :null_count, :blank_count, :distinct_count, :samples)
                        ON CONFLICT (run_id, entity, column_name) DO NOTHING"""
                ),
                rows,
            )
    except SQLAlchemyError as exc:
        raise DatabaseError(f"persisting profiles failed: {exc}") from exc
    return len(rows)

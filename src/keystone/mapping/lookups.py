"""Lookup tables: the business's own translations, kept out of the code.

A lookup is a two-column CSV under ``mappings/lookups/``. Keeping them as
files rather than as dictionaries in Python is what lets the sales operations
team own the country list, and what makes "who added Monaco, and when?" a
question ``git log`` answers.

Matching is deliberately forgiving on the way in -- trimmed, upper-cased,
accents removed -- because the source contains 'FR', ' fr ' and 'France' for
the same country. It is deliberately unforgiving on a miss: an unknown value
is reported, not guessed. 'Frnace' is a typo that a human must decide about.
"""

from __future__ import annotations

import csv
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from keystone.exceptions import MappingError
from keystone.logging_config import get_logger

logger = get_logger(__name__)

_MISSING = object()


def normalise_key(value: object) -> str:
    """The comparison form: trimmed, accent-free, upper case."""
    text = unicodedata.normalize("NFKD", str(value).strip())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(text.upper().split())


@dataclass(frozen=True)
class LookupTable:
    """One loaded translation table."""

    name: str
    path: Path
    entries: dict[str, str]

    def get(self, value: object) -> object:
        return self.entries.get(normalise_key(value), _MISSING)

    def contains(self, value: object) -> bool:
        return normalise_key(value) in self.entries

    def __len__(self) -> int:
        return len(self.entries)


def load_lookup(name: str, directory: Path) -> LookupTable:
    """Read one lookup table from ``<directory>/<name>.csv``."""
    path = directory / f"{name}.csv"
    if not path.is_file():
        raise MappingError(f"lookup table {name!r} not found", path=str(path))

    entries: dict[str, str] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        if (
            reader.fieldnames is None
            or "source" not in reader.fieldnames
            or "target" not in reader.fieldnames
        ):
            raise MappingError("a lookup table needs `source` and `target` columns", path=str(path))
        for line_number, row in enumerate(reader, start=2):
            source = row.get("source")
            target = row.get("target")
            if source is None or source.strip() == "":
                continue
            if target is None:
                raise MappingError(f"line {line_number}: missing target", path=str(path))
            key = normalise_key(source)
            if key in entries and entries[key] != target:
                raise MappingError(
                    f"line {line_number}: {source!r} is mapped to both "
                    f"{entries[key]!r} and {target!r}",
                    path=str(path),
                )
            entries[key] = target

    logger.debug("lookup loaded", extra={"table": name, "entries": len(entries)})
    return LookupTable(name=name, path=path, entries=entries)


@lru_cache(maxsize=32)
def _cached(name: str, directory: str) -> LookupTable:
    return load_lookup(name, Path(directory))


def get_lookup(name: str, directory: Path) -> LookupTable:
    """Cached access, keyed on the directory so tests can use their own."""
    return _cached(name, str(directory))


def clear_lookup_cache() -> None:
    _cached.cache_clear()


MISSING = _MISSING

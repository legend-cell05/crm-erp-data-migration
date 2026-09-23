"""Loading the mapping set from disk.

A mapping *set* is every ``*.yaml`` under the mapping directory, plus two
things that only exist once they are considered together:

**A load order.** Contacts reference accounts, so accounts must be migrated
first. That order is derived from the ``depends_on`` declarations rather than
from the alphabet or from a hard-coded list, and a cycle is an error rather
than an infinite loop.

**A fingerprint.** The version of a single file says what its author intended;
the fingerprint says what is actually on disk, across every file and lookup
table. It is what the dry-run gate compares, because "there was a dry-run for
version 1.3.0" is worth nothing if someone edited 1.3.0 afterwards.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from keystone.config import Settings, get_settings
from keystone.exceptions import MappingError
from keystone.logging_config import get_logger
from keystone.mapping.models import EntityMapping

logger = get_logger(__name__)


@dataclass(frozen=True)
class MappingSet:
    """Every entity mapping, in the order they must be migrated."""

    entities: dict[str, EntityMapping]
    order: tuple[str, ...]
    fingerprint: str
    lookup_dir: Path

    @property
    def version(self) -> str:
        """Human-readable version: highest file version plus the fingerprint.

        Both halves are needed. The semantic version is what a person quotes
        in a change request; the fingerprint is what the gate actually
        compares, and it changes when a lookup table changes even though no
        file version did.
        """
        highest = max(
            (m.version for m in self.entities.values()),
            default="0.0.0",
            key=lambda v: tuple(int(part) for part in v.split(".")),
        )
        return f"{highest}+{self.fingerprint[:12]}"

    def __getitem__(self, entity: str) -> EntityMapping:
        try:
            return self.entities[entity]
        except KeyError:
            raise MappingError(f"no mapping for entity {entity!r}") from None

    def __iter__(self):  # type: ignore[no-untyped-def]
        return (self.entities[name] for name in self.order)

    def __len__(self) -> int:
        return len(self.entities)


def _read_one(path: Path) -> EntityMapping:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise MappingError(f"invalid YAML: {exc}", path=str(path)) from exc
    if not isinstance(raw, dict):
        raise MappingError("a mapping file must be a YAML mapping", path=str(path))
    try:
        return EntityMapping.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise MappingError(details, path=str(path)) from exc


def _topological_order(entities: dict[str, EntityMapping]) -> tuple[str, ...]:
    """Dependencies first; ties broken alphabetically so the order is stable."""
    resolved: list[str] = []
    permanent: set[str] = set()
    temporary: set[str] = set()

    def visit(name: str, trail: tuple[str, ...]) -> None:
        if name in permanent:
            return
        if name in temporary:
            cycle = " -> ".join([*trail, name])
            raise MappingError(f"circular dependency between mappings: {cycle}")
        if name not in entities:
            raise MappingError(
                f"{trail[-1] if trail else '?'} depends on {name!r}, which has no mapping"
            )
        temporary.add(name)
        for dependency in sorted(entities[name].depends_on):
            visit(dependency, (*trail, name))
        temporary.discard(name)
        permanent.add(name)
        resolved.append(name)

    for name in sorted(entities):
        visit(name, ())
    return tuple(resolved)


def _fingerprint(paths: list[Path], lookup_dir: Path) -> str:
    """SHA-256 over every mapping file and lookup table, content and name.

    File names are hashed too: renaming ``account.yaml`` to ``accounts.yaml``
    changes what runs, so it must change the fingerprint.
    """
    digest = hashlib.sha256()
    for path in sorted(paths) + sorted(lookup_dir.glob("*.csv")):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def load_mapping_set(
    settings: Settings | None = None, *, directory: Path | None = None
) -> MappingSet:
    """Read, validate and order every mapping."""
    settings = settings or get_settings()
    directory = directory or settings.mapping_dir
    if not directory.is_dir():
        raise MappingError(f"mapping directory not found: {directory}")

    paths = sorted(p for p in directory.glob("*.yaml") if p.is_file())
    if not paths:
        raise MappingError(f"no mapping files under {directory}")

    entities: dict[str, EntityMapping] = {}
    for path in paths:
        mapping = _read_one(path)
        if mapping.entity in entities:
            raise MappingError(f"entity {mapping.entity!r} is defined twice", path=str(path))
        if path.stem != mapping.entity:
            raise MappingError(
                f"file is named {path.stem!r} but declares entity {mapping.entity!r}",
                path=str(path),
            )
        entities[mapping.entity] = mapping

    order = _topological_order(entities)
    lookup_dir = directory / "lookups"
    fingerprint = _fingerprint(paths, lookup_dir)

    logger.info(
        "mapping set loaded",
        extra={"entities": len(entities), "order": list(order), "fingerprint": fingerprint[:12]},
    )
    return MappingSet(
        entities=entities, order=order, fingerprint=fingerprint, lookup_dir=lookup_dir
    )

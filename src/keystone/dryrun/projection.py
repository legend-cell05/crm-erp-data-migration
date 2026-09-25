"""Resolving references against records that do not exist yet.

The subtlety that makes a dry-run worth having, and the one a naive
implementation gets wrong.

On a first run the crosswalk is empty. A contact whose account is about to be
created in the very same run has no crosswalk entry, so a plain resolver
reports "account C-000123 has not been migrated" -- and the dry-run predicts
that every contact, opportunity and activity will be rejected. The report is
alarming, useless, and wrong.

So during a dry-run the resolver answers from two sources: what the crosswalk
already holds, and what earlier entities in this same run have *decided to
create*. Entities are planned in dependency order, so by the time contacts are
planned the accounts are known.

The projected ids are deliberately fake and obviously so (``planned:...``).
Nothing writes them anywhere: their only job is to let a reference resolve so
that the rest of the record can be evaluated.
"""

from __future__ import annotations

from keystone.load.crosswalk import Action, CrosswalkStore
from keystone.mapping.engine import MappedRecord


class ProjectedCrosswalk:
    """A crosswalk view that also knows what this run intends to create."""

    def __init__(self, crosswalk: CrosswalkStore) -> None:
        self._crosswalk = crosswalk
        self._projected: dict[str, set[str]] = {}

    def resolve(self, entity: str, source_id: str) -> str | None:
        real = self._crosswalk.resolve(entity, source_id)
        if real is not None:
            return real
        if source_id in self._projected.get(entity, set()):
            return f"planned:{entity}:{source_id}"
        return None

    def plan(self, record: MappedRecord) -> Action:
        return self._crosswalk.plan(record)

    def project(self, entity: str, source_id: str) -> None:
        """Remember that this run would produce this record."""
        self._projected.setdefault(entity, set()).add(source_id)

    def projected_count(self, entity: str) -> int:
        return len(self._projected.get(entity, set()))

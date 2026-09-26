"""Administrative endpoints: health, statistics, reset.

Not part of the simulated CRM's contract -- a real Atlas Cloud would not let a
client empty it -- but the migration's own tests need a way to start from a
known state, and pretending otherwise would mean tests that depend on the
order they run in.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from keystone import __version__
from keystone.target.dependencies import get_store, require_api_key
from keystone.target.store import AtlasStore

router = APIRouter(tags=["admin"])


@router.get("/health", summary="Liveness")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "atlas-cloud-simulator", "version": __version__}


@router.get("/admin/stats", summary="Row counts per entity set")
def stats(store: Annotated[AtlasStore, Depends(get_store)]) -> dict[str, Any]:
    counts = store.counts()
    return {"counts": counts, "total": sum(counts.values()), "writes_applied": store.writes}


@router.post("/admin/reset", summary="Empty the target", dependencies=[Depends(require_api_key)])
def reset(store: Annotated[AtlasStore, Depends(get_store)]) -> dict[str, Any]:
    before = sum(store.counts().values())
    store.reset()
    return {"deleted": before, "counts": store.counts()}

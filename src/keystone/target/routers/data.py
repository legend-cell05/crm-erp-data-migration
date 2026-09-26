"""The data endpoints: batch upsert, read, count.

Shaped after a Dataverse-style Web API rather than a REST tutorial, because
the awkward parts are the point:

* writes go through ``$batch`` and return **one result per record**, so a
  client must read the array and cannot infer success from the status code;
* the batch limit is enforced and is lower than a client would guess;
* rate limiting answers 429 with ``Retry-After`` and expects to be obeyed;
* the occasional 503 arrives mid-migration, as it does in production.
"""

from __future__ import annotations

import random
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from keystone.config import get_settings
from keystone.logging_config import get_logger
from keystone.target.dependencies import get_rng, get_store, require_api_key
from keystone.target.schemas import LOAD_ORDER, MAX_BATCH_SIZE, SCHEMAS
from keystone.target.store import AtlasStore

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["data"], dependencies=[Depends(require_api_key)])


class BatchRequest(BaseModel):
    """A batch of records to upsert on the entity set's alternate key."""

    records: list[dict[str, Any]] = Field(min_length=1)


def _known_entity_set(entity_set: str) -> str:
    if entity_set not in SCHEMAS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "UNKNOWN_ENTITY_SET",
                "message": f"{entity_set!r} does not exist",
                "known": sorted(SCHEMAS),
            },
        )
    return entity_set


def _maybe_fail(rng: random.Random) -> None:
    """Inject the failures a real API produces.

    Rate limiting first, because it is the one a client is most likely to get
    wrong: a 429 carries advice, and ignoring it is how a client goes from
    throttled to blocked.
    """
    settings = get_settings()
    if rng.random() < settings.target_rate_limit_rate:
        retry_after = rng.choice([1, 1, 2, 3])
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "RATE_LIMITED", "message": "too many requests"},
            headers={"Retry-After": str(retry_after)},
        )
    if rng.random() < settings.target_fault_rate:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "SERVICE_UNAVAILABLE", "message": "upstream temporarily unavailable"},
        )


# Declared before the "/{entity_set}" routes below: FastAPI matches in
# declaration order, and a path parameter happily swallows "$metadata".
@router.get("/$metadata", summary="Entity sets and their rules")
def metadata() -> dict[str, Any]:
    """What a client needs to know before writing anything.

    Published rather than documented in a PDF, so the load order and the batch
    limit cannot drift apart from what the server actually enforces.
    """
    return {
        "load_order": list(LOAD_ORDER),
        "max_batch_size": MAX_BATCH_SIZE,
        "entity_sets": {
            name: {
                "alternate_key": schema.alternate_key,
                "fields": {
                    field_name: {
                        "required": rule.required,
                        "max_length": rule.max_length,
                        "allowed": sorted(rule.allowed) if rule.allowed else None,
                        "references": rule.references,
                    }
                    for field_name, rule in schema.fields.items()
                },
            }
            for name, schema in SCHEMAS.items()
        },
    }


@router.post("/{entity_set}/$batch", summary="Upsert a batch of records")
def upsert_batch(
    entity_set: str,
    request: BatchRequest,
    response: Response,
    store: Annotated[AtlasStore, Depends(get_store)],
    rng: Annotated[random.Random, Depends(get_rng)],
) -> dict[str, Any]:
    _known_entity_set(entity_set)

    if len(request.records) > MAX_BATCH_SIZE:
        # 413, not 429: sending 400 records will never work, however long the
        # client waits, so this must not be retried.
        raise HTTPException(
            # 413. Starlette renamed the constant; the status code did not change.
            status_code=413,
            detail={
                "code": "BATCH_TOO_LARGE",
                "message": f"a batch may contain at most {MAX_BATCH_SIZE} records",
                "max_batch_size": MAX_BATCH_SIZE,
            },
        )

    _maybe_fail(rng)

    results = store.upsert_batch(entity_set, request.records)
    failed = sum(1 for result in results if result.status == "failed")
    # 207: some succeeded and some did not, and the client must look at each.
    response.status_code = status.HTTP_207_MULTI_STATUS if failed else status.HTTP_200_OK

    return {
        "entity_set": entity_set,
        "submitted": len(results),
        "failed": failed,
        "results": [result.as_dict() for result in results],
    }


@router.get("/{entity_set}/$count", summary="Row count")
def count(entity_set: str, store: Annotated[AtlasStore, Depends(get_store)]) -> dict[str, Any]:
    _known_entity_set(entity_set)
    return {"entity_set": entity_set, "count": store.count(entity_set)}


@router.get("/{entity_set}/$checksum", summary="Sum of a numeric field")
def checksum(
    entity_set: str,
    store: Annotated[AtlasStore, Depends(get_store)],
    field: Annotated[str, Query(description="Field to total")],
) -> dict[str, Any]:
    _known_entity_set(entity_set)
    return {"entity_set": entity_set, "field": field, "total": store.checksum(entity_set, field)}


@router.get("/{entity_set}", summary="List records")
def list_records(
    entity_set: str,
    store: Annotated[AtlasStore, Depends(get_store)],
    top: Annotated[int, Query(ge=1, le=1000, alias="$top")] = 100,
    skip: Annotated[int, Query(ge=0, alias="$skip")] = 0,
) -> dict[str, Any]:
    _known_entity_set(entity_set)
    records = store.page(entity_set, skip=skip, top=top)
    return {
        "entity_set": entity_set,
        "count": store.count(entity_set),
        "skip": skip,
        "top": top,
        "value": records,
    }


@router.get("/{entity_set}/by-key/{alternate_key}", summary="Fetch by alternate key")
def get_by_key(
    entity_set: str, alternate_key: str, store: Annotated[AtlasStore, Depends(get_store)]
) -> dict[str, Any]:
    _known_entity_set(entity_set)
    record = store.get_by_alternate_key(entity_set, alternate_key)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": f"no record with key {alternate_key!r}"},
        )
    return record

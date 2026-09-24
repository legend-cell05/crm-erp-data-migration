"""Shared state and authentication for the simulated target."""

from __future__ import annotations

import random

from fastapi import Header, HTTPException, status

from keystone.config import get_settings
from keystone.target.store import AtlasStore

_STORE = AtlasStore()
_RNG = random.Random(4242)


def get_store() -> AtlasStore:
    return _STORE


def get_rng() -> random.Random:
    return _RNG


async def require_api_key(x_api_key: str | None = Header(default=None)) -> str:
    """Every data endpoint requires a key.

    The simulator checks it because a migration client that was only ever
    tested against an unauthenticated endpoint has never exercised the code
    path where the key is missing, wrong, or expired halfway through a load.
    """
    expected = get_settings().target_api_key.get_secret_value()
    if x_api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "MISSING_API_KEY", "message": "X-API-Key header is required"},
        )
    if x_api_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_API_KEY", "message": "the API key is not recognised"},
        )
    return x_api_key

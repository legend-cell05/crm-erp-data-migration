"""HTTP client for the Atlas Cloud API.

The only module that knows the target speaks HTTP. Everything above it works
with :class:`BatchOutcome` objects and never sees a status code, which is what
allows the loader to be tested against a fake target and the client to be
tested against a fake transport.

The translation it performs is the important part: HTTP failures become the
exception taxonomy in :mod:`keystone.exceptions`, and the transient/permanent
split made here is what the retry policy acts on. Getting that split wrong in
one direction retries a 413 forever; in the other, it gives up on a 503 that
would have cleared in two seconds.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import httpx

from keystone.config import Settings, get_settings
from keystone.exceptions import PermanentTargetError, TransientTargetError
from keystone.load.retry import RetryPolicy, with_retry
from keystone.logging_config import get_logger

logger = get_logger(__name__)

_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class RecordOutcome:
    """What the target did with one record."""

    index: int
    alternate_key: str
    status: str
    target_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    field: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status in {"created", "updated", "unchanged"}


@dataclass(frozen=True)
class BatchOutcome:
    """What the target did with one batch."""

    entity_set: str
    submitted: int
    outcomes: list[RecordOutcome]
    retries: int = 0

    @property
    def created(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "created")

    @property
    def updated(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "updated")

    @property
    def unchanged(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "unchanged")

    @property
    def failed(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "failed")


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None  # HTTP-date form; the backoff formula takes over


def _detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
    if isinstance(detail, dict):
        return f"{detail.get('code', '?')}: {detail.get('message', '')}".strip()
    return str(detail)[:200]


class AtlasClient:
    """Typed access to the target API, with the retry policy applied."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        settings: Settings | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._client = client
        self._settings = settings or get_settings()
        self._rng = rng
        self._policy = RetryPolicy(
            max_attempts=self._settings.retry_max_attempts,
            base_delay_seconds=self._settings.retry_base_delay_seconds,
            max_delay_seconds=self._settings.retry_max_delay_seconds,
        )
        self.retries = 0

    # -- Plumbing -----------------------------------------------------------

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise TransientTargetError(f"timeout calling {url}") from exc
        except httpx.TransportError as exc:
            raise TransientTargetError(f"transport error calling {url}: {exc}") from exc

        if response.status_code in _RETRYABLE_STATUSES:
            raise TransientTargetError(
                f"{response.status_code} from {url}: {_detail(response)}",
                status_code=response.status_code,
                retry_after_seconds=_retry_after(response),
            )
        if response.status_code >= 400 and response.status_code not in {207}:
            raise PermanentTargetError(
                f"{response.status_code} from {url}: {_detail(response)}",
                status_code=response.status_code,
            )
        return response

    def _with_retry(self, description: str, method: str, url: str, **kwargs: Any) -> httpx.Response:
        def count_retry(attempt: int, delay: float, exc: TransientTargetError) -> None:
            self.retries += 1

        return with_retry(
            lambda: self._request(method, url, **kwargs),
            policy=self._policy,
            description=description,
            rng=self._rng,
            on_retry=count_retry,
        )

    # -- API ----------------------------------------------------------------

    def metadata(self) -> dict[str, Any]:
        response = self._with_retry("metadata", "GET", "/api/v1/$metadata")
        return dict(response.json())

    def count(self, entity_set: str) -> int:
        response = self._with_retry(f"count {entity_set}", "GET", f"/api/v1/{entity_set}/$count")
        return int(response.json()["count"])

    def checksum(self, entity_set: str, field: str) -> str | None:
        response = self._with_retry(
            f"checksum {entity_set}.{field}",
            "GET",
            f"/api/v1/{entity_set}/$checksum",
            params={"field": field},
        )
        total = response.json().get("total")
        return None if total is None else str(total)

    def get_by_alternate_key(self, entity_set: str, key: str) -> dict[str, Any] | None:
        try:
            response = self._with_retry(
                f"get {entity_set}/{key}", "GET", f"/api/v1/{entity_set}/by-key/{key}"
            )
        except PermanentTargetError as exc:
            if exc.status_code == 404:
                return None
            raise
        return dict(response.json())

    def upsert_batch(self, entity_set: str, records: list[dict[str, Any]]) -> BatchOutcome:
        """Send one batch and translate the per-record results."""
        before = self.retries
        response = self._with_retry(
            f"upsert {entity_set} x{len(records)}",
            "POST",
            f"/api/v1/{entity_set}/$batch",
            json={"records": records},
        )
        payload = response.json()
        outcomes = [
            RecordOutcome(
                index=int(item["index"]),
                alternate_key=str(item.get("alternate_key", "")),
                status=str(item["status"]),
                target_id=item.get("id"),
                error_code=(item.get("error") or {}).get("code"),
                error_message=(item.get("error") or {}).get("message"),
                field=(item.get("error") or {}).get("field"),
            )
            for item in payload.get("results", [])
        ]
        return BatchOutcome(
            entity_set=entity_set,
            submitted=int(payload.get("submitted", len(records))),
            outcomes=outcomes,
            retries=self.retries - before,
        )


@contextmanager
def atlas_client(settings: Settings | None = None) -> Iterator[AtlasClient]:
    """Open a configured client for the duration of a run."""
    settings = settings or get_settings()
    headers = {
        "X-API-Key": settings.target_api_key.get_secret_value(),
        "Content-Type": "application/json",
        "User-Agent": "keystone-migration/1.0",
    }
    with httpx.Client(
        base_url=settings.target_base_url,
        headers=headers,
        timeout=settings.target_timeout_seconds,
    ) as http:
        yield AtlasClient(http, settings=settings)

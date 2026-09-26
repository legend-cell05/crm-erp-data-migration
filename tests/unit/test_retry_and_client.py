"""The retry policy and the HTTP client's translation of failures."""

from __future__ import annotations

import contextlib
import random

import httpx
import pytest

from keystone.config import Settings
from keystone.exceptions import (
    PermanentTargetError,
    RetryBudgetExhausted,
    TransientTargetError,
)
from keystone.load.client import AtlasClient
from keystone.load.retry import RetryPolicy, compute_delay, with_retry


class TestPolicy:
    def test_refuses_an_incoherent_configuration(self) -> None:
        with pytest.raises(ValueError, match="max_attempts"):
            RetryPolicy(max_attempts=0)
        with pytest.raises(ValueError, match="max_delay_seconds"):
            RetryPolicy(base_delay_seconds=10, max_delay_seconds=1)


class TestBackoff:
    POLICY = RetryPolicy(max_attempts=6, base_delay_seconds=0.5, max_delay_seconds=8.0)

    def test_the_ceiling_doubles_and_then_stops(self) -> None:
        """Full jitter: the delay is a draw from [0, ceiling], so the ceiling
        is what the formula controls and the draw is what avoids collisions."""
        always_one = random.Random()
        always_one.random = lambda: 1.0  # type: ignore[method-assign]
        ceilings = [compute_delay(self.POLICY, attempt, rng=always_one) for attempt in range(1, 7)]
        assert ceilings == [0.5, 1.0, 2.0, 4.0, 8.0, 8.0]

    def test_the_delay_is_never_the_ceiling_itself(self) -> None:
        rng = random.Random(1)
        draws = {compute_delay(self.POLICY, 3, rng=rng) for _ in range(50)}
        assert len(draws) > 40, "every client would otherwise retry at the same moment"
        assert all(0 <= draw <= 2.0 for draw in draws)

    def test_retry_after_wins_over_the_formula(self) -> None:
        assert compute_delay(self.POLICY, 1, retry_after_seconds=3) == 3.0

    def test_retry_after_is_still_capped(self) -> None:
        assert compute_delay(self.POLICY, 1, retry_after_seconds=3600) == 8.0


class TestWithRetry:
    POLICY = RetryPolicy(max_attempts=4, base_delay_seconds=0.1, max_delay_seconds=1.0)

    def test_succeeds_after_transient_failures(self) -> None:
        attempts = {"n": 0}
        slept: list[float] = []

        def operation() -> str:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise TransientTargetError("503", status_code=503)
            return "ok"

        result = with_retry(operation, policy=self.POLICY, description="test", sleeper=slept.append)
        assert result == "ok"
        assert len(slept) == 2

    def test_gives_up_and_says_why(self) -> None:
        def operation() -> str:
            raise TransientTargetError("still 503", status_code=503)

        with pytest.raises(RetryBudgetExhausted, match="after 4 attempts"):
            with_retry(operation, policy=self.POLICY, description="test", sleeper=lambda _: None)

    def test_a_permanent_error_is_not_retried(self) -> None:
        attempts = {"n": 0}

        def operation() -> str:
            attempts["n"] += 1
            raise PermanentTargetError("400", status_code=400)

        with pytest.raises(PermanentTargetError):
            with_retry(operation, policy=self.POLICY, description="test", sleeper=lambda _: None)
        assert attempts["n"] == 1, "retrying a 400 spends the budget and delays the report"


class TestClientTranslation:
    """HTTP status codes become the exception taxonomy the retry acts on."""

    def _client(self, handler) -> AtlasClient:  # type: ignore[no-untyped-def]
        transport = httpx.MockTransport(handler)
        http = httpx.Client(transport=transport, base_url="http://target.invalid")
        settings = Settings(retry_max_attempts=2, retry_base_delay_seconds=0.01)
        return AtlasClient(http, settings=settings)

    @pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
    def test_retryable_statuses_become_transient(self, status: int) -> None:
        client = self._client(lambda request: httpx.Response(status, json={"detail": "x"}))
        with pytest.raises(RetryBudgetExhausted):
            client.count("accounts")

    @pytest.mark.parametrize("status", [400, 401, 403, 413, 422])
    def test_client_errors_become_permanent(self, status: int) -> None:
        client = self._client(lambda request: httpx.Response(status, json={"detail": "x"}))
        with pytest.raises(PermanentTargetError) as caught:
            client.count("accounts")
        assert caught.value.status_code == status

    def test_retry_after_is_read_from_the_header(self) -> None:
        seen: list[float | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "7"}, json={"detail": "slow down"})

        client = self._client(handler)
        with contextlib.suppress(RetryBudgetExhausted):
            client.count("accounts")
        # The header is parsed by the client, not by the policy; asserting it
        # here keeps the two from drifting apart.
        response = httpx.Response(429, headers={"Retry-After": "7"})
        from keystone.load.client import _retry_after

        seen.append(_retry_after(response))
        assert seen == [7.0]

    def test_an_unparseable_retry_after_falls_back_to_the_formula(self) -> None:
        from keystone.load.client import _retry_after

        response = httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
        assert _retry_after(response) is None

    def test_a_timeout_is_transient(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("too slow", request=request)

        client = self._client(handler)
        with pytest.raises(RetryBudgetExhausted):
            client.count("accounts")

    def test_a_batch_result_array_is_translated_record_by_record(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                207,
                json={
                    "entity_set": "accounts",
                    "submitted": 2,
                    "failed": 1,
                    "results": [
                        {"index": 0, "alternate_key": "C-1", "status": "created", "id": "u1"},
                        {
                            "index": 1,
                            "alternate_key": "C-2",
                            "status": "failed",
                            "error": {"code": "INVALID_VALUE", "message": "bad", "field": "status"},
                        },
                    ],
                },
            )

        outcome = self._client(handler).upsert_batch("accounts", [{}, {}])
        assert outcome.created == 1
        assert outcome.failed == 1
        assert outcome.outcomes[1].error_code == "INVALID_VALUE"
        assert outcome.outcomes[1].field == "status"
        assert not outcome.outcomes[1].succeeded

"""Retry policy for the target API.

Two decisions, both of which are usually got wrong.

**What to retry.** Only ``TransientTargetError``: timeouts, connection
resets, 429, 5xx. A 400, a 401 or a 413 will be the same on the tenth attempt,
and retrying them spends the budget that the next genuine outage needs while
delaying the error report by a minute.

**How long to wait.** Exponential, with **full jitter** -- a uniform draw over
the whole interval rather than the interval's end. Every client that failed at
the same moment would otherwise retry at the same moment and collide again;
the uniform draw is the variant AWS measured as best in *Exponential Backoff
and Jitter* (2015). A ``Retry-After`` from the server always wins, because
ignoring one is how a client goes from throttled to blocked.

``sleeper`` and ``rng`` are injected so the tests can assert the delay
sequence against the formula without waiting for it.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from keystone.exceptions import RetryBudgetExhausted, TransientTargetError
from keystone.logging_config import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    """How many attempts, and how long between them."""

    max_attempts: int = 5
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay_seconds <= 0:
            raise ValueError("base_delay_seconds must be positive")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must be >= base_delay_seconds")


def compute_delay(
    policy: RetryPolicy,
    attempt: int,
    *,
    retry_after_seconds: float | None = None,
    rng: random.Random | None = None,
) -> float:
    """Seconds to wait before attempt ``attempt + 1`` (attempts are 1-based)."""
    if retry_after_seconds is not None:
        return min(float(retry_after_seconds), policy.max_delay_seconds)
    ceiling: float = min(policy.base_delay_seconds * (2 ** (attempt - 1)), policy.max_delay_seconds)
    draw: float = (rng or random).random()
    return draw * ceiling


def with_retry(
    operation: Callable[[], T],
    *,
    policy: RetryPolicy,
    description: str,
    sleeper: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    on_retry: Callable[[int, float, TransientTargetError], None] | None = None,
) -> T:
    """Run ``operation``, retrying transient failures.

    Raises ``RetryBudgetExhausted`` when the budget runs out, chained to the
    last failure so the report says what actually went wrong rather than only
    that it kept going wrong.
    """
    last: TransientTargetError | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return operation()
        except TransientTargetError as exc:
            last = exc
            if attempt == policy.max_attempts:
                break
            delay = compute_delay(
                policy, attempt, retry_after_seconds=exc.retry_after_seconds, rng=rng
            )
            logger.warning(
                "transient failure, retrying",
                extra={
                    "operation": description,
                    "attempt": attempt,
                    "delay_seconds": round(delay, 3),
                    "status_code": exc.status_code,
                    "error": str(exc),
                },
            )
            if on_retry is not None:
                on_retry(attempt, delay, exc)
            sleeper(delay)

    raise RetryBudgetExhausted(
        f"{description} failed after {policy.max_attempts} attempts: {last}"
    ) from last

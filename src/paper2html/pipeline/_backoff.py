"""Exponential backoff retry helper for async provider calls.

Design notes
------------
* ``sleep_fn`` is injectable so tests can pass ``asyncio.sleep`` replaced with
  a no-op to avoid real delays.
* The caller wraps the final exception in ``ProviderError``; this module only
  re-raises it so the error type stays clean.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Mapping
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def backoff_retry(
    coro_fn: Callable[[], Awaitable[T]],
    retryable: tuple[type[BaseException], ...],
    *,
    delays: tuple[float, ...] = (2.0, 4.0, 8.0),
    request_timeout_seconds: float = 60.0,
    sleep_fn: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> T:
    """Retry ``coro_fn`` with exponential backoff on ``retryable`` exceptions.

    Args:
        coro_fn: Zero-argument async callable to call and possibly retry.
        retryable: Tuple of exception types that trigger a retry.
        delays: Sleep durations (seconds) between retries.  Length determines
            max retry count — ``len(delays)`` retries after the first failure.
        sleep_fn: Async sleep function (injected in tests to avoid real waits).

    Returns:
        The return value of ``coro_fn`` on success.

    Raises:
        The last exception from ``coro_fn`` if all retries are exhausted.
    """
    last_exc: BaseException | None = None
    for attempt, delay in enumerate([-1.0] + list(delays)):  # first pass has no delay
        if attempt > 0:
            logger.warning(
                "backoff_retry: attempt %d/%d after %.1fs delay",
                attempt,
                len(delays),
                delay,
            )
            guidance = retry_after_seconds(last_exc)
            if guidance is not None:
                delay = max(delay, guidance)
            await sleep_fn(delay)
        try:
            return await asyncio.wait_for(coro_fn(), timeout=request_timeout_seconds)
        except retryable as exc:
            status = getattr(exc, "status_code", None)
            if (
                status is not None
                and status not in (408, 409, 429)
                and not 500 <= status <= 599
            ):
                raise
            last_exc = exc
            logger.warning("backoff_retry: retryable exception: %s", exc)
        except BaseException:
            raise

    assert last_exc is not None
    raise last_exc


def retry_after_seconds(exc: BaseException | None) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if not isinstance(headers, Mapping):
        return None
    try:
        value = float(headers.get("retry-after"))
        return min(30.0, max(0.0, value)) if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def transient_status(status: int | None) -> bool:
    return status in (408, 409, 429) or (status is not None and 500 <= status <= 599)

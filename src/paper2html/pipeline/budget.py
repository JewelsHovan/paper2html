"""Estimated spending admission, reservations and usage accounting.

Built-in prices are unverified and can drift. Text/dimension allowances are
conservative heuristics, not provider tokenization or invoice guarantees.
``check_before_call`` remains a legacy advisory API; pipeline calls MUST use
``complete`` for atomic admission, concurrency and output-bound enforcement.
"""

from __future__ import annotations

import logging
import asyncio
import math
from collections.abc import Awaitable, Callable
from paper2html.pipeline._estimates import input_tokens as estimate_input_tokens
from paper2html.pipeline.providers.base import (
    ProviderInterface,
    ProviderResponse,
    Message,
    ToolDef,
    validate_request_limits,
)
from paper2html.pipeline.errors import ProviderError
from dataclasses import dataclass, field

from paper2html.pipeline.errors import BudgetExceededError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cost table — USD per 1 million tokens (input, output)
# Verify against provider pricing pages listed above before each release.
# ---------------------------------------------------------------------------
COST_TABLE: dict[str, tuple[float, float]] = {
    # (input $/1M, output $/1M)
    "claude-opus-4-8": (15.00, 75.00),
    "claude-haiku-4-5": (0.80, 4.00),
    "gpt-4o": (5.00, 15.00),
    "gpt-4o-mini": (0.15, 0.60),
}

_CHARS_PER_TOKEN: int = 4
_ESTIMATED_OUTPUT_TOKENS: int = 512


@dataclass
class _CallRecord:
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


@dataclass
class BudgetTracker:
    """Accumulates estimated costs and enforces an admission cap.

    Args:
        max_cost_usd: Estimated admission cap in USD. ``None`` means no cap.
    """

    max_cost_usd: float | None = None
    max_concurrency: int = 4
    retry_delays: tuple[float, ...] = (1.0, 2.0, 4.0)
    sleep_fn: Callable[[float], Awaitable[None]] = field(
        default=asyncio.sleep, repr=False
    )
    _semaphore: asyncio.Semaphore | None = field(default=None, init=False, repr=False)
    _reserved: float = field(default=0.0, init=False)
    _halted: bool = field(default=False, init=False)
    _records: list[_CallRecord] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if len(self.retry_delays) > 3 or any(
            not math.isfinite(d) or not 0 <= d <= 30 for d in self.retry_delays
        ):
            raise ValueError(
                "At most three finite retry delays in [0,30] are supported"
            )
        if type(self.max_concurrency) is not int or self.max_concurrency <= 0:
            raise ValueError("max_concurrency must be a positive integer")
        if self.max_cost_usd is not None and (
            not math.isfinite(self.max_cost_usd) or self.max_cost_usd <= 0
        ):
            raise ValueError("max_cost_usd must be finite and positive")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def cost_for_tokens(
        self, model: str, input_tokens: int, output_tokens: int
    ) -> float:
        """Compute USD cost for the given token counts.

        Args:
            model: Model identifier from ``COST_TABLE``.
            input_tokens: Number of input tokens.
            output_tokens: Number of output tokens.

        Returns:
            Estimated cost in USD.  Returns 0.0 for unknown models (with a
            warning logged).
        """
        if model not in COST_TABLE:
            logger.warning(
                "BudgetTracker: unknown model '%s' — treating cost as 0.0", model
            )
            return 0.0
        in_rate, out_rate = COST_TABLE[model]
        return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000

    def check_before_call(self, model: str, estimated_input_chars: int) -> None:
        """Raise ``BudgetExceededError`` if the call would breach the cap.

        Legacy advisory-only API; use complete() for actual admission. It omits
        images/tools/reservations and does not enforce provider output bounds.
        The estimation uses
        ``estimated_input_chars / _CHARS_PER_TOKEN`` as the input-token estimate
        plus ``_ESTIMATED_OUTPUT_TOKENS`` for the output estimate.

        Args:
            model: Model that will be called.
            estimated_input_chars: Character count of the content being sent
                (used as a token estimate heuristic).

        Raises:
            BudgetExceededError: When cumulative cost + projected cost > cap.
        """
        if self.max_cost_usd is None:
            return
        estimated_input_tokens = max(1, estimated_input_chars // _CHARS_PER_TOKEN)
        projected = self.cost_for_tokens(
            model, estimated_input_tokens, _ESTIMATED_OUTPUT_TOKENS
        )
        if self._total_cost + projected > self.max_cost_usd:
            raise BudgetExceededError(
                current_usd=self._total_cost,
                projected_usd=projected,
                limit_usd=self.max_cost_usd,
            )

    def record(self, model: str, input_tokens: int, output_tokens: int) -> None:
        """Record actual token usage from a completed call.

        Args:
            model: Model that was called.
            input_tokens: Actual input tokens consumed.
            output_tokens: Actual output tokens generated.
        """
        cost = self.cost_for_tokens(model, input_tokens, output_tokens)
        self._records.append(
            _CallRecord(
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost,
            )
        )

    def summary(self) -> dict[str, object]:
        """Return a summary dict with totals.

        Returns:
            Dict with keys ``total_input_tokens``, ``total_output_tokens``,
            ``total_cost_usd``, ``call_count``.
        """
        return {
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cost_usd": self.total_cost_usd,
            "call_count": len(self._records),
        }

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def total_cost_usd(self) -> float:
        """Recorded estimated costs plus outstanding/uncertain liabilities."""
        return self._total_cost + self.uncertain_cost_usd

    @property
    def uncertain_cost_usd(self) -> float:
        """Outstanding reservations, including dispatched unknown usage."""
        return self._reserved

    @property
    def total_input_tokens(self) -> int:
        """Cumulative input tokens across all recorded calls."""
        return sum(r.input_tokens for r in self._records)

    @property
    def total_output_tokens(self) -> int:
        """Cumulative output tokens across all recorded calls."""
        return sum(r.output_tokens for r in self._records)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @property
    def _total_cost(self) -> float:
        return sum(r.cost_usd for r in self._records)

    def validate_model(self, provider: ProviderInterface, model: str) -> None:
        if (
            self.max_cost_usd is not None
            and not provider.free_of_charge
            and model not in COST_TABLE
        ):
            raise ValueError(f"Unknown capped pricing for model: {model}")

    async def complete(
        self,
        provider: ProviderInterface,
        *,
        messages: list[Message],
        images: list[bytes],
        tools: list[ToolDef],
        model: str,
        max_output_tokens: int = 8192,
        request_timeout_seconds: float = 60.0,
    ) -> ProviderResponse:
        """Reserve every transport attempt; corrective retries enter afresh.

        429 and explicit rejected 4xx responses are unbilled; 5xx, disconnects
        and timeouts are uncertain and retain liability. Cancellation while
        queued/backing off reserves nothing; dispatched cancellation is uncertain.
        The request deadline starts after acquiring a concurrency slot. Queue
        waiting is cancellable but does not consume the request/retry deadline.
        """
        validate_request_limits(max_output_tokens, request_timeout_seconds)
        self.validate_model(provider, model)
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrency)
        # Mock/free providers have no vision billing policy; real providers must
        # validate image bounds before admission, even with unlimited budgets.
        tokens = (
            0
            if provider.free_of_charge
            else estimate_input_tokens(messages, tools, images, model)
        )
        # Anthropic cache creation can cost 2x input. Reads are conservatively
        # charged at base rate in reconciliation, never double counted as base.
        projected = (
            0.0
            if provider.free_of_charge
            else self.cost_for_tokens(
                model,
                tokens * (2 if model.startswith("claude-") else 1),
                max_output_tokens,
            )
        )
        deadline = request_timeout_seconds * (len(self.retry_delays) + 1) + 30 * len(
            self.retry_delays
        )
        # Keep a slot for the bounded retry sequence; queued sections receive
        # their own complete deadline, regardless of the paper's section count.
        async with self._semaphore, asyncio.timeout(deadline):
            for attempt in range(len(self.retry_delays) + 1):
                try:
                    current = self.total_cost_usd
                    if self.max_cost_usd is not None and (
                        self._halted or current + projected > self.max_cost_usd
                    ):
                        raise BudgetExceededError(current, projected, self.max_cost_usd)
                    self._reserved += projected  # admission and reservation: no await
                    dispatched = False

                    async def dispatch() -> ProviderResponse:
                        nonlocal dispatched
                        dispatched = True
                        return await provider.complete(
                            messages=messages,
                            images=images,
                            tools=tools,
                            model=model,
                            max_output_tokens=max_output_tokens,
                            transport_retries=False,
                            request_timeout_seconds=request_timeout_seconds,
                        )

                    try:
                        response = await asyncio.wait_for(
                            dispatch(), request_timeout_seconds
                        )
                    except BaseException as exc:
                        if not dispatched or (
                            isinstance(exc, ProviderError) and exc.known_unbilled
                        ):
                            self._reserved -= projected
                        raise
                    total_input = (
                        response.input_tokens
                        + response.cache_read_input_tokens
                        + response.cache_creation_input_tokens
                    )
                    cost = (
                        0.0
                        if provider.free_of_charge
                        else self.cost_for_tokens(
                            model,
                            total_input + response.cache_creation_input_tokens,
                            response.output_tokens,
                        )
                    )
                    self._reserved -= projected
                    self._records.append(
                        _CallRecord(model, total_input, response.output_tokens, cost)
                    )
                    if (
                        self.max_cost_usd is not None
                        and self.total_cost_usd > self.max_cost_usd
                    ):
                        self._halted = True
                        raise BudgetExceededError(
                            self._total_cost, self._reserved, self.max_cost_usd
                        )
                    return response
                except (ProviderError, TimeoutError) as exc:
                    retryable = isinstance(exc, TimeoutError) or exc.retryable
                    if not retryable or attempt == len(self.retry_delays):
                        raise
                    guidance = (
                        exc.retry_after if isinstance(exc, ProviderError) else None
                    )
                    delay = self.retry_delays[attempt]
                    if guidance is not None and math.isfinite(guidance):
                        delay = max(delay, min(30.0, max(0.0, guidance)))
                    await self.sleep_fn(delay)
        raise AssertionError("retry loop exhausted")  # pragma: no cover

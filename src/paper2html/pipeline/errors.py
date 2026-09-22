"""Pipeline-specific exceptions.

All exceptions inherit from ``PipelineError`` so callers can catch the whole
family with a single ``except PipelineError``.
"""

from __future__ import annotations


class PipelineError(Exception):
    """Base class for all pipeline errors."""


class ProviderError(PipelineError):
    """Raised when a provider API call fails after all retries.

    Attributes:
        message: Human-readable description.
        provider: Provider name (``"anthropic"`` / ``"openai"`` / ``"mock"``).
        retries: Number of retries attempted before giving up.
    """

    def __init__(
        self,
        message: str,
        provider: str = "",
        retries: int = 0,
        *,
        retryable: bool = False,
        status_code: int | None = None,
        retry_after: float | None = None,
        known_unbilled: bool = False,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.retries = retries
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after = retry_after
        self.known_unbilled = known_unbilled


class WorkerValidationError(PipelineError):
    """Raised when a worker cannot produce valid JSON after max retries.

    Attributes:
        section_id: The section the worker was processing.
        raw_response: The raw (invalid) response text from the provider.
    """

    def __init__(self, section_id: str, raw_response: str) -> None:
        super().__init__(
            f"Worker for section '{section_id}' produced invalid JSON after"
            " max retries."
        )
        self.section_id = section_id
        self.raw_response = raw_response


class BudgetExceededError(PipelineError):
    """Raised BEFORE a provider call that would exceed ``max_cost_usd``.

    The error is raised before the call so no unaccounted tokens are spent.

    Attributes:
        current_usd: Cumulative cost so far (USD).
        projected_usd: Estimated cost of the would-be call (USD).
        limit_usd: The hard-cap value.
    """

    def __init__(
        self,
        current_usd: float,
        projected_usd: float,
        limit_usd: float,
    ) -> None:
        super().__init__(
            f"Budget cap exceeded: current=${current_usd:.6f} USD, "
            f"projected call=${projected_usd:.6f} USD, "
            f"limit=${limit_usd:.6f} USD."
        )
        self.current_usd = current_usd
        self.projected_usd = projected_usd
        self.limit_usd = limit_usd

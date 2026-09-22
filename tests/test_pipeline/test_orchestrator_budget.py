"""Orchestrator budget-breach propagation tests.

AC-6 invariants:
  - BudgetExceededError must NEVER be converted to SectionFailure.
  - No cache entry must be written when a run aborts via BudgetExceededError.

Two scenarios:
  1. Planning passes; budget breaches at a worker pre-call check (mid-gather).
  2. All workers complete; budget breaches at the synthesis pre-call check.
"""

from __future__ import annotations

import pytest

from paper2html.models.document import NormalizedDocument
from paper2html.pipeline.budget import BudgetTracker
from paper2html.pipeline.errors import BudgetExceededError
from paper2html.pipeline.orchestrator import OrchestratorRunner
from paper2html.pipeline.providers.mock_provider import MockProvider
from paper2html.pipeline.schemas import PipelineConfig

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Scenario 1: budget breaches at worker pre-call check (mid-gather)
# ---------------------------------------------------------------------------


async def test_budget_breach_at_worker_sweep_reraises_and_no_cache_write(
    synthetic_document: NormalizedDocument,
    tmp_cache,
) -> None:
    """BudgetExceededError raised at a worker pre-call check must propagate.

    Planning (call 1) is admitted; the first worker pre-call check (call 2
    onwards) raises. The error must NOT be converted to SectionFailure and no
    cache entry may be written.
    """
    provider = MockProvider()
    config = PipelineConfig()
    budget = BudgetTracker()

    call_count = 0
    original_check = budget.complete

    async def _patched_check(provider, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise BudgetExceededError(
                current_usd=0.001,
                projected_usd=0.999,
                limit_usd=0.001,
            )
        return await original_check(provider, **kwargs)

    budget.complete = _patched_check  # type: ignore[method-assign]

    runner = OrchestratorRunner(provider, config, budget, cache=tmp_cache)

    with pytest.raises(BudgetExceededError):
        await runner.run(synthetic_document)

    assert len(list(tmp_cache)) == 0


# ---------------------------------------------------------------------------
# Scenario 2: budget breaches at synthesis pre-call check
# ---------------------------------------------------------------------------


async def test_budget_breach_at_synthesis_reraises_and_no_cache_write(
    synthetic_document: NormalizedDocument,
    tmp_cache,
) -> None:
    """BudgetExceededError raised at the synthesis pre-call check must propagate.

    Planning (call 1) and all 5 worker calls (calls 2-6) are admitted; the
    synthesis pre-call check (call 7) raises. The error must propagate and no
    cache entry may be written.

    The threshold is 1 (planning) + len(synthetic_document.sections) (workers) + 1
    = 7 for the 5-section synthetic_document fixture.
    """
    provider = MockProvider()
    config = PipelineConfig()
    budget = BudgetTracker()

    # Number of check_before_call invocations before synthesis:
    #   1 planning + 5 workers = 6; synthesis is the 7th.
    synthesis_call_index = 1 + len(synthetic_document.sections) + 1

    call_count = 0
    original_check = budget.complete

    async def _patched_check(provider, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count >= synthesis_call_index:
            raise BudgetExceededError(
                current_usd=0.005,
                projected_usd=0.999,
                limit_usd=0.005,
            )
        return await original_check(provider, **kwargs)

    budget.complete = _patched_check  # type: ignore[method-assign]

    runner = OrchestratorRunner(provider, config, budget, cache=tmp_cache)

    with pytest.raises(BudgetExceededError):
        await runner.run(synthetic_document)

    assert len(list(tmp_cache)) == 0

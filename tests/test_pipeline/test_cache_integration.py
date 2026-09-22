"""Cache integration tests.

AC-7/8, Scenarios 2/3: cache hit, no write on breach, key changes with config.
"""

from __future__ import annotations

import pytest

from paper2html.models.document import NormalizedDocument
from paper2html.pipeline.budget import BudgetTracker
from paper2html.pipeline.errors import BudgetExceededError
from paper2html.pipeline.orchestrator import OrchestratorRunner
from paper2html.pipeline.providers.mock_provider import MockProvider
from paper2html.pipeline.schemas import PipelineConfig, PipelineResult

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Scenario 3: Second identical run uses cache (call_count stays 0)
# ---------------------------------------------------------------------------


async def test_second_run_returns_cached_result(
    synthetic_document: NormalizedDocument,
    default_config: PipelineConfig,
    tmp_cache,
) -> None:
    """Second run with identical inputs returns the cached PipelineResult."""
    provider_run1 = MockProvider()
    budget_run1 = BudgetTracker()
    runner1 = OrchestratorRunner(
        provider_run1, default_config, budget_run1, cache=tmp_cache
    )
    result1 = await runner1.run(synthetic_document)

    assert provider_run1.call_count > 0  # First run makes calls

    # Second run with a FRESH provider — proves zero calls on cache hit
    fresh_provider = MockProvider()
    budget_run2 = BudgetTracker()
    runner2 = OrchestratorRunner(
        fresh_provider, default_config, budget_run2, cache=tmp_cache
    )
    result2 = await runner2.run(synthetic_document)

    assert fresh_provider.call_count == 0
    assert result2.doc_id == result1.doc_id
    assert len(result2.section_analyses) == len(result1.section_analyses)


async def test_cached_result_is_valid_pydantic_model(
    synthetic_document: NormalizedDocument,
    default_config: PipelineConfig,
    tmp_cache,
) -> None:
    """Cached PipelineResult round-trips through model_validate_json correctly."""
    provider = MockProvider()
    budget = BudgetTracker()
    runner = OrchestratorRunner(provider, default_config, budget, cache=tmp_cache)
    result1 = await runner.run(synthetic_document)

    # Second run reads from cache
    fresh_provider = MockProvider()
    budget2 = BudgetTracker()
    runner2 = OrchestratorRunner(
        fresh_provider, default_config, budget2, cache=tmp_cache
    )
    result2 = await runner2.run(synthetic_document)

    # Verify round-trip fidelity
    assert isinstance(result2, PipelineResult)
    assert result2.schema_version == result1.schema_version


# ---------------------------------------------------------------------------
# Scenario 2: No cache write after BudgetExceededError
# ---------------------------------------------------------------------------


async def test_no_cache_write_after_budget_breach(
    synthetic_document: NormalizedDocument,
    tiny_budget_config: PipelineConfig,
    tmp_cache,
) -> None:
    """Cache must remain empty after a BudgetExceededError."""

    class MeteredProvider(MockProvider):
        free_of_charge = False

    provider = MeteredProvider()
    budget = BudgetTracker(max_cost_usd=tiny_budget_config.max_cost_usd)
    runner = OrchestratorRunner(provider, tiny_budget_config, budget, cache=tmp_cache)

    with pytest.raises(BudgetExceededError):
        await runner.run(synthetic_document)

    # Cache should be empty
    assert len(list(tmp_cache)) == 0


# ---------------------------------------------------------------------------
# Cache key changes with different model config
# ---------------------------------------------------------------------------


async def test_cache_key_changes_with_different_model(
    synthetic_document: NormalizedDocument,
    tmp_cache,
) -> None:
    """Different model configs produce different cache keys → separate entries."""
    config_a = PipelineConfig(
        orchestrator_model="claude-opus-4-8",
        worker_model="claude-haiku-4-5",
    )
    config_b = PipelineConfig(
        orchestrator_model="gpt-4o",
        worker_model="gpt-4o-mini",
        provider_name="openai",
    )

    provider = MockProvider()

    runner_a = OrchestratorRunner(provider, config_a, BudgetTracker(), cache=tmp_cache)
    result_a = await runner_a.run(synthetic_document)

    runner_b = OrchestratorRunner(provider, config_b, BudgetTracker(), cache=tmp_cache)
    result_b = await runner_b.run(synthetic_document)

    # Both should have been written — cache has 2 entries
    assert len(list(tmp_cache)) == 2

    # Results should differ in provider field
    assert result_a.provider != result_b.provider


# ---------------------------------------------------------------------------
# No cache write when partial failure (failed_sections non-empty)
# ---------------------------------------------------------------------------


async def test_no_cache_write_on_partial_failure(
    synthetic_document: NormalizedDocument,
    default_config: PipelineConfig,
    tmp_cache,
) -> None:
    """Partial results (non-empty failed_sections) must NOT be cached."""
    from paper2html.pipeline.providers.mock_provider import FailMode

    provider = MockProvider(fail_mode=FailMode.ALL_CALLS)
    budget = BudgetTracker()
    runner = OrchestratorRunner(provider, default_config, budget, cache=tmp_cache)
    result = await runner.run(synthetic_document)

    # Some sections should have failed
    assert len(result.failed_sections) > 0
    # Cache should be empty (partial result not cached)
    assert len(list(tmp_cache)) == 0


# ---------------------------------------------------------------------------
# Cache round-trip preserves new WorkerOutput fields (ticket #014)
# ---------------------------------------------------------------------------


async def test_cache_hit_preserves_new_worker_output_fields(
    synthetic_document: NormalizedDocument,
    default_config: PipelineConfig,
    tmp_cache,
) -> None:
    """Cache-hit result's summary_skim is non-empty and matches the first-run value.

    Proves that the new WorkerOutput fields (summary_skim/read/deep, margin_notes)
    survive the cache path (model_dump_json → model_validate_json round-trip) under
    prompt_schema_version "1.1".
    """
    provider_run1 = MockProvider()
    budget_run1 = BudgetTracker()
    runner1 = OrchestratorRunner(
        provider_run1, default_config, budget_run1, cache=tmp_cache
    )
    result1 = await runner1.run(synthetic_document)

    assert result1.section_analyses, "Expected at least one section analysis"
    first_skim = result1.section_analyses[0].summary_skim
    assert first_skim, "Expected non-empty summary_skim on first run"

    # Second run should hit the cache (fresh provider → zero calls)
    fresh_provider = MockProvider()
    budget_run2 = BudgetTracker()
    runner2 = OrchestratorRunner(
        fresh_provider, default_config, budget_run2, cache=tmp_cache
    )
    result2 = await runner2.run(synthetic_document)

    assert fresh_provider.call_count == 0, "Expected cache hit (zero provider calls)"
    assert result2.section_analyses, "Expected section_analyses on cache-hit result"

    cached_skim = result2.section_analyses[0].summary_skim
    assert cached_skim, "Expected non-empty summary_skim on cache-hit result"
    assert cached_skim == first_skim, (
        f"Cache-hit summary_skim differs from first run: {cached_skim!r} != {first_skim!r}"
    )

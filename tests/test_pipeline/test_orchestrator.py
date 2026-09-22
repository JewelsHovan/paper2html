"""Orchestrator integration tests.

AC-3, Scenarios 1/4/6: full loop, spawn_subquery dispatch, partial failure.
"""

from __future__ import annotations

import pytest

from paper2html.models.document import NormalizedDocument
from paper2html.pipeline.budget import BudgetTracker
from paper2html.pipeline.orchestrator import OrchestratorRunner
from paper2html.pipeline.providers.mock_provider import FailMode, MockProvider
from paper2html.pipeline.schemas import PipelineConfig, PipelineResult

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Scenario 1: Full 5-section loop → 5 section_analyses
# ---------------------------------------------------------------------------


async def test_full_loop_produces_five_section_analyses(
    synthetic_document: NormalizedDocument,
    mock_provider: MockProvider,
    default_config: PipelineConfig,
) -> None:
    """Full loop with MockProvider on 5-section document returns 5 analyses."""
    budget = BudgetTracker()
    runner = OrchestratorRunner(mock_provider, default_config, budget)
    result = await runner.run(synthetic_document)

    assert isinstance(result, PipelineResult)
    assert len(result.section_analyses) == 5
    assert len(result.failed_sections) == 0


async def test_pipeline_result_pydantic_roundtrip(
    synthetic_document: NormalizedDocument,
    mock_provider: MockProvider,
    default_config: PipelineConfig,
) -> None:
    """PipelineResult round-trips through Pydantic without data loss."""
    budget = BudgetTracker()
    runner = OrchestratorRunner(mock_provider, default_config, budget)
    result = await runner.run(synthetic_document)

    json_str = result.model_dump_json()
    restored = PipelineResult.model_validate_json(json_str)
    assert restored.doc_id == result.doc_id
    assert len(restored.section_analyses) == len(result.section_analyses)


async def test_pipeline_result_doc_id_arxiv(
    synthetic_document: NormalizedDocument,
    mock_provider: MockProvider,
    default_config: PipelineConfig,
) -> None:
    """doc_id is the arXiv id when metadata.arxiv_id is set."""
    budget = BudgetTracker()
    runner = OrchestratorRunner(mock_provider, default_config, budget)
    result = await runner.run(synthetic_document)
    assert result.doc_id == "2301.00001"


async def test_pipeline_result_doc_id_pdf(
    synthetic_document_pdf: NormalizedDocument,
    mock_provider: MockProvider,
    default_config: PipelineConfig,
) -> None:
    """doc_id is a sha256 prefix when no arXiv id is present (PDF path)."""
    budget = BudgetTracker()
    runner = OrchestratorRunner(mock_provider, default_config, budget)
    result = await runner.run(synthetic_document_pdf)
    # Should be a 16-char hex string, not empty
    assert len(result.doc_id) == 16
    assert all(c in "0123456789abcdef" for c in result.doc_id)


async def test_pipeline_result_carries_figures(
    synthetic_document: NormalizedDocument,
    mock_provider: MockProvider,
    default_config: PipelineConfig,
) -> None:
    """PipelineResult.figures contains all document figures for ticket 007."""
    budget = BudgetTracker()
    runner = OrchestratorRunner(mock_provider, default_config, budget)
    result = await runner.run(synthetic_document)
    assert len(result.figures) == 3
    figure_ids = {f.figure_id for f in result.figures}
    assert figure_ids == {"fig-1", "fig-2", "fig-3"}


# ---------------------------------------------------------------------------
# Scenario 4: spawn_subquery dispatch + merge
# ---------------------------------------------------------------------------


async def test_spawn_subquery_dispatches_sub_worker(
    synthetic_document: NormalizedDocument,
    default_config: PipelineConfig,
) -> None:
    """spawn_subquery tool call dispatches a sub-worker with narrowed context.

    The result has exactly one WorkerOutput per section (subquery output merged
    into section 2's entry — not duplicated).
    """
    provider = MockProvider(spawn_subquery_section="sec-2")
    budget = BudgetTracker()
    runner = OrchestratorRunner(provider, default_config, budget)
    result = await runner.run(synthetic_document)

    # Still exactly 5 analyses (one per section)
    assert len(result.section_analyses) == 5

    # Find sec-2's analysis
    sec2_analysis = next(
        (a for a in result.section_analyses if a.section_id == "sec-2"), None
    )
    assert sec2_analysis is not None


async def test_spawn_subquery_retains_authoritative_context(
    synthetic_document: NormalizedDocument,
    default_config: PipelineConfig,
) -> None:
    """Generated excerpts never replace source evidence; focus is guidance only."""
    provider = MockProvider(spawn_subquery_section="sec-2")
    budget = BudgetTracker()
    runner = OrchestratorRunner(provider, default_config, budget)
    await runner.run(synthetic_document)

    # Find calls that contain the subquery text slice
    found_subquery = False
    for messages, _, _, _ in provider._calls:
        for msg in messages:
            if "mock text slice for sub-query" in msg.content:
                found_subquery = True
                break
    assert not found_subquery, "Generated source replacement must be ignored"
    worker_messages = [
        m.content
        for messages, _, _, _ in provider._calls
        for m in messages
        if "SECTION_ID: sec-2" in m.content
    ]
    assert synthetic_document.sections[1].text_content in worker_messages[0]
    assert "What are the main technical contributions?" in worker_messages[0]


# ---------------------------------------------------------------------------
# Scenario 6: Partial failure — 1 section fails, rest succeed
# ---------------------------------------------------------------------------


async def test_partial_failure_produces_failed_sections_entry(
    synthetic_document: NormalizedDocument,
    default_config: PipelineConfig,
) -> None:
    """When one section worker fails, it appears in failed_sections; others succeed."""
    provider = MockProvider(fail_mode=FailMode.ALL_CALLS)
    budget = BudgetTracker()
    runner = OrchestratorRunner(provider, default_config, budget)
    result = await runner.run(synthetic_document)

    # All sections failed
    assert len(result.failed_sections) == 5
    assert len(result.section_analyses) == 0


async def test_single_section_failure_does_not_abort_others(
    synthetic_document: NormalizedDocument,
    default_config: PipelineConfig,
) -> None:
    """Single section failure leaves other 4 sections intact.

    Uses a custom provider subclass that always fails for sec-3 (both retries),
    simulating WorkerValidationError for exactly one section.
    """
    from paper2html.pipeline.providers.base import ProviderResponse

    class _AlwaysFailSec3(MockProvider):
        """Fails both retries for sec-3 only."""

        async def complete(
            self,
            messages,
            images,
            tools,
            *,
            model,
            max_output_tokens=8192,
            transport_retries=True,
            request_timeout_seconds=60.0,
        ):
            user_content = messages[-1].content if messages else ""
            section_id = self._extract_section_id(user_content)
            if section_id == "sec-3":
                self.call_count += 1
                self._calls.append((messages, images, tools, model))
                if section_id:
                    self._section_call_counts[section_id] = (
                        self._section_call_counts.get(section_id, 0) + 1
                    )
                return ProviderResponse(
                    content="not valid json {",
                    input_tokens=10,
                    output_tokens=5,
                )
            return await super().complete(messages, images, tools, model=model)

    provider = _AlwaysFailSec3()
    budget = BudgetTracker()
    runner = OrchestratorRunner(provider, default_config, budget)
    result = await runner.run(synthetic_document)

    failed_ids = {f.section_id for f in result.failed_sections}
    assert "sec-3" in failed_ids
    assert len(result.section_analyses) == 4


async def test_synthesis_fields_populated(
    synthetic_document: NormalizedDocument,
    mock_provider: MockProvider,
    default_config: PipelineConfig,
) -> None:
    """Orchestrator synthesis populates overall_summary and key_contributions."""
    budget = BudgetTracker()
    runner = OrchestratorRunner(mock_provider, default_config, budget)
    result = await runner.run(synthetic_document)

    assert result.overall_summary == "Mock overall paper summary."
    assert "Contribution A" in result.key_contributions
    assert "term_a" in result.terminology

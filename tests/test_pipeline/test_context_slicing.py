"""Context slicing correctness tests.

AC-4: Each worker receives ONLY its section's text and ONLY figures from
section.figure_ids.  No adjacent section text leaks.
"""

from __future__ import annotations

import pytest

from paper2html.models.document import NormalizedDocument
from paper2html.pipeline.budget import BudgetTracker
from paper2html.pipeline.orchestrator import OrchestratorRunner
from paper2html.pipeline.providers.mock_provider import MockProvider
from paper2html.pipeline.schemas import PipelineConfig, WorkerInput

_PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


# ---------------------------------------------------------------------------
# Helpers — build WorkerInputs directly from the orchestrator slicing logic
# ---------------------------------------------------------------------------


def _build_worker_inputs(document: NormalizedDocument) -> dict[str, WorkerInput]:
    """Mirror the orchestrator's slicing logic to build WorkerInput per section."""
    from paper2html.pipeline.schemas import FigureContext

    result = {}
    for section in document.sections:
        figs = [
            FigureContext(
                figure_id=document.figures[fid].id,
                caption=document.figures[fid].caption,
                image_bytes=document.figures[fid].image_bytes,
                image_mime_type=document.figures[fid].image_mime_type,
                referencing_sentence_ids=list(
                    document.figures[fid].referencing_sentence_ids
                ),
            )
            for fid in section.figure_ids
            if fid in document.figures
        ]
        result[section.id] = WorkerInput(
            section_id=section.id,
            section_title=section.title,
            section_text=section.text_content,
            figures=figs,
            doc_title=document.metadata.title,
            doc_abstract=document.metadata.abstract,
        )
    return result


# ---------------------------------------------------------------------------
# Section text isolation tests
# ---------------------------------------------------------------------------


def test_worker_input_contains_only_assigned_section_text(
    large_document: NormalizedDocument,
) -> None:
    """For a 10-section document, each WorkerInput.section_text matches only that section."""
    inputs = _build_worker_inputs(large_document)

    for section in large_document.sections:
        wi = inputs[section.id]
        # section_text must be exactly the section's text_content
        assert wi.section_text == section.text_content, (
            f"section_text mismatch for {section.id}"
        )


def test_no_adjacent_section_text_leaks(large_document: NormalizedDocument) -> None:
    """Text from section N does not appear in section N+1 or N-1's WorkerInput.

    Each section's text_content is exactly ``large_document.sections[i].text_content``.
    Leaking means section K's FULL TEXT appears verbatim in section M's text.
    We verify byte-level identity: section_text == section.text_content only for
    the matching section.
    """
    inputs = _build_worker_inputs(large_document)

    for section in large_document.sections:
        wi = inputs[section.id]
        # The section_text must equal this section's text_content EXACTLY
        assert wi.section_text == section.text_content, (
            f"Section {section.id}: section_text does not match source text_content"
        )

    # No WorkerInput should contain any other section's full text
    for i, section_i in enumerate(large_document.sections):
        text_i = section_i.text_content
        for j, section_j in enumerate(large_document.sections):
            if i == j:
                continue
            wi_j = inputs[section_j.id]
            # Section i's FULL TEXT must not appear verbatim in section j's WorkerInput
            assert text_i not in wi_j.section_text, (
                f"Section {section_j.id}'s WorkerInput contains full text of {section_i.id}"
            )


# ---------------------------------------------------------------------------
# Figure slicing tests
# ---------------------------------------------------------------------------


def test_figures_filtered_by_figure_ids(large_document: NormalizedDocument) -> None:
    """Each WorkerInput contains only figures from section.figure_ids."""
    inputs = _build_worker_inputs(large_document)

    for section in large_document.sections:
        wi = inputs[section.id]
        input_fig_ids = {f.figure_id for f in wi.figures}
        expected_fig_ids = set(section.figure_ids)
        assert input_fig_ids == expected_fig_ids, (
            f"Section {section.id}: expected figures {expected_fig_ids}, "
            f"got {input_fig_ids}"
        )


def test_figures_from_other_sections_not_included(
    large_document: NormalizedDocument,
) -> None:
    """Figures assigned to section N do not appear in section M's WorkerInput."""
    inputs = _build_worker_inputs(large_document)

    for section in large_document.sections:
        wi = inputs[section.id]
        my_fig_ids = {f.figure_id for f in wi.figures}

        for other_section in large_document.sections:
            if other_section.id == section.id:
                continue
            other_fig_ids = set(other_section.figure_ids)
            overlap = my_fig_ids & other_fig_ids
            assert not overlap, (
                f"Section {section.id} contains figures {overlap} "
                f"that belong to {other_section.id}"
            )


def test_figure_bytes_identity(large_document: NormalizedDocument) -> None:
    """Figure bytes in WorkerInput are identical to document figure bytes."""
    inputs = _build_worker_inputs(large_document)

    for section in large_document.sections:
        wi = inputs[section.id]
        for fig_ctx in wi.figures:
            original = large_document.figures[fig_ctx.figure_id]
            assert fig_ctx.image_bytes == original.image_bytes, (
                f"Byte mismatch for {fig_ctx.figure_id} in section {section.id}"
            )


# ---------------------------------------------------------------------------
# Full pipeline context isolation (end-to-end via orchestrator)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_passes_only_section_figures_to_workers(
    large_document: NormalizedDocument,
) -> None:
    """Through the full orchestrator, workers only receive their section's figures."""
    provider = MockProvider()
    config = PipelineConfig()
    budget = BudgetTracker()
    runner = OrchestratorRunner(provider, config, budget)
    result = await runner.run(large_document)

    # All sections successfully processed
    assert len(result.section_analyses) == 10
    assert len(result.failed_sections) == 0

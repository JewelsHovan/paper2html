"""Tests for review.glue.attach_reading_aids (tickets #013 and #014).

Covers:
1. E2E merge via orchestrator + mock provider (async).
2. E2E render: term-def spans appear in static HTML for terms present in text.
3. Synthesis wins on key conflict (sync unit).
4. Empty PipelineResult degrades gracefully (sync).
5. Input bundle and result are not mutated (sync).
6. Malformed worker term_definitions entries are skipped (sync).
7. (ticket #014) section_summaries: populated, never-partial, whitespace exclusion,
   pipeline-wins, pre-existing preserved.
8. (ticket #014) margin_notes: order, empty bundle, invalid entries skipped,
   no mutation.
9. (ticket #014) WorkerOutput null coercion (schema-level).
10. (ticket #014) E2E render: summary-block and margin-note asides in HTML.

Per-project convention (commit 96f060a): per-function @pytest.mark.asyncio
on async tests only — no module-level pytestmark.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from paper2html.models.document import (
    DocumentMetadata,
    Figure,
    IngestionPath,
    NormalizedDocument,
    Section,
)
from paper2html.pipeline.budget import BudgetTracker
from paper2html.pipeline.orchestrator import OrchestratorRunner
from paper2html.pipeline.schemas import PipelineResult, WorkerOutput
from paper2html.renderer import render_html
from paper2html.review.glue import attach_reading_aids
from paper2html.review.schemas import AnnotationBundle

# ---------------------------------------------------------------------------
# Tiny valid PNG bytes (avoids empty-image guards in renderer)
# ---------------------------------------------------------------------------
_TINY_PNG: bytes = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc`h\xf8\x0f\x00\x02\x03\x01\x80$a\xf5\x97"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


# ---------------------------------------------------------------------------
# Helper: build a minimal PipelineResult with required fields but no data
# ---------------------------------------------------------------------------


def _empty_pipeline_result() -> PipelineResult:
    """Construct a minimal valid PipelineResult with all required fields set."""
    return PipelineResult(
        doc_id="test-doc",
        provider="mock",
        orchestrator_model="claude-opus-4-8",
        worker_model="claude-haiku-4-5",
    )


# ---------------------------------------------------------------------------
# 1. E2E merge via orchestrator + mock provider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attach_reading_aids_merges_terminology(
    synthetic_document: NormalizedDocument,
    mock_provider,
    default_config,
) -> None:
    """E2E: glue populates bundle with both synthesis and per-section worker terms."""
    budget = BudgetTracker()
    runner = OrchestratorRunner(mock_provider, default_config, budget)
    result = await runner.run(synthetic_document)

    bundle = attach_reading_aids(AnnotationBundle(items=[]), result)

    # Synthesis-level terms (from mock _synthesis_response)
    assert "term_a" in bundle.term_definitions
    assert "term_b" in bundle.term_definitions
    assert bundle.term_definitions["term_a"] == "definition a"
    assert bundle.term_definitions["term_b"] == "definition b"

    # At least one per-section worker term should be present (e.g. term_sec-1)
    worker_terms = {f"term_{wa.section_id}" for wa in result.section_analyses}
    found_worker_terms = worker_terms & set(bundle.term_definitions)
    assert found_worker_terms, (
        f"Expected at least one worker term in bundle; got {set(bundle.term_definitions)}"
    )


# ---------------------------------------------------------------------------
# 2. E2E render: term-def spans for terms present in section text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attach_reading_aids_renders_term_def_spans(
    mock_provider,
    default_config,
    tmp_path: Path,
) -> None:
    """E2E: glue → render_html emits .term-def spans for terms present in text.

    We build a custom single-section document whose text literally contains
    "term_a" and "term_b" so that server-side span injection fires for those
    synthesis terms (MockProvider always returns these in terminology).

    Also verifies the empty-bundle path inline: no .term-def spans when
    term_definitions is empty.
    """
    # Build a single-section doc with text that contains the synthesis terms
    sec1 = Section(
        id="sec-1",
        title="Introduction",
        level=1,
        text_content=("The term_a method is important. term_b is also relevant"),
    )
    fig1 = Figure(
        id="fig-1",
        caption="A figure",
        image_bytes=_TINY_PNG,
        image_mime_type="image/png",
        referencing_sentence_ids=[],
    )
    doc = NormalizedDocument(
        metadata=DocumentMetadata(
            arxiv_id="2301.00099",
            title="Glue Test Paper",
            abstract="Testing glue.",
            ingestion_path=IngestionPath.ARXIV_HTML,
        ),
        sections=[sec1],
        figures={"fig-1": fig1},
    )

    budget = BudgetTracker()
    runner = OrchestratorRunner(mock_provider, default_config, budget)
    result = await runner.run(doc)

    bundle = attach_reading_aids(AnnotationBundle(items=[]), result)

    # --- Populated bundle: term-def spans must be present ---
    out_path = tmp_path / "out.html"
    render_html(doc, bundle, out_path)
    soup = BeautifulSoup(out_path.read_text(encoding="utf-8"), "html.parser")
    term_def_spans = soup.find_all(class_="term-def")
    assert len(term_def_spans) > 0, (
        "Expected at least one .term-def span when term_definitions is populated"
    )

    # --- Empty bundle: no term-def spans ---
    empty_bundle = AnnotationBundle(items=[])
    out_empty = tmp_path / "out_empty.html"
    render_html(doc, empty_bundle, out_empty)
    soup_empty = BeautifulSoup(out_empty.read_text(encoding="utf-8"), "html.parser")
    empty_spans = soup_empty.find_all(class_="term-def")
    assert len(empty_spans) == 0, (
        "Expected no .term-def spans when bundle.term_definitions is empty"
    )


# ---------------------------------------------------------------------------
# 3. Synthesis wins on key conflict (sync unit)
# ---------------------------------------------------------------------------


def test_attach_reading_aids_synthesis_wins_on_conflict() -> None:
    """Synthesis terminology overrides worker-level term on the same key."""
    worker_out = WorkerOutput(
        section_id="sec-1",
        term_definitions=[{"term": "foo", "definition": "worker_val"}],
    )
    result = _empty_pipeline_result()
    result.section_analyses = [worker_out]
    result.terminology = {"foo": "synthesis_val"}

    bundle = attach_reading_aids(AnnotationBundle(items=[]), result)

    assert bundle.term_definitions["foo"] == "synthesis_val"


# ---------------------------------------------------------------------------
# 4. Empty PipelineResult degrades gracefully
# ---------------------------------------------------------------------------


def test_attach_reading_aids_empty_result() -> None:
    """Empty PipelineResult produces an empty term_definitions dict (no error)."""
    result = _empty_pipeline_result()
    # No section_analyses, no terminology — all defaults

    bundle = attach_reading_aids(AnnotationBundle(items=[]), result)

    assert bundle.term_definitions == {}
    assert bundle.margin_notes == []
    assert bundle.section_summaries == {}


# ---------------------------------------------------------------------------
# 5. No mutation of inputs
# ---------------------------------------------------------------------------


def test_attach_reading_aids_no_mutation() -> None:
    """attach_reading_aids does not mutate the input bundle or result."""
    worker_out = WorkerOutput(
        section_id="sec-x",
        term_definitions=[{"term": "alpha", "definition": "first"}],
    )
    result = _empty_pipeline_result()
    result.section_analyses = [worker_out]
    result.terminology = {"beta": "second"}

    input_bundle = AnnotationBundle(items=[])
    # Capture original state
    orig_bundle_terms = dict(input_bundle.term_definitions)
    orig_terminology = dict(result.terminology)

    bundle1 = attach_reading_aids(input_bundle, result)
    bundle2 = attach_reading_aids(input_bundle, result)

    # Input bundle untouched
    assert input_bundle.term_definitions == orig_bundle_terms
    # Input result.terminology untouched
    assert result.terminology == orig_terminology
    # Two calls yield the same merge
    assert bundle1.term_definitions == bundle2.term_definitions


# ---------------------------------------------------------------------------
# 6. Malformed worker term_definitions entries are skipped
# ---------------------------------------------------------------------------


def test_attach_reading_aids_skips_malformed_dicts() -> None:
    """Malformed term_definitions entries (missing keys) are skipped without error."""
    worker_out = WorkerOutput(
        section_id="sec-bad",
        term_definitions=[
            {"term": "ok", "definition": "d"},  # valid
            {"term": "bad"},  # missing "definition"
            {},  # empty
            {"definition": "x"},  # missing "term"
        ],
    )
    result = _empty_pipeline_result()
    result.section_analyses = [worker_out]

    bundle = attach_reading_aids(AnnotationBundle(items=[]), result)

    assert "ok" in bundle.term_definitions
    assert bundle.term_definitions["ok"] == "d"
    # Only the valid entry survives
    assert len(bundle.term_definitions) == 1


# ---------------------------------------------------------------------------
# 7. section_summaries (ticket #014) — unit tests
# ---------------------------------------------------------------------------


def _make_worker(
    section_id: str,
    skim: str = "Skim.",
    read: str = "Read paragraph.",
    deep: str = "Deep comprehensive.",
) -> WorkerOutput:
    """Build a WorkerOutput with the given three-level summary values."""
    return WorkerOutput(
        section_id=section_id,
        summary_skim=skim,
        summary_read=read,
        summary_deep=deep,
    )


def test_attach_reading_aids_populates_section_summaries() -> None:
    """Worker with all 3 levels → exact entry keyed by section_id."""
    worker = _make_worker("sec-1")
    result = _empty_pipeline_result()
    result.section_analyses = [worker]

    bundle = attach_reading_aids(AnnotationBundle(items=[]), result)

    assert "sec-1" in bundle.section_summaries
    entry = bundle.section_summaries["sec-1"]
    assert entry == {
        "skim": "Skim.",
        "read": "Read paragraph.",
        "deep": "Deep comprehensive.",
    }


def test_attach_reading_aids_never_partial() -> None:
    """Workers with incomplete levels are excluded; complete sections unaffected."""
    worker_complete = _make_worker("sec-1")
    worker_missing_deep = _make_worker("sec-2", deep="")  # missing deep
    worker_missing_read = _make_worker("sec-3", read="")  # missing read

    result = _empty_pipeline_result()
    result.section_analyses = [
        worker_complete,
        worker_missing_deep,
        worker_missing_read,
    ]

    bundle = attach_reading_aids(AnnotationBundle(items=[]), result)

    assert "sec-1" in bundle.section_summaries
    assert "sec-2" not in bundle.section_summaries
    assert "sec-3" not in bundle.section_summaries


def test_attach_reading_aids_whitespace_level_excluded() -> None:
    """A whitespace-only summary level is treated as missing — no entry emitted."""
    worker = _make_worker("sec-ws", read="   ")  # whitespace-only read

    result = _empty_pipeline_result()
    result.section_analyses = [worker]

    bundle = attach_reading_aids(AnnotationBundle(items=[]), result)

    assert "sec-ws" not in bundle.section_summaries


def test_attach_reading_aids_pipeline_wins_per_section_id() -> None:
    """Pipeline worker replaces any pre-existing bundle entry for the same section_id."""
    pre_existing: dict[str, dict[str, str]] = {
        "sec-1": {"skim": "old skim", "read": "old read", "deep": "old deep"},
    }
    input_bundle = AnnotationBundle(items=[], section_summaries=pre_existing)

    worker = _make_worker("sec-1", skim="new skim", read="new read", deep="new deep")
    result = _empty_pipeline_result()
    result.section_analyses = [worker]

    bundle = attach_reading_aids(input_bundle, result)

    assert bundle.section_summaries["sec-1"]["skim"] == "new skim"
    assert bundle.section_summaries["sec-1"]["read"] == "new read"
    assert bundle.section_summaries["sec-1"]["deep"] == "new deep"


def test_attach_reading_aids_pre_existing_summaries_preserved() -> None:
    """Pre-existing bundle entry for a section absent from the pipeline is preserved."""
    pre_existing: dict[str, dict[str, str]] = {
        "sec-2": {"skim": "pre skim", "read": "pre read", "deep": "pre deep"},
    }
    input_bundle = AnnotationBundle(items=[], section_summaries=pre_existing)

    worker = _make_worker("sec-1")
    result = _empty_pipeline_result()
    result.section_analyses = [worker]

    bundle = attach_reading_aids(input_bundle, result)

    assert "sec-1" in bundle.section_summaries
    assert "sec-2" in bundle.section_summaries
    assert bundle.section_summaries["sec-2"]["skim"] == "pre skim"


# ---------------------------------------------------------------------------
# 8. margin_notes (ticket #014) — unit tests
# ---------------------------------------------------------------------------


def test_attach_reading_aids_margin_notes_order() -> None:
    """Bundle notes come first; worker notes appended in section_analyses order."""
    input_bundle = AnnotationBundle(items=[], margin_notes=["pre-existing note"])

    wo1 = WorkerOutput(section_id="sec-1", margin_notes=["note-A"])
    wo2 = WorkerOutput(section_id="sec-2", margin_notes=["note-B"])
    result = _empty_pipeline_result()
    result.section_analyses = [wo1, wo2]

    bundle = attach_reading_aids(input_bundle, result)

    assert bundle.margin_notes == ["pre-existing note", "note-A", "note-B"]


def test_attach_reading_aids_margin_notes_from_empty_bundle() -> None:
    """Bundle with NO margin_notes + worker notes → worker notes present."""
    input_bundle = AnnotationBundle(items=[])  # margin_notes defaults to []

    wo = WorkerOutput(section_id="sec-1", margin_notes=["worker note"])
    result = _empty_pipeline_result()
    result.section_analyses = [wo]

    bundle = attach_reading_aids(input_bundle, result)

    assert "worker note" in bundle.margin_notes


def test_attach_reading_aids_margin_notes_skips_invalid() -> None:
    """Non-string and empty/whitespace notes are skipped."""
    wo = WorkerOutput.model_construct(
        section_id="sec-bad",
        summary="",
        key_claims=[],
        term_definitions=[],
        summary_skim="",
        summary_read="",
        summary_deep="",
        # Bypass pydantic validators to inject non-string entries directly
        margin_notes=["valid", "", "  ", "also-valid"],  # type: ignore[arg-type]
    )
    # Manually inject non-string values after construction (validate_assignment off by default)
    object.__setattr__(wo, "margin_notes", ["valid", "", "  ", 42, None, "also-valid"])

    result = _empty_pipeline_result()
    result.section_analyses = [wo]

    bundle = attach_reading_aids(AnnotationBundle(items=[]), result)

    assert bundle.margin_notes == ["valid", "also-valid"]


def test_attach_reading_aids_no_mutation_new_fields() -> None:
    """Input bundle margin_notes/section_summaries unchanged after two calls.

    Also verifies the returned bundle's inner summary dicts are independent
    (mutating the returned dict does not affect source data).
    """
    pre_summaries: dict[str, dict[str, str]] = {}
    input_bundle = AnnotationBundle(
        items=[],
        margin_notes=["original"],
        section_summaries=pre_summaries,
    )

    wo = WorkerOutput(
        section_id="sec-1",
        summary_skim="Skim.",
        summary_read="Read.",
        summary_deep="Deep.",
        margin_notes=["worker note"],
    )
    result = _empty_pipeline_result()
    result.section_analyses = [wo]

    bundle1 = attach_reading_aids(input_bundle, result)
    bundle2 = attach_reading_aids(input_bundle, result)

    # Input bundle is unchanged
    assert input_bundle.margin_notes == ["original"]
    assert input_bundle.section_summaries == {}

    # Both calls yield identical results
    assert bundle1.margin_notes == bundle2.margin_notes
    assert bundle1.section_summaries == bundle2.section_summaries

    # Mutating the returned bundle's inner dict does NOT affect the worker or bundle2
    bundle1.section_summaries["sec-1"]["skim"] = "MUTATED"
    assert bundle2.section_summaries["sec-1"]["skim"] == "Skim."
    assert wo.summary_skim == "Skim."


# ---------------------------------------------------------------------------
# 9. WorkerOutput null coercion (schema-level, ticket #014)
# ---------------------------------------------------------------------------


def test_worker_output_null_coercion_in_glue_context() -> None:
    """WorkerOutput.model_validate with None new fields succeeds with defaults."""
    output = WorkerOutput.model_validate(
        {
            "section_id": "sec-null",
            "summary_skim": None,
            "summary_read": None,
            "summary_deep": None,
            "margin_notes": None,
        }
    )
    assert output.summary_skim == ""
    assert output.summary_read == ""
    assert output.summary_deep == ""
    assert output.margin_notes == []

    # When margin_notes contains mixed types, non-strings are dropped
    output2 = WorkerOutput.model_validate(
        {
            "section_id": "sec-mixed",
            "margin_notes": ["ok", 42, None],
        }
    )
    assert output2.margin_notes == ["ok"]


# ---------------------------------------------------------------------------
# 10. E2E render: summary-block + margin-note asides (ticket #014)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attach_reading_aids_renders_summaries_and_margin_notes(
    mock_provider,
    default_config,
    tmp_path: Path,
) -> None:
    """E2E: glue → render_html emits .summary-block with [data-level] divs and aside.margin-note.

    MockProvider returns all three summary levels and a margin note for each
    section.  We build a single-section document and assert that:
      - sec-1's .summary-block has [data-level="skim"] containing the mock skim text
      - [data-level="read"] containing the mock read text
      - [data-level="deep"] containing the mock deep text
      - an aside.margin-note containing the mock margin note text

    The input bundle starts empty (margin_notes=[]) to exercise the
    combine-then-guard pattern in attach_reading_aids.
    """
    sec1 = Section(
        id="sec-1",
        title="Introduction",
        level=1,
        text_content=(
            "This section covers the main topic. "
            "It describes the primary approach used."
        ),
    )
    fig1 = Figure(
        id="fig-1",
        caption="A figure",
        image_bytes=_TINY_PNG,
        image_mime_type="image/png",
        referencing_sentence_ids=[],
    )
    doc = NormalizedDocument(
        metadata=DocumentMetadata(
            arxiv_id="2301.00099",
            title="Summary Test Paper",
            abstract="Testing summaries.",
            ingestion_path=IngestionPath.ARXIV_HTML,
        ),
        sections=[sec1],
        figures={"fig-1": fig1},
    )

    budget = BudgetTracker()
    runner = OrchestratorRunner(mock_provider, default_config, budget)
    result = await runner.run(doc)

    # Input bundle MUST start empty
    input_bundle = AnnotationBundle(items=[], margin_notes=[])
    bundle = attach_reading_aids(input_bundle, result)

    # Verify the bundle has the expected data before rendering
    assert "sec-1" in bundle.section_summaries, (
        f"Expected sec-1 in section_summaries; got keys: {list(bundle.section_summaries)}"
    )
    assert bundle.margin_notes, "Expected at least one margin note in bundle"

    out_path = tmp_path / "summaries.html"
    render_html(doc, bundle, out_path)
    soup = BeautifulSoup(out_path.read_text(encoding="utf-8"), "html.parser")

    # --- summary-block assertions ---
    # The section renders inside <section id="sec-1">
    section_el = soup.find("section", id="sec-1")
    assert section_el is not None, "Expected <section id='sec-1'> in HTML"

    summary_block = section_el.find(class_="summary-block")
    assert summary_block is not None, "Expected .summary-block inside sec-1"

    skim_div = summary_block.find(attrs={"data-level": "skim"})
    read_div = summary_block.find(attrs={"data-level": "read"})
    deep_div = summary_block.find(attrs={"data-level": "deep"})

    assert skim_div is not None, "Expected [data-level='skim'] inside .summary-block"
    assert read_div is not None, "Expected [data-level='read'] inside .summary-block"
    assert deep_div is not None, "Expected [data-level='deep'] inside .summary-block"

    assert "Mock skim summary for sec-1." in skim_div.get_text()
    assert "Mock read summary for sec-1." in read_div.get_text()
    assert "Mock deep summary for sec-1." in deep_div.get_text()

    # --- margin-note assertions ---
    margin_note_els = soup.find_all("aside", class_="margin-note")
    assert len(margin_note_els) > 0, "Expected at least one aside.margin-note in HTML"
    margin_texts = [el.get_text() for el in margin_note_els]
    assert any("Mock margin note for sec-1." in t for t in margin_texts), (
        f"Expected 'Mock margin note for sec-1.' in margin notes; got: {margin_texts}"
    )
